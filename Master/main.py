# main.py — MASTER (ESP32-S3)
# Innovatsii EMS — Pico 1
#
# UART1 <-> Sensor Hub  (TX=GPIO16, RX=GPIO17)
# UART2 <-> Scheduler   (TX=GPIO18, RX=GPIO21)
#
# Production rules applied:
#   - Pre-allocated fixed bytearray RX buffers (no unbounded growth)
#   - All JSON built as dict then serialised once
#   - All exception paths explicitly handled and logged
#   - Tick arithmetic uses ticks_diff for rollover safety
#   - Config written atomically (write + rename pattern)
#   - Every shared state access is lock-protected
#   - UART TX serialised through a helper — never interleaved

import utime
import ujson as json
from machine import UART
import _thread
import master_config as cfg

# ============================================================================
# CONSTANTS
# ============================================================================

# Maximum bytes held in RX accumulation buffer before forced flush.
# Protects against a sender that never sends a newline.
UART_RX_BUF_MAX  = 512

# How often the main loop wakes to check pending timers (milliseconds)
MAIN_LOOP_TICK_MS = 1000

# ============================================================================
# UART INITIALISATION
# ============================================================================

uart_sensor = UART(cfg.SENSOR_UART_ID,
                   baudrate=cfg.SENSOR_UART_BAUD,
                   tx=cfg.SENSOR_UART_TX,
                   rx=cfg.SENSOR_UART_RX)

uart_sched  = UART(cfg.SCHED_UART_ID,
                   baudrate=cfg.SCHED_UART_BAUD,
                   tx=cfg.SCHED_UART_TX,
                   rx=cfg.SCHED_UART_RX)

# Pre-allocated RX accumulation buffers — fixed size, never reallocated
_rx_sensor_buf = bytearray(UART_RX_BUF_MAX)
_rx_sched_buf  = bytearray(UART_RX_BUF_MAX)
_rx_sensor_pos = 0
_rx_sched_pos  = 0

# ============================================================================
# LOCKS
# One lock guards all shared mutable state.
# TX lock prevents interleaved UART writes from multiple threads.
# ============================================================================

_state_lock = _thread.allocate_lock()
_tx_sensor_lock = _thread.allocate_lock()
_tx_sched_lock  = _thread.allocate_lock()

# ============================================================================
# RUNTIME STATE
# All fields have explicit types and safe defaults.
# ============================================================================

state = {
    "last_sensor_status":    "vacant",    # str: "occupied" | "vacant"
    "last_scheduler_status": None,        # str | None
    "current_decided_status": None,       # str | None — None forces first send
    "pending_status":        None,        # str | None
    "pending_apply_epoch":   0,           # int UTC epoch
}

# ============================================================================
# PERSISTENT CONFIG
# ============================================================================

conf = {}
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

    # Merge any missing keys from defaults — forward compatible
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
    """
    Atomic config write.
    Write to .tmp file first, then rename to final path.
    Prevents partial writes from corrupting the config on power loss.
    """
    tmp = cfg.CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(conf, f)
        # MicroPython uos.rename is atomic on LittleFS
        import uos
        uos.rename(tmp, cfg.CONFIG_FILE)
    except Exception as e:
        print("CONFIG save failed:", e)


# ============================================================================
# UART TX HELPERS
# All UART writes go through these helpers.
# Lock prevents output from two threads interleaving mid-message.
# ============================================================================

def _uart_send(uart_obj, lock_obj, payload_dict):
    """
    Serialise dict to JSON, append newline, write to UART.
    Never raises — all exceptions are caught and logged.
    """
    try:
        msg = json.dumps(payload_dict) + "\n"
        with lock_obj:
            uart_obj.write(msg.encode("utf-8"))
    except Exception as e:
        print("UART TX error:", repr(e))


def send_to_sensor_hub(payload_dict):
    _uart_send(uart_sensor, _tx_sensor_lock, payload_dict)


def send_to_scheduler(payload_dict):
    _uart_send(uart_sched, _tx_sched_lock, payload_dict)


# ============================================================================
# UTC TIME HELPERS
# ============================================================================

