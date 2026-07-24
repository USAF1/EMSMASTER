#!/usr/bin/env python3
"""
debugger.py — Innovatsii EMS Pico 1 Desktop Debugger
Phase 7

Connects to Master (ESP32-S3) over TCP port 8765.
Compatible with Python 3.8 through 3.14.

Usage:
    python debugger.py
    python debugger.py --ip 192.168.0.211
    python debugger.py --ip 192.168.200.234 --port 8765
"""

import socket
import threading
import json
import time
import argparse
import queue
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext
from tkinter import messagebox
from tkinter import simpledialog
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

DEFAULT_IP            = "192.168.0.211"
DEFAULT_PORT          = 8765
RECONNECT_INTERVAL_S  = 5
RELAY_NO_DATA_TIMEOUT = 10   # seconds before "waiting for Scheduler" shown

# ============================================================================
# COLOURS — dark theme
# ============================================================================

C_BG     = "#1e1e2e"
C_PANEL  = "#2a2a3e"
C_BORDER = "#44475a"
C_TEXT   = "#cdd6f4"
C_DIM    = "#6c7086"
C_GREEN  = "#a6e3a1"
C_RED    = "#f38ba8"
C_YELLOW = "#f9e2af"
C_BLUE   = "#89b4fa"
C_ORANGE = "#fab387"
C_PURPLE = "#cba6f7"
C_TEAL   = "#94e2d5"

STATUS_COLOURS = {
    "Occupied":        C_GREEN,
    "Vacant":          C_TEAL,
    "Sold Vacant":     C_YELLOW,
    "UnSold Occupied": C_ORANGE,
    "Unknown":         C_DIM,
}

# ============================================================================
# TCP CLIENT
# ============================================================================

