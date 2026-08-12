# main.py — MASTER (ESP32-S3)
# Innovatsii EMS — Pico 1
# Firmware Version: 0.3.0
#
# ARCHITECTURE — V5.0 (Hub sends raw events, Master calculates unit occupancy)
#
# SENSOR SET (Hub firmware 0.3.0):
#   ZG-204ZL  PIR presence  — Tuya EF00: DP1 occupancy, DP4 battery,
#                             DP9 sensitivity (0=low,1=med,2=high),
#                             DP10 keep_time (10/30/60/120 s)
#   ZG-102Z/ZA door contact — IAS Zone, sleepy device, never marked offline
#
#   Neither sensor reports temperature or humidity. The 'environment' message
#   and all temp/hum fields have been removed from the protocol.
#   Illuminance (DP12) is discarded by the Hub and never reaches the Master.
#
# Hub responsibility:
#   - Reports sensor_presence, door, battery, sensor_health
#   - Reports hub_aggregate (OR of all online presence sensors — no door logic)
#   - Sends real UTC timestamps on all messages (from utc_epoch in hub_init)
#
# Master responsibility:
#   - Tracks latest state of each presence sensor and door sensor with timestamp
#   - Calculates unit occupancy: door close + presence evaluation
#   - Applies booking window → 4-state decision
#   - Applies buffer period (buffer_minutes)
#   - Sends set_status to Scheduler
#   - Pushes per-sensor keep_time / motion sensitivity to the Hub
#
# Unit occupancy rules (Master):
#   The ZG-204ZL is a PIR: it latches YES for keep_time (10-120 s) after the
#   last motion, so a departing guest's own movement keeps it YES through the
#   door close. Presence sampled at that instant can never prove absence.
#
#   -> OCCUPIED: door CLOSED transition with any presence YES. Taken
#                immediately — the safe direction.
#   -> VACANT:   never taken at the door close. The close arms a confirmation
#                window (vacancy_confirm_sec); vacancy is concluded only if no
#                motion is seen after the stale exit latch has expired. Fresh
#                motion, or the door reopening, cancels it.
#
# set_sensor_config: Debugger → Master → Hub. Pushes keep_time_sec / sensitivity
#                    to a ZG-204ZL PIR sensor.

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

UART_RX_BUF_MAX          = 2048
MAIN_LOOP_TICK_MS        = 1000
TCP_PORT                 = 8765
TCP_RX_BUF_MAX           = 1024
INTERNET_CHECK_HOST      = "8.8.8.8"
INTERNET_CHECK_PORT      = 53
INTERNET_CHECK_TIMEOUT_S = 3
INTERNET_RECHECK_S       = 60
NTP_SYNC_INTERVAL_S      = 3600
HUB_PING_ATTEMPTS        = 10
HUB_PING_INTERVAL_MS     = 2000
HUB_INIT_ACK_TIMEOUT_MS  = 5000
HUB_READY_TIMEOUT_MS     = 120000
WATCHDOG_ACK_TIMEOUT_MS  = 5000

MPY_TO_UNIX_EPOCH        = 946684800

# ── ZG-204ZL PIR limits (Tuya EF00) ─────────────────────────────────────────
# DP10 keep_time is an ENUM, not a free-running seconds value. Only these four
# values exist; anything else is snapped to the nearest one by the Hub.
PIR_KEEP_TIME_CHOICES = (10, 30, 60, 120)
PIR_KEEP_TIME_DEFAULT = 30
# DP9 sensitivity enum: 0=low, 1=medium, 2=high.
PIR_SENSITIVITY_MIN   = 0
PIR_SENSITIVITY_MAX   = 2
PIR_SENSITIVITY_DEFAULT = 1


def _snap_keep_time(sec):
    """Snap an arbitrary seconds value to the nearest valid DP10 enum value."""
    try:
        sec = int(sec)
    except Exception:
        return PIR_KEEP_TIME_DEFAULT
    best = PIR_KEEP_TIME_CHOICES[0]
    for c in PIR_KEEP_TIME_CHOICES:
        if abs(c - sec) < abs(best - sec):
            best = c
    return best


def _clamp_sensitivity(val):
    """Clamp to the DP9 enum range 0..2."""
    try:
        val = int(val)
    except Exception:
        return PIR_SENSITIVITY_DEFAULT
    if val < PIR_SENSITIVITY_MIN: return PIR_SENSITIVITY_MIN
    if val > PIR_SENSITIVITY_MAX: return PIR_SENSITIVITY_MAX
    return val

# ============================================================================
# UART INITIALISATION
# ============================================================================

uart_sensor = UART(cfg.SENSOR_UART_ID,
                   baudrate=cfg.SENSOR_UART_BAUD,
                   tx=cfg.SENSOR_UART_TX,
                   rx=cfg.SENSOR_UART_RX)

uart_sched = None

_rx_sensor_buf = bytearray(UART_RX_BUF_MAX)
_rx_sched_buf  = bytearray(UART_RX_BUF_MAX)
_rx_sensor_pos = 0
_rx_sched_pos  = 0

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
_hub_state            = "UNKNOWN"
_tcp_client           = None
_mac_str              = ""
_ntp_synced           = False
_last_ntp_sync        = 0
_hub_config_push_in_progress = False
_tcp_server_started          = False