def parse_utc_to_epoch(dt_str):
    """
    Parse "YYYY-MM-DD HH:MM:SS" to seconds since epoch.
    Returns 0 on any parse error — safe default (epoch 0 = Jan 1 2000 in MicroPython).
    """
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
    """
    Return current UTC epoch.
    If use_manual_utc_now is True, advance the manual base by elapsed ms.
    This allows offline testing without NTP.
    """
    if conf.get("use_manual_utc_now", False):
        base    = parse_utc_to_epoch(conf.get("manual_utc_now", "2000-01-01 00:00:00"))
        elapsed = utime.ticks_diff(utime.ticks_ms(), boot_ms) // 1000
        return base + elapsed
    return utime.time()


def in_booking_window(now_ep, checkin_ep, checkout_ep):
    return checkin_ep <= now_ep <= checkout_ep


# ============================================================================
# UNIT STATE DECISION
# ============================================================================

def decide_status(sensor_status, now_ep):
    """
    Map sensor occupancy + booking window to one of the four unit states.
    Returns one of: "Occupied", "Vacant", "Sold Vacant", "UnSold Occupied"
    """
    checkin_ep  = parse_utc_to_epoch(conf.get("check_in_utc",  "2000-01-01 00:00:00"))
    checkout_ep = parse_utc_to_epoch(conf.get("check_out_utc", "2000-01-01 00:00:00"))
    inside      = in_booking_window(now_ep, checkin_ep, checkout_ep)

    if sensor_status == "occupied":
        return "Occupied"      if inside else "UnSold Occupied"
    else:
        return "Sold Vacant"   if inside else "Vacant"


# ============================================================================
# SCHEDULER COMMANDS
# ============================================================================

def send_status_to_scheduler(status):
    send_to_scheduler({"type": "set_status", "status": status})
    print("MASTER -> SCHED: set_status={}".format(status))


def apply_immediate(status, force_send=False):
    if force_send or state["current_decided_status"] != status:
        state["current_decided_status"] = status
        conf["last_decided_status"] = status
        save_config()
        send_status_to_scheduler(status)
    else:
        print("STATUS unchanged, not re-sent: {}".format(status))


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


def recalc_and_act():
    now_ep = now_epoch_utc()
    sensor = state["last_sensor_status"]
    target = decide_status(sensor, now_ep)
    print("DECIDE: sensor={} now={} => {}".format(sensor, now_ep, target))

    if target in ("Sold Vacant", "Vacant"):
        schedule_buffered(target, now_ep)
    else:
        state["pending_status"]      = None
        state["pending_apply_epoch"] = 0
        apply_immediate(target)


# ============================================================================
# SENSOR HUB MESSAGE HANDLERS
# One function per message type.
# Each handler validates required fields before acting.
# Missing or invalid fields produce a warning — never a crash.
# ============================================================================

def _handle_unit_occupancy(msg):
    """
    {"type":"unit_occupancy","state":"OCCUPIED","ts_utc":"HH:MM:SS"}
    Core message — drives the four-state unit logic.
    """
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
        print("SENSOR HUB occupancy={} ts={}".format(
              sensor_status, msg.get("ts_utc", "?")))
        recalc_and_act()


def _handle_sensor_presence(msg):
    """
    {"type":"sensor_presence","sensor":"Living_Room",
     "model":"ZG-204ZV","state":"YES","ts_utc":"HH:MM:SS"}
    Individual sensor presence — forward to MQTT (Phase 6).
    """
    sensor = msg.get("sensor", "unknown")
    state_val = msg.get("state", "?")
    ts     = msg.get("ts_utc", "?")
    model  = msg.get("model", "?")
    print("PRESENCE: sensor={} model={} state={} ts={}".format(
          sensor, model, state_val, ts))
    # TODO Phase 6: publish to MQTT
    # mqtt_publish("sensor/presence", msg)


def _handle_environment(msg):
    """
    {"type":"environment","sensor":"Living_Room",
     "temp_c_x100":3290,"hum_pct_x100":6370,"ts_utc":"HH:MM:SS"}
    Temperature and humidity — forward to MQTT (Phase 6).
    Note: values are x100 integers from Sensor Hub to avoid float issues.
    """
    sensor       = msg.get("sensor", "unknown")
    temp_x100    = msg.get("temp_c_x100", 0)
    hum_x100     = msg.get("hum_pct_x100", 0)
    ts           = msg.get("ts_utc", "?")

    if not isinstance(temp_x100, (int, float)):
        print("WARN environment: invalid temp field")
        return

    temp_c   = temp_x100  / 100.0
    hum_pct  = hum_x100   / 100.0
    print("ENVIRONMENT: sensor={} temp={:.2f}C hum={:.2f}% ts={}".format(
          sensor, temp_c, hum_pct, ts))
    # TODO Phase 6: publish to MQTT


