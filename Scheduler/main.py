# main.py - Scheduler (ESP32-S3)
# - GPIO-keyed relay schedules (R4, R5, R16...)
# - Receives status from Master over UART: {"type":"set_status","status":"Occupied"}
# - Sends updates to Master: {"type":"scheduler_update", ...}

import utime
import ujson as json
from machine import Pin, UART
import _thread
import config

# ---------------- USER SETTINGS ----------------
RELAY_PINS = [4, 5, 16, 17, 18, 21, 35, 36, 37, 38]  # your relay GPIOs
UART_ID = 1
UART_BAUD = 115200
UART_TX_PIN = 12   # Scheduler TX -> Master RX2
UART_RX_PIN = 13   # Scheduler RX <- Master TX2
# -----------------------------------------------

relays = []
current_config = {}
state_lock = _thread.allocate_lock()

relay_state = [None] * len(RELAY_PINS)
last_applied_status = None

uart = None
last_telemetry_json = None
last_telemetry_ms = 0


def load_config():
    global current_config
    try:
        with open(config.CONFIG_FILE, "r") as f:
            current_config = json.load(f)
        print("✅ Loaded config from file")
    except Exception as e:
        print("⚠️ Config load failed, using default:", e)
        current_config = config.DEFAULT_CONFIG.copy()
        save_config()


def save_config():
    try:
        with open(config.CONFIG_FILE, "w") as f:
            json.dump(current_config, f)
    except Exception as e:
        print("❌ Config save failed:", e)


def normalize_text(s):
    if s is None:
        return ""
    return str(s).strip().lower()


def parse_duration_to_seconds(raw):
    s = normalize_text(raw)
    s = s.replace("hours", "hr").replace("hour", "hr").replace("hrs", "hr")
    s = s.replace("minutes", "min").replace("minute", "min").replace("mins", "min")
    s = s.replace("seconds", "sec").replace("second", "sec").replace("secs", "sec")
    s = s.replace(" ", "")

    # 1:30hr => 1h30m
    if ":" in s and s.endswith("hr"):
        a = s[:-2]
        h, m = a.split(":")
        return int(h) * 3600 + int(m) * 60

    # 1:30min => 1m30s
    if ":" in s and s.endswith("min"):
        a = s[:-3]
        m, sec = a.split(":")
        return int(m) * 60 + int(sec)

    # 1:30 => H:M
    if ":" in s:
        h, m = s.split(":")
        return int(h) * 3600 + int(m) * 60

    if s.endswith("hr"):
        return int(s[:-2]) * 3600
    if s.endswith("min"):
        return int(s[:-3]) * 60
    if s.endswith("sec"):
        return int(s[:-3])

    # plain number => minutes
    return int(s) * 60


def parse_schedule(cmd):
    """
    Returns:
      ("unused",)
      ("always_on",)
      ("always_off",)
      ("cycle", on_sec, off_sec)
    """
    if cmd is None:
        return ("unused",)

    text = normalize_text(cmd)
    if not text:
        return ("unused",)

    if "on all time" in text:
        return ("always_on",)
    if "off all time" in text:
        return ("always_off",)

    # parse: "<dur> on : <dur> off"
    try:
        on_pos = text.find(" on")
        off_pos = text.rfind(" off")
        if on_pos != -1 and off_pos != -1 and off_pos > on_pos:
            on_part = text[:on_pos].strip()
            between = text[on_pos + 3:off_pos].strip()
            if between.startswith(":"):
                between = between[1:].strip()
            off_part = between

            on_s = parse_duration_to_seconds(on_part)
            off_s = parse_duration_to_seconds(off_part)

            if on_s > 0 and off_s > 0:
                return ("cycle", on_s, off_s)
    except Exception:
        pass

    return ("always_off",)


def set_relay(i, is_on):
    pin = relays[i]
    if pin is None:
        return
    try:
        pin.value(1 if is_on else 0)  # active-high relay board
        print("R{} (GPIO{}) = {}".format(RELAY_PINS[i], RELAY_PINS[i], "ON" if is_on else "OFF"))
    except Exception as e:
        print("Relay write error GPIO{}: {}".format(RELAY_PINS[i], e))


def init_relays():
    global relays
    relays = []
    for gpio in RELAY_PINS:
        try:
            p = Pin(gpio, Pin.OUT, value=0)
            relays.append(p)
            print("R{} -> GPIO{}".format(gpio, gpio))
        except Exception as e:
            print("Init failed GPIO{}: {}".format(gpio, e))
            relays.append(None)


def apply_status(new_status):
    global last_applied_status
    with state_lock:
        sch = current_config.get("Scheduler", {})
        if new_status not in sch:
            print("❌ Unknown status:", new_status)
            return False
        current_config["currentStatus"] = new_status
        save_config()
        last_applied_status = None
    print("✅ Status changed to:", new_status)
    return True


