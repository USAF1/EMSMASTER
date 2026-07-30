# main.py — MASTER (ESP32-S3)
# Innovatsii EMS — Pico 1
# Firmware Version: 0.2.5
#
# UART1 <-> Sensor Hub  (TX=GPIO16, RX=GPIO17) at 9600 baud
# UART2 <-> Scheduler   — NOT INITIALISED (Phase 4)
# TCP   <-> Debugger App (port 8765)
#
# Boot sequence (v0.2.5):
#   Master is the active boot controller. Hub is passive.
#
#   PHASE A — Hub Discovery:
#     Send {"type":"ping"} every 2 seconds, up to 10 attempts.
#     Hub responds with {"type":"pong"}.
#     No response after 10 attempts → hub_status.fault=true, halt.
#
#   PHASE B — Hub Init:
#     Send hub_init with full config.
#     Hub ACKs, starts Zigbee, forms network, sends hub_ready.
#
#   PHASE C — Sensor Rejoin:
#     Hub sends sensor_joined or sensor_status for each sensor.
#     Hub sends sensor_list_complete when done.
#
#   PHASE D — Watchdog Start:
#     Master sends start_watchdog.
#     Hub ACKs, starts watchdog and data flow.
#
#   PHASE E — Pairing (operator triggered only):
#     hub_ready with needs_pairing=true → alert Debugger.
#     Pairing NEVER opens automatically.
#
# Changes in v0.2.5 vs previous:
#   - New boot protocol (ping/pong/hub_init/start_watchdog)
#   - hub_boot spontaneous handling removed
#   - sensor_joined, sensor_status, sensor_list_complete handlers added
#   - new_sensor_joined, pairing_complete handlers added
#   - Force command system (force_status, cancel_force, force expiry)
#   - Production/Debug mode — MQTT paused when debugger connected
#   - hub_status object tracked in config
#   - presence_fading_time_sec, door_sensor_max_silence_hours in hub_init
#   - recalc_and_act blocked during active force
#   - pending_worker checks force expiry every second
#   - buffer cancellation always sends unit_state_update (force_send)
#   - Debugger connect/disconnect switches mode at runtime
#   - firmware_version in state snapshot and hub_init

import utime
import ujson as json
from machine import UART, reset
import _thread
import network
import socket
import master_config as cfg

# ============================================================================
# CONSTANTS
# ============================================================================

UART_RX_BUF_MAX             = 512
MAIN_LOOP_TICK_MS           = 1000
TCP_PORT                    = 8765
TCP_RX_BUF_MAX              = 1024
INTERNET_CHECK_HOST         = "8.8.8.8"
INTERNET_CHECK_PORT         = 53
INTERNET_CHECK_TIMEOUT_S    = 3
INTERNET_RECHECK_S          = 60
NTP_SYNC_INTERVAL_S         = 3600
HUB_PING_ATTEMPTS           = 10
HUB_PING_INTERVAL_MS        = 2000
HUB_INIT_ACK_TIMEOUT_MS     = 5000
HUB_READY_TIMEOUT_MS        = 120000   # 2 minutes for network formation
WATCHDOG_ACK_TIMEOUT_MS     = 5000

# ============================================================================
# UART INITIALISATION
# ============================================================================

uart_sensor = UART(cfg.SENSOR_UART_ID,
                   baudrate=cfg.SENSOR_UART_BAUD,
                   tx=cfg.SENSOR_UART_TX,
                   rx=cfg.SENSOR_UART_RX)

uart_sched = None

# Pre-allocated static receive buffers
_rx_sensor_buf = bytearray(UART_RX_BUF_MAX)
_rx_sched_buf  = bytearray(UART_RX_BUF_MAX)
_rx_sensor_pos = 0
_rx_sched_pos  = 0

# Pre-allocated static TCP receive buffer (module level — not in thread)
_rx_tcp_buf = bytearray(TCP_RX_BUF_MAX)

# ============================================================================
# LOCKS
# ============================================================================

_state_lock      = _thread.allocate_lock()
_tx_sensor_lock  = _thread.allocate_lock()
_tx_sched_lock   = _thread.allocate_lock()
_tx_tcp_lock     = _thread.allocate_lock()

# ============================================================================
# RUNTIME STATE
# ============================================================================

state = {
    "last_sensor_status":     "vacant",
    "last_scheduler_status":  None,
    "current_decided_status": None,
    "pending_status":         None,
    "pending_apply_epoch":    0,
}

_internet_up          = False
_wifi_ip              = ""
_hub_state            = "UNKNOWN"   # UNKNOWN / BOOTING / READY / FAULT
_tcp_client           = None
_mac_str              = ""
_ntp_synced           = False
_last_ntp_sync        = 0
_hub_config_push_in_progress = False
_tcp_server_started          = False

# Boot phase tracking
_hub_pong_received        = False
_hub_init_acked           = False
_hub_ready_received       = False
_sensor_list_complete     = False
_watchdog_start_acked     = False
_boot_phase               = "IDLE"   # IDLE/PING/INIT/REJOIN/WATCHDOG/READY/FAULT

# Debug/Production mode
# True when a debugger TCP client is connected
_debug_mode = False

# ============================================================================
# PERSISTENT CONFIG
# ============================================================================

conf    = {}
boot_ms = utime.ticks_ms()


def load_config():
    global conf
    try:
        with open(cfg.CONFIG_FILE, "r") as f:
            conf = json.load(f)
        print("CONFIG loaded")
    except Exception as e:
        print("CONFIG load failed, using defaults:", e)
        conf = {}
    changed = False
    def _merge(target, source):
        nonlocal changed
        for k, v in source.items():
            if k not in target:
                target[k] = v
                changed = True
            elif isinstance(v, dict) and isinstance(target.get(k), dict):
                _merge(target[k], v)
    _merge(conf, cfg.DEFAULT_CONFIG)
    if changed:
        save_config()


def save_config():
    tmp = cfg.CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(conf, f)
        import uos
        uos.rename(tmp, cfg.CONFIG_FILE)
    except Exception as e:
        print("CONFIG save failed:", e)


# ============================================================================
# UART TX HELPERS
# ============================================================================

def _uart_send(uart_obj, lock_obj, payload_dict):
    try:
        msg = json.dumps(payload_dict) + "\n"
        with lock_obj:
            uart_obj.write(msg.encode("utf-8"))
    except Exception as e:
        print("UART TX error:", repr(e))


def send_to_sensor_hub(payload_dict):
    msg_type = payload_dict.get("type", "?")
    print("MASTER -> HUB: {}".format(msg_type))
    _uart_send(uart_sensor, _tx_sensor_lock, payload_dict)


def send_to_scheduler(payload_dict):
    print("SCHED TX (disabled Phase 4): {}".format(
          payload_dict.get("type", "?")))


# ============================================================================
# TCP TX HELPER
# ============================================================================

def tcp_forward(msg_dict):
    global _tcp_client
    if _tcp_client is None:
        return
    try:
        msg = json.dumps(msg_dict) + "\n"
        with _tx_tcp_lock:
            if _tcp_client is not None:
                _tcp_client.sendall(msg.encode("utf-8"))
    except Exception:
        _tcp_client = None


