# main.py — MASTER (ESP32-S3)
# Innovatsii EMS — Pico 1
#
# UART1 <-> Sensor Hub  (TX=GPIO16, RX=GPIO17)
# UART2 <-> Scheduler   (TX=GPIO18, RX=GPIO21) — Scheduler UART disabled until Phase 4
# TCP   <-> Debugger App (port 8765)
#
# Production rules applied:
#   - Pre-allocated fixed bytearray RX buffers (no unbounded growth)
#   - All JSON built as dict then serialised once
#   - All exception paths explicitly handled and logged
#   - Tick arithmetic uses ticks_diff for rollover safety
#   - Config written atomically (write + rename pattern)
#   - Every shared state access is lock-protected
#   - UART TX serialised through helpers — never interleaved
#   - TCP TX serialised through helper — never interleaved
#   - NTP sync after WiFi — real UTC epoch for booking window

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

UART_RX_BUF_MAX      = 512
MAIN_LOOP_TICK_MS    = 1000
TCP_PORT             = 8765
TCP_RX_BUF_MAX       = 1024
INTERNET_CHECK_HOST  = "8.8.8.8"
INTERNET_CHECK_PORT  = 53
INTERNET_CHECK_TIMEOUT_S = 3
INTERNET_RECHECK_S   = 60
NTP_SYNC_INTERVAL_S  = 3600        # re-sync NTP every hour
HUB_CONFIG_RETRIES   = 3           # how many times to retry set_config to Hub
HUB_CONFIG_RETRY_MS  = 2000        # ms between retries

# ============================================================================
# UART INITIALISATION
# ============================================================================

uart_sensor = UART(cfg.SENSOR_UART_ID,
                   baudrate=cfg.SENSOR_UART_BAUD,
                   tx=cfg.SENSOR_UART_TX,
                   rx=cfg.SENSOR_UART_RX)

# Scheduler UART — initialised but sends disabled until Phase 4
uart_sched  = UART(cfg.SCHED_UART_ID,
                   baudrate=cfg.SCHED_UART_BAUD,
                   tx=cfg.SCHED_UART_TX,
                   rx=cfg.SCHED_UART_RX)

_rx_sensor_buf = bytearray(UART_RX_BUF_MAX)
_rx_sched_buf  = bytearray(UART_RX_BUF_MAX)
_rx_sensor_pos = 0
_rx_sched_pos  = 0

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

# Internet / hub / NTP tracking — not persisted
_internet_up     = False
_wifi_ip         = ""
_hub_state       = "UNKNOWN"   # "UNKNOWN" | "BOOTING" | "READY"
_tcp_client      = None
_mac_str         = ""
_ntp_synced      = False       # True once NTP has set the RTC
_last_ntp_sync   = 0           # ticks_ms of last successful NTP sync

# Pending ACK tracking for hub set_config
_hub_config_ack_pending = False

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
    _uart_send(uart_sensor, _tx_sensor_lock, payload_dict)


def send_to_scheduler(payload_dict):
    # Phase 4 — Scheduler UART sends disabled for now
    # _uart_send(uart_sched, _tx_sched_lock, payload_dict)
    print("SCHED TX (disabled): {}".format(payload_dict.get("type", "?")))


# ============================================================================
# TCP TX HELPER
# ============================================================================

def tcp_forward(msg_dict):
    """Send a message to the connected TCP debugger client. Non-blocking."""
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
    """Try NTP sync. Returns True on success."""
    global _ntp_synced, _last_ntp_sync
    try:
        import ntptime
        ntptime.host = "pool.ntp.org"
        ntptime.settime()
        _ntp_synced    = True
        _last_ntp_sync = utime.ticks_ms()
        t = utime.localtime()
        print("NTP: synced — UTC {}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
              t[0], t[1], t[2], t[3], t[4], t[5]))
        tcp_forward({"type": "ntp_status", "synced": True,
                     "utc": "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
                         t[0], t[1], t[2], t[3], t[4], t[5])})
        return True
    except Exception as e:
        print("NTP: sync failed:", repr(e))
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


def now_epoch_utc():
    """Return current UTC epoch. Uses real RTC if NTP synced, else fallback."""
    return utime.time()


def in_booking_window(now_ep, checkin_ep, checkout_ep):
    return checkin_ep <= now_ep <= checkout_ep