class TcpClient:
    def __init__(self, ip, port, on_message, on_connect, on_disconnect):
        self.ip            = ip
        self.port          = port
        self.on_message    = on_message
        self.on_connect    = on_connect
        self.on_disconnect = on_disconnect
        self._sock         = None
        self._running      = False
        self._tx_lock      = threading.Lock()
        self._thread       = None

    def start(self):
        self._running = True
        self._thread  = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        self._close()

    def send(self, payload_dict):
        try:
            msg = json.dumps(payload_dict) + "\n"
            with self._tx_lock:
                if self._sock:
                    self._sock.sendall(msg.encode("utf-8"))
        except Exception as e:
            print("TCP send error:", e)
            self._close()

    def _close(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _run(self):
        buf = ""
        while self._running:
            try:
                self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self._sock.settimeout(5)
                self._sock.connect((self.ip, self.port))
                self._sock.settimeout(None)
                self.on_connect()
            except Exception as e:
                print("TCP connect failed:", e)
                self._close()
                time.sleep(RECONNECT_INTERVAL_S)
                continue

            buf = ""
            try:
                while self._running:
                    data = self._sock.recv(1024)
                    if not data:
                        break
                    buf += data.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                            self.on_message(msg)
                        except Exception:
                            pass
            except Exception as e:
                print("TCP read error:", e)
            finally:
                self._close()
                self.on_disconnect()

            if self._running:
                time.sleep(RECONNECT_INTERVAL_S)


# ============================================================================
# HELPERS
# ============================================================================

def make_frame(parent, title, row=0, col=0,
               rowspan=1, colspan=1, sticky="nsew",
               padx=8, pady=6):
    outer = tk.LabelFrame(
        parent,
        text="  {}  ".format(title),
        bg=C_BG, fg=C_BLUE,
        font=("Segoe UI", 10, "bold"),
        bd=1, relief=tk.GROOVE,
        labelanchor="nw"
    )
    outer.grid(row=row, column=col,
               rowspan=rowspan, columnspan=colspan,
               sticky=sticky, padx=padx, pady=pady)
    return outer


def make_button(parent, text, cmd, fg=C_TEXT,
                row=0, col=0, sticky="ew",
                padx=4, pady=4, width=None):
    kw = dict(
        text=text, command=cmd,
        bg=C_PANEL, fg=fg,
        activebackground=C_BORDER,
        activeforeground=fg,
        relief=tk.FLAT,
        font=("Segoe UI", 9),
        cursor="hand2",
        pady=5
    )
    if width:
        kw["width"] = width
    btn = tk.Button(parent, **kw)
    btn.grid(row=row, column=col,
             sticky=sticky, padx=padx, pady=pady)
    return btn


# ============================================================================
# MAIN APPLICATION
# ============================================================================

class DebuggerApp:

    def __init__(self, root, ip, port):
        self.root     = root
        self.ip       = ip
        self.port     = port
        self._q       = queue.Queue()
        self._sensors = {}          # str(idx) -> sensor dict
        self._hub_cfg = {}
        self._last_relay_time = None  # time.monotonic() of last scheduler_update

        root.title(
            "Innovatsii EMS — Pico 1 Debugger  |  {}:{}".format(ip, port))
        root.configure(bg=C_BG)
        root.geometry("1400x900")
        root.minsize(1100, 700)

        self._build_ui()
        self._tcp = TcpClient(
            ip, port,
            on_message    = self._on_message,
            on_connect    = self._on_connect,
            on_disconnect = self._on_disconnect
        )
        self._tcp.start()
        self._poll_queue()
        self._check_relay_timeout()

    # -----------------------------------------------------------------------
    # UI BUILD
    # -----------------------------------------------------------------------

    def _build_ui(self):
        top = tk.Frame(self.root, bg=C_PANEL, height=56)
        top.pack(fill=tk.X)
        top.pack_propagate(False)

        self._lbl_conn = tk.Label(
            top, text="⬤  Connecting...",
            fg=C_YELLOW, bg=C_PANEL,
            font=("Segoe UI", 11, "bold"))
        self._lbl_conn.pack(side=tk.LEFT, padx=16, pady=8)

        self._lbl_unit = tk.Label(
            top, text="Unit: —",
            fg=C_TEXT, bg=C_PANEL,
            font=("Segoe UI", 13, "bold"))
        self._lbl_unit.pack(side=tk.LEFT, padx=24)

        self._lbl_occ = tk.Label(
            top, text="Sensor: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 11))
        self._lbl_occ.pack(side=tk.LEFT, padx=12)

        self._lbl_inet = tk.Label(
            top, text="Internet: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_inet.pack(side=tk.LEFT, padx=12)

        self._lbl_pending = tk.Label(
            top, text="",
            fg=C_YELLOW, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_pending.pack(side=tk.LEFT, padx=12)

        self._lbl_ip = tk.Label(
            top, text="IP: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_ip.pack(side=tk.RIGHT, padx=16)

        self._lbl_hub = tk.Label(
            top, text="Hub: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_hub.pack(side=tk.RIGHT, padx=16)

        style = ttk.Style()
        style.theme_use("default")
        style.configure("TNotebook", background=C_BG, borderwidth=0)
        style.configure("TNotebook.Tab",
                        background=C_PANEL, foreground=C_TEXT,
                        padding=[14, 6], font=("Segoe UI", 10))
        style.map("TNotebook.Tab",
                  background=[("selected", C_BG)],
                  foreground=[("selected", C_BLUE)])
        style.configure("Treeview",
                        background=C_PANEL, foreground=C_TEXT,
                        fieldbackground=C_PANEL,
                        rowheight=26, font=("Segoe UI", 10))
        style.configure("Treeview.Heading",
                        background=C_BORDER, foreground=C_TEXT,
                        font=("Segoe UI", 10, "bold"))

        nb = ttk.Notebook(self.root)
        nb.pack(fill=tk.BOTH, expand=True)

        self._tab_dashboard = tk.Frame(nb, bg=C_BG)
        self._tab_sensors   = tk.Frame(nb, bg=C_BG)
        self._tab_relays    = tk.Frame(nb, bg=C_BG)
        self._tab_config    = tk.Frame(nb, bg=C_BG)
        self._tab_logs      = tk.Frame(nb, bg=C_BG)

        nb.add(self._tab_dashboard, text="  Dashboard  ")
        nb.add(self._tab_sensors,   text="  Sensors  ")
        nb.add(self._tab_relays,    text="  Relays  ")
        nb.add(self._tab_config,    text="  Configuration  ")
        nb.add(self._tab_logs,      text="  Logs  ")

        self._build_dashboard()
        self._build_sensors()
        self._build_relays()
        self._build_config()
        self._build_logs()

    # -----------------------------------------------------------------------
    # DASHBOARD TAB
    # -----------------------------------------------------------------------

    def _build_dashboard(self):
        p = self._tab_dashboard
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=1)
        p.columnconfigure(2, weight=1)
        p.rowconfigure(1, weight=1)

        uf = make_frame(p, "Unit Occupancy State", row=0, col=0)
        uf.columnconfigure(0, weight=1)

        self._lbl_unit_big = tk.Label(
            uf, text="—", fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 28, "bold"))
        self._lbl_unit_big.grid(row=0, column=0, pady=16, padx=20)

        self._lbl_sensor_occ = tk.Label(
            uf, text="Sensor: —", fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 11))
        self._lbl_sensor_occ.grid(row=1, column=0, pady=2)

        self._lbl_inet_mode = tk.Label(
            uf, text="Mode: waiting...",
            fg=C_YELLOW, bg=C_BG,
            font=("Segoe UI", 10))
        self._lbl_inet_mode.grid(row=2, column=0, pady=2)

        self._lbl_pending2 = tk.Label(
            uf, text="", fg=C_YELLOW, bg=C_BG,
            font=("Segoe UI", 10))
        self._lbl_pending2.grid(row=3, column=0, pady=2)

        qf = make_frame(p, "Quick Actions", row=0, col=1)
        qf.columnconfigure(0, weight=1)
        for i, (label, status) in enumerate([
            ("Force Occupied",        "Occupied"),
            ("Force Vacant",          "Vacant"),
            ("Force Sold Vacant",     "Sold Vacant"),
            ("Force UnSold Occupied", "UnSold Occupied"),
        ]):
            colour = STATUS_COLOURS.get(status, C_TEXT)
            make_button(qf, label,
                        lambda s=status: self._force_status(s),
                        fg=colour, row=i, col=0,
                        sticky="ew", padx=8, pady=3)
        make_button(qf, "Cancel Pending",
                    self._cancel_pending,
                    fg=C_YELLOW, row=4, col=0,
                    sticky="ew", padx=8, pady=6)

        hf = make_frame(p, "Sensor Hub Control", row=0, col=2)
        hf.columnconfigure(0, weight=1)
        make_button(hf, "Open Pairing (120s)",
                    lambda: self._start_pairing(120),
                    fg=C_GREEN, row=0, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Stop Pairing",
                    self._stop_pairing,
                    fg=C_RED, row=1, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Refresh Sensors",
                    self._get_sensor_config,
                    fg=C_BLUE, row=2, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Get Logs",
                    self._get_hub_logs,
                    fg=C_DIM, row=3, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Restart Hub",
                    self._restart_hub,
                    fg=C_YELLOW, row=4, col=0, sticky="ew", padx=8, pady=3)

        ef = make_frame(p, "Live Events",
                        row=1, col=0, colspan=3, sticky="nsew")
        ef.columnconfigure(0, weight=1)
        ef.rowconfigure(0, weight=1)

        self._event_log = scrolledtext.ScrolledText(
            ef, bg="#11111b", fg=C_TEXT,
            font=("Courier New", 9),
            state=tk.DISABLED, wrap=tk.WORD,
            height=14)
        self._event_log.grid(row=0, column=0,
                              sticky="nsew", padx=4, pady=4)
        for tag, colour in [
            ("door",     C_BLUE),
            ("presence", C_GREEN),
            ("alarm",    C_RED),
            ("health",   C_ORANGE),
            ("battery",  C_YELLOW),
            ("env",      C_TEAL),
            ("unit",     C_PURPLE),
            ("hub",      C_BLUE),
            ("inet",     C_ORANGE),
            ("dim",      C_DIM),
            ("info",     C_TEXT),
            ("sched",    C_TEAL),
        ]:
            self._event_log.tag_config(tag, foreground=colour)

    # -----------------------------------------------------------------------
    # SENSORS TAB
    # -----------------------------------------------------------------------

    def _build_sensors(self):
        p = self._tab_sensors
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        bf = tk.Frame(p, bg=C_BG)
        bf.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        make_button(bf, "Refresh Sensor List",
                    self._get_sensor_config,
                    fg=C_BLUE, row=0, col=0, padx=4)
        make_button(bf, "Open Pairing",
                    lambda: self._start_pairing(120),
                    fg=C_GREEN, row=0, col=1, padx=4)
        make_button(bf, "Stop Pairing",
                    self._stop_pairing,
                    fg=C_RED, row=0, col=2, padx=4)
        make_button(bf, "Remove Selected",
                    self._remove_sensor,
                    fg=C_RED, row=0, col=3, padx=4)

        cols = ("index", "name", "model", "role",
                "online", "state", "battery", "temp", "hum")
        self._sensor_tree = ttk.Treeview(
            p, columns=cols, show="headings", height=15)

        headers = [
            ("index",   "#",       50),
            ("name",    "Name",   160),
            ("model",   "Model",  120),
            ("role",    "Role",    90),
            ("online",  "Online",  70),
            ("state",   "State",   90),
            ("battery", "Battery", 70),
            ("temp",    "Temp °C", 80),
            ("hum",     "Hum %",   70),
        ]
        for col_id, heading, width in headers:
            self._sensor_tree.heading(col_id, text=heading)
            self._sensor_tree.column(col_id, width=width, anchor="center")

        self._sensor_tree.grid(
            row=1, column=0, sticky="nsew", padx=8, pady=4)
        self._sensor_tree.bind("<Double-1>", self._on_sensor_double_click)

        tk.Label(
            p,
            text="Double-click a sensor to rename it.  Select a row then click Remove to delete.",
            fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 9)
        ).grid(row=2, column=0, sticky="w", padx=12, pady=2)

    # -----------------------------------------------------------------------
    # RELAYS TAB
    # -----------------------------------------------------------------------

    def _build_relays(self):
        p = self._tab_relays
        RELAY_KEYS = ["R4","R5","R16","R17","R18",
                      "R21","R35","R36","R37","R38"]

        for i in range(len(RELAY_KEYS)):
            p.columnconfigure(i, weight=1)

        self._lbl_relay_header = tk.Label(
            p,
            text="Waiting for Scheduler data...",
            fg=C_YELLOW, bg=C_BG,
            font=("Segoe UI", 9))
        self._lbl_relay_header.grid(
            row=0, column=0, columnspan=len(RELAY_KEYS),
            sticky="w", padx=12, pady=6)

        self._relay_labels = {}
        for i, key in enumerate(RELAY_KEYS):
            frame = tk.Frame(p, bg=C_PANEL, bd=1, relief=tk.GROOVE)
            frame.grid(row=1, column=i, sticky="nsew", padx=6, pady=8)
            tk.Label(
                frame, text=key,
                fg=C_BLUE, bg=C_PANEL,
                font=("Segoe UI", 11, "bold")
            ).pack(pady=(10, 2))
            lbl = tk.Label(
                frame, text="—",
                fg=C_DIM, bg=C_PANEL,
                font=("Segoe UI", 14, "bold"))
            lbl.pack(pady=(2, 10))
            self._relay_labels[key] = lbl

        self._lbl_sched_status = tk.Label(
            p,
            text="Scheduler status: —",
            fg=C_TEXT, bg=C_BG,
            font=("Segoe UI", 11))
        self._lbl_sched_status.grid(
            row=2, column=0,
            columnspan=len(RELAY_KEYS),
            sticky="w", padx=12, pady=8)

    # -----------------------------------------------------------------------
    # CONFIGURATION TAB
    # -----------------------------------------------------------------------

    def _build_config(self):
        p = self._tab_config
        p.columnconfigure(0, weight=1)
        p.columnconfigure(1, weight=1)

        # Unit & booking config
        uf = make_frame(p, "Unit & Booking Configuration", row=0, col=0)
        uf.columnconfigure(1, weight=1)

        unit_fields = [
            ("tenant_id",      "Tenant ID"),
            ("unit_id",        "Unit ID"),
            ("check_in_utc",   "Check-in UTC  (YYYY-MM-DD HH:MM:SS)"),
            ("check_out_utc",  "Check-out UTC (YYYY-MM-DD HH:MM:SS)"),
            ("buffer_minutes", "Buffer minutes"),
        ]
        self._cfg_entries = {}
        for r, (key, label) in enumerate(unit_fields):
            tk.Label(
                uf, text=label,
                fg=C_TEXT, bg=C_BG,
                font=("Segoe UI", 10)
            ).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            e = tk.Entry(
                uf, bg=C_PANEL, fg=C_TEXT,
                insertbackground=C_TEXT,
                font=("Segoe UI", 10), width=32)
            e.grid(row=r, column=1, sticky="ew", padx=8, pady=4)
            self._cfg_entries[key] = e

        make_button(uf, "Save Unit Config",
                    self._save_unit_config,
                    fg=C_GREEN,
                    row=len(unit_fields), col=0,
                    sticky="ew", padx=8, pady=8)

        # Hub config
        hf = make_frame(p, "Sensor Hub Configuration", row=0, col=1)
        hf.columnconfigure(1, weight=1)

        hub_fields = [
            ("pairing_duration_sec",      "Pairing duration (sec)"),
            ("watchdog_interval_min",     "Watchdog interval (min)"),
            ("watchdog_ping_timeout_sec", "Ping timeout (sec)"),
            ("door_alarm_threshold_min",  "Door alarm threshold (min)"),
            ("heartbeat_interval_min",    "Heartbeat interval (min)"),
        ]
        self._hub_entries = {}
        for r, (key, label) in enumerate(hub_fields):
            tk.Label(
                hf, text=label,
                fg=C_TEXT, bg=C_BG,
                font=("Segoe UI", 10)
            ).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            e = tk.Entry(
                hf, bg=C_PANEL, fg=C_TEXT,
                insertbackground=C_TEXT,
                font=("Segoe UI", 10), width=20)
            e.grid(row=r, column=1, sticky="ew", padx=8, pady=4)
            self._hub_entries[key] = e

        self._wd_enable_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            hf, text="Watchdog enabled",
            variable=self._wd_enable_var,
            bg=C_BG, fg=C_TEXT,
            activebackground=C_BG,
            selectcolor=C_PANEL,
            font=("Segoe UI", 10)
        ).grid(row=len(hub_fields), column=0,
               columnspan=2, sticky="w", padx=8, pady=4)

        make_button(hf, "Save & Push to Hub",
                    self._save_hub_config,
                    fg=C_GREEN,
                    row=len(hub_fields) + 1, col=0,
                    sticky="ew", padx=8, pady=8)

        # WiFi
        wf = make_frame(p, "WiFi & Credentials", row=1, col=0)
        wf.columnconfigure(1, weight=1)

        for r, (key, label, show) in enumerate([
            ("wifi_ssid",     "WiFi SSID",     ""),
            ("wifi_password", "WiFi Password", "*"),
        ]):
            tk.Label(
                wf, text=label,
                fg=C_TEXT, bg=C_BG,
                font=("Segoe UI", 10)
            ).grid(row=r, column=0, sticky="w", padx=8, pady=4)
            e = tk.Entry(
                wf, bg=C_PANEL, fg=C_TEXT,
                insertbackground=C_TEXT,
                font=("Segoe UI", 10), width=28,
                show=show)
            e.grid(row=r, column=1, sticky="ew", padx=8, pady=4)
            self._cfg_entries[key] = e

        make_button(wf, "Save WiFi (reboot to apply)",
                    self._save_wifi_config,
                    fg=C_YELLOW, row=2, col=0,
                    sticky="ew", padx=8, pady=8)

        tk.Label(
            wf,
            text="IP is assigned by router DHCP reservation.",
            fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 9), justify=tk.LEFT
        ).grid(row=3, column=0, columnspan=2,
               sticky="w", padx=8, pady=4)

        # System commands
        df = make_frame(p, "System Commands", row=1, col=1)
        df.columnconfigure(0, weight=1)
        make_button(df, "Restart Master",
                    self._restart_master,
                    fg=C_RED, row=0, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Restart Sensor Hub",
                    self._restart_hub,
                    fg=C_ORANGE, row=1, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Hub Factory Reset",
                    self._hub_factory_reset,
                    fg=C_RED, row=2, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Restart Scheduler",
                    self._restart_scheduler,
                    fg=C_ORANGE, row=3, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Scheduler Factory Reset",
                    self._scheduler_factory_reset,
                    fg=C_RED, row=4, col=0, sticky="ew", padx=8, pady=4)

    # -----------------------------------------------------------------------
    # LOGS TAB
    # -----------------------------------------------------------------------

    def _build_logs(self):
        p = self._tab_logs
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)

        bf = tk.Frame(p, bg=C_BG)
        bf.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        make_button(bf, "Request Hub Logs",
                    self._get_hub_logs,
                    fg=C_BLUE, row=0, col=0, padx=4)
        make_button(bf, "Clear",
                    self._clear_logs,
                    fg=C_DIM, row=0, col=1, padx=4)

        self._log_box = scrolledtext.ScrolledText(
            p, bg="#11111b", fg=C_TEXT,
            font=("Courier New", 9),
            state=tk.DISABLED, wrap=tk.WORD)
        self._log_box.grid(row=1, column=0,
                           sticky="nsew", padx=8, pady=4)

        for tag, colour in [
            ("error",   C_RED),
            ("warn",    C_YELLOW),
            ("info",    C_TEXT),
            ("rx",      C_TEAL),
            ("tx",      C_PURPLE),
            ("hub_log", C_ORANGE),
            ("dim",     C_DIM),
        ]:
            self._log_box.tag_config(tag, foreground=colour)

    # -----------------------------------------------------------------------
    # TCP CALLBACKS
    # -----------------------------------------------------------------------

    def _on_connect(self):
        self._q.put(("connect", None))

    def _on_disconnect(self):
        self._q.put(("disconnect", None))

    def _on_message(self, msg):
        self._q.put(("msg", msg))

    def _poll_queue(self):
        try:
            while True:
                kind, data = self._q.get_nowait()
                if   kind == "connect":    self._ui_on_connect()
                elif kind == "disconnect": self._ui_on_disconnect()
                elif kind == "msg":        self._ui_on_message(data)
        except queue.Empty:
            pass
        self.root.after(50, self._poll_queue)

    def _check_relay_timeout(self):
        """Show warning on relay tab if no Scheduler data received yet."""
        if self._last_relay_time is None:
            self._lbl_relay_header.config(
                text="Waiting for Scheduler data...", fg=C_YELLOW)
        else:
            elapsed = time.monotonic() - self._last_relay_time
            if elapsed > RELAY_NO_DATA_TIMEOUT:
                self._lbl_relay_header.config(
                    text="No data — Scheduler may be offline", fg=C_RED)
            else:
                self._lbl_relay_header.config(
                    text="Live relay states — updated from Scheduler every 5s",
                    fg=C_DIM)
        self.root.after(2000, self._check_relay_timeout)

    # -----------------------------------------------------------------------
    # UI EVENT HANDLERS
    # -----------------------------------------------------------------------

    def _ui_on_connect(self):
        self._lbl_conn.config(text="⬤  Connected", fg=C_GREEN)
        self._log("Connected to {}:{}".format(self.ip, self.port), "info")
        # Request state snapshot only — no automatic get_sensor_config
        self._tcp.send({"type": "get_state"})

    def _ui_on_disconnect(self):
        self._lbl_conn.config(
            text="⬤  Disconnected — reconnecting...", fg=C_RED)
        self._log(
            "Disconnected. Reconnecting in {}s...".format(
                RECONNECT_INTERVAL_S), "warn")

    def _ui_on_message(self, msg):
        t = msg.get("type", "")
        self._log_raw(msg)

        if t == "state_snapshot":
            self._apply_snapshot(msg)

        elif t == "internet_status":
            up = msg.get("status", "") == "up"
            self._update_internet_indicator(up)
            self._log_event(
                "INTERNET {}".format("RESTORED" if up else "LOST"), "inet")

        elif t == "unit_occupancy":
            s = msg.get("state", "VACANT")
            self._log_event("UNIT OCCUPANCY → {}".format(s), "unit")

        elif t == "hub_boot":
            count = msg.get("sensor_count", 0)
            self._log_event(
                "HUB BOOTED — {} sensors known".format(count), "hub")
            self._lbl_hub.config(text="Hub: BOOTING", fg=C_YELLOW)

        elif t == "hub_ready":
            on  = msg.get("online_count",  0)
            off = msg.get("offline_count", 0)
            self._log_event(
                "HUB READY — online={} offline={}".format(on, off), "hub")
            self._lbl_hub.config(
                text="Hub: READY ({} online)".format(on), fg=C_GREEN)
            # Auto-refresh sensor list when hub is ready
            self._tcp.send({"type": "get_sensor_config"})

        elif t == "sensor_presence":
            sensor = msg.get("sensor", "")
            sv     = msg.get("state", "")
            self._log_event(
                "PRESENCE  {}  {}".format(sensor, sv), "presence")
            self._update_sensor_state_col(sensor, sv)

        elif t == "environment":
            tc  = msg.get("temp_c_x100",  0) / 100.0
            hum = msg.get("hum_pct_x100", 0) / 100.0
            self._log_event(
                "ENV  {}  {:.1f}°C  {:.1f}%".format(
                    msg.get("sensor", ""), tc, hum), "env")
            self._update_sensor_env(msg.get("sensor", ""), tc, hum)

        elif t == "door":
            sensor = msg.get("sensor", "")
            sv     = msg.get("state", "")
            self._log_event(
                "DOOR  {}  {}".format(sensor, sv), "door")
            self._update_sensor_state_col(sensor, sv)

        elif t == "door_alarm":
            self._log_event(
                "⚠ DOOR ALARM  {}  {}  {}s".format(
                    msg.get("sensor", ""),
                    msg.get("state", ""),
                    msg.get("duration_sec", 0)), "alarm")

        elif t == "sensor_health":
            online = msg.get("state", "") == "ONLINE"
            self._log_event(
                "HEALTH  {}  {}".format(
                    msg.get("sensor", ""), msg.get("state", "")), "health")
            self._update_sensor_health(msg.get("sensor", ""), online)

        elif t == "battery":
            self._log_event(
                "BATTERY  {}  {}%".format(
                    msg.get("sensor", ""), msg.get("battery_pct", 0)), "battery")
            self._update_sensor_battery(
                msg.get("sensor", ""), msg.get("battery_pct", 0))

        elif t == "heartbeat":
            self._log_event(
                "HEARTBEAT  unit={}".format(msg.get("unit_state", "")), "dim")
            for s in msg.get("sensors", []):
                self._refresh_sensor_row(s)

        elif t == "config_response":
            sensors = msg.get("sensors", [])
            self._apply_sensor_list(sensors)
            self._log_event(
                "SENSOR LIST updated — {} sensors".format(len(sensors)), "hub")

        elif t == "scheduler_update":
            self._last_relay_time = time.monotonic()
            self._apply_relay_snapshot(
                msg.get("relays", {}),
                msg.get("status", ""))

        elif t == "ack":
            cmd = msg.get("command", "?")
            st  = msg.get("status", "?")
            self._log_event("ACK  {}  {}".format(cmd, st), "dim")
            # Log config saves in event feed
            if cmd in ("set_unit_config", "set_hub_config", "set_wifi_config"):
                self._log_event(
                    "Config saved — Master acknowledged ({})".format(cmd), "info")

        elif t == "log_response":
            self._log_hub(msg.get("line", ""))

    # -----------------------------------------------------------------------
    # STATE UPDATES
    # -----------------------------------------------------------------------

    def _apply_snapshot(self, snap):
        unit      = snap.get("unit_state",       "Unknown")
        sensor    = snap.get("sensor_occupancy", "vacant")
        pending   = snap.get("pending_status")
        ip        = snap.get("wifi_ip",          "")
        inet_up   = snap.get("internet_up",      False)
        sched     = snap.get("scheduler_status", "")
        hub_cfg   = snap.get("sensor_hub_config", {})
        hub_state = snap.get("hub_state",        "UNKNOWN")

        colour = STATUS_COLOURS.get(unit, C_DIM)
        self._lbl_unit_big.config(text=unit, fg=colour)
        self._lbl_unit.config(text="Unit: {}".format(unit), fg=colour)

        occ_colour = C_GREEN if sensor == "occupied" else C_DIM
        self._lbl_sensor_occ.config(
            text="Sensor: {}".format(sensor.upper()), fg=occ_colour)
        self._lbl_occ.config(
            text="Sensor: {}".format(sensor.upper()), fg=occ_colour)

        self._update_internet_indicator(inet_up)

        # Hub label from snapshot — solves "Hub: —" after connect
        if hub_state == "READY":
            self._lbl_hub.config(text="Hub: READY", fg=C_GREEN)
        elif hub_state == "BOOTING":
            self._lbl_hub.config(text="Hub: BOOTING", fg=C_YELLOW)
        else:
            self._lbl_hub.config(text="Hub: UNKNOWN", fg=C_DIM)

        if pending:
            txt = "⏳ Pending: {}".format(pending)
            self._lbl_pending.config(text=txt)
            self._lbl_pending2.config(text=txt)
        else:
            self._lbl_pending.config(text="")
            self._lbl_pending2.config(text="")

        self._lbl_ip.config(
            text="IP: {}".format(ip) if ip else "IP: —")

        if sched:
            self._apply_relay_snapshot({}, sched)

        # Populate config tab entries
        for key, entry in self._cfg_entries.items():
            if key == "wifi_password":
                continue   # never populate password field from snapshot
            val = snap.get(key, "")
            if val is None:
                val = ""
            entry.delete(0, tk.END)
            entry.insert(0, str(val))

        for key, entry in self._hub_entries.items():
            val = hub_cfg.get(key, "")
            if val is None:
                val = ""
            entry.delete(0, tk.END)
            entry.insert(0, str(val))

        if "watchdog_enable" in hub_cfg:
            self._wd_enable_var.set(bool(hub_cfg["watchdog_enable"]))

        self._log_event("State snapshot received — unit={}".format(unit), "dim")

    def _update_internet_indicator(self, is_up):
        if is_up:
            self._lbl_inet.config(text="Internet: ✓ UP", fg=C_GREEN)
            self._lbl_inet_mode.config(
                text="Mode: 4-state (full booking logic)", fg=C_GREEN)
        else:
            self._lbl_inet.config(text="Internet: ✗ DOWN", fg=C_RED)
            self._lbl_inet_mode.config(
                text="Mode: 2-state fallback (Occupied/Vacant only)",
                fg=C_YELLOW)

    def _apply_sensor_list(self, sensors):
        self._sensors = {}
        for row in self._sensor_tree.get_children():
            self._sensor_tree.delete(row)
        for s in sensors:
            if not isinstance(s, dict):
                continue
            idx    = s.get("index", "?")
            name   = s.get("name",  "?")
            model  = s.get("model", "?")
            role   = s.get("role",  "?")
            online = "✓" if s.get("online", False) else "✗"
            batt   = "{}%".format(s.get("battery", "?"))
            # State column: door sensors show OPEN/CLOSED, presence show YES/NO
            if role == "DOOR":
                contact = s.get("contact", "—")
                state_v = contact if contact else "—"
            else:
                pres = s.get("presence", None)
                state_v = ("YES" if pres else "NO") if pres is not None else "—"
            self._sensor_tree.insert(
                "", tk.END,
                iid=str(idx),
                values=(idx, name, model, role,
                        online, state_v, batt, "—", "—"))
            self._sensors[str(idx)] = s
            self._sensors[name]     = s

    def _refresh_sensor_row(self, s):
        """Update a single sensor row from heartbeat data."""
        name = s.get("name", "")
        # Find by name
        for iid in self._sensor_tree.get_children():
            vals = list(self._sensor_tree.item(iid, "values"))
            if vals[1] == name:
                vals[4] = "✓" if s.get("online", False) else "✗"
                batt = s.get("battery", None)
                if batt is not None:
                    vals[6] = "{}%".format(batt)
                # contact / presence
                contact = s.get("contact", None)
                if contact is not None:
                    vals[5] = contact
                else:
                    pres = s.get("presence", None)
                    if pres is not None:
                        vals[5] = "YES" if pres else "NO"
                self._sensor_tree.item(iid, values=vals)
                break

    def _update_sensor_state_col(self, name, state_val):
        """Update the State column for a sensor by name."""
        for iid in self._sensor_tree.get_children():
            vals = list(self._sensor_tree.item(iid, "values"))
            if vals[1] == name:
                vals[5] = state_val
                self._sensor_tree.item(iid, values=vals)
                break

    def _update_sensor_env(self, name, temp_c, hum_pct):
        for iid in self._sensor_tree.get_children():
            vals = list(self._sensor_tree.item(iid, "values"))
            if vals[1] == name:
                vals[7] = "{:.1f}".format(temp_c)
                vals[8] = "{:.1f}".format(hum_pct)
                self._sensor_tree.item(iid, values=vals)
                break

    def _update_sensor_health(self, name, is_online):
        for iid in self._sensor_tree.get_children():
            vals = list(self._sensor_tree.item(iid, "values"))
            if vals[1] == name:
                vals[4] = "✓" if is_online else "✗"
                self._sensor_tree.item(iid, values=vals)
                break

    def _update_sensor_battery(self, name, pct):
        for iid in self._sensor_tree.get_children():
            vals = list(self._sensor_tree.item(iid, "values"))
            if vals[1] == name:
                vals[6] = "{}%".format(pct)
                self._sensor_tree.item(iid, values=vals)
                break

    def _apply_relay_snapshot(self, relays, sched_status):
        for key, lbl in self._relay_labels.items():
            val = relays.get(key, -1)
            if val == -1:
                lbl.config(text="N/A", fg=C_DIM)
            elif val == 1:
                lbl.config(text="ON",  fg=C_GREEN)
            else:
                lbl.config(text="OFF", fg=C_RED)
        if sched_status:
            colour = STATUS_COLOURS.get(sched_status, C_TEXT)
            self._lbl_sched_status.config(
                text="Scheduler status: {}".format(sched_status),
                fg=colour)

    # -----------------------------------------------------------------------
    # LOGGING
    # -----------------------------------------------------------------------

    def _ts(self):
        return datetime.now().strftime("%H:%M:%S")

    def _log_event(self, text, tag="info"):
        box = self._event_log
        box.config(state=tk.NORMAL)
        box.insert(tk.END, "[{}]  {}\n".format(self._ts(), text), tag)
        box.see(tk.END)
        box.config(state=tk.DISABLED)

    def _log(self, text, tag="info"):
        box = self._log_box
        box.config(state=tk.NORMAL)
        box.insert(tk.END, "[{}]  {}\n".format(self._ts(), text), tag)
        box.see(tk.END)
        box.config(state=tk.DISABLED)

    def _log_raw(self, msg):
        t = msg.get("type", "")
        if t in ("state_snapshot", "pong", "scheduler_update"):
            return   # too noisy — skip these in raw log
        self._log("← RX  {}".format(json.dumps(msg)[:140]), "rx")

    def _log_hub(self, line):
        box = self._log_box
        box.config(state=tk.NORMAL)
        box.insert(tk.END, "[HUB]  {}\n".format(line), "hub_log")
        box.see(tk.END)
        box.config(state=tk.DISABLED)

    def _clear_logs(self):
        self._log_box.config(state=tk.NORMAL)
        self._log_box.delete("1.0", tk.END)
        self._log_box.config(state=tk.DISABLED)

    # -----------------------------------------------------------------------
    # USER ACTIONS
    # -----------------------------------------------------------------------

    def _force_status(self, status):
        if messagebox.askyesno(
            "Force Status",
            "Force unit state to:\n\n  {}\n\n"
            "This bypasses all booking and sensor logic.".format(status)
        ):
            self._tcp.send({"type": "force_status", "status": status})
            self._log("TX → force_status: {}".format(status), "tx")

    def _cancel_pending(self):
        self._tcp.send({"type": "cancel_pending"})
        self._log("TX → cancel_pending", "tx")

    def _start_pairing(self, dur=120):
        self._tcp.send({"type": "start_pairing", "duration_sec": dur})
        self._log("TX → start_pairing {}s".format(dur), "tx")

    def _stop_pairing(self):
        self._tcp.send({"type": "stop_pairing"})
        self._log("TX → stop_pairing", "tx")

    def _get_sensor_config(self):
        self._tcp.send({"type": "get_sensor_config"})
        self._log("TX → get_sensor_config", "tx")

    def _get_hub_logs(self):
        self._tcp.send({"type": "get_hub_logs"})
        self._log("TX → get_hub_logs", "tx")

    def _restart_hub(self):
        if messagebox.askyesno(
            "Restart Hub",
            "Restart the Sensor Hub?\nSensors will reconnect automatically."
        ):
            self._tcp.send({"type": "hub_restart"})
            self._log("TX → hub_restart", "tx")

    def _hub_factory_reset(self):
        if messagebox.askyesno(
            "Factory Reset Sensor Hub",
            "⚠  Factory reset the Sensor Hub?\n\n"
            "Sensor names will be erased.\n"
            "Sensors re-pair automatically — no button press needed.",
            icon="warning"
        ):
            self._tcp.send({"type": "hub_factory_reset"})
            self._log("TX → hub_factory_reset", "tx")

    def _restart_master(self):
        if messagebox.askyesno(
            "Restart Master",
            "Restart the Master ESP32-S3?"
        ):
            self._tcp.send({"type": "master_restart"})
            self._log("TX → master_restart", "tx")

    def _restart_scheduler(self):
        if messagebox.askyesno(
            "Restart Scheduler",
            "Restart the Scheduler ESP32-S3?\n"
            "Relay schedules will reload from saved config."
        ):
            self._tcp.send({"type": "scheduler_restart"})
            self._log("TX → scheduler_restart", "tx")

    def _scheduler_factory_reset(self):
        if messagebox.askyesno(
            "Scheduler Factory Reset",
            "⚠  Factory reset the Scheduler?\n\n"
            "This will ERASE all relay schedules and reboot the Scheduler.\n"
            "Default schedules will be applied on next boot.",
            icon="warning"
        ):
            self._tcp.send({"type": "scheduler_factory_reset"})
            self._log("TX → scheduler_factory_reset", "tx")

    def _save_unit_config(self):
        payload = {"type": "set_unit_config"}
        for key, entry in self._cfg_entries.items():
            if key in ("wifi_ssid", "wifi_password"):
                continue
            val = entry.get().strip()
            if key == "buffer_minutes":
                try:
                    val = int(val)
                except Exception:
                    val = 15
            if val != "":
                payload[key] = val
        self._tcp.send(payload)
        self._log("TX → set_unit_config", "tx")

    def _save_hub_config(self):
        payload = {
            "type":            "set_hub_config",
            "watchdog_enable": self._wd_enable_var.get()
        }
        for key in ("pairing_duration_sec", "watchdog_interval_min",
                    "watchdog_ping_timeout_sec", "door_alarm_threshold_min",
                    "heartbeat_interval_min"):
            entry = self._hub_entries.get(key)
            if entry:
                try:
                    payload[key] = int(entry.get().strip())
                except Exception:
                    pass
        self._tcp.send(payload)
        self._log("TX → set_hub_config", "tx")

    def _save_wifi_config(self):
        ssid = self._cfg_entries.get("wifi_ssid")
        pwd  = self._cfg_entries.get("wifi_password")
        if ssid and pwd:
            self._tcp.send({
                "type":          "set_wifi_config",
                "wifi_ssid":     ssid.get().strip(),
                "wifi_password": pwd.get().strip(),
            })
            self._log("TX → set_wifi_config (credentials hidden)", "tx")

    def _remove_sensor(self):
        sel = self._sensor_tree.selection()
        if not sel:
            messagebox.showinfo("Remove Sensor",
                                "Select a sensor row first.")
            return
        iid  = sel[0]
        vals = self._sensor_tree.item(iid, "values")
        idx  = vals[0]
        name = vals[1]
        if messagebox.askyesno(
            "Remove Sensor",
            "Remove sensor:\n\n  [{}] {}\n\n"
            "The sensor will be removed from the registry.\n"
            "It can re-pair next time pairing is opened.".format(idx, name)
        ):
            self._tcp.send({
                "type":         "remove_sensor",
                "sensor_index": int(idx)
            })
            self._sensor_tree.delete(iid)
            self._log(
                "TX → remove_sensor index={} ({})".format(idx, name), "tx")

    def _on_sensor_double_click(self, event):
        sel = self._sensor_tree.selection()
        if not sel:
            return
        iid  = sel[0]
        vals = self._sensor_tree.item(iid, "values")
        idx  = vals[0]
        name = vals[1]
        new_name = simpledialog.askstring(
            "Rename Sensor",
            "Current name: {}\n\nNew name:".format(name),
            initialvalue=name,
            parent=self.root
        )
        if new_name and new_name.strip() and new_name.strip() != name:
            new_name = new_name.strip()
            self._tcp.send({
                "type":         "set_sensor_name",
                "sensor_index": int(idx),
                "name":         new_name
            })
            # Update treeview locally immediately — no round-trip wait
            new_vals    = list(vals)
            new_vals[1] = new_name
            self._sensor_tree.item(iid, values=new_vals)
            self._log(
                "TX → set_sensor_name {} → '{}'".format(idx, new_name), "tx")


# ============================================================================
# ENTRY POINT
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Innovatsii EMS Pico 1 Desktop Debugger")
    parser.add_argument(
        "--ip", default=DEFAULT_IP,
        help="Master IP address (default: {})".format(DEFAULT_IP))
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT,
        help="TCP port (default: {})".format(DEFAULT_PORT))
    args = parser.parse_args()

    root = tk.Tk()
    app  = DebuggerApp(root, args.ip, args.port)
    root.mainloop()


if __name__ == "__main__":
    main()