def _handle_door(msg):
    """
    {"type":"door","sensor":"Front_Door",
     "state":"OPEN","ts_utc":"HH:MM:SS"}
    Door open/close event — forward to MQTT (Phase 6).
    """
    sensor    = msg.get("sensor", "unknown")
    door_state = msg.get("state", "?")
    ts        = msg.get("ts_utc", "?")
    print("DOOR: sensor={} state={} ts={}".format(sensor, door_state, ts))
    # TODO Phase 6: publish to MQTT


def _handle_door_alarm(msg):
    """
    {"type":"door_alarm","sensor":"Front_Door",
     "state":"ALARM","duration_sec":612,"ts_utc":"HH:MM:SS"}
    Door alarm — forward to MQTT immediately (Phase 6).
    This is an alert-level event — gets priority MQTT publish.
    """
    sensor   = msg.get("sensor", "unknown")
    alarm    = msg.get("state", "?")
    duration = msg.get("duration_sec", 0)
    ts       = msg.get("ts_utc", "?")
    print("DOOR ALARM: sensor={} state={} duration={}s ts={}".format(
          sensor, alarm, duration, ts))
    # TODO Phase 6: publish to MQTT alert topic


def _handle_sensor_health(msg):
    """
    {"type":"sensor_health","sensor":"Bedroom_1",
     "state":"OFFLINE","ts_utc":"HH:MM:SS"}
    Sensor health — forward to MQTT as notification (Phase 6).
    """
    sensor     = msg.get("sensor", "unknown")
    health     = msg.get("state", "?")
    ts         = msg.get("ts_utc", "?")
    print("SENSOR HEALTH: sensor={} state={} ts={}".format(
          sensor, health, ts))
    # TODO Phase 6: publish to MQTT notification topic


def _handle_heartbeat(msg):
    """
    Periodic heartbeat from Sensor Hub.
    Contains full sensor snapshot — forward to MQTT (Phase 6).
    """
    unit  = msg.get("unit_state", "?")
    ts    = msg.get("ts_utc", "?")
    print("HEARTBEAT: unit={} ts={}".format(unit, ts))
    # TODO Phase 6: publish to MQTT


def _handle_config_response(msg):
    """
    Reply to get_config command.
    Update local knowledge of sensor names.
    """
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
    print("CONFIG RESPONSE: sensor names updated from hub")


def _handle_ack(msg):
    """
    Acknowledgement from Sensor Hub for a command we sent.
    Log it. In Phase 8 this will feed the retry/timeout mechanism.
    """
    cmd    = msg.get("command", "?")
    status = msg.get("status", "?")
    ts     = msg.get("ts_utc", "?")
    print("ACK: command={} status={} ts={}".format(cmd, status, ts))


def _handle_log_response(msg):
    """
    Log dump from Sensor Hub in response to get_logs command.
    Print locally and forward to MQTT debug topic (Phase 8).
    """
    line = msg.get("line", "")
    print("SENSORHUB LOG:", line)
    # TODO Phase 8: publish to MQTT debug/response topic


# ============================================================================
# SENSOR HUB MESSAGE DISPATCH TABLE
# Add new message types here — handler function is the only change needed.
# ============================================================================