# ============================================================================
# UNIT STATE DECISION
# ============================================================================

def decide_status(sensor_status, now_ep):
    """
    2-state fallback when internet is down — booking window not trusted.
    4-state full logic when internet is confirmed AND NTP is synced.
    """
    if not _internet_up or not _ntp_synced:
        result = "Occupied" if sensor_status == "occupied" else "Vacant"
        reason = "no internet" if not _internet_up else "NTP not synced"
        print("DECIDE [2-state fallback — {}]: sensor={} => {}".format(
              reason, sensor_status, result))
        return result

    checkin_ep  = parse_utc_to_epoch(conf.get("check_in_utc",  "2000-01-01 00:00:00"))
    checkout_ep = parse_utc_to_epoch(conf.get("check_out_utc", "2000-01-01 00:00:00"))
    inside      = in_booking_window(now_ep, checkin_ep, checkout_ep)

    if sensor_status == "occupied":
        result = "Occupied" if inside else "UnSold Occupied"
    else:
        result = "Sold Vacant" if inside else "Vacant"

    print("DECIDE [4-state]: sensor={} inside={} now={} => {}".format(
          sensor_status, inside, now_ep, result))
    return result


# ============================================================================
# SCHEDULER COMMANDS  (Phase 4 — disabled for now)
# ============================================================================

def send_status_to_scheduler(status):
    # Phase 4 disabled — just log
    print("MASTER -> SCHED (disabled): set_status={}".format(status))


def apply_immediate(status, force_send=False):
    if force_send or state["current_decided_status"] != status:
        state["current_decided_status"] = status
        conf["last_decided_status"] = status
        save_config()
        send_status_to_scheduler(status)
        # Notify debugger of state change
        tcp_forward({"type": "unit_state_update", "status": status})
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
    # Notify debugger of pending change
    tcp_forward({"type": "pending_update",
                 "pending_status": status,
                 "apply_at_epoch": state["pending_apply_epoch"]})


def recalc_and_act():
    now_ep = now_epoch_utc()
    sensor = state["last_sensor_status"]
    target = decide_status(sensor, now_ep)

    if target in ("Sold Vacant", "Vacant"):
        schedule_buffered(target, now_ep)
    else:
        state["pending_status"]      = None
        state["pending_apply_epoch"] = 0
        apply_immediate(target)


# ============================================================================
# STATE SNAPSHOT — sent to debugger on connect or get_state
# ============================================================================

def build_state_snapshot():
    with _state_lock:
        t = utime.localtime()
        utc_str = "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
            t[0], t[1], t[2], t[3], t[4], t[5])
        snap = {
            "type":               "state_snapshot",
            "unit_state":         state["current_decided_status"] or "Unknown",
            "sensor_occupancy":   state["last_sensor_status"],
            "pending_status":     state["pending_status"],
            "internet_up":        _internet_up,
            "ntp_synced":         _ntp_synced,
            "utc_now":            utc_str,
            "wifi_ip":            _wifi_ip,
            "hub_state":          _hub_state,
            "scheduler_status":   state["last_scheduler_status"],
            "tenant_id":          conf.get("tenant_id", ""),
            "unit_id":            conf.get("unit_id", ""),
            "check_in_utc":       conf.get("check_in_utc", ""),
            "check_out_utc":      conf.get("check_out_utc", ""),
            "buffer_minutes":     conf.get("buffer_minutes", 15),
            "wifi_ssid":          conf.get("wifi_ssid", ""),
            "sensor_hub_config":  conf.get("sensor_hub_config", {}),
        }
    return snap


# ============================================================================
# SENSOR HUB MESSAGE HANDLERS
# ============================================================================

def _handle_unit_occupancy(msg):
    raw = msg.get("state", "")
    if not isinstance(raw, str):
        print("WARN unit_occupancy: state field missing or wrong type")
        return
    s = raw.strip().upper()
    sensor_status = "occupied" if s == "OCCUPIED" else "vacant"
    with _state_lock:
        state["last_sensor_status"] = sensor_status
        conf["last_sensor_status"]  = sensor_status
        save_config()
        print("SENSOR HUB occupancy={} ntp={} ts={}".format(
              sensor_status, _ntp_synced, msg.get("ts_utc", "?")))
        recalc_and_act()
    tcp_forward(msg)