# Boot phase tracking
_hub_pong_received    = False
_hub_init_acked       = False
_hub_ready_received   = False
_sensor_list_complete = False
_watchdog_start_acked = False
_boot_phase           = "IDLE"

_debug_mode = False

# ============================================================================
# MASTER UNIT OCCUPANCY ENGINE
# ============================================================================

_presence      = {}   # per presence sensor: state + timestamp
_door          = {}   # per door sensor: state + timestamp + closed_at
_hub_aggregate = "vacant"

# Live per-sensor config captured from config_response (for snapshots/UI)
_sensor_live   = {}   # idx(int) -> {keep_time_sec, sensitivity, supports_config, ...}

# ── Vacancy confirmation window ─────────────────────────────────────────────
#
# WHY THIS EXISTS
#
# The ZG-204ZL is a PIR, not an mmWave presence sensor. It reports MOTION and
# then latches YES for keep_time (10/30/60/120 s, default 30) after the last
# movement it saw. Two consequences drive this design:
#
#   1. To leave the unit a guest must walk past the PIR to reach the door.
#      At the instant the door clicks shut the PIR is therefore ALWAYS still
#      latched YES. Sampling presence at that moment can never yield "vacant",
#      so the old "door closed + all NO -> VACANT" rule could never fire.
#
#   2. A PIR cannot see a motionless person. mmWave could. So "no motion" is
#      only evidence of absence once enough time has passed.
#
# The two directions are therefore NOT symmetric, because the risks are not
# symmetric: a false "occupied" wastes a little power, a false "vacant" cuts
# power on a guest who is actually home. So:
#
#   -> OCCUPIED  is taken immediately and optimistically on a door close.
#   -> VACANT    is never taken on the door close itself. The close arms a
#                confirmation window; vacancy is only concluded at the end of
#                it, and any fresh motion cancels it.
#
_vacancy_eval = {
    "active":       False,
    "quiet_until":  0,    # epoch after which the stale exit latch has expired
    "decide_at":    0,    # epoch at which we conclude occupied / vacant
    "door":         "",   # door whose close armed this window
}


def _utc_now_str():
    t = utime.localtime()
    return "{}-{:02d}-{:02d} {:02d}:{:02d}:{:02d}".format(
        t[0], t[1], t[2], t[3], t[4], t[5])


def _any_presence_yes():
    """True if any ONLINE presence sensor currently reads YES."""
    for v in _presence.values():
        if v.get("online", True) and v.get("state", False):
            return True
    return False


def _max_keep_time_sec():
    """
    Longest PIR keep_time across known PRESENCE sensors. This is how long a
    sensor can stay latched YES after the last motion, so the stale exit latch
    cannot outlive it. Falls back to the configured global default.

    Only PRESENCE sensors are considered: the Hub reports a keep_time_sec field
    for every sensor, but on a door it is just the uninitialised default (30),
    which would otherwise inflate the quiet period and delay every vacancy
    decision — even after the PIRs were tuned down to 10 s.
    """
    longest = 0
    for live in _sensor_live.values():
        if live.get("role") != "PRESENCE":
            continue
        try:
            kt = int(live.get("keep_time_sec", 0))
        except Exception:
            kt = 0
        if kt > longest:
            longest = kt
    if longest <= 0:
        hub_cfg = conf.get("sensor_hub_config", {})
        longest = _snap_keep_time(
            hub_cfg.get("presence_fading_time_sec", PIR_KEEP_TIME_DEFAULT))
    return longest


def _set_sensor_status(new_status):
    """Commit a new sensor-derived occupancy and re-run the state machine."""
    if new_status == state["last_sensor_status"]:
        return
    with _state_lock:
        state["last_sensor_status"] = new_status
        conf["last_sensor_status"]  = new_status
        save_config()
        recalc_and_act()


def _cancel_vacancy_eval(reason):
    if not _vacancy_eval["active"]:
        return
    _vacancy_eval["active"] = False
    print("UNIT EVAL: vacancy window cancelled — {}".format(reason))
    tcp_forward({"type": "vacancy_eval", "active": False, "reason": reason})


def _arm_vacancy_eval(door_name, now_ep):
    """Arm the post-door-close confirmation window."""
    guard      = max(0, int(conf.get("motion_quiet_guard_sec", 5)))
    confirm    = max(30, int(conf.get("vacancy_confirm_sec", 180)))
    keep       = _max_keep_time_sec()
    quiet_until = now_ep + keep + guard
    decide_at   = now_ep + confirm

    # The window must outlast the stale exit latch, otherwise we would decide
    # while the PIR is still holding YES from the departing guest.
    if decide_at <= quiet_until:
        decide_at = quiet_until + 10

    _vacancy_eval["active"]      = True
    _vacancy_eval["quiet_until"] = quiet_until
    _vacancy_eval["decide_at"]   = decide_at
    _vacancy_eval["door"]        = door_name

    print("UNIT EVAL: vacancy window armed by '{}' — keep={}s guard={}s "
          "quiet_in={}s decide_in={}s".format(
              door_name, keep, guard, quiet_until - now_ep, decide_at - now_ep))
    tcp_forward({"type":          "vacancy_eval",
                 "active":        True,
                 "door":          door_name,
                 "quiet_until":   quiet_until,
                 "decide_epoch":  decide_at})