# ============================================================================
# UTC TIME HELPERS
# ============================================================================

def _sync_ntp():
    global _ntp_synced, _last_ntp_sync
    try:
        import ntptime
        ntptime.host = "pool.ntp.org"
        ntptime.settime()
        _ntp_synced    = True
        _last_ntp_sync = utime.ticks_ms()
        t = utime.localtime()
        utc_str = "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5])
        print("NTP synced — UTC {}".format(utc_str))
        tcp_forward({"type": "ntp_status", "synced": True, "utc": utc_str})
        return True
    except Exception as e:
        print("NTP sync failed:", repr(e))
        tcp_forward({"type": "ntp_status", "synced": False})
        return False


def parse_utc_to_epoch(dt_str):
    try:
        dt_str = dt_str.strip()
        d, t = dt_str.split(" ")
        y, mo, da = [int(x) for x in d.split("-")]
        hh, mm, ss = [int(x) for x in t.split(":")]
        return utime.mktime((y, mo, da, hh, mm, ss, 0, 0))
    except Exception as e:
        print("parse_utc_to_epoch failed for '{}': {}".format(dt_str, e))
        return 0


def epoch_to_utc_str(ep):
    try:
        t = utime.localtime(ep)
        return "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5])
    except Exception:
        return "2000-01-01 00:00:00"


def now_epoch_utc():
    return utime.time()


def in_booking_window(now_ep, checkin_ep, checkout_ep):
    return checkin_ep <= now_ep <= checkout_ep


# ============================================================================
# UNIT STATE DECISION
# ============================================================================

def decide_status(sensor_status, now_ep):
    if not _internet_up or not _ntp_synced:
        result = "Occupied" if sensor_status == "occupied" else "Vacant"
        reason = "no internet" if not _internet_up else "NTP not synced"
        print("DECIDE [2-state {}]: sensor={} => {}".format(
              reason, sensor_status, result))
        return result
    checkin_ep  = parse_utc_to_epoch(conf.get("check_in_utc",  "2000-01-01 00:00:00"))
    checkout_ep = parse_utc_to_epoch(conf.get("check_out_utc", "2000-01-01 00:00:00"))
    inside      = in_booking_window(now_ep, checkin_ep, checkout_ep)
    if sensor_status == "occupied":
        result = "Occupied" if inside else "UnSold Occupied"
    else:
        result = "Sold Vacant" if inside else "Vacant"
    print("DECIDE [4-state]: sensor={} inside={} => {}".format(
          sensor_status, inside, result))
    return result


# ============================================================================
# FORCE COMMAND SYSTEM
# ============================================================================

def _force_active():
    """Returns True if a force is currently active and not expired."""
    f = conf.get("force", {})
    if not f.get("active", False):
        return False
    expires = f.get("expires_epoch", 0)
    if expires == 0:
        return False
    return now_epoch_utc() < expires


def _apply_force(status, duration_hours, reason="Manual force"):
    """Set a force. Replaces any existing force immediately."""
    now_ep      = now_epoch_utc()
    expires_ep  = now_ep + duration_hours * 3600
    expires_str = epoch_to_utc_str(expires_ep)

    conf["force"] = {
        "active":        True,
        "status":        status,
        "expires_utc":   expires_str,
        "expires_epoch": expires_ep,
        "reason":        reason if reason else "Manual force"
    }
    save_config()

    with _state_lock:
        state["current_decided_status"] = status
        state["pending_status"]         = None
        state["pending_apply_epoch"]    = 0
        conf["last_decided_status"]     = status

    send_status_to_scheduler(status)
    tcp_forward({"type": "unit_state_update", "status": status})
    tcp_forward({
        "type":        "force_update",
        "active":      True,
        "status":      status,
        "expires_utc": expires_str,
        "expires_epoch": expires_ep,
        "reason":      conf["force"]["reason"]
    })
    print("FORCE SET: status={} duration={}h expires={}".format(
          status, duration_hours, expires_str))


def _clear_force(recalculate=True):
    """Clear an active force. Optionally recalculate state."""
    conf["force"] = {
        "active":        False,
        "status":        "",
        "expires_utc":   "",
        "expires_epoch": 0,
        "reason":        ""
    }
    save_config()
    tcp_forward({
        "type":   "force_update",
        "active": False
    })
    print("FORCE CLEARED")
    if recalculate:
        with _state_lock:
            recalc_and_act()


# ============================================================================
# SCHEDULER COMMANDS
# ============================================================================

def send_status_to_scheduler(status):
    print("MASTER -> SCHED (disabled): set_status={}".format(status))


def apply_immediate(status, force_send=False):
    if force_send or state["current_decided_status"] != status:
        state["current_decided_status"] = status
        conf["last_decided_status"]     = status
        save_config()
        send_status_to_scheduler(status)
        tcp_forward({"type": "unit_state_update", "status": status})
        print("STATUS -> {}{}".format(status,
              " (forced)" if force_send else ""))
    else:
        print("STATUS unchanged: {}".format(status))


def schedule_buffered(status, now_ep):
    buffer_min = max(0, int(conf.get("buffer_minutes", 0)))
    if buffer_min == 0:
        state["pending_status"]      = None
        state["pending_apply_epoch"] = 0
        apply_immediate(status)
        return
    state["pending_status"]      = status
    state["pending_apply_epoch"] = now_ep + buffer_min * 60
    print("BUFFERED: target={} apply_at={} now={}".format(
          status, state["pending_apply_epoch"], now_ep))
    tcp_forward({"type": "pending_update",
                 "pending_status":  status,
                 "apply_at_epoch":  state["pending_apply_epoch"]})


def recalc_and_act():
    """
    Called ONLY when _state_lock is already held by the caller.
    Skipped if force is active — force overrides all automatic logic.
    """
    if _force_active():
        print("RECALC skipped — force active")
        return

    now_ep = now_epoch_utc()
    sensor = state["last_sensor_status"]
    target = decide_status(sensor, now_ep)

    if target in ("Sold Vacant", "Vacant"):
        schedule_buffered(target, now_ep)
    else:
        had_pending = state["pending_status"] is not None
        state["pending_status"]      = None
        state["pending_apply_epoch"] = 0
        if had_pending:
            tcp_forward({"type": "pending_update", "pending_status": None})
            print("PENDING cancelled — returning to: {}".format(target))
        apply_immediate(target, force_send=had_pending)


# ============================================================================
# STATE SNAPSHOT
# ============================================================================