def rebuild_runtime_for_status(status):
    global relay_state
    now = utime.ticks_ms()

    with state_lock:
        sch = current_config.get("Scheduler", {})
        status_map = sch.get(status, {})

    print("Applying schedule for status:", status)

    for i, gpio in enumerate(RELAY_PINS):
        if relays[i] is None:
            relay_state[i] = None
            continue

        key = "R{}".format(gpio)
        cmd = status_map.get(key, None)
        parsed = parse_schedule(cmd)

        if parsed[0] == "unused":
            relay_state[i] = {"mode": "always_off"}
            set_relay(i, False)

        elif parsed[0] == "always_on":
            relay_state[i] = {"mode": "always_on"}
            set_relay(i, True)

        elif parsed[0] == "always_off":
            relay_state[i] = {"mode": "always_off"}
            set_relay(i, False)

        else:
            _, on_s, off_s = parsed
            relay_state[i] = {
                "mode": "cycle",
                "phase": "on",
                "on_ms": on_s * 1000,
                "off_ms": off_s * 1000,
                "next_toggle_ms": utime.ticks_add(now, on_s * 1000),
            }
            print("R{} cycle: {}s ON / {}s OFF".format(gpio, on_s, off_s))
            set_relay(i, True)


def get_relay_snapshot():
    snap = {}
    for i, gpio in enumerate(RELAY_PINS):
        p = relays[i]
        snap["R{}".format(gpio)] = -1 if p is None else int(p.value())
    return snap


def send_scheduler_update(force=False):
    global last_telemetry_json, last_telemetry_ms

    if uart is None:
        return

    with state_lock:
        status = current_config.get("currentStatus", "Vacant")

    payload = {
        "type": "scheduler_update",
        "status": status,
        "relays": get_relay_snapshot(),
        "ts_utc": utime.time()
    }

    try:
        msg = json.dumps(payload)
    except Exception:
        return

    now = utime.ticks_ms()
    changed = (msg != last_telemetry_json)
    heartbeat_due = utime.ticks_diff(now, last_telemetry_ms) > 5000

    if force or changed or heartbeat_due:
        try:
            uart.write(msg + "\n")
            last_telemetry_json = msg
            last_telemetry_ms = now
        except Exception as e:
            print("UART TX error:", repr(e))


def scheduler_loop():
    global last_applied_status

    while True:
        try:
            with state_lock:
                status = current_config.get("currentStatus", "Vacant")

            if status != last_applied_status:
                rebuild_runtime_for_status(status)
                last_applied_status = status
                send_scheduler_update(force=True)

            now = utime.ticks_ms()
            changed_any = False

            for i in range(len(RELAY_PINS)):
                st = relay_state[i]
                if not st or st.get("mode") != "cycle":
                    continue

                if utime.ticks_diff(now, st["next_toggle_ms"]) >= 0:
                    if st["phase"] == "on":
                        set_relay(i, False)
                        st["phase"] = "off"
                        st["next_toggle_ms"] = utime.ticks_add(now, st["off_ms"])
                    else:
                        set_relay(i, True)
                        st["phase"] = "on"
                        st["next_toggle_ms"] = utime.ticks_add(now, st["on_ms"])
                    changed_any = True

            if changed_any:
                send_scheduler_update(force=True)
            else:
                send_scheduler_update(force=False)

            utime.sleep_ms(200)

        except Exception as e:
            print("Scheduler error:", repr(e))
            utime.sleep_ms(500)


def uart_rx_loop():
    """
    Master -> Scheduler:
    {"type":"set_status","status":"Occupied"}
    """
    buf = b""

    while True:
        try:
            if uart and uart.any():
                data = uart.read()
                if not data:
                    utime.sleep_ms(20)
                    continue

                buf += data

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue

                    try:
                        s = line.decode("utf-8").strip()
                    except Exception:
                        continue  # ignore garbage bytes

                    if not s:
                        continue

                    try:
                        msg = json.loads(s)
                    except Exception:
                        print("UART bad JSON:", s)
                        continue

                    if msg.get("type") == "set_status":
                        st = msg.get("status")
                        if isinstance(st, str):
                            if apply_status(st):
                                send_scheduler_update(force=True)
                        else:
                            print("Invalid set_status payload:", msg)

            utime.sleep_ms(30)

        except Exception as e:
            print("UART RX error:", repr(e))
            utime.sleep_ms(100)


# ===== START =====
load_config()
init_relays()

try:
    uart = UART(UART_ID, baudrate=UART_BAUD, tx=UART_TX_PIN, rx=UART_RX_PIN)
    print("UART ready: id={}, tx={}, rx={}, baud={}".format(UART_ID, UART_TX_PIN, UART_RX_PIN, UART_BAUD))
except Exception as e:
    uart = None
    print("❌ UART init failed:", repr(e))

print("🚀 Scheduler Started")
print("Active Relays:", sum(1 for r in relays if r is not None))

_thread.start_new_thread(scheduler_loop, ())
_thread.start_new_thread(uart_rx_loop, ())

while True:
    utime.sleep(30)