def _evaluate_unit_occupancy_on_door_close(door_name=""):
    """
    Called on a door OPEN->CLOSED transition, with _state_lock NOT held.

    Takes the immediate optimistic OCCUPIED decision, and always arms the
    vacancy confirmation window so the opposite conclusion can be reached
    later if nobody turns out to be inside.
    """
    if _force_active():
        return
    if len(_presence) == 0:
        print("UNIT EVAL: door closed but no presence sensors — ignored")
        return

    now_ep  = utime.time()
    any_yes = _any_presence_yes()
    current = state["last_sensor_status"]

    # Fast path toward comfort. Safe direction: at worst this wastes power
    # until the confirmation window below corrects it.
    if any_yes and current == "vacant":
        print("UNIT EVAL: door closed + presence YES → OCCUPIED (immediate)")
        _set_sensor_status("occupied")

    # Always arm the window — it both confirms an arrival and is the only path
    # to vacancy. Covers the "opened the door, never came in" case too.
    _arm_vacancy_eval(door_name, now_ep)


def _maybe_evaluate_on_presence_change(sensor=""):
    """
    Called on a RISING presence edge (NO -> YES) only.

    While a confirmation window is open, motion after quiet_until proves
    somebody is inside: conclude OCCUPIED at once and close the window. Motion
    BEFORE quiet_until is ignored because it may still be the departing
    guest's own latch.
    """
    if _force_active():
        return
    if not _vacancy_eval["active"]:
        return

    now_ep = utime.time()
    if now_ep < _vacancy_eval["quiet_until"]:
        print("UNIT EVAL: motion during quiet period — ignored "
              "(may be the departing guest)")
        return

    print("UNIT EVAL: fresh motion from '{}' after quiet period → OCCUPIED"
          .format(sensor))
    _cancel_vacancy_eval("fresh motion confirmed occupancy")
    _set_sensor_status("occupied")


def _tick_vacancy_eval():
    """
    Called once per second from pending_worker with _state_lock NOT held.
    Concludes the confirmation window when it expires.
    """
    if not _vacancy_eval["active"]:
        return
    if _force_active():
        _cancel_vacancy_eval("force active")
        return

    now_ep = utime.time()
    if now_ep < _vacancy_eval["decide_at"]:
        return

    _vacancy_eval["active"] = False

    if _any_presence_yes():
        # Someone is still moving in the unit.
        print("UNIT EVAL: window expired, presence YES → OCCUPIED")
        tcp_forward({"type": "vacancy_eval", "active": False,
                     "reason": "presence still YES"})
        _set_sensor_status("occupied")
    else:
        # No motion since the stale exit latch expired — the unit is empty.
        print("UNIT EVAL: window expired, no motion since quiet period → VACANT")
        tcp_forward({"type": "vacancy_eval", "active": False,
                     "reason": "no motion — vacant"})
        _set_sensor_status("vacant")

# ============================================================================
# CONFIG  —  master_config.py is the ONLY source of truth
# ============================================================================
#
# There is no master_config.json. Configuration is loaded from
# master_config.DEFAULT_CONFIG at every boot, so what you put in
# master_config.py is exactly what the Master runs — copy the file to the
# device, reset, and the change is live. No stale on-device state can
# silently override it.
#
# TRADE-OFF: nothing survives a reboot. Runtime changes (force commands,
# booking times pushed over MQTT, operator sensor renames, Wi-Fi changes made
# from the Debugger) apply immediately and stay in RAM for the session, but
# are lost on power cycle and revert to the values in master_config.py.
# To make a change permanent, edit master_config.py and re-copy it.

conf    = {}
boot_ms = utime.ticks_ms()