def build_state_snapshot():
    with _state_lock:
        t = utime.localtime()
        utc_str = "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5])
        force    = conf.get("force", {})
        hub_stat = conf.get("hub_status", {})
        snap = {
            "type":                 "state_snapshot",
            "firmware_version":     cfg.FIRMWARE_VERSION,
            "unit_state":           state["current_decided_status"] or "Unknown",
            "sensor_occupancy":     state["last_sensor_status"],
            "pending_status":       state["pending_status"],
            "pending_apply_epoch":  state["pending_apply_epoch"],
            "force_active":         force.get("active",        False),
            "force_status":         force.get("status",        ""),
            "force_expires_utc":    force.get("expires_utc",   ""),
            "force_expires_epoch":  force.get("expires_epoch", 0),
            "force_reason":         force.get("reason",        ""),
            "internet_up":          _internet_up,
            "ntp_synced":           _ntp_synced,
            "utc_now":              utc_str,
            "wifi_ip":              _wifi_ip,
            "hub_state":            _hub_state,
            "hub_fault":            hub_stat.get("fault", False),
            "hub_firmware_version": hub_stat.get("firmware_version", ""),
            "boot_phase":           _boot_phase,
            "scheduler_status":     state["last_scheduler_status"],
            "tenant_id":            conf.get("tenant_id", ""),
            "unit_id":              conf.get("unit_id", ""),
            "check_in_utc":         conf.get("check_in_utc", ""),
            "check_out_utc":        conf.get("check_out_utc", ""),
            "buffer_minutes":       conf.get("buffer_minutes", 15),
            "wifi_ssid":            conf.get("wifi_ssid", ""),
            "sensor_hub_config":    conf.get("sensor_hub_config", {}),
            "mode":                 "debug" if _debug_mode else "production",
        }
    return snap


# ============================================================================
# HUB INIT COMMAND BUILDER
# ============================================================================

def _build_hub_init():
    """Build the hub_init command dict from current config."""
    hub_cfg = conf.get("sensor_hub_config", {})
    return {
        "type":                          "hub_init",
        "mode":                          "debug" if _debug_mode else "production",
        "firmware_version":              cfg.FIRMWARE_VERSION,
        "pairing_duration_sec":          hub_cfg.get("pairing_duration_sec",          120),
        "watchdog_enable":               hub_cfg.get("watchdog_enable",                True),
        "watchdog_interval_min":         hub_cfg.get("watchdog_interval_min",          60),
        "watchdog_ping_timeout_sec":     hub_cfg.get("watchdog_ping_timeout_sec",      30),
        "door_alarm_threshold_min":      hub_cfg.get("door_alarm_threshold_min",       10),
        "heartbeat_interval_min":        hub_cfg.get("heartbeat_interval_min",         30),
        "presence_fading_time_sec":      hub_cfg.get("presence_fading_time_sec",       30),
        "door_sensor_max_silence_hours": hub_cfg.get("door_sensor_max_silence_hours",  24),
    }


# ============================================================================
# BOOT CONTROLLER — Phases A through D run in a background thread
# ============================================================================

