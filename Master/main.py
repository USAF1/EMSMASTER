# main.py - MASTER (ESP32-S3)
# UART1 <-> SensorHub
# UART2 <-> Scheduler

import utime
import ujson as json
from machine import UART
import _thread
import master_config as cfg

lock = _thread.allocate_lock()

uart_sensor = UART(1, baudrate=115200, tx=cfg.SENSOR_UART_TX, rx=cfg.SENSOR_UART_RX)
uart_sched = UART(2, baudrate=115200, tx=cfg.SCHED_UART_TX, rx=cfg.SCHED_UART_RX)

conf = {}

state = {
    "last_sensor_status": "vacant",
    "last_scheduler_status": None,
    "current_decided_status": None,   # desired status currently active on master
    "pending_status": None,
    "pending_apply_epoch": 0
}

boot_ms = utime.ticks_ms()


# ---------- Config ----------
def load_config():
    global conf
    try:
        with open(cfg.CONFIG_FILE, "r") as f:
            conf = json.load(f)
        print("✅ MASTER config loaded")
    except Exception as e:
        print("⚠️ Config load failed, using default:", e)
        conf = cfg.DEFAULT_CONFIG.copy()
        save_config()

    changed = False
    for k, v in cfg.DEFAULT_CONFIG.items():
        if k not in conf:
            conf[k] = v
            changed = True
    if changed:
        save_config()


def save_config():
    try:
        with open(cfg.CONFIG_FILE, "w") as f:
            json.dump(conf, f)
    except Exception as e:
        print("❌ Config save failed:", e)


# ---------- Time ----------
def parse_utc_to_epoch(dt_str):
    # format: YYYY-MM-DD HH:MM:SS
    d, t = dt_str.strip().split(" ")
    y, mo, da = [int(x) for x in d.split("-")]
    hh, mm, ss = [int(x) for x in t.split(":")]
    return utime.mktime((y, mo, da, hh, mm, ss, 0, 0))


def now_epoch_utc():
    # Moving manual UTC clock for testing
    if bool(conf.get("use_manual_utc_now", False)):
        base = parse_utc_to_epoch(conf["manual_utc_now"])
        elapsed = utime.ticks_diff(utime.ticks_ms(), boot_ms) // 1000
        return base + elapsed
    return utime.time()


def in_booking_window(now_ep, checkin_ep, checkout_ep):
    return checkin_ep <= now_ep <= checkout_ep


# ---------- Decision ----------
def decide_status(sensor_status, now_ep):
    checkin_ep = parse_utc_to_epoch(conf["check_in_utc"])
    checkout_ep = parse_utc_to_epoch(conf["check_out_utc"])
    inside = in_booking_window(now_ep, checkin_ep, checkout_ep)

    if inside:
        return "Occupied" if sensor_status == "occupied" else "Sold Vacant"
    else:
        return "UnSold Occupied" if sensor_status == "occupied" else "Vacant"


def send_status_to_scheduler(status):
    msg = {"type": "set_status", "status": status}
    try:
        uart_sched.write(json.dumps(msg) + "\n")
        print("MASTER -> SCHED:", msg)
    except Exception as e:
        print("❌ Send to scheduler failed:", repr(e))


def apply_immediate(status, force_send=False):
    # force_send=True used for boot sync
    if force_send or state["current_decided_status"] != status:
        state["current_decided_status"] = status
        conf["last_decided_status"] = status
        save_config()
        send_status_to_scheduler(status)
    else:
        print("ℹ️ Status unchanged, not re-sent:", status)


def schedule_buffered(status, now_ep):
    buffer_min = int(conf.get("buffer_minutes", 0))
    if buffer_min < 0:
        buffer_min = 0

    if buffer_min == 0:
        print("⚡ buffer=0, applying immediately:", status)
        state["pending_status"] = None
        state["pending_apply_epoch"] = 0
        apply_immediate(status)
        return

    state["pending_status"] = status
    state["pending_apply_epoch"] = now_ep + buffer_min * 60
    print("⏳ Buffered target:", status, "apply_at=", state["pending_apply_epoch"], "now=", now_ep)