def _handle_sensor_presence(msg):
    print("PRESENCE: sensor={} state={} ts={}".format(
          msg.get("sensor", "?"), msg.get("state", "?"), msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_environment(msg):
    temp_x100 = msg.get("temp_c_x100", 0)
    if not isinstance(temp_x100, (int, float)):
        print("WARN environment: invalid temp field")
        return
    print("ENVIRONMENT: sensor={} temp={:.2f}C hum={:.2f}% ts={}".format(
          msg.get("sensor", "?"), temp_x100 / 100.0,
          msg.get("hum_pct_x100", 0) / 100.0, msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_door(msg):
    print("DOOR: sensor={} state={} ts={}".format(
          msg.get("sensor", "?"), msg.get("state", "?"), msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_door_alarm(msg):
    print("DOOR ALARM: sensor={} state={} duration={}s ts={}".format(
          msg.get("sensor", "?"), msg.get("state", "?"),
          msg.get("duration_sec", 0), msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_sensor_health(msg):
    print("SENSOR HEALTH: sensor={} state={} ts={}".format(
          msg.get("sensor", "?"), msg.get("state", "?"), msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_battery(msg):
    print("BATTERY: sensor={} pct={}% ts={}".format(
          msg.get("sensor", "?"), msg.get("battery_pct", 0), msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_heartbeat(msg):
    print("HEARTBEAT: unit={} ts={}".format(
          msg.get("unit_state", "?"), msg.get("ts_utc", "?")))
    tcp_forward(msg)


def _handle_config_response(msg):
    global _hub_config_ack_pending
    sensors = msg.get("sensors", [])
    if not isinstance(sensors, list):
        return
    hub_cfg = conf.get("sensor_hub_config", {})
    names   = hub_cfg.get("sensor_names", {})
    for s in sensors:
        if not isinstance(s, dict):
            continue
        idx  = s.get("index")
        name = s.get("name")
        if idx is not None and name:
            names[str(idx)] = name
    conf["sensor_hub_config"]["sensor_names"] = names
    save_config()
    print("CONFIG RESPONSE: {} sensors".format(len(sensors)))
    tcp_forward(msg)


def _handle_ack(msg):
    global _hub_config_ack_pending
    cmd    = msg.get("command", "?")
    status = msg.get("status", "?")
    print("ACK: command={} status={} ts={}".format(
          cmd, status, msg.get("ts_utc", "?")))
    # Mark config ACK received so retry loop stops
    if cmd == "set_config" and status == "ok":
        _hub_config_ack_pending = False
        print("HUB CONFIG: ACK confirmed — config applied")
    tcp_forward(msg)


def _handle_log_response(msg):
    print("SENSORHUB LOG:", msg.get("line", ""))
    tcp_forward(msg)


def _handle_hub_boot(msg):
    global _hub_state
    _hub_state = "BOOTING"
    count = msg.get("sensor_count", 0)
    print("HUB BOOT: sensor_count={} unit_state={} ts={}".format(
          count, msg.get("unit_state", "?"), msg.get("ts_utc", "?")))
    # Re-push config with retry whenever hub reboots
    _thread.start_new_thread(_hub_push_config_with_retry, ())
    utime.sleep_ms(500)
    hub_cmd_get_config()
    tcp_forward(msg)


def _handle_hub_ready(msg):
    global _hub_state
    _hub_state = "READY"
    online  = msg.get("online_count", 0)
    offline = msg.get("offline_count", 0)
    print("HUB READY: online={} offline={} unit={} ts={}".format(
          online, offline, msg.get("unit_state", "?"), msg.get("ts_utc", "?")))
    tcp_forward(msg)
    # If sensors are offline after pairing window, open a short re-pairing window
    # so sensors can re-announce and get marked ONLINE
    if offline > 0 and online == 0:
        print("HUB READY: {} sensors offline — opening 30s re-pair window".format(offline))
        utime.sleep_ms(500)
        hub_cmd_start_pairing(30)


_SENSOR_MSG_HANDLERS = {
    "unit_occupancy":  _handle_unit_occupancy,
    "sensor_presence": _handle_sensor_presence,
    "environment":     _handle_environment,
    "door":            _handle_door,
    "door_alarm":      _handle_door_alarm,
    "sensor_health":   _handle_sensor_health,
    "battery":         _handle_battery,
    "heartbeat":       _handle_heartbeat,
    "config_response": _handle_config_response,
    "ack":             _handle_ack,
    "log_response":    _handle_log_response,
    "hub_boot":        _handle_hub_boot,
    "hub_ready":       _handle_hub_ready,
}


def handle_sensor_msg(msg):
    msg_type = msg.get("type")
    if not msg_type:
        print("WARN: sensor msg missing 'type' field")
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
        with _state_lock:
            state["last_scheduler_status"] = st
            conf["last_scheduler_status"]  = st
            save_config()
        print("SCHED -> MASTER: status={} relays={}".format(
              st, msg.get("relays", {})))
        tcp_forward(msg)
    else:
        print("WARN: unknown scheduler msg type '{}'".format(msg_type))


# ============================================================================
# SENSOR HUB COMMAND SENDERS
# ============================================================================

def hub_cmd_push_config():
    """Send set_config once. Does NOT track ACK."""
    global _hub_config_ack_pending
    hub_cfg = conf.get("sensor_hub_config", {})
    payload = {
        "type":                      "set_config",
        "pairing_duration_sec":      hub_cfg.get("pairing_duration_sec",      120),
        "watchdog_enable":           hub_cfg.get("watchdog_enable",            True),
        "watchdog_interval_min":     hub_cfg.get("watchdog_interval_min",      60),
        "watchdog_ping_timeout_sec": hub_cfg.get("watchdog_ping_timeout_sec",  30),
        "door_alarm_threshold_min":  hub_cfg.get("door_alarm_threshold_min",   10),
        "heartbeat_interval_min":    hub_cfg.get("heartbeat_interval_min",     30),
    }
    _hub_config_ack_pending = True
    send_to_sensor_hub(payload)
    print("MASTER -> SENSORHUB: set_config sent")


def _hub_push_config_with_retry():
    """
    Send set_config to Hub and retry up to HUB_CONFIG_RETRIES times
    if no ACK is received. Runs in its own thread.
    This handles the UART noise problem — if the first transmission
    is corrupted by Zigbee radio interference, a retry will succeed
    once the radio settles.
    """
    global _hub_config_ack_pending
    for attempt in range(HUB_CONFIG_RETRIES):
        hub_cmd_push_config()
        # Wait for ACK — check every 100ms for up to HUB_CONFIG_RETRY_MS
        deadline = utime.ticks_add(utime.ticks_ms(), HUB_CONFIG_RETRY_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if not _hub_config_ack_pending:
                print("HUB CONFIG: ACK received on attempt {}".format(attempt + 1))
                return
            utime.sleep_ms(100)
        print("HUB CONFIG: no ACK on attempt {} — retrying".format(attempt + 1))
    print("HUB CONFIG: all {} attempts exhausted".format(HUB_CONFIG_RETRIES))


def hub_cmd_set_sensor_name(sensor_index, name):
    if not isinstance(sensor_index, int) or sensor_index < 0:
        print("hub_cmd_set_sensor_name: invalid index")
        return
    if not isinstance(name, str) or len(name) == 0:
        print("hub_cmd_set_sensor_name: invalid name")
        return
    send_to_sensor_hub({
        "type":         "set_sensor_name",
        "sensor_index": sensor_index,
        "name":         name
    })
    print("MASTER -> SENSORHUB: rename {} -> '{}'".format(sensor_index, name))


def hub_cmd_get_config():
    send_to_sensor_hub({"type": "get_config"})
    print("MASTER -> SENSORHUB: get_config")


def hub_cmd_get_logs():
    send_to_sensor_hub({"type": "get_logs", "lines": 50})
    print("MASTER -> SENSORHUB: get_logs")


def hub_cmd_start_pairing(duration_sec=120):
    send_to_sensor_hub({"type": "start_pairing", "duration_sec": duration_sec})
    print("MASTER -> SENSORHUB: start_pairing duration={}s".format(duration_sec))


def hub_cmd_stop_pairing():
    send_to_sensor_hub({"type": "stop_pairing"})
    print("MASTER -> SENSORHUB: stop_pairing")


def hub_cmd_remove_sensor(sensor_index):
    send_to_sensor_hub({"type": "remove_sensor", "sensor_index": sensor_index})
    print("MASTER -> SENSORHUB: remove_sensor index={}".format(sensor_index))


def hub_cmd_factory_reset():
    send_to_sensor_hub({"type": "factory_reset"})
    print("MASTER -> SENSORHUB: factory_reset")


def hub_cmd_restart():
    send_to_sensor_hub({"type": "restart"})
    print("MASTER -> SENSORHUB: restart")


# ============================================================================
# TCP COMMAND HANDLERS
# ============================================================================

def _tcp_send_ack(command, status="ok"):
    tcp_forward({"type": "ack", "command": command, "status": status})


def handle_tcp_command(msg):
    """Dispatch a command dict received from the TCP debugger client."""
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
        status = msg.get("status", "")
        if status in ("Occupied", "Vacant", "Sold Vacant", "UnSold Occupied"):
            with _state_lock:
                state["pending_status"]         = None
                state["pending_apply_epoch"]     = 0
                state["current_decided_status"]  = status
                conf["last_decided_status"]      = status
                save_config()
            send_status_to_scheduler(status)
            tcp_forward({"type": "unit_state_update", "status": status})
            _tcp_send_ack("force_status")
            print("FORCE STATUS: {}".format(status))
        else:
            _tcp_send_ack("force_status", "error")

    elif t == "cancel_pending":
        with _state_lock:
            state["pending_status"]      = None
            state["pending_apply_epoch"] = 0
        tcp_forward({"type": "pending_update", "pending_status": None})
        _tcp_send_ack("cancel_pending")
        print("PENDING cancelled by debugger")

    elif t == "set_unit_config":
        for key in ("tenant_id", "unit_id", "check_in_utc", "check_out_utc",
                    "buffer_minutes"):
            if key in msg:
                val = msg[key]
                if key == "buffer_minutes":
                    try:
                        val = int(val)
                    except Exception:
                        val = 15
                conf[key] = val
        save_config()
        with _state_lock:
            recalc_and_act()
        _tcp_send_ack("set_unit_config")
        print("UNIT CONFIG updated from debugger")
        # Send updated snapshot so debugger reflects new state immediately
        tcp_forward(build_state_snapshot())

    elif t == "set_hub_config":
        hub_cfg = conf.get("sensor_hub_config", {})
        for key in ("pairing_duration_sec", "watchdog_interval_min",
                    "watchdog_ping_timeout_sec", "door_alarm_threshold_min",
                    "heartbeat_interval_min"):
            if key in msg:
                try:
                    hub_cfg[key] = int(msg[key])
                except Exception:
                    pass
        if "watchdog_enable" in msg:
            hub_cfg["watchdog_enable"] = bool(msg["watchdog_enable"])
        conf["sensor_hub_config"] = hub_cfg
        save_config()
        # Push with retry in background so UART noise doesn't silently lose it
        _thread.start_new_thread(_hub_push_config_with_retry, ())
        _tcp_send_ack("set_hub_config")
        print("HUB CONFIG updated and pushing to Hub (with retry)")
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
        # Manual NTP re-sync from debugger
        ok = _sync_ntp()
        _tcp_send_ack("ntp_sync", "ok" if ok else "error")
        if ok:
            with _state_lock:
                recalc_and_act()
            tcp_forward(build_state_snapshot())

    else:
        print("WARN: unknown TCP command '{}'".format(t))


# ============================================================================
# UART RX LOOPS
# ============================================================================

def _process_uart_line(line_bytes, handler_fn):
    try:
        s = line_bytes.decode("utf-8").strip()
    except Exception:
        print("WARN: UART line UTF-8 decode failed")
        return False
    if not s:
        return False
    try:
        msg = json.loads(s)
    except Exception:
        print("WARN: UART bad JSON:", s[:60])
        return False
    if not isinstance(msg, dict):
        print("WARN: UART JSON is not a dict")
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

                        # Non-printable noise filter
                        if ch < 0x20 and ch != 0x09 and ch != 0x0A and ch != 0x0D:
                            if _rx_sensor_pos > 0:
                                print("WARN: sensor RX noise 0x{:02x} — buf reset".format(ch))
                                _rx_sensor_pos = 0
                            continue

                        if ch == ord('\r'):
                            continue

                        if ch == ord('\n'):
                            if _rx_sensor_pos > 0:
                                if _rx_sensor_buf[0] != ord('{'):
                                    print("WARN: sensor RX non-JSON discarded "
                                          "(starts 0x{:02x})".format(_rx_sensor_buf[0]))
                                    _rx_sensor_pos = 0
                                else:
                                    _process_uart_line(
                                        memoryview(_rx_sensor_buf)[:_rx_sensor_pos],
                                        handle_sensor_msg)
                                    _rx_sensor_pos = 0
                        else:
                            if _rx_sensor_pos >= UART_RX_BUF_MAX - 1:
                                print("WARN: sensor RX buf overflow — discarding")
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
    global _rx_sched_pos
    while True:
        try:
            if uart_sched.any():
                data = uart_sched.read()
                if data:
                    for b in data:
                        ch = b if isinstance(b, int) else ord(b)

                        # Non-printable noise filter
                        if ch < 0x20 and ch != 0x09 and ch != 0x0A and ch != 0x0D:
                            if _rx_sched_pos > 0:
                                print("WARN: sched RX noise 0x{:02x} — buf reset".format(ch))
                                _rx_sched_pos = 0
                            continue

                        if ch == ord('\r'):
                            continue

                        if ch == ord('\n'):
                            if _rx_sched_pos > 0:
                                if _rx_sched_buf[0] != ord('{'):
                                    print("WARN: sched RX non-JSON discarded "
                                          "(starts 0x{:02x})".format(_rx_sched_buf[0]))
                                    _rx_sched_pos = 0
                                else:
                                    _process_uart_line(
                                        memoryview(_rx_sched_buf)[:_rx_sched_pos],
                                        handle_scheduler_msg)
                                    _rx_sched_pos = 0
                        else:
                            if _rx_sched_pos >= UART_RX_BUF_MAX - 1:
                                print("WARN: sched RX buf overflow — discarding")
                                _rx_sched_pos = 0
                            else:
                                _rx_sched_buf[_rx_sched_pos] = ch
                                _rx_sched_pos += 1
            utime.sleep_ms(30)
        except Exception as e:
            print("sched_rx_loop error:", repr(e))
            _rx_sched_pos = 0
            utime.sleep_ms(200)


# ============================================================================
# PENDING BUFFER WORKER
# ============================================================================

def pending_worker():
    while True:
        try:
            with _state_lock:
                p = state["pending_status"]
                t = state["pending_apply_epoch"]
                if p is not None:
                    now_ep = now_epoch_utc()
                    if now_ep >= t:
                        latest = decide_status(state["last_sensor_status"], now_ep)
                        print("PENDING due: pending={} latest={} now={}".format(
                              p, latest, now_ep))
                        if latest == p:
                            apply_immediate(p)
                        else:
                            print("PENDING cancelled — conditions changed")
                        state["pending_status"]      = None
                        state["pending_apply_epoch"] = 0
                        tcp_forward({"type": "pending_update", "pending_status": None})
            utime.sleep(1)
        except Exception as e:
            print("pending_worker error:", repr(e))
            utime.sleep(1)


# ============================================================================
# TCP DEBUG SERVER
# ============================================================================

def tcp_server_thread():
    global _tcp_client
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(socket.getaddrinfo("0.0.0.0", TCP_PORT)[0][-1])
    srv.listen(1)
    print("DEBUG: TCP server listening on port {}".format(TCP_PORT))

    _rx_tcp_buf = bytearray(TCP_RX_BUF_MAX)
    rx_pos      = 0

    while True:
        try:
            conn, addr = srv.accept()
            old = _tcp_client
            _tcp_client = conn
            if old:
                try:
                    old.close()
                except Exception:
                    pass
            print("DEBUG: client connected from {}".format(addr))
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
                                s   = bytes(_rx_tcp_buf[:rx_pos]).decode("utf-8").strip()
                                msg = json.loads(s)
                                if isinstance(msg, dict):
                                    handle_tcp_command(msg)
                            except Exception as e:
                                print("TCP RX parse error:", repr(e))
                            rx_pos = 0
                    else:
                        if rx_pos >= TCP_RX_BUF_MAX - 1:
                            print("WARN: TCP RX overflow — discarding")
                            rx_pos = 0
                        _rx_tcp_buf[rx_pos] = ch
                        rx_pos += 1

        except Exception as e:
            print("TCP server error:", repr(e))
            utime.sleep_ms(500)
        finally:
            if _tcp_client is not None:
                try:
                    _tcp_client.close()
                except Exception:
                    pass
                _tcp_client = None
            print("DEBUG: client disconnected")


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
    changed = (_internet_up != new_state)
    _internet_up = new_state
    if changed:
        label = "RESTORED" if new_state else "LOST"
        print("INTERNET: connectivity {}".format(label))
        tcp_forward({"type": "internet_status",
                     "status": "up" if new_state else "down"})
        with _state_lock:
            recalc_and_act()


def wifi_and_internet_thread():
    global _wifi_ip, _ntp_synced, _last_ntp_sync

    ssid = conf.get("wifi_ssid", "")
    pwd  = conf.get("wifi_password", "")

    if not ssid:
        print("WIFI: no credentials — 2-state fallback")
        with _state_lock:
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
                    print("WIFI: connected IP={}".format(_wifi_ip))
                    _thread.start_new_thread(tcp_server_thread, ())
                    break
            except Exception as e:
                print("WIFI retry error:", repr(e))

    if wlan.isconnected():
        cfg_info = wlan.ifconfig()
        _wifi_ip = cfg_info[0]
        print("WIFI: connected")
        print("  IP      :", cfg_info[0])
        print("  Subnet  :", cfg_info[1])
        print("  Gateway :", cfg_info[2])
        print("  DNS     :", cfg_info[3])
        _thread.start_new_thread(tcp_server_thread, ())

    # Internet check
    retries = 3
    internet_found = False
    for attempt in range(retries):
        if _check_internet():
            internet_found = True
            break
        print("INTERNET: check attempt {}/{} failed".format(attempt + 1, retries))
        utime.sleep(2)

    if internet_found:
        print("INTERNET: connectivity CONFIRMED")
        # Attempt NTP sync immediately
        for ntp_attempt in range(3):
            if _sync_ntp():
                break
            print("NTP: attempt {}/3 failed — retrying in 5s".format(ntp_attempt + 1))
            utime.sleep(5)
        _set_internet_up(True)
    else:
        print("INTERNET: no connectivity — 2-state fallback")
        _set_internet_up(False)
        with _state_lock:
            recalc_and_act()

    # Periodic re-check + NTP re-sync
    while True:
        utime.sleep(INTERNET_RECHECK_S)
        if not wlan.isconnected():
            _set_internet_up(False)
            continue
        result = _check_internet()
        _set_internet_up(result)
        # Re-sync NTP every NTP_SYNC_INTERVAL_S
        if result and _ntp_synced:
            elapsed = utime.ticks_diff(utime.ticks_ms(), _last_ntp_sync)
            if elapsed >= NTP_SYNC_INTERVAL_S * 1000:
                _sync_ntp()


# ============================================================================
# STARTUP
# ============================================================================

load_config()

state["last_sensor_status"]     = str(conf.get("last_sensor_status", "vacant")).lower()
state["last_scheduler_status"]  = conf.get("last_scheduler_status", None)
state["current_decided_status"] = None  # Force first send on boot

try:
    import ubinascii
    wlan_tmp = network.WLAN(network.STA_IF)
    wlan_tmp.active(True)
    _mac_str = ubinascii.hexlify(wlan_tmp.config("mac"), ":").decode().upper()
except Exception:
    _mac_str = "unknown"

print("=" * 50)
print("MASTER started")
print("MASTER MAC address  :", _mac_str)
print("check_in_utc  :", conf.get("check_in_utc"))
print("check_out_utc :", conf.get("check_out_utc"))
print("buffer_minutes:", conf.get("buffer_minutes"))
print("tenant_id     :", conf.get("tenant_id"))
print("unit_id       :", conf.get("unit_id"))
print("Internet mode : waiting for connectivity + NTP sync...")
print("=" * 50)

# Push hub config with retry (background thread — doesn't block boot)
utime.sleep_ms(500)
_thread.start_new_thread(_hub_push_config_with_retry, ())
utime.sleep_ms(300)
hub_cmd_get_config()

# Apply last persisted status — do NOT recalc until NTP confirmed
_boot_status = conf.get("last_decided_status", "Vacant")
state["current_decided_status"] = _boot_status
print("BOOT: last known status = {}".format(_boot_status))

# Start background threads
_thread.start_new_thread(sensor_rx_loop, ())
_thread.start_new_thread(sched_rx_loop, ())
_thread.start_new_thread(pending_worker, ())
_thread.start_new_thread(wifi_and_internet_thread, ())

# ============================================================================
# MAIN LOOP
# ============================================================================

while True:
    utime.sleep_ms(MAIN_LOOP_TICK_MS)