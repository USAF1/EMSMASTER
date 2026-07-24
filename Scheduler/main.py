# main.py - Scheduler (ESP32-S3)
# Innovatsii EMS — Pico 1
#
# - GPIO-keyed relay schedules (R4, R5, R16...)
# - Receives commands from Master over UART
# - Sends scheduler_update to Master every 5s or on relay change
#
# Production rules applied:
#   - Pre-allocated fixed bytearray RX buffer (no unbounded growth)
#   - Atomic config save (write-to-tmp then rename)
#   - factory_reset and restart commands handled
#   - All exception paths explicitly handled

import utime
import ujson as json
from machine import Pin, UART, reset
import _thread
import uos
import config

# ---------------- SETTINGS ----------------
RELAY_PINS   = [4, 5, 16, 17, 18, 21, 35, 36, 37, 38]
UART_ID      = 1
UART_BAUD    = 115200
UART_TX_PIN  = 12    # Scheduler TX -> Master RX (GPIO21)
UART_RX_PIN  = 11    # Scheduler RX <- Master TX (GPIO18)  ← fixed from 13
UART_RX_BUF  = 512
# ------------------------------------------

relays             = []
current_config     = {}
state_lock         = _thread.allocate_lock()
relay_state        = [None] * len(RELAY_PINS)
last_applied_status = None
uart               = None
last_telemetry_json = None
last_telemetry_ms   = 0


def load_config():
    global current_config
    try:
        with open(config.CONFIG_FILE, "r") as f:
            current_config = json.load(f)
        print("Scheduler: config loaded")
    except Exception as e:
        print("Scheduler: config load failed, using default:", e)
        current_config = config.DEFAULT_CONFIG.copy()
        save_config()


def save_config():
    """Atomic write — write to .tmp then rename."""
    tmp = config.CONFIG_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(current_config, f)
        uos.rename(tmp, config.CONFIG_FILE)
    except Exception as e:
        print("Scheduler: config save failed:", e)


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

    if ":" in s and s.endswith("hr"):
        a = s[:-2]; h, m = a.split(":")
        return int(h) * 3600 + int(m) * 60
    if ":" in s and s.endswith("min"):
        a = s[:-3]; m, sec = a.split(":")
        return int(m) * 60 + int(sec)
    if ":" in s:
        h, m = s.split(":")
        return int(h) * 3600 + int(m) * 60
    if s.endswith("hr"):
        return int(s[:-2]) * 3600
    if s.endswith("min"):
        return int(s[:-3]) * 60
    if s.endswith("sec"):
        return int(s[:-3])
    return int(s) * 60


def parse_schedule(cmd):
    if cmd is None:
        return ("unused",)
    text = normalize_text(cmd)
    if not text:
        return ("unused",)
    if "on all time" in text:
        return ("always_on",)
    if "off all time" in text:
        return ("always_off",)
    try:
        on_pos  = text.find(" on")
        off_pos = text.rfind(" off")
        if on_pos != -1 and off_pos != -1 and off_pos > on_pos:
            on_part = text[:on_pos].strip()
            between = text[on_pos + 3:off_pos].strip()
            if between.startswith(":"):
                between = between[1:].strip()
            on_s  = parse_duration_to_seconds(on_part)
            off_s = parse_duration_to_seconds(between)
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
        pin.value(1 if is_on else 0)
        print("R{} = {}".format(RELAY_PINS[i], "ON" if is_on else "OFF"))
    except Exception as e:
        print("Relay write error GPIO{}: {}".format(RELAY_PINS[i], e))


def init_relays():
    global relays
    relays = []
    for gpio in RELAY_PINS:
        try:
            p = Pin(gpio, Pin.OUT, value=0)
            relays.append(p)
            print("Relay GPIO{} initialised".format(gpio))
        except Exception as e:
            print("Relay init failed GPIO{}: {}".format(gpio, e))
            relays.append(None)


def apply_status(new_status):
    global last_applied_status
    with state_lock:
        sch = current_config.get("Scheduler", {})
        if new_status not in sch:
            print("Scheduler: unknown status:", new_status)
            return False
        current_config["currentStatus"] = new_status
        save_config()
        last_applied_status = None
    print("Scheduler: status -> {}".format(new_status))
    return True