def recalc_and_act():
    now_ep = now_epoch_utc()
    sensor = state["last_sensor_status"]
    target = decide_status(sensor, now_ep)

    print("DECIDE: sensor=", sensor, "now=", now_ep, "=>", target)

    # Buffer only these
    if target in ("Sold Vacant", "Vacant"):
        schedule_buffered(target, now_ep)
    else:
        state["pending_status"] = None
        state["pending_apply_epoch"] = 0
        apply_immediate(target)


# ---------- RX handlers ----------
def handle_sensor_msg(msg):
    if msg.get("type") != "sensor_status":
        return

    s = str(msg.get("status", "vacant")).strip().lower()
    if s not in ("occupied", "vacant"):
        print("⚠️ invalid sensor status:", s)
        return

    with lock:
        state["last_sensor_status"] = s
        conf["last_sensor_status"] = s
        save_config()
        print("SENSOR -> MASTER:", s)
        recalc_and_act()


def handle_scheduler_msg(msg):
    if msg.get("type") != "scheduler_update":
        return

    st = msg.get("status")
    with lock:
        state["last_scheduler_status"] = st
        conf["last_scheduler_status"] = st
        save_config()

    print("SCHED -> MASTER:", msg)


def sensor_rx_loop():
    buf = b""
    while True:
        try:
            if uart_sensor.any():
                data = uart_sensor.read()
                if data:
                    buf += data

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        s = line.decode("utf-8").strip()
                    except Exception:
                        continue
                    if not s:
                        continue
                    try:
                        msg = json.loads(s)
                    except Exception:
                        print("⚠️ bad sensor json:", s)
                        continue
                    handle_sensor_msg(msg)
            utime.sleep_ms(30)
        except Exception as e:
            print("Sensor RX loop error:", repr(e))
            utime.sleep_ms(100)


def sched_rx_loop():
    buf = b""
    while True:
        try:
            if uart_sched.any():
                data = uart_sched.read()
                if data:
                    buf += data

                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        s = line.decode("utf-8").strip()
                    except Exception:
                        continue
                    if not s:
                        continue
                    try:
                        msg = json.loads(s)
                    except Exception:
                        print("⚠️ bad scheduler json:", s)
                        continue
                    handle_scheduler_msg(msg)
            utime.sleep_ms(30)
        except Exception as e:
            print("Sched RX loop error:", repr(e))
            utime.sleep_ms(100)


def pending_worker():
    while True:
        try:
            with lock:
                p = state["pending_status"]
                t = state["pending_apply_epoch"]
                if p:
                    now_ep = now_epoch_utc()
                    if now_ep >= t:
                        latest = decide_status(state["last_sensor_status"], now_ep)
                        print("⏰ Pending due. pending=", p, "latest=", latest, "now=", now_ep)
                        if latest == p:
                            apply_immediate(p)
                        else:
                            print("ℹ️ Pending canceled (condition changed)")
                        state["pending_status"] = None
                        state["pending_apply_epoch"] = 0
            utime.sleep(1)
        except Exception as e:
            print("Pending worker error:", repr(e))
            utime.sleep(1)


# ---------- START ----------
load_config()

state["last_sensor_status"] = str(conf.get("last_sensor_status", "vacant")).lower()
state["last_scheduler_status"] = conf.get("last_scheduler_status", None)

# IMPORTANT: do not trust persisted decided status for boot send decisions
# set None so first apply sends command.
state["current_decided_status"] = None

print("🚀 MASTER started")
print("UTC now epoch:", now_epoch_utc())
print("check_in_utc:", conf.get("check_in_utc"))
print("check_out_utc:", conf.get("check_out_utc"))
print("buffer_minutes:", conf.get("buffer_minutes"))
print("use_manual_utc_now:", conf.get("use_manual_utc_now", False))
print("manual_utc_now:", conf.get("manual_utc_now", None))

with lock:
    recalc_and_act()

    # Boot sync: force-send decided status once (important)
    if state["current_decided_status"] is not None:
        send_status_to_scheduler(state["current_decided_status"])

_thread.start_new_thread(sensor_rx_loop, ())
_thread.start_new_thread(sched_rx_loop, ())
_thread.start_new_thread(pending_worker, ())

while True:
    utime.sleep(10)