def _deep_copy(obj):
    """
    Recursive copy of plain dict/list structures. MicroPython has no reliable
    `copy` module. Used so that runtime mutation of `conf` can never write
    back into cfg.DEFAULT_CONFIG (nested dicts would otherwise be shared
    references, corrupting the defaults for the rest of the session).
    """
    if isinstance(obj, dict):
        return dict((k, _deep_copy(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [_deep_copy(v) for v in obj]
    return obj


def load_config():
    """Build the running config from master_config.py. No file I/O."""
    global conf
    conf = _deep_copy(cfg.DEFAULT_CONFIG)
    print("CONFIG loaded from master_config.py (no JSON persistence)")


def save_config():
    """
    No-op. Kept so the ~20 call sites that mutate `conf` remain valid and
    self-documenting; changes live in RAM for the current session only.
    Persistence was removed deliberately — master_config.py is the single
    source of truth.
    """
    pass


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
        _push_utc_epoch_to_hub()
        return True
    except Exception as e:
        print("NTP sync failed:", repr(e))
        tcp_forward({"type": "ntp_status", "synced": False})
        return False


def _push_utc_epoch_to_hub():
    """
    Re-anchor the Hub's clock after NTP.

    hub_init is sent the moment the Hub answers our ping, which is normally
    several seconds BEFORE the background NTP sync completes. The Hub therefore
    starts up believing the epoch is ~946684800 (year 2000) and stamps every
    outbound message with a ts_utc that is ~26 years wrong.

    Sending the corrected epoch here makes all subsequent Hub timestamps real
    wall-clock UTC, as required by the architecture ("All timestamps are UTC").
    Safe to call on every resync — the Hub simply re-anchors.
    """
    # Only meaningful once the Hub has accepted hub_init; before that it is
    # not yet listening for config and hub_init will carry the epoch itself.
    if not _hub_init_acked:
        return
    try:
        send_to_sensor_hub({
            "type":      "set_config",
            "utc_epoch": now_epoch_utc() + MPY_TO_UNIX_EPOCH,
        })
        print("HUB: UTC epoch resynced after NTP")
    except Exception as e:
        print("HUB: UTC epoch resync failed:", repr(e))


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
    f = conf.get("force", {})
    if not f.get("active", False):
        return False
    expires = f.get("expires_epoch", 0)
    if expires == 0:
        return False
    return now_epoch_utc() < expires


def _apply_force(status, duration_hours, reason="Manual force"):
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
        "type":          "force_update",
        "active":        True,
        "status":        status,
        "expires_utc":   expires_str,
        "expires_epoch": expires_ep,
        "reason":        conf["force"]["reason"]
    })
    print("FORCE SET: status={} duration={}h expires={}".format(
          status, duration_hours, expires_str))


def _clear_force(recalculate=True):
    conf["force"] = {
        "active":        False,
        "status":        "",
        "expires_utc":   "",
        "expires_epoch": 0,
        "reason":        ""
    }
    save_config()
    tcp_forward({"type": "force_update", "active": False})
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
    """Called ONLY when _state_lock is already held."""
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
            "hub_aggregate":        _hub_aggregate,
            "pending_status":       state["pending_status"],
            "pending_apply_epoch":  state["pending_apply_epoch"],
            "vacancy_pending":      _vacancy_eval["active"],
            "vacancy_decide_epoch": _vacancy_eval["decide_at"] if _vacancy_eval["active"] else 0,
            "vacancy_quiet_until":  _vacancy_eval["quiet_until"] if _vacancy_eval["active"] else 0,
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
    hub_cfg = conf.get("sensor_hub_config", {})
    return {
        "type":                          "hub_init",
        "mode":                          "debug" if _debug_mode else "production",
        "firmware_version":              cfg.FIRMWARE_VERSION,
        "utc_epoch":                     now_epoch_utc() + MPY_TO_UNIX_EPOCH,
        "pairing_duration_sec":          hub_cfg.get("pairing_duration_sec",          120),
        "watchdog_enable":               hub_cfg.get("watchdog_enable",                True),
        "watchdog_interval_min":         hub_cfg.get("watchdog_interval_min",          60),
        "watchdog_ping_timeout_sec":     hub_cfg.get("watchdog_ping_timeout_sec",      30),
        "door_alarm_threshold_min":      hub_cfg.get("door_alarm_threshold_min",       10),
        "heartbeat_interval_min":        hub_cfg.get("heartbeat_interval_min",         30),
        "presence_fading_time_sec":      _snap_keep_time(
                                            hub_cfg.get("presence_fading_time_sec",
                                                        PIR_KEEP_TIME_DEFAULT)),
        "door_sensor_max_silence_hours": hub_cfg.get("door_sensor_max_silence_hours",  24),
        "motion_sensitivity":            _clamp_sensitivity(
                                            hub_cfg.get("motion_sensitivity",
                                                        PIR_SENSITIVITY_DEFAULT)),
    }


# ============================================================================
# BOOT CONTROLLER
# ============================================================================

def _boot_controller_thread():
    global _hub_state, _boot_phase
    global _hub_pong_received, _hub_init_acked
    global _hub_ready_received, _sensor_list_complete, _watchdog_start_acked

    utime.sleep_ms(500)

    # ── PHASE A — Hub Discovery ──────────────────────────────────────────────
    _boot_phase = "PING"
    print("BOOT [A] Hub discovery — ping up to {} times".format(HUB_PING_ATTEMPTS))
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
        msg = "Hub not responding after {} attempts — check UART wiring".format(HUB_PING_ATTEMPTS)
        print("BOOT [A] FAULT:", msg)
        tcp_forward({"type": "boot_fault", "reason": msg})
        return

    conf["hub_status"]["fault"] = False
    conf["hub_status"]["known"] = True
    save_config()
    tcp_forward({"type": "boot_phase", "phase": "A_DONE"})

    # ── PHASE B — Hub Init ───────────────────────────────────────────────────
    _boot_phase = "INIT"
    _hub_state  = "BOOTING"
    print("BOOT [B] Sending hub_init")
    tcp_forward({"type": "boot_phase", "phase": "B_INIT"})

    init_acked = False
    for init_attempt in range(1, 3):
        _hub_init_acked = False
        send_to_sensor_hub(_build_hub_init())
        deadline = utime.ticks_add(utime.ticks_ms(), HUB_INIT_ACK_TIMEOUT_MS)
        while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
            if _hub_init_acked:
                init_acked = True
                break
            utime.sleep_ms(100)
        if init_acked:
            print("BOOT [B] hub_init ACK received (attempt {})".format(init_attempt))
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
        msg = "Hub did not send hub_ready within {}s".format(HUB_READY_TIMEOUT_MS // 1000)
        print("BOOT [B] FAULT:", msg)
        tcp_forward({"type": "boot_fault", "reason": msg})
        return

    tcp_forward({"type": "boot_phase", "phase": "B_DONE"})

    # ── PHASE C — Sensor Rejoin ───────────────────────────────────────────────
    _boot_phase = "REJOIN"
    print("BOOT [C] Waiting for sensor rejoin to complete...")
    tcp_forward({"type": "boot_phase", "phase": "C_REJOIN"})

    # Do NOT reset _sensor_list_complete here. The Hub's rejoin_task sends
    # sensor_status/sensor_list_complete BEFORE hub_ready_task sends hub_ready,
    # so the flag is normally already set while we were still in Phase B.
    # Clearing it discarded that fact and forced a full 240 s stall, which
    # meant start_watchdog was never sent and heartbeats never started.
    if _sensor_list_complete:
        print("BOOT [C] sensor_list_complete already received during Phase B")
    deadline = utime.ticks_add(utime.ticks_ms(), 240000)
    while utime.ticks_diff(deadline, utime.ticks_ms()) > 0:
        if _sensor_list_complete:
            break
        utime.sleep_ms(500)

    if not _sensor_list_complete:
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

    # Close the boot race: hub_init may have been built before NTP completed,
    # leaving the Hub anchored to year 2000. If we have real time by now,
    # re-anchor it so every Hub ts_utc is true wall-clock UTC.
    if _ntp_synced:
        _push_utc_epoch_to_hub()

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

    with _state_lock:
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
    print("HUB READY: v={} sensors={} online={} offline={} needs_pairing={}".format(
          fw, sensor_count, online, offline, needs_pairing))
    conf["hub_status"]["sensor_count"]     = sensor_count
    conf["hub_status"]["firmware_version"] = fw
    save_config()
    tcp_forward(msg)
    if needs_pairing:
        print("HUB READY: no sensors — open pairing from Debugger or MQTT")
        tcp_forward({"type": "notification",
                     "level":   "info",
                     "message": "No sensors registered — open pairing to add sensors"})


def _handle_sensor_joined(msg):
    idx     = msg.get("index",  -1)
    name    = msg.get("name",   "?")
    model   = msg.get("model",  "?")
    role    = msg.get("role",   "?")
    online  = msg.get("online", False)
    battery = msg.get("battery", 0)
    print("SENSOR JOINED: [{}] {} {} {} online={} batt={}%".format(
          idx, name, model, role, online, battery))
    hub_cfg = conf.get("sensor_hub_config", {})
    names   = hub_cfg.get("sensor_names", {})
    if isinstance(idx, int) and idx >= 0 and isinstance(name, str):
        names[str(idx)] = name
    conf["sensor_hub_config"]["sensor_names"] = names
    save_config()
    tcp_forward(msg)


def _handle_sensor_status(msg):
    idx    = msg.get("index",  -1)
    name   = msg.get("name",   "?")
    online = msg.get("online", False)
    role   = msg.get("role",   "")
    print("SENSOR STATUS: [{}] {} online={}".format(idx, name, online))

    # Seed the occupancy dicts from the boot-time sensor list. These messages
    # arrive during Phase B/C, well before the first config_response, so
    # without this a door close early in the session would find empty dicts.
    if isinstance(name, str) and name:
        if role == "PRESENCE":
            pres  = msg.get("presence")
            entry = _presence.get(name, {})
            if "state" not in entry:
                entry["state"] = bool(pres) if pres is not None else False
                entry["ts"]    = msg.get("ts_utc", _utc_now_str())
            entry["online"] = bool(online)
            _presence[name] = entry
        elif role == "DOOR":
            contact = msg.get("contact")
            if name not in _door:
                _door[name] = {"state":     contact if contact else "CLOSED",
                               "ts":        msg.get("ts_utc", _utc_now_str()),
                               "opened_at": 0,
                               "closed_at": 0}
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


def _handle_hub_aggregate(msg):
    """
    Hub aggregate = OR of all online presence sensors. No door logic.
    Updates the red box in the Debugger. Does NOT change unit state directly.
    """
    global _hub_aggregate
    raw = msg.get("state", "VACANT")
    if not isinstance(raw, str):
        return
    _hub_aggregate = raw.strip().upper()
    print("HUB AGGREGATE: {}".format(_hub_aggregate))
    tcp_forward(msg)
    occ_lower = "occupied" if _hub_aggregate == "OCCUPIED" else "vacant"
    tcp_forward({"type": "hub_aggregate_update", "state": occ_lower})


def _handle_sensor_presence(msg):
    sensor    = msg.get("sensor", "")
    state_val = msg.get("state", "NO")
    ts        = msg.get("ts_utc", _utc_now_str())

    if not isinstance(sensor, str) or not isinstance(state_val, str):
        print("WARN sensor_presence: invalid fields")
        return

    is_yes = (state_val.strip().upper() == "YES")
    # Preserve the online flag; a sensor that is reporting is by definition online.
    entry = _presence.get(sensor, {})
    was_yes = entry.get("state", False)
    entry["state"]  = is_yes
    entry["ts"]     = ts
    entry["online"] = True
    _presence[sensor] = entry

    print("PRESENCE: {} {}".format(sensor, state_val))
    tcp_forward(msg)

    # Only a RISING edge is evidence of new movement. A sensor going NO must
    # never be read as "fresh motion" — that previously cancelled the vacancy
    # window whenever any other sensor happened to still be latched YES.
    if is_yes and not was_yes:
        _maybe_evaluate_on_presence_change(sensor)


def _handle_door(msg):
    sensor    = msg.get("sensor", "")
    state_val = msg.get("state", "")
    ts        = msg.get("ts_utc", _utc_now_str())

    if not isinstance(sensor, str) or not isinstance(state_val, str):
        print("WARN door: invalid fields")
        return

    was       = _door.get(sensor, {}).get("state", "")
    now_epoch = utime.time()

    if state_val == "OPEN":
        _door[sensor] = {
            "state":     "OPEN",
            "ts":        ts,
            "opened_at": now_epoch,
            "closed_at": _door.get(sensor, {}).get("closed_at", 0)
        }
    else:
        _door[sensor] = {
            "state":     "CLOSED",
            "ts":        ts,
            "opened_at": _door.get(sensor, {}).get("opened_at", 0),
            "closed_at": now_epoch
        }

    print("DOOR: {} {}".format(sensor, state_val))
    tcp_forward(msg)

    if state_val == "CLOSED" and was == "OPEN":
        print("DOOR CLOSED transition — evaluating unit occupancy")
        _evaluate_unit_occupancy_on_door_close(sensor)
    elif state_val == "OPEN":
        # The door reopening invalidates any in-flight vacancy decision: people
        # may be arriving or leaving. The next close re-arms the window.
        _cancel_vacancy_eval("door reopened")


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
    if state_val.upper() == "OFFLINE":
        # Do NOT delete the sensor from _presence. Deleting can empty the dict,
        # which makes _evaluate_unit_occupancy_on_door_close() take its
        # "no presence sensors" early return and freeze the unit state.
        # Force the entry to False instead: an offline sensor cannot assert
        # presence, but the unit still has a known presence sensor set.
        if sensor in _presence:
            _presence[sensor]["state"]  = False
            _presence[sensor]["online"] = False
            print("PRESENCE dict: {} forced NO (OFFLINE)".format(sensor))
        tcp_forward({"type":    "notification",
                     "level":   "warning",
                     "sensor":  sensor,
                     "message": "Sensor {} is OFFLINE".format(sensor)})
    elif state_val.upper() == "ONLINE":
        if sensor in _presence:
            _presence[sensor]["online"] = True
    tcp_forward(msg)


def _handle_battery(msg):
    sensor = msg.get("sensor", "?")
    pct    = msg.get("battery_pct", 0)
    if not isinstance(pct, (int, float)): return
    pct = int(pct)
    print("BATTERY: {} {}%".format(sensor, pct))
    tcp_forward(msg)


def _handle_heartbeat(msg):
    agg = msg.get("hub_aggregate", "")
    fw  = msg.get("firmware_version", "?")
    print("HEARTBEAT: hub_aggregate={} fw={}".format(agg, fw))
    for s in msg.get("sensors", []):
        if not isinstance(s, dict): continue
        role = s.get("role", "")
        name = s.get("name", "")
        ts   = msg.get("ts_utc", _utc_now_str())
        if role == "PRESENCE" and name:
            pres = s.get("presence", False)
            if isinstance(pres, bool):
                entry = _presence.get(name, {})
                entry["state"]  = pres
                entry["ts"]     = ts
                entry["online"] = bool(s.get("online", True))
                _presence[name] = entry
            # Heartbeat carries the live PIR tuning values (keep_time_sec /
            # sensitivity). Mirror them into _sensor_live so the Debugger's
            # tuning tab stays current without an explicit get_config.
            _merge_sensor_live_by_name(name, s)
        elif role == "DOOR" and name:
            contact = s.get("contact", "CLOSED")
            if name not in _door:
                _door[name] = {"state": contact, "ts": ts,
                               "opened_at": 0, "closed_at": 0}
            else:
                _door[name]["state"] = contact
                _door[name]["ts"]    = ts
            _merge_sensor_live_by_name(name, s)
    tcp_forward(msg)


def _merge_sensor_live_by_name(name, s):
    """
    Update the _sensor_live entry matching `name` from a heartbeat sensor dict.
    Heartbeat has no index, so match on name. Only touches fields the
    heartbeat actually carries; leaves index/supports_config from
    config_response intact.
    """
    for idx, live in _sensor_live.items():
        if live.get("name") != name:
            continue
        if "online" in s:
            live["online"] = s.get("online", False)
        if "battery" in s:
            live["battery"] = s.get("battery", 0)
        if "keep_time_sec" in s:
            live["keep_time_sec"] = s.get("keep_time_sec", PIR_KEEP_TIME_DEFAULT)
        if "sensitivity" in s:
            live["sensitivity"] = s.get("sensitivity", PIR_SENSITIVITY_DEFAULT)
        return


def _reconcile_sensor_dicts(sensors):
    """
    Rebuild _presence / _door to match the Hub's authoritative sensor list.

    CRITICAL: both dicts are keyed by sensor NAME, and the Hub renames sensors
    on operator request. Before this existed, renaming "Sensor_1" to "Hall"
    left a phantom "Sensor_1" entry holding its last known value forever —
    nothing would ever update it again. If that value was YES, then
    _any_presence_yes() returned True permanently and the unit could NEVER
    reach VACANT: every vacancy window was cancelled or expired to OCCUPIED.

    Called from config_response, which the Master requests after every rename,
    pairing change and sensor removal.

    Also SEEDS entries for known sensors so that a door close occurring before
    any motion has been reported still finds a populated dict (the engine
    early-returns when len(_presence) == 0).
    """
    live_presence = set()
    live_door     = set()

    for s in sensors:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        role = s.get("role", "")
        if not name or not isinstance(name, str):
            continue
        if role == "PRESENCE":
            live_presence.add(name)
            entry = _presence.get(name)
            if entry is None:
                # Seed from the Hub's live reading when it provides one, so a
                # door close occurring before the sensor's first event still
                # evaluates against reality rather than a guess.
                pres = s.get("presence")
                _presence[name] = {"state":  bool(pres) if pres is not None else False,
                                   "ts":     _utc_now_str(),
                                   "online": bool(s.get("online", False))}
            else:
                entry["online"] = bool(s.get("online", False))
        elif role == "DOOR":
            live_door.add(name)
            if name not in _door:
                contact = s.get("contact")
                _door[name] = {"state":     contact if contact else "CLOSED",
                               "ts":        _utc_now_str(),
                               "opened_at": 0,
                               "closed_at": 0}

    # Drop keys the Hub no longer knows about — renamed or removed sensors.
    for stale in [k for k in _presence if k not in live_presence]:
        del _presence[stale]
        print("PRESENCE dict: dropped stale entry '{}' "
              "(renamed or removed)".format(stale))
    for stale in [k for k in _door if k not in live_door]:
        del _door[stale]
        print("DOOR dict: dropped stale entry '{}' "
              "(renamed or removed)".format(stale))


def _handle_config_response(msg):
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
        if idx is not None and name and isinstance(name, str):
            names[str(idx)] = name
        if isinstance(idx, int):
            _sensor_live[idx] = {
                "name":            name,
                "model":           s.get("model", ""),
                "role":            s.get("role", ""),
                "online":          s.get("online", False),
                "battery":         s.get("battery", 0),
                # ZG-204ZL PIR tuning. The Hub sends keep_time_sec (DP10 enum,
                # one of 10/30/60/120) and sensitivity (DP9 enum, 0..2).
                "keep_time_sec":   s.get("keep_time_sec",  PIR_KEEP_TIME_DEFAULT),
                "sensitivity":     s.get("sensitivity",    PIR_SENSITIVITY_DEFAULT),
                "supports_config": s.get("supports_config", False),
            }
    conf["sensor_hub_config"]["sensor_names"] = names
    save_config()

    # Prune renamed/removed sensors and seed any we have not heard from yet.
    # This is what keeps a rename from permanently blocking VACANT.
    _reconcile_sensor_dicts(sensors)

    print("CONFIG RESPONSE: {} sensors".format(len(sensors)))
    # Forward raw — carries keep_time_sec/sensitivity/supports_config for the Debugger
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


_SENSOR_MSG_HANDLERS = {
    "pong":                 _handle_pong,
    "hub_ready":            _handle_hub_ready,
    "sensor_joined":        _handle_sensor_joined,
    "sensor_status":        _handle_sensor_status,
    "sensor_list_complete": _handle_sensor_list_complete,
    "new_sensor_joined":    _handle_new_sensor_joined,
    "pairing_complete":     _handle_pairing_complete,
    "hub_aggregate":        _handle_hub_aggregate,
    "sensor_presence":      _handle_sensor_presence,
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


def hub_cmd_set_sensor_config(sensor_index, keep_time_sec=None, sensitivity=None):
    """
    Push keep_time and/or motion sensitivity to a ZG-204ZL PIR sensor.
    Pass None to leave a field unchanged.
      keep_time_sec : snapped to one of 10/30/60/120 (DP10 enum)
      sensitivity   : 0=low, 1=medium, 2=high (DP9 enum)

    Sent as 'keep_time_sec'; 'fading_time' is included as a legacy alias so
    older Hub builds that parse the old key still apply the value.
    """
    if not isinstance(sensor_index, int) or sensor_index < 0:
        return
    payload = {"type": "set_sensor_config", "sensor_index": sensor_index}
    if keep_time_sec is not None:
        kt = _snap_keep_time(keep_time_sec)
        payload["keep_time_sec"] = kt
        payload["fading_time"]   = kt
    if sensitivity is not None:
        payload["sensitivity"] = _clamp_sensitivity(sensitivity)
    send_to_sensor_hub(payload)
    print("HUB set_sensor_config idx={} keep_time={} sensitivity={}".format(
          sensor_index, payload.get("keep_time_sec"), payload.get("sensitivity")))


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
        "presence_fading_time_sec":      _snap_keep_time(
                                            hub_cfg.get("presence_fading_time_sec",
                                                        PIR_KEEP_TIME_DEFAULT)),
        "door_sensor_max_silence_hours": hub_cfg.get("door_sensor_max_silence_hours",  24),
        "motion_sensitivity":            _clamp_sensitivity(
                                            hub_cfg.get("motion_sensitivity",
                                                        PIR_SENSITIVITY_DEFAULT)),
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
            # A rename changes the key used by _presence / _door. Pull the new
            # sensor list so the old key is pruned; leaving it behind would
            # strand a stale presence value and permanently block VACANT.
            _thread.start_new_thread(
                lambda: (utime.sleep_ms(800), hub_cmd_get_config()), ())
            _tcp_send_ack("set_sensor_name")
        else:
            _tcp_send_ack("set_sensor_name", "error")

    elif t == "set_sensor_config":
        idx = msg.get("sensor_index")
        if not isinstance(idx, int) or idx < 0:
            _tcp_send_ack("set_sensor_config", "error")
            return
        # Accept keep_time_sec (current) or fading_time (legacy Debugger builds).
        kt = msg.get("keep_time_sec")
        if kt is None:
            kt = msg.get("fading_time")
        hub_cmd_set_sensor_config(idx, kt, msg.get("sensitivity"))
        # Ask the Hub to report back applied values so the Debugger refreshes.
        _thread.start_new_thread(
            lambda: (utime.sleep_ms(1500), hub_cmd_get_config()), ())
        _tcp_send_ack("set_sensor_config")

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
            print("FORCE: invalid duration_hours {} (must be 1-24)".format(dur_hours))
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
                    "door_sensor_max_silence_hours", "motion_sensitivity"):
            if key in msg:
                try:    val = int(msg[key])
                except Exception: continue
                # Constrain the two PIR values to what the ZG-204ZL accepts.
                if key == "presence_fading_time_sec":
                    val = _snap_keep_time(val)
                elif key == "motion_sensitivity":
                    val = _clamp_sensitivity(val)
                hub_cfg[key] = val
        if "watchdog_enable" in msg:
            hub_cfg["watchdog_enable"] = bool(msg["watchdog_enable"])
        conf["sensor_hub_config"] = hub_cfg
        save_config()
        with _state_lock:
            if not _hub_config_push_in_progress:
                _hub_config_push_in_progress = True
                _thread.start_new_thread(lambda: (hub_cmd_push_live_config()), ())
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
            # There is no persistence: a reboot reloads master_config.py and
            # discards this. Do NOT tell the operator to reboot to apply.
            print("WIFI CONFIG set in RAM for this session only.")
            print("WIFI CONFIG: to make it permanent, edit wifi_ssid /"
                  " wifi_password in master_config.py and re-copy the file.")
            tcp_forward({
                "type":    "notification",
                "level":   "warning",
                "message": "Wi-Fi set for this session only. Reboot reverts "
                           "to master_config.py — edit that file to persist."
            })
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
    while True:
        utime.sleep_ms(500)


# ============================================================================
# PENDING BUFFER WORKER + FORCE EXPIRY CHECK
# ============================================================================

def pending_worker():
    while True:
        try:
            with _state_lock:

                f = conf.get("force", {})
                if f.get("active", False):
                    expires_ep = f.get("expires_epoch", 0)
                    now_ep     = now_epoch_utc()
                    if expires_ep > 0 and now_ep >= expires_ep:
                        print("FORCE EXPIRED — returning to automatic")
                        conf["force"] = {
                            "active":        False,
                            "status":        "",
                            "expires_utc":   "",
                            "expires_epoch": 0,
                            "reason":        ""
                        }
                        save_config()
                        tcp_forward({"type": "force_update", "active": False})
                        recalc_and_act()

                if not _force_active():
                    p = state["pending_status"]
                    t = state["pending_apply_epoch"]
                    if p is not None:
                        now_ep = now_epoch_utc()
                        if now_ep >= t:
                            latest = decide_status(
                                state["last_sensor_status"], now_ep)
                            print("PENDING due: pending={} latest={}".format(p, latest))
                            if latest == p:
                                apply_immediate(p)
                            else:
                                print("PENDING cancelled — conditions changed to {}".format(latest))
                                apply_immediate(latest, force_send=True)
                            state["pending_status"]      = None
                            state["pending_apply_epoch"] = 0
                            tcp_forward({"type":           "pending_update",
                                         "pending_status": None})

            # Vacancy confirmation window. MUST run outside the `with
            # _state_lock` block above: it may call _set_sensor_status(),
            # which acquires that same non-reentrant lock.
            _tick_vacancy_eval()

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
                                s   = bytes(_rx_tcp_buf[:rx_pos]).decode("utf-8").strip()
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
        print("WIFI: check that ssid '{}' is correct in master_config.py".format(ssid))
        # Scan and report what is actually in range. A single mistyped
        # character in the ssid is otherwise indistinguishable from a wrong
        # password or an AP that is down.
        try:
            found = False
            print("WIFI: networks in range —")
            for net in wlan.scan():
                try:
                    seen = net[0].decode("utf-8")
                except Exception:
                    continue
                rssi = net[3] if len(net) > 3 else 0
                if seen == ssid:
                    found = True
                    print("   * {} ({} dBm)  <-- matches configured ssid".format(
                          seen, rssi))
                else:
                    print("     {} ({} dBm)".format(seen, rssi))
            if not found:
                print("WIFI: ssid '{}' NOT FOUND — it is a typo or the AP"
                      " is down".format(ssid))
            else:
                print("WIFI: ssid found but association failed — check the"
                      " password")
        except Exception as e:
            print("WIFI: scan failed:", repr(e))
        with _state_lock:
            if not _force_active():
                recalc_and_act()
        while True:
            utime.sleep(30)
            try:
                # The STA may still be mid-association from the previous
                # attempt. Calling connect() again in that state raises
                # "Wifi Internal State Error" (esp: "sta is connecting, cannot
                # set config") and the retry can never succeed. Tear the
                # interface down first so every retry starts from a clean state.
                try:
                    wlan.disconnect()
                except Exception:
                    pass
                wlan.active(False)
                utime.sleep_ms(500)
                wlan.active(True)
                utime.sleep_ms(200)

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
                print("WIFI: retry failed — ssid '{}' not found or wrong"
                      " password".format(ssid))
            except Exception as e:
                print("WIFI retry error:", repr(e))

    if wlan.isconnected():
        cfg_info = wlan.ifconfig()
        _wifi_ip = cfg_info[0]
        print("WIFI: connected IP={}".format(_wifi_ip))
        _maybe_start_tcp_server()

    retries = 3
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