def _boot_controller_thread():
    """
    Runs as a background thread immediately on boot.
    Executes the full boot sequence:
      Phase A — Hub discovery (ping/pong)
      Phase B — Hub init (hub_init / ACK / hub_ready)
      Phase C — Sensor rejoin (sensor_joined/sensor_status/sensor_list_complete)
      Phase D — Watchdog start
    """
    global _hub_state, _boot_phase
    global _hub_pong_received, _hub_init_acked
    global _hub_ready_received, _sensor_list_complete, _watchdog_start_acked

    utime.sleep_ms(500)   # brief delay for UART to settle

    # ── PHASE A — Hub Discovery ──────────────────────────────────────────────
    _boot_phase = "PING"
    print("BOOT [A] Hub discovery — ping up to {} times".format(
          HUB_PING_ATTEMPTS))
    tcp_forward({"type": "boot_phase", "phase": "A_PING"})

    pong_received = False
    for attempt in range(1, HUB_PING_ATTEMPTS + 1):
        send_to_sensor_hub({"type": "ping"})
        print("BOOT [A] ping attempt {}/{}".format(attempt, HUB_PING_ATTEMPTS))

        deadline = utime.ticks_add(utime.ticks_ms(), HUB_PING_INTERVAL_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if _hub_pong_received:
                pong_received = True
                break
            utime.sleep_ms(100)

        if pong_received:
            print("BOOT [A] pong received on attempt {}".format(attempt))
            break

    if not pong_received:
        _boot_phase = "FAULT"
        _hub_state  = "FAULT"
        conf["hub_status"]["fault"]        = True
        conf["hub_status"]["fault_reason"] = "no_response_to_ping"
        save_config()
        msg = "Hub not responding after {} attempts — check UART wiring".format(
              HUB_PING_ATTEMPTS)
        print("BOOT [A] FAULT:", msg)
        tcp_forward({"type": "boot_fault", "reason": msg})
        return

    conf["hub_status"]["fault"]  = False
    conf["hub_status"]["known"]  = True
    save_config()
    tcp_forward({"type": "boot_phase", "phase": "A_DONE"})

    # ── PHASE B — Hub Init ───────────────────────────────────────────────────
    _boot_phase = "INIT"
    _hub_state  = "BOOTING"
    print("BOOT [B] Sending hub_init")
    tcp_forward({"type": "boot_phase", "phase": "B_INIT"})

    init_acked = False
    for init_attempt in range(1, 3):   # 2 attempts
        _hub_init_acked = False
        send_to_sensor_hub(_build_hub_init())

        deadline = utime.ticks_add(utime.ticks_ms(), HUB_INIT_ACK_TIMEOUT_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if _hub_init_acked:
                init_acked = True
                break
            utime.sleep_ms(100)

        if init_acked:
            print("BOOT [B] hub_init ACK received (attempt {})".format(
                  init_attempt))
            break
        print("BOOT [B] hub_init no ACK (attempt {}/2)".format(init_attempt))

    if not init_acked:
        _boot_phase = "FAULT"
        _hub_state  = "FAULT"
        conf["hub_status"]["fault"]        = True
        conf["hub_status"]["fault_reason"] = "hub_init_no_ack"
        save_config()
        msg = "Hub did not ACK hub_init"
        print("BOOT [B] FAULT:", msg)
        tcp_forward({"type": "boot_fault", "reason": msg})
        return

    # Wait for hub_ready (network formation can take up to 60s)
    print("BOOT [B] Waiting for hub_ready (network formation)...")
    _hub_ready_received = False
    deadline = utime.ticks_add(utime.ticks_ms(), HUB_READY_TIMEOUT_MS)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if _hub_ready_received:
            break
        utime.sleep_ms(200)

    if not _hub_ready_received:
        _boot_phase = "FAULT"
        _hub_state  = "FAULT"
        conf["hub_status"]["fault"]        = True
        conf["hub_status"]["fault_reason"] = "hub_ready_timeout"
        save_config()
        msg = "Hub did not send hub_ready within {}s".format(
              HUB_READY_TIMEOUT_MS // 1000)
        print("BOOT [B] FAULT:", msg)
        tcp_forward({"type": "boot_fault", "reason": msg})
        return

    tcp_forward({"type": "boot_phase", "phase": "B_DONE"})

    # ── PHASE C — Sensor Rejoin ───────────────────────────────────────────────
    _boot_phase = "REJOIN"
    print("BOOT [C] Waiting for sensor rejoin to complete...")
    tcp_forward({"type": "boot_phase", "phase": "C_REJOIN"})

    # sensor_list_complete sets _sensor_list_complete flag in its handler.
    # Maximum wait: 6 retries × 10s × 15 sensors = 900s worst case.
    # In practice 3 sensors × 6 × 10s = 180s maximum.
    _sensor_list_complete = False
    deadline = utime.ticks_add(utime.ticks_ms(), 240000)  # 4 minute cap
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if _sensor_list_complete:
            break
        utime.sleep_ms(500)

    if not _sensor_list_complete:
        # Proceed anyway — sensors may still be joining
        print("BOOT [C] sensor_list_complete not received within cap — proceeding")
    else:
        print("BOOT [C] Sensor rejoin phase complete")

    tcp_forward({"type": "boot_phase", "phase": "C_DONE"})

    # ── PHASE D — Watchdog Start ──────────────────────────────────────────────
    _boot_phase = "WATCHDOG"
    print("BOOT [D] Sending start_watchdog")
    tcp_forward({"type": "boot_phase", "phase": "D_WATCHDOG"})

    _watchdog_start_acked = False
    send_to_sensor_hub({"type": "start_watchdog"})
    deadline = utime.ticks_add(utime.ticks_ms(), WATCHDOG_ACK_TIMEOUT_MS)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if _watchdog_start_acked:
            break
        utime.sleep_ms(100)

    if not _watchdog_start_acked:
        print("BOOT [D] WARNING — start_watchdog not ACKed, continuing anyway")

    _boot_phase = "READY"
    _hub_state  = "READY"

    t = utime.localtime()
    boot_utc = "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        t[0], t[1], t[2], t[3], t[4], t[5])
    conf["hub_status"]["last_boot_utc"] = boot_utc
    save_config()

    print("BOOT complete — system READY")
    tcp_forward({"type": "boot_phase", "phase": "READY"})
    tcp_forward(build_state_snapshot())

    # Calculate initial unit state
    with _state_lock:
        # Check if force survived reboot and is still valid
        if _force_active():
            f = conf.get("force", {})
            print("FORCE restored from config: status={} expires={}".format(
                  f.get("status", ""), f.get("expires_utc", "")))
            state["current_decided_status"] = f.get("status", "Vacant")
            send_status_to_scheduler(f.get("status", "Vacant"))
            tcp_forward({"type": "force_update",
                         "active":        True,
                         "status":        f.get("status", ""),
                         "expires_utc":   f.get("expires_utc", ""),
                         "expires_epoch": f.get("expires_epoch", 0),
                         "reason":        f.get("reason", "")})
        else:
            if _force_active() is False and conf.get("force", {}).get("active"):
                # Force was active but expired while Master was down
                print("BOOT: force expired during reboot — clearing")
                _clear_force(recalculate=False)
            recalc_and_act()


# ============================================================================
# SENSOR HUB MESSAGE HANDLERS
# ============================================================================

def _handle_pong(msg):
    global _hub_pong_received
    _hub_pong_received = True
    fw = msg.get("firmware_version", "?")
    print("HUB PONG received — hub firmware v{}".format(fw))
    if not isinstance(conf.get("hub_status"), dict):
        conf["hub_status"] = {}
    conf["hub_status"]["firmware_version"] = fw
    tcp_forward(msg)


def _handle_hub_ready(msg):
    global _hub_ready_received, _hub_state
    _hub_ready_received = True
    _hub_state          = "READY"
    online        = msg.get("online_count",  0)
    offline       = msg.get("offline_count", 0)
    sensor_count  = msg.get("sensor_count",  0)
    needs_pairing = msg.get("needs_pairing", False)
    fw            = msg.get("firmware_version", "?")
    if not isinstance(online,        int):  online        = 0
    if not isinstance(offline,       int):  offline       = 0
    if not isinstance(needs_pairing, bool): needs_pairing = False
    print("HUB READY: v={} sensors={} online={} offline={} "
          "needs_pairing={} unit={}".format(
          fw, sensor_count, online, offline, needs_pairing,
          msg.get("unit_state", "?")))
    conf["hub_status"]["sensor_count"]    = sensor_count
    conf["hub_status"]["firmware_version"] = fw
    save_config()
    tcp_forward(msg)
    if needs_pairing:
        print("HUB READY: no sensors registered — "
              "open pairing from Debugger or MQTT")
        tcp_forward({"type": "notification",
                     "level":   "info",
                     "message": "No sensors registered — "
                                "open pairing to add sensors"})


def _handle_sensor_joined(msg):
    """Individual sensor rejoined successfully at boot."""
    idx     = msg.get("index",  -1)
    name    = msg.get("name",   "?")
    model   = msg.get("model",  "?")
    role    = msg.get("role",   "?")
    online  = msg.get("online", False)
    battery = msg.get("battery", 0)
    print("SENSOR JOINED: [{}] {} {} {} online={} batt={}%".format(
          idx, name, model, role, online, battery))
    # Update sensor_names cache in config
    hub_cfg = conf.get("sensor_hub_config", {})
    names   = hub_cfg.get("sensor_names", {})
    if isinstance(idx, int) and idx >= 0 and isinstance(name, str):
        names[str(idx)] = name
    conf["sensor_hub_config"]["sensor_names"] = names
    save_config()
    tcp_forward(msg)


def _handle_sensor_status(msg):
    """Individual sensor failed to rejoin at boot."""
    idx    = msg.get("index",  -1)
    name   = msg.get("name",   "?")
    online = msg.get("online", False)
    print("SENSOR STATUS: [{}] {} online={}".format(idx, name, online))
    tcp_forward(msg)


def _handle_sensor_list_complete(msg):
    global _sensor_list_complete
    _sensor_list_complete = True
    total   = msg.get("total",   0)
    online  = msg.get("online",  0)
    offline = msg.get("offline", 0)
    print("SENSOR LIST COMPLETE: total={} online={} offline={}".format(
          total, online, offline))
    conf["hub_status"]["sensor_count"] = total
    save_config()
    tcp_forward(msg)


def _handle_new_sensor_joined(msg):
    """New sensor joined during pairing window."""
    idx   = msg.get("index",  -1)
    name  = msg.get("name",   "Sensor_{}".format(idx + 1))
    model = msg.get("model",  "?")
    role  = msg.get("role",   "?")
    print("NEW SENSOR JOINED: [{}] {} {} {}".format(idx, name, model, role))
    hub_cfg = conf.get("sensor_hub_config", {})
    names   = hub_cfg.get("sensor_names", {})
    if isinstance(idx, int) and idx >= 0:
        names[str(idx)] = name
    conf["sensor_hub_config"]["sensor_names"] = names
    save_config()
    tcp_forward(msg)


def _handle_pairing_complete(msg):
    new_sensors   = msg.get("new_sensors",   0)
    total_sensors = msg.get("total_sensors", 0)
    print("PAIRING COMPLETE: {} new sensors, {} total".format(
          new_sensors, total_sensors))
    conf["hub_status"]["sensor_count"] = total_sensors
    save_config()
    tcp_forward(msg)
    tcp_forward(build_state_snapshot())


def _handle_unit_occupancy(msg):
    raw = msg.get("state", "")
    if not isinstance(raw, str):
        print("WARN unit_occupancy: invalid state field")
        return
    sensor_status = "occupied" if raw.strip().upper() == "OCCUPIED" else "vacant"

    # Always store the sensor status even during force
    with _state_lock:
        state["last_sensor_status"] = sensor_status
        conf["last_sensor_status"]  = sensor_status
        save_config()

    # If force is active, do NOT act on this — just log and forward
    if _force_active():
        print("UNIT OCCUPANCY: sensor={} — FORCE ACTIVE, not acting".format(
              sensor_status))
        tcp_forward(msg)
        return

    with _state_lock:
        print("UNIT OCCUPANCY: sensor={} ntp={}".format(
              sensor_status, _ntp_synced))
        recalc_and_act()
    tcp_forward(msg)


def _handle_sensor_presence(msg):
    sensor    = msg.get("sensor", "?")
    state_val = msg.get("state", "?")
    if not isinstance(sensor, str) or not isinstance(state_val, str):
        print("WARN sensor_presence: invalid fields")
        return
    print("PRESENCE: {} {}".format(sensor, state_val))
    tcp_forward(msg)


def _handle_environment(msg):
    temp_x100 = msg.get("temp_c_x100", 0)
    hum_x100  = msg.get("hum_pct_x100", 0)
    if not isinstance(temp_x100, (int, float)): return
    if not isinstance(hum_x100,  (int, float)): return
    sensor  = msg.get("sensor", "?")
    temp_c  = temp_x100 / 100.0
    hum_pct = hum_x100  / 100.0
    print("ENV: {} {:.1f}C {:.1f}%".format(sensor, temp_c, hum_pct))
    tcp_forward(msg)


def _handle_door(msg):
    sensor    = msg.get("sensor", "?")
    state_val = msg.get("state", "?")
    if not isinstance(sensor, str) or not isinstance(state_val, str):
        print("WARN door: invalid fields")
        return
    print("DOOR: {} {}".format(sensor, state_val))
    tcp_forward(msg)


def _handle_door_alarm(msg):
    duration = msg.get("duration_sec", 0)
    if not isinstance(duration, (int, float)): duration = 0
    print("DOOR ALARM: {} {} {}s".format(
          msg.get("sensor", "?"), msg.get("state", "?"), int(duration)))
    tcp_forward(msg)


def _handle_sensor_health(msg):
    sensor    = msg.get("sensor", "?")
    state_val = msg.get("state", "?")
    if not isinstance(sensor, str) or not isinstance(state_val, str):
        print("WARN sensor_health: invalid fields")
        return
    print("SENSOR HEALTH: {} {}".format(sensor, state_val))
    # Presence sensor offline — forward as alert
    if state_val.upper() == "OFFLINE":
        tcp_forward({"type":    "notification",
                     "level":   "warning",
                     "sensor":  sensor,
                     "message": "Sensor {} is OFFLINE".format(sensor)})
    tcp_forward(msg)


def _handle_battery(msg):
    sensor = msg.get("sensor", "?")
    pct    = msg.get("battery_pct", 0)
    if not isinstance(pct, (int, float)): return
    pct = int(pct)
    print("BATTERY: {} {}%".format(sensor, pct))
    tcp_forward(msg)


def _handle_heartbeat(msg):
    unit = msg.get("unit_state", "?")
    fw   = msg.get("firmware_version", "?")
    if not isinstance(unit, str): return
    print("HEARTBEAT: unit={} fw={}".format(unit, fw))
    tcp_forward(msg)


def _handle_config_response(msg):
    sensors = msg.get("sensors", [])
    if not isinstance(sensors, list): return
    hub_cfg = conf.get("sensor_hub_config", {})
    names   = hub_cfg.get("sensor_names", {})
    for s in sensors:
        if not isinstance(s, dict): continue
        idx  = s.get("index")
        name = s.get("name")
        if idx is not None and name and isinstance(name, str):
            names[str(idx)] = name
    conf["sensor_hub_config"]["sensor_names"] = names
    save_config()
    print("CONFIG RESPONSE: {} sensors".format(len(sensors)))
    tcp_forward(msg)


def _handle_ack(msg):
    global _hub_init_acked, _watchdog_start_acked, _hub_config_push_in_progress
    cmd    = msg.get("command", "?")
    status = msg.get("status",  "?")
    if not isinstance(cmd, str): return
    print("ACK: command={} status={}".format(cmd, status))

    if cmd == "hub_init" and status == "ok":
        _hub_init_acked = True
    elif cmd == "start_watchdog" and status == "ok":
        _watchdog_start_acked = True
    elif cmd == "set_config" and status == "ok":
        _hub_config_push_in_progress = False
        print("HUB CONFIG: set_config confirmed applied")

    tcp_forward(msg)


def _handle_log_response(msg):
    line = msg.get("line", "")
    if not isinstance(line, str): line = str(line)
    print("HUB LOG:", line)
    tcp_forward(msg)


def _handle_boot_fault_internal(reason):
    """Called when boot controller detects a fault."""
    global _hub_state, _boot_phase
    _hub_state  = "FAULT"
    _boot_phase = "FAULT"
    tcp_forward({"type":    "notification",
                 "level":   "error",
                 "message": "Sensor Hub fault: {}".format(reason)})


_SENSOR_MSG_HANDLERS = {
    "pong":                 _handle_pong,
    "hub_ready":            _handle_hub_ready,
    "sensor_joined":        _handle_sensor_joined,
    "sensor_status":        _handle_sensor_status,
    "sensor_list_complete": _handle_sensor_list_complete,
    "new_sensor_joined":    _handle_new_sensor_joined,
    "pairing_complete":     _handle_pairing_complete,
    "unit_occupancy":       _handle_unit_occupancy,
    "sensor_presence":      _handle_sensor_presence,
    "environment":          _handle_environment,
    "door":                 _handle_door,
    "door_alarm":           _handle_door_alarm,
    "sensor_health":        _handle_sensor_health,
    "battery":              _handle_battery,
    "heartbeat":            _handle_heartbeat,
    "config_response":      _handle_config_response,
    "ack":                  _handle_ack,
    "log_response":         _handle_log_response,
}


def handle_sensor_msg(msg):
    msg_type = msg.get("type")
    if not msg_type or not isinstance(msg_type, str):
        print("WARN: sensor msg missing 'type'")
        return
    handler = _SENSOR_MSG_HANDLERS.get(msg_type)
    if handler:
        try:
            handler(msg)
        except Exception as e:
            print("ERROR in handler for '{}': {}".format(msg_type, repr(e)))
    else:
        print("WARN: unknown sensor msg type '{}'".format(msg_type))


# ============================================================================
# SCHEDULER MESSAGE HANDLER
# ============================================================================

def handle_scheduler_msg(msg):
    msg_type = msg.get("type")
    if msg_type == "scheduler_update":
        st = msg.get("status")
        if not isinstance(st, str): return
        with _state_lock:
            state["last_scheduler_status"] = st
            conf["last_scheduler_status"]  = st
            save_config()
        print("SCHED: status={} relays={}".format(st, msg.get("relays", {})))
        tcp_forward(msg)
    else:
        print("WARN: unknown scheduler msg type '{}'".format(msg_type))


# ============================================================================
# SENSOR HUB COMMAND SENDERS
# ============================================================================

def hub_cmd_set_sensor_name(sensor_index, name):
    if not isinstance(sensor_index, int) or sensor_index < 0: return
    if not isinstance(name, str) or len(name) == 0: return
    send_to_sensor_hub({
        "type":         "set_sensor_name",
        "sensor_index": sensor_index,
        "name":         name
    })


def hub_cmd_get_config():
    send_to_sensor_hub({"type": "get_config"})


def hub_cmd_get_logs():
    send_to_sensor_hub({"type": "get_logs", "lines": 50})


def hub_cmd_start_pairing(duration_sec=120):
    send_to_sensor_hub({"type": "start_pairing", "duration_sec": duration_sec})
    print("PAIRING OPENED: {}s".format(duration_sec))


def hub_cmd_stop_pairing():
    send_to_sensor_hub({"type": "stop_pairing"})


def hub_cmd_remove_sensor(sensor_index):
    send_to_sensor_hub({"type": "remove_sensor", "sensor_index": sensor_index})


def hub_cmd_factory_reset():
    send_to_sensor_hub({"type": "factory_reset"})


def hub_cmd_restart():
    send_to_sensor_hub({"type": "restart"})


def hub_cmd_push_live_config():
    """Push updated config to Hub via set_config (live update, not boot)."""
    global _hub_config_push_in_progress
    hub_cfg = conf.get("sensor_hub_config", {})
    payload = {
        "type":                          "set_config",
        "pairing_duration_sec":          hub_cfg.get("pairing_duration_sec",          120),
        "watchdog_enable":               hub_cfg.get("watchdog_enable",                True),
        "watchdog_interval_min":         hub_cfg.get("watchdog_interval_min",          60),
        "watchdog_ping_timeout_sec":     hub_cfg.get("watchdog_ping_timeout_sec",      30),
        "door_alarm_threshold_min":      hub_cfg.get("door_alarm_threshold_min",       10),
        "heartbeat_interval_min":        hub_cfg.get("heartbeat_interval_min",         30),
        "presence_fading_time_sec":      hub_cfg.get("presence_fading_time_sec",       30),
        "door_sensor_max_silence_hours": hub_cfg.get("door_sensor_max_silence_hours",  24),
    }
    _hub_config_push_in_progress = True
    send_to_sensor_hub(payload)


# ============================================================================
# TCP COMMAND HANDLERS
# ============================================================================

def _tcp_send_ack(command, status="ok"):
    tcp_forward({"type": "ack", "command": command, "status": status})


def handle_tcp_command(msg):
    global _hub_config_push_in_progress

    t = msg.get("type", "")

    if t == "get_state":
        tcp_forward(build_state_snapshot())

    elif t == "get_sensor_config":
        hub_cmd_get_config()
        _tcp_send_ack("get_sensor_config")

    elif t == "set_sensor_name":
        idx  = msg.get("sensor_index")
        name = msg.get("name", "")
        if isinstance(idx, int) and name:
            hub_cmd_set_sensor_name(idx, name)
            _tcp_send_ack("set_sensor_name")
        else:
            _tcp_send_ack("set_sensor_name", "error")

    elif t == "start_pairing":
        dur = msg.get("duration_sec", 120)
        hub_cmd_start_pairing(int(dur))
        _tcp_send_ack("start_pairing")

    elif t == "stop_pairing":
        hub_cmd_stop_pairing()
        _tcp_send_ack("stop_pairing")

    elif t == "get_hub_logs":
        hub_cmd_get_logs()
        _tcp_send_ack("get_hub_logs")

    elif t == "hub_restart":
        hub_cmd_restart()
        _tcp_send_ack("hub_restart")

    elif t == "hub_factory_reset":
        hub_cmd_factory_reset()
        _tcp_send_ack("hub_factory_reset")

    elif t == "master_restart":
        _tcp_send_ack("master_restart")
        utime.sleep_ms(300)
        reset()

    elif t == "force_status":
        # Force command — duration in hours, minimum 1, maximum 24
        status_val = msg.get("status", "")
        dur_hours  = msg.get("duration_hours", 0)
        reason     = msg.get("reason", "Manual force")

        if status_val not in ("Occupied", "Vacant",
                              "Sold Vacant", "UnSold Occupied"):
            _tcp_send_ack("force_status", "error")
            print("FORCE: invalid status '{}'".format(status_val))
            return

        if not isinstance(dur_hours, int) or dur_hours < 1 or dur_hours > 24:
            _tcp_send_ack("force_status", "error")
            print("FORCE: invalid duration_hours {} "
                  "(must be 1-24)".format(dur_hours))
            return

        _apply_force(status_val, dur_hours, reason)
        _tcp_send_ack("force_status")
        tcp_forward(build_state_snapshot())

    elif t == "cancel_force":
        _clear_force(recalculate=True)
        _tcp_send_ack("cancel_force")
        tcp_forward(build_state_snapshot())
        print("FORCE cancelled by operator")

    elif t == "cancel_pending":
        with _state_lock:
            state["pending_status"]      = None
            state["pending_apply_epoch"] = 0
        tcp_forward({"type": "pending_update", "pending_status": None})
        # Re-send current status so debugger is always correct
        current = state.get("current_decided_status")
        if current:
            tcp_forward({"type": "unit_state_update", "status": current})
        _tcp_send_ack("cancel_pending")
        print("PENDING cancelled by operator")

    elif t == "set_unit_config":
        for key in ("tenant_id", "unit_id", "check_in_utc", "check_out_utc",
                    "buffer_minutes"):
            if key in msg:
                val = msg[key]
                if key == "buffer_minutes":
                    try:   val = int(val)
                    except Exception: val = 15
                conf[key] = val
        save_config()
        if not _force_active():
            with _state_lock:
                recalc_and_act()
        _tcp_send_ack("set_unit_config")
        print("UNIT CONFIG updated from debugger")
        tcp_forward(build_state_snapshot())

    elif t == "set_hub_config":
        hub_cfg = conf.get("sensor_hub_config", {})
        for key in ("pairing_duration_sec", "watchdog_interval_min",
                    "watchdog_ping_timeout_sec", "door_alarm_threshold_min",
                    "heartbeat_interval_min", "presence_fading_time_sec",
                    "door_sensor_max_silence_hours"):
            if key in msg:
                try:    hub_cfg[key] = int(msg[key])
                except Exception: pass
        if "watchdog_enable" in msg:
            hub_cfg["watchdog_enable"] = bool(msg["watchdog_enable"])
        conf["sensor_hub_config"] = hub_cfg
        save_config()
        with _state_lock:
            if not _hub_config_push_in_progress:
                _hub_config_push_in_progress = True
                _thread.start_new_thread(
                    lambda: (hub_cmd_push_live_config()), ())
        _tcp_send_ack("set_hub_config")
        print("HUB CONFIG updated — pushing to Hub")
        tcp_forward(build_state_snapshot())

    elif t == "set_wifi_config":
        ssid = msg.get("wifi_ssid", "")
        pwd  = msg.get("wifi_password", "")
        if ssid:
            conf["wifi_ssid"]     = ssid
            conf["wifi_password"] = pwd
            save_config()
            _tcp_send_ack("set_wifi_config")
            print("WIFI CONFIG saved — reboot to apply")
        else:
            _tcp_send_ack("set_wifi_config", "error")

    elif t == "remove_sensor":
        idx = msg.get("sensor_index")
        if isinstance(idx, int) and idx >= 0:
            hub_cmd_remove_sensor(idx)
            _tcp_send_ack("remove_sensor")
        else:
            _tcp_send_ack("remove_sensor", "error")

    elif t == "scheduler_restart":
        send_to_scheduler({"type": "restart"})
        _tcp_send_ack("scheduler_restart")

    elif t == "scheduler_factory_reset":
        send_to_scheduler({"type": "factory_reset"})
        _tcp_send_ack("scheduler_factory_reset")

    elif t == "ntp_sync":
        ok = _sync_ntp()
        _tcp_send_ack("ntp_sync", "ok" if ok else "error")
        if ok and not _force_active():
            with _state_lock:
                recalc_and_act()
            tcp_forward(build_state_snapshot())

    elif t == "start_watchdog":
        # Operator can also manually trigger start_watchdog
        send_to_sensor_hub({"type": "start_watchdog"})
        _tcp_send_ack("start_watchdog")

    else:
        print("WARN: unknown TCP command '{}'".format(t))


# ============================================================================
# UART RX LOOPS
# ============================================================================

def _process_uart_line(line_bytes, handler_fn):
    try:
        s = bytes(line_bytes).decode("utf-8").strip()
    except Exception:
        print("WARN: UART UTF-8 decode failed")
        return False
    if not s:
        return False
    try:
        msg = json.loads(s)
    except Exception:
        print("WARN: UART bad JSON:", s[:80])
        return False
    if not isinstance(msg, dict):
        print("WARN: UART JSON not a dict")
        return False
    handler_fn(msg)
    return True


def sensor_rx_loop():
    global _rx_sensor_pos
    while True:
        try:
            if uart_sensor.any():
                data = uart_sensor.read()
                if data:
                    for b in data:
                        ch = b if isinstance(b, int) else ord(b)
                        if ch < 0x20 and ch != 0x09 \
                                     and ch != 0x0A \
                                     and ch != 0x0D:
                            if _rx_sensor_pos > 0:
                                print("WARN: RX noise 0x{:02x} — reset".format(ch))
                                _rx_sensor_pos = 0
                            continue
                        if ch == ord('\r'):
                            continue
                        if ch == ord('\n'):
                            if _rx_sensor_pos > 0:
                                if _rx_sensor_buf[0] != ord('{'):
                                    print("WARN: RX non-JSON 0x{:02x}".format(
                                          _rx_sensor_buf[0]))
                                    _rx_sensor_pos = 0
                                else:
                                    _process_uart_line(
                                        memoryview(_rx_sensor_buf)
                                        [:_rx_sensor_pos],
                                        handle_sensor_msg)
                                    _rx_sensor_pos = 0
                        else:
                            if _rx_sensor_pos >= UART_RX_BUF_MAX - 1:
                                print("WARN: RX buf overflow")
                                _rx_sensor_pos = 0
                            else:
                                _rx_sensor_buf[_rx_sensor_pos] = ch
                                _rx_sensor_pos += 1
            utime.sleep_ms(30)
        except Exception as e:
            print("sensor_rx_loop error:", repr(e))
            _rx_sensor_pos = 0
            utime.sleep_ms(200)


def sched_rx_loop():
    """Scheduler UART disabled until Phase 4."""
    while True:
        utime.sleep_ms(500)


# ============================================================================
# PENDING BUFFER WORKER + FORCE EXPIRY CHECK
# ============================================================================

def pending_worker():
    """
    Runs every second.
    1. Checks force expiry — clears and recalculates if expired.
    2. Checks pending buffer — applies when timer expires.
    """
    while True:
        try:
            with _state_lock:

                # ── Force expiry check ────────────────────────────────────
                f = conf.get("force", {})
                if f.get("active", False):
                    expires_ep = f.get("expires_epoch", 0)
                    now_ep     = now_epoch_utc()
                    if expires_ep > 0 and now_ep >= expires_ep:
                        print("FORCE EXPIRED — returning to automatic")
                        # Clear force inside lock — recalc will run below
                        conf["force"] = {
                            "active":        False,
                            "status":        "",
                            "expires_utc":   "",
                            "expires_epoch": 0,
                            "reason":        ""
                        }
                        save_config()
                        tcp_forward({"type": "force_update", "active": False})
                        # Buffer restarts fresh from now if natural state
                        # would be Vacant or Sold Vacant
                        recalc_and_act()

                # ── Pending buffer check ──────────────────────────────────
                if not _force_active():
                    p = state["pending_status"]
                    t = state["pending_apply_epoch"]
                    if p is not None:
                        now_ep = now_epoch_utc()
                        if now_ep >= t:
                            latest = decide_status(
                                state["last_sensor_status"], now_ep)
                            print("PENDING due: pending={} latest={}".format(
                                  p, latest))
                            if latest == p:
                                apply_immediate(p)
                            else:
                                print("PENDING cancelled — conditions changed "
                                      "to {}".format(latest))
                                apply_immediate(latest, force_send=True)
                            state["pending_status"]      = None
                            state["pending_apply_epoch"] = 0
                            tcp_forward({"type":           "pending_update",
                                         "pending_status": None})

            utime.sleep(1)
        except Exception as e:
            print("pending_worker error:", repr(e))
            utime.sleep(1)


# ============================================================================
# TCP DEBUG SERVER
# ============================================================================

def _maybe_start_tcp_server():
    global _tcp_server_started
    if not _tcp_server_started:
        _tcp_server_started = True
        _thread.start_new_thread(tcp_server_thread, ())
        print("TCP server started on port {}".format(TCP_PORT))


def tcp_server_thread():
    """
    Handles one debugger client at a time.
    On connect: switch to Debug mode, pause MQTT.
    On disconnect: switch to Production mode, resume MQTT.
    """
    global _tcp_client, _debug_mode

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("0.0.0.0", TCP_PORT)[0][-1])
    srv.listen(1)
    print("TCP: listening on port {}".format(TCP_PORT))

    rx_pos = 0

    while True:
        try:
            conn, addr = srv.accept()
            old = _tcp_client
            _tcp_client  = conn
            _debug_mode  = True
            if old:
                try:    old.close()
                except Exception: pass
            print("DEBUGGER connected from {} — Debug mode".format(addr))
            tcp_forward(build_state_snapshot())
            rx_pos = 0

            while True:
                try:
                    data = conn.recv(256)
                except Exception:
                    data = None
                if not data:
                    break
                for b in data:
                    ch = b if isinstance(b, int) else ord(b)
                    if ch == ord('\r'):
                        continue
                    if ch == ord('\n'):
                        if rx_pos > 0:
                            try:
                                s   = bytes(_rx_tcp_buf[:rx_pos]).decode(
                                          "utf-8").strip()
                                msg = json.loads(s)
                                if isinstance(msg, dict):
                                    handle_tcp_command(msg)
                            except Exception as e:
                                print("TCP parse error:", repr(e))
                            rx_pos = 0
                    else:
                        if rx_pos >= TCP_RX_BUF_MAX - 1:
                            print("WARN: TCP RX overflow")
                            rx_pos = 0
                        _rx_tcp_buf[rx_pos] = ch
                        rx_pos += 1

        except Exception as e:
            print("TCP server error:", repr(e))
            utime.sleep_ms(500)
        finally:
            if _tcp_client is not None:
                try:    _tcp_client.close()
                except Exception: pass
                _tcp_client = None
            _debug_mode = False
            print("DEBUGGER disconnected — Production mode")


# ============================================================================
# INTERNET + NTP
# ============================================================================

def _check_internet():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(INTERNET_CHECK_TIMEOUT_S)
        s.connect(socket.getaddrinfo(INTERNET_CHECK_HOST,
                                     INTERNET_CHECK_PORT)[0][-1])
        s.close()
        return True
    except Exception:
        return False


def _set_internet_up(new_state):
    global _internet_up
    changed      = (_internet_up != new_state)
    _internet_up = new_state
    if changed:
        label = "RESTORED" if new_state else "LOST"
        print("INTERNET: {}".format(label))
        tcp_forward({"type": "internet_status",
                     "status": "up" if new_state else "down"})
        if not _force_active():
            with _state_lock:
                recalc_and_act()


def wifi_and_internet_thread():
    global _wifi_ip, _ntp_synced, _last_ntp_sync

    ssid = conf.get("wifi_ssid", "")
    pwd  = conf.get("wifi_password", "")

    if not ssid:
        print("WIFI: no credentials — 2-state fallback")
        with _state_lock:
            if not _force_active():
                recalc_and_act()
        while True:
            utime.sleep(INTERNET_RECHECK_S)

    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    print("WIFI: connecting to '{}'...".format(ssid))
    wlan.connect(ssid, pwd)
    deadline = utime.ticks_add(utime.ticks_ms(), 30000)
    while not wlan.isconnected():
        if utime.ticks_diff(deadline, utime.ticks_ms()) <= 0:
            break
        utime.sleep_ms(500)

    if not wlan.isconnected():
        print("WIFI: failed — 2-state fallback")
        with _state_lock:
            if not _force_active():
                recalc_and_act()
        while True:
            utime.sleep(30)
            try:
                wlan.connect(ssid, pwd)
                t2 = utime.ticks_add(utime.ticks_ms(), 15000)
                while not wlan.isconnected():
                    if utime.ticks_diff(t2, utime.ticks_ms()) <= 0:
                        break
                    utime.sleep_ms(500)
                if wlan.isconnected():
                    cfg_info = wlan.ifconfig()
                    _wifi_ip = cfg_info[0]
                    print("WIFI: reconnected IP={}".format(_wifi_ip))
                    _maybe_start_tcp_server()
                    break
            except Exception as e:
                print("WIFI retry error:", repr(e))

    if wlan.isconnected():
        cfg_info = wlan.ifconfig()
        _wifi_ip = cfg_info[0]
        print("WIFI: connected IP={}".format(_wifi_ip))
        _maybe_start_tcp_server()

    retries       = 3
    internet_found = False
    for attempt in range(retries):
        if _check_internet():
            internet_found = True
            break
        print("INTERNET: check {}/{} failed".format(attempt + 1, retries))
        utime.sleep(2)

    if internet_found:
        print("INTERNET: confirmed")
        for ntp_attempt in range(3):
            if _sync_ntp():
                break
            print("NTP: attempt {}/3 failed".format(ntp_attempt + 1))
            utime.sleep(5)
        _set_internet_up(True)
    else:
        print("INTERNET: no connectivity — 2-state fallback")
        _set_internet_up(False)
        with _state_lock:
            if not _force_active():
                recalc_and_act()

    while True:
        utime.sleep(INTERNET_RECHECK_S)
        if not wlan.isconnected():
            _set_internet_up(False)
            continue
        result = _check_internet()
        _set_internet_up(result)
        if result and _ntp_synced:
            elapsed = utime.ticks_diff(utime.ticks_ms(), _last_ntp_sync)
            if elapsed >= NTP_SYNC_INTERVAL_S * 1000:
                _sync_ntp()


# ============================================================================
# STARTUP
# ============================================================================

load_config()

state["last_sensor_status"]     = str(
    conf.get("last_sensor_status", "vacant")).lower()
state["last_scheduler_status"]  = conf.get("last_scheduler_status", None)
state["current_decided_status"] = conf.get("last_decided_status", "Vacant")

try:
    import ubinascii
    wlan_tmp = network.WLAN(network.STA_IF)
    wlan_tmp.active(True)
    _mac_str = ubinascii.hexlify(
        wlan_tmp.config("mac"), ":").decode().upper()
except Exception:
    _mac_str = "unknown"

print("=" * 52)
print("MASTER v{}  ({})".format(cfg.FIRMWARE_VERSION, cfg.FIRMWARE_COMPONENT))
print("MAC          :", _mac_str)
print("UART1 Hub    : TX=GPIO{}  RX=GPIO{}  BAUD={}".format(
      cfg.SENSOR_UART_TX, cfg.SENSOR_UART_RX, cfg.SENSOR_UART_BAUD))
print("UART2 Sched  : DISABLED (Phase 4)")
print("check_in     :", conf.get("check_in_utc"))
print("check_out    :", conf.get("check_out_utc"))
print("buffer_min   :", conf.get("buffer_minutes"))
print("tenant_id    :", conf.get("tenant_id"))
print("unit_id      :", conf.get("unit_id"))
print("mode         :", conf.get("mode", "production"))
force_cfg = conf.get("force", {})
if force_cfg.get("active") and force_cfg.get("expires_epoch", 0) > now_epoch_utc():
    print("FORCE ACTIVE : {} until {}".format(
          force_cfg.get("status", "?"), force_cfg.get("expires_utc", "?")))
print("=" * 52)

# Start threads
_thread.start_new_thread(sensor_rx_loop,            ())
_thread.start_new_thread(sched_rx_loop,             ())
_thread.start_new_thread(pending_worker,            ())
_thread.start_new_thread(wifi_and_internet_thread,  ())
_thread.start_new_thread(_boot_controller_thread,   ())

# ============================================================================
# MAIN LOOP
# ============================================================================

while True:
    utime.sleep_ms(MAIN_LOOP_TICK_MS)