_SENSOR_MSG_HANDLERS = {
    "unit_occupancy":   _handle_unit_occupancy,
    "sensor_presence":  _handle_sensor_presence,
    "environment":      _handle_environment,
    "door":             _handle_door,
    "door_alarm":       _handle_door_alarm,
    "sensor_health":    _handle_sensor_health,
    "heartbeat":        _handle_heartbeat,
    "config_response":  _handle_config_response,
    "ack":              _handle_ack,
    "log_response":     _handle_log_response,
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
        # TODO Phase 6: publish relay states to MQTT
    else:
        print("WARN: unknown scheduler msg type '{}'".format(msg_type))


# ============================================================================
# SENSOR HUB COMMAND SENDERS
# Master -> Sensor Hub outbound commands.
# ============================================================================

def hub_cmd_push_config():
    """Push the sensor_hub_config block to the Sensor Hub."""
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
    send_to_sensor_hub(payload)
    print("MASTER -> SENSORHUB: set_config sent")


def hub_cmd_set_sensor_name(sensor_index, name):
    """Rename a sensor on the Sensor Hub."""
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
# UART RX LOOPS
# Each UART runs in its own thread.
# Uses pre-allocated bytearray — no runtime allocation.
# Lines accumulate until newline found, then dispatched.
# Oversized lines (buffer fills before newline) are discarded with warning.
# ============================================================================

def _process_uart_line(line_bytes, handler_fn):
    """
    Decode a complete newline-terminated line and dispatch to handler.
    Returns True if successfully handled, False otherwise.
    """
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
                        if ch == ord('\r'):
                            continue
                        if ch == ord('\n'):
                            if _rx_sensor_pos > 0:
                                _process_uart_line(
                                    memoryview(_rx_sensor_buf)[:_rx_sensor_pos],
                                    handle_sensor_msg)
                                _rx_sensor_pos = 0
                        else:
                            if _rx_sensor_pos >= UART_RX_BUF_MAX - 1:
                                print("WARN: sensor RX buf overflow — discarding")
                                _rx_sensor_pos = 0
                            _rx_sensor_buf[_rx_sensor_pos] = ch
                            _rx_sensor_pos += 1
            utime.sleep_ms(30)
        except Exception as e:
            print("sensor_rx_loop error:", repr(e))
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
                        if ch == ord('\r'):
                            continue
                        if ch == ord('\n'):
                            if _rx_sched_pos > 0:
                                _process_uart_line(
                                    memoryview(_rx_sched_buf)[:_rx_sched_pos],
                                    handle_scheduler_msg)
                                _rx_sched_pos = 0
                        else:
                            if _rx_sched_pos >= UART_RX_BUF_MAX - 1:
                                print("WARN: sched RX buf overflow — discarding")
                                _rx_sched_pos = 0
                            _rx_sched_buf[_rx_sched_pos] = ch
                            _rx_sched_pos += 1
            utime.sleep_ms(30)
        except Exception as e:
            print("sched_rx_loop error:", repr(e))
            utime.sleep_ms(200)


# ============================================================================
# PENDING BUFFER WORKER
# Checks every second if a buffered status change is due.
# Re-evaluates conditions at apply time — cancels if situation changed.
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
            utime.sleep(1)
        except Exception as e:
            print("pending_worker error:", repr(e))
            utime.sleep(1)


# ============================================================================
# STARTUP
# ============================================================================

load_config()

# Restore runtime state from persisted config
state["last_sensor_status"]    = str(conf.get("last_sensor_status",  "vacant")).lower()
state["last_scheduler_status"] = conf.get("last_scheduler_status",  None)
state["current_decided_status"] = None  # Force first send on boot

print("MASTER started")
print("UTC epoch     :", now_epoch_utc())
print("check_in_utc  :", conf.get("check_in_utc"))
print("check_out_utc :", conf.get("check_out_utc"))
print("buffer_minutes:", conf.get("buffer_minutes"))

# Push sensor hub config on boot
utime.sleep_ms(500)   # brief delay — let Sensor Hub finish booting
hub_cmd_push_config()
utime.sleep_ms(200)
hub_cmd_get_config()  # request sensor list to sync names

# Calculate and apply initial status
with _state_lock:
    recalc_and_act()
    # Force-send to Scheduler on boot regardless of saved state
    if state["current_decided_status"] is not None:
        send_status_to_scheduler(state["current_decided_status"])

# Start background threads
_thread.start_new_thread(sensor_rx_loop, ())
_thread.start_new_thread(sched_rx_loop,  ())
_thread.start_new_thread(pending_worker, ())

# ============================================================================
# MAIN LOOP
# Keeps the main thread alive.
# Phase 6 will add MQTT polling here.
# Phase 7 will add TCP debug server polling here.
# ============================================================================

while True:
    utime.sleep_ms(MAIN_LOOP_TICK_MS)