def rebuild_runtime_for_status(status):
    global relay_state
    now = utime.ticks_ms()
    with state_lock:
        status_map = current_config.get("Scheduler", {}).get(status, {})
    print("Scheduler: building schedule for", status)
    for i, gpio in enumerate(RELAY_PINS):
        if relays[i] is None:
            relay_state[i] = None
            continue
        key    = "R{}".format(gpio)
        cmd    = status_map.get(key, None)
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
                "mode":           "cycle",
                "phase":          "on",
                "on_ms":          on_s * 1000,
                "off_ms":         off_s * 1000,
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
        "type":    "scheduler_update",
        "status":  status,
        "relays":  get_relay_snapshot(),
        "ts_utc":  utime.time()
    }
    try:
        msg = json.dumps(payload)
    except Exception:
        return
    now          = utime.ticks_ms()
    changed      = (msg != last_telemetry_json)
    heartbeat_due = utime.ticks_diff(now, last_telemetry_ms) > 5000
    if force or changed or heartbeat_due:
        try:
            uart.write(msg + "\n")
            last_telemetry_json = msg
            last_telemetry_ms   = now
        except Exception as e:
            print("Scheduler: UART TX error:", repr(e))


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

            now         = utime.ticks_ms()
            changed_any = False
            for i in range(len(RELAY_PINS)):
                st = relay_state[i]
                if not st or st.get("mode") != "cycle":
                    continue
                if utime.ticks_diff(now, st["next_toggle_ms"]) >= 0:
                    if st["phase"] == "on":
                        set_relay(i, False)
                        st["phase"]          = "off"
                        st["next_toggle_ms"] = utime.ticks_add(now, st["off_ms"])
                    else:
                        set_relay(i, True)
                        st["phase"]          = "on"
                        st["next_toggle_ms"] = utime.ticks_add(now, st["on_ms"])
                    changed_any = True

            if changed_any:
                send_scheduler_update(force=True)
            else:
                send_scheduler_update(force=False)
            utime.sleep_ms(200)
        except Exception as e:
            print("Scheduler loop error:", repr(e))
            utime.sleep_ms(500)


def uart_rx_loop():
    """
    Receives commands from Master.
    Pre-allocated bytearray accumulation — no unbounded growth.
    Handles: set_status, restart, factory_reset
    """
    rx_buf = bytearray(UART_RX_BUF)
    rx_pos = 0

    while True:
        try:
            if uart and uart.any():
                data = uart.read()
                if data:
                    for b in data:
                        ch = b if isinstance(b, int) else ord(b)
                        if ch == ord('\r'):
                            continue
                        if ch == ord('\n'):
                            if rx_pos > 0:
                                try:
                                    s = rx_buf[:rx_pos].decode("utf-8").strip()
                                except Exception:
                                    rx_pos = 0
                                    continue
                                rx_pos = 0
                                if not s:
                                    continue
                                try:
                                    msg = json.loads(s)
                                except Exception:
                                    print("Scheduler: bad JSON:", s[:60])
                                    continue
                                if not isinstance(msg, dict):
                                    continue

                                t = msg.get("type", "")

                                if t == "set_status":
                                    st = msg.get("status")
                                    if isinstance(st, str):
                                        if apply_status(st):
                                            send_scheduler_update(force=True)
                                    else:
                                        print("Scheduler: invalid set_status payload")

                                elif t == "restart":
                                    print("Scheduler: restart command received — rebooting")
                                    utime.sleep_ms(200)
                                    reset()

                                elif t == "factory_reset":
                                    print("Scheduler: factory_reset — erasing config and rebooting")
                                    for fname in (config.CONFIG_FILE,
                                                  config.CONFIG_FILE + ".tmp"):
                                        try:
                                            uos.remove(fname)
                                            print("Scheduler: deleted", fname)
                                        except Exception:
                                            pass
                                    utime.sleep_ms(200)
                                    reset()

                                else:
                                    print("Scheduler: unknown command '{}'".format(t))
                        else:
                            if rx_pos >= UART_RX_BUF - 1:
                                print("Scheduler: RX overflow — discarding")
                                rx_pos = 0
                            else:
                                rx_buf[rx_pos] = ch
                                rx_pos += 1
            utime.sleep_ms(30)
        except Exception as e:
            print("Scheduler UART RX error:", repr(e))
            utime.sleep_ms(100)


# ===== START =====
load_config()
init_relays()

try:
    uart = UART(UART_ID, baudrate=UART_BAUD, tx=UART_TX_PIN, rx=UART_RX_PIN)
    print("Scheduler: UART ready tx={} rx={} baud={}".format(
          UART_TX_PIN, UART_RX_PIN, UART_BAUD))
except Exception as e:
    uart = None
    print("Scheduler: UART init failed:", repr(e))

print("Scheduler started — active relays: {}".format(
      sum(1 for r in relays if r is not None)))

_thread.start_new_thread(scheduler_loop, ())
_thread.start_new_thread(uart_rx_loop,   ())

while True:
    utime.sleep(30)