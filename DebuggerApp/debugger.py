#!/usr/bin/env python3
"""
debugger.py — Innovatsii EMS Pico 1 Desktop Debugger
Version: 0.2.5

Connects to Master (ESP32-S3) over TCP port 8765.
Compatible with Python 3.8 through 3.14.

Changes in v0.2.5:
  - Force command dialog — asks for duration (1-24 hours) and optional reason
  - Force indicator panel on Dashboard — shows status, expiry, reason
  - Cancel Force button in force panel
  - force_update message handler
  - hub_boot handler removed (no longer sent by Hub)
  - hub_ready updated — firmware_version, needs_pairing alert
  - sensor_joined, sensor_status, sensor_list_complete handlers added
  - new_sensor_joined, pairing_complete handlers added
  - boot_phase handler — progress indicator in header
  - boot_fault handler — shows fault reason, marks hub FAULT
  - notification handler — info/warning/error alerts in event log
  - Hub state FAULT added
  - Firmware version shown in header
  - Boot phase shown in header
  - Config tab: presence_fading_time_sec, door_sensor_max_silence_hours added
  - _apply_snapshot: restores force indicator on reconnect
  - Start Watchdog button added to Hub Control panel

Usage:
    python debugger.py
    python debugger.py --ip 192.168.0.211
    python debugger.py --ip 192.168.0.211 --port 8765
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
RELAY_NO_DATA_TIMEOUT = 10

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
            try:   self._sock.close()
            except Exception: pass
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
# FORCE DURATION DIALOG
# ============================================================================

class ForceDurationDialog(simpledialog.Dialog):
    """
    Modal dialog that asks for force duration (hours) and optional reason.
    Minimum 1 hour. Maximum 24 hours.
    """

    def __init__(self, parent, status):
        self.status        = status
        self.result_hours  = None
        self.result_reason = None
        super().__init__(parent,
                         title="Force: {}".format(status))

    def body(self, master):
        master.configure(bg=C_BG)

        tk.Label(
            master,
            text="Force unit state to:  {}".format(self.status),
            fg=STATUS_COLOURS.get(self.status, C_TEXT),
            bg=C_BG,
            font=("Segoe UI", 11, "bold")
        ).grid(row=0, column=0, columnspan=2,
               sticky="w", padx=12, pady=(12, 6))

        tk.Label(
            master,
            text="Duration (hours, 1–24):",
            fg=C_TEXT, bg=C_BG,
            font=("Segoe UI", 10)
        ).grid(row=1, column=0, sticky="w", padx=12, pady=4)

        self._hours_var = tk.StringVar(value="2")
        hours_entry = tk.Entry(
            master,
            textvariable=self._hours_var,
            bg=C_PANEL, fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Segoe UI", 10), width=8
        )
        hours_entry.grid(row=1, column=1, sticky="w", padx=12, pady=4)

        tk.Label(
            master,
            text="Reason (optional):",
            fg=C_TEXT, bg=C_BG,
            font=("Segoe UI", 10)
        ).grid(row=2, column=0, sticky="w", padx=12, pady=4)

        self._reason_var = tk.StringVar(value="")
        reason_entry = tk.Entry(
            master,
            textvariable=self._reason_var,
            bg=C_PANEL, fg=C_TEXT,
            insertbackground=C_TEXT,
            font=("Segoe UI", 10), width=28
        )
        reason_entry.grid(row=2, column=1, sticky="w", padx=12, pady=4)

        tk.Label(
            master,
            text="Overrides all sensor and booking logic\n"
                 "for the specified duration.",
            fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 9), justify=tk.LEFT
        ).grid(row=3, column=0, columnspan=2,
               sticky="w", padx=12, pady=(4, 12))

        return hours_entry

    def apply(self):
        try:
            h = int(self._hours_var.get().strip())
            if h < 1:  h = 1
            if h > 24: h = 24
        except Exception:
            h = 1
        self.result_hours  = h
        self.result_reason = self._reason_var.get().strip() or "Manual force"


# ============================================================================
# MAIN APPLICATION
# ============================================================================

class DebuggerApp:

    def __init__(self, root, ip, port):
        self.root     = root
        self.ip       = ip
        self.port     = port
        self._q       = queue.Queue()
        self._sensors = {}
        self._hub_cfg = {}
        self._last_relay_time     = None
        self._last_snapshot_time  = 0.0
        self._last_snapshot_unit  = ""

        # Force state tracked locally for UI updates
        self._force_active  = False
        self._force_status  = ""
        self._force_expires = ""
        self._force_reason  = ""

        root.title(
            "Innovatsii EMS — Pico 1 Debugger  v0.2.5  |  {}:{}".format(
                ip, port))
        root.configure(bg=C_BG)
        root.geometry("1450x980")
        root.minsize(1100, 750)

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
        # ── Top bar ──────────────────────────────────────────────────────────
        top = tk.Frame(self.root, bg=C_PANEL, height=62)
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
        self._lbl_inet.pack(side=tk.LEFT, padx=10)

        self._lbl_ntp = tk.Label(
            top, text="NTP: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_ntp.pack(side=tk.LEFT, padx=8)

        self._lbl_utc = tk.Label(
            top, text="UTC: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_utc.pack(side=tk.LEFT, padx=8)

        self._lbl_pending = tk.Label(
            top, text="",
            fg=C_YELLOW, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_pending.pack(side=tk.LEFT, padx=10)

        # RIGHT side of top bar
        self._lbl_ip = tk.Label(
            top, text="IP: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_ip.pack(side=tk.RIGHT, padx=16)

        self._lbl_hub = tk.Label(
            top, text="Hub: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 10))
        self._lbl_hub.pack(side=tk.RIGHT, padx=12)

        self._lbl_fw = tk.Label(
            top, text="fw: —",
            fg=C_DIM, bg=C_PANEL,
            font=("Segoe UI", 9))
        self._lbl_fw.pack(side=tk.RIGHT, padx=8)

        self._lbl_boot_phase = tk.Label(
            top, text="",
            fg=C_YELLOW, bg=C_PANEL,
            font=("Segoe UI", 9))
        self._lbl_boot_phase.pack(side=tk.RIGHT, padx=8)

        btn_refresh = tk.Button(
            top, text="⟳  Refresh All",
            command=self._refresh_all,
            bg=C_BORDER, fg=C_BLUE,
            activebackground=C_PANEL,
            activeforeground=C_BLUE,
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            padx=10, pady=4
        )
        btn_refresh.pack(side=tk.RIGHT, padx=8, pady=10)

        btn_ntp = tk.Button(
            top, text="🕐 NTP Sync",
            command=self._ntp_sync,
            bg=C_BORDER, fg=C_TEAL,
            activebackground=C_PANEL,
            activeforeground=C_TEAL,
            relief=tk.FLAT,
            font=("Segoe UI", 9),
            cursor="hand2",
            padx=8, pady=4
        )
        btn_ntp.pack(side=tk.RIGHT, padx=4, pady=10)

        # ── Notebook ─────────────────────────────────────────────────────────
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
        p.rowconfigure(2, weight=1)

        # ── Unit State panel ─────────────────────────────────────────────────
        uf = make_frame(p, "Unit Occupancy State", row=0, col=0)
        uf.columnconfigure(0, weight=1)

        self._lbl_unit_big = tk.Label(
            uf, text="—", fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 28, "bold"))
        self._lbl_unit_big.grid(row=0, column=0, pady=10, padx=20)

        self._lbl_sensor_occ = tk.Label(
            uf, text="Sensor: —", fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 11))
        self._lbl_sensor_occ.grid(row=1, column=0, pady=2)

        self._lbl_inet_mode = tk.Label(
            uf, text="Mode: waiting...",
            fg=C_YELLOW, bg=C_BG,
            font=("Segoe UI", 10))
        self._lbl_inet_mode.grid(row=2, column=0, pady=2)

        self._lbl_utc_big = tk.Label(
            uf, text="UTC: —", fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 9))
        self._lbl_utc_big.grid(row=3, column=0, pady=1)

        self._lbl_pending2 = tk.Label(
            uf, text="", fg=C_YELLOW, bg=C_BG,
            font=("Segoe UI", 10))
        self._lbl_pending2.grid(row=4, column=0, pady=2)

        # ── Quick Actions panel ───────────────────────────────────────────────
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

        # ── Hub Control panel ─────────────────────────────────────────────────
        hf = make_frame(p, "Sensor Hub Control", row=0, col=2)
        hf.columnconfigure(0, weight=1)
        make_button(hf, "Open Pairing (120s)",
                    lambda: self._start_pairing(120),
                    fg=C_GREEN, row=0, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Stop Pairing",
                    self._stop_pairing,
                    fg=C_RED, row=1, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Start Watchdog",
                    self._start_watchdog,
                    fg=C_TEAL, row=2, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Refresh Sensors",
                    self._get_sensor_config,
                    fg=C_BLUE, row=3, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Get Logs",
                    self._get_hub_logs,
                    fg=C_DIM, row=4, col=0, sticky="ew", padx=8, pady=3)
        make_button(hf, "Restart Hub",
                    self._restart_hub,
                    fg=C_YELLOW, row=5, col=0, sticky="ew", padx=8, pady=3)

        # ── Force Indicator panel (hidden when not active) ────────────────────
        self._force_frame = tk.LabelFrame(
            p,
            text="  ⚠  FORCE ACTIVE  ",
            bg=C_BG, fg=C_ORANGE,
            font=("Segoe UI", 10, "bold"),
            bd=2, relief=tk.GROOVE,
            labelanchor="nw"
        )
        # Placed at row=1 spanning all columns — hidden by default
        self._force_frame.grid(row=1, column=0, columnspan=3,
                                sticky="ew", padx=8, pady=4)
        self._force_frame.columnconfigure(0, weight=1)
        self._force_frame.columnconfigure(1, weight=1)
        self._force_frame.columnconfigure(2, weight=1)
        self._force_frame.columnconfigure(3, weight=0)

        self._lbl_force_status = tk.Label(
            self._force_frame, text="Status: —",
            fg=C_ORANGE, bg=C_BG,
            font=("Segoe UI", 11, "bold"))
        self._lbl_force_status.grid(row=0, column=0,
                                     sticky="w", padx=12, pady=6)

        self._lbl_force_expires = tk.Label(
            self._force_frame, text="Expires: —",
            fg=C_YELLOW, bg=C_BG,
            font=("Segoe UI", 10))
        self._lbl_force_expires.grid(row=0, column=1,
                                      sticky="w", padx=12, pady=6)

        self._lbl_force_reason = tk.Label(
            self._force_frame, text="Reason: —",
            fg=C_DIM, bg=C_BG,
            font=("Segoe UI", 10))
        self._lbl_force_reason.grid(row=0, column=2,
                                     sticky="w", padx=12, pady=6)

        self._btn_cancel_force = tk.Button(
            self._force_frame,
            text="  Cancel Force  ",
            command=self._cancel_force,
            bg=C_RED, fg=C_BG,
            activebackground=C_ORANGE,
            activeforeground=C_BG,
            relief=tk.FLAT,
            font=("Segoe UI", 9, "bold"),
            cursor="hand2",
            padx=10, pady=4
        )
        self._btn_cancel_force.grid(row=0, column=3,
                                     sticky="e", padx=12, pady=6)

        # Initially hide the force frame
        self._force_frame.grid_remove()

        # ── Live Events ───────────────────────────────────────────────────────
        ef = make_frame(p, "Live Events",
                        row=2, col=0, colspan=3, sticky="nsew")
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
            ("door",         C_BLUE),
            ("presence",     C_GREEN),
            ("alarm",        C_RED),
            ("health",       C_ORANGE),
            ("battery",      C_YELLOW),
            ("env",          C_TEAL),
            ("unit",         C_PURPLE),
            ("hub",          C_BLUE),
            ("inet",         C_ORANGE),
            ("ntp",          C_TEAL),
            ("dim",          C_DIM),
            ("info",         C_TEXT),
            ("sched",        C_TEAL),
            ("force",        C_ORANGE),
            ("boot",         C_YELLOW),
            ("warn",         C_YELLOW),
            ("error",        C_RED),
            ("notification", C_ORANGE),
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
        make_button(bf, "Start Watchdog",
                    self._start_watchdog,
                    fg=C_TEAL, row=0, col=3, padx=4)
        make_button(bf, "Remove Selected",
                    self._remove_sensor,
                    fg=C_RED, row=0, col=4, padx=4)

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
            text="Double-click a sensor to rename it.  "
                 "Select a row then click Remove to delete.",
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

        # Unit config
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

        btn_row = len(unit_fields)
        make_button(uf, "Save Unit Config",
                    self._save_unit_config,
                    fg=C_GREEN, row=btn_row, col=0,
                    sticky="ew", padx=8, pady=8)

        # Hub config — now includes v0.2.5 fields
        hf = make_frame(p, "Sensor Hub Configuration", row=0, col=1)
        hf.columnconfigure(1, weight=1)

        hub_fields = [
            ("pairing_duration_sec",          "Pairing duration (sec)"),
            ("watchdog_interval_min",         "Watchdog interval (min)"),
            ("watchdog_ping_timeout_sec",     "Ping timeout (sec)"),
            ("door_alarm_threshold_min",      "Door alarm threshold (min)"),
            ("heartbeat_interval_min",        "Heartbeat interval (min)"),
            ("presence_fading_time_sec",      "Presence fading time (sec)"),
            ("door_sensor_max_silence_hours", "Door silence alert (hours)"),
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

        # WiFi config
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
        make_button(df, "Refresh All",
                    self._refresh_all,
                    fg=C_BLUE, row=0, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "NTP Sync Now",
                    self._ntp_sync,
                    fg=C_TEAL, row=1, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Start Watchdog",
                    self._start_watchdog,
                    fg=C_TEAL, row=2, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Restart Master",
                    self._restart_master,
                    fg=C_RED, row=3, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Restart Sensor Hub",
                    self._restart_hub,
                    fg=C_ORANGE, row=4, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Hub Factory Reset",
                    self._hub_factory_reset,
                    fg=C_RED, row=5, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Restart Scheduler",
                    self._restart_scheduler,
                    fg=C_ORANGE, row=6, col=0, sticky="ew", padx=8, pady=4)
        make_button(df, "Scheduler Factory Reset",
                    self._scheduler_factory_reset,
                    fg=C_RED, row=7, col=0, sticky="ew", padx=8, pady=4)

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
        # Master sends state_snapshot automatically on connect — no get_state needed

    def _ui_on_disconnect(self):
        self._lbl_conn.config(
            text="⬤  Disconnected — reconnecting...", fg=C_RED)
        self._lbl_boot_phase.config(text="")
        self._log(
            "Disconnected. Reconnecting in {}s...".format(
                RECONNECT_INTERVAL_S), "warn")

    def _ui_on_message(self, msg):
        t = msg.get("type", "")
        self._log_raw(msg)

        # ── State snapshot ────────────────────────────────────────────────────
        if t == "state_snapshot":
            now      = time.monotonic()
            unit_val = msg.get("unit_state", "")
            if (now - self._last_snapshot_time < 0.5 and
                    unit_val == self._last_snapshot_unit):
                return   # deduplicate
            self._last_snapshot_time = now
            self._last_snapshot_unit = unit_val
            self._apply_snapshot(msg)

        # ── Internet / NTP ────────────────────────────────────────────────────
        elif t == "internet_status":
            up = msg.get("status", "") == "up"
            self._update_internet_indicator(up)
            self._log_event(
                "INTERNET {}".format("RESTORED" if up else "LOST"), "inet")

        elif t == "ntp_status":
            synced = msg.get("synced", False)
            utc    = msg.get("utc", "")
            if synced:
                self._lbl_ntp.config(text="NTP: ✓", fg=C_GREEN)
                self._lbl_utc.config(text="UTC: {}".format(utc), fg=C_GREEN)
                self._lbl_utc_big.config(text="UTC: {}".format(utc),
                                         fg=C_GREEN)
                self._log_event("NTP SYNCED — UTC: {}".format(utc), "ntp")
            else:
                self._lbl_ntp.config(text="NTP: ✗", fg=C_RED)
                self._log_event("NTP SYNC FAILED", "alarm")

        # ── Boot protocol (v0.2.5) ────────────────────────────────────────────
        elif t == "boot_phase":
            phase = msg.get("phase", "")
            phase_labels = {
                "A_PING":    "Boot: Discovering Hub...",
                "A_DONE":    "Boot: Hub found ✓",
                "B_INIT":    "Boot: Initialising Hub...",
                "B_DONE":    "Boot: Hub initialised ✓",
                "C_REJOIN":  "Boot: Sensors rejoining...",
                "C_DONE":    "Boot: Sensors done ✓",
                "D_WATCHDOG":"Boot: Starting watchdog...",
                "READY":     "",
            }
            label = phase_labels.get(phase, "Boot: {}".format(phase))
            self._lbl_boot_phase.config(
                text=label,
                fg=C_GREEN if phase == "READY" else C_YELLOW)
            if phase != "READY":
                self._log_event("BOOT PHASE: {}".format(phase), "boot")

        elif t == "boot_fault":
            reason = msg.get("reason", "Unknown")
            self._lbl_hub.config(text="Hub: FAULT", fg=C_RED)
            self._lbl_boot_phase.config(
                text="⚠ Hub FAULT", fg=C_RED)
            self._log_event(
                "⚠ HUB FAULT — {}".format(reason), "error")
            messagebox.showerror(
                "Sensor Hub Fault",
                "Sensor Hub is not responding.\n\n"
                "Reason: {}\n\n"
                "Check UART wiring and Hub power supply.".format(reason))

        # ── Hub ready / sensor rejoin ─────────────────────────────────────────
        elif t == "hub_ready":
            on       = msg.get("online_count",  0)
            off      = msg.get("offline_count", 0)
            fw       = msg.get("firmware_version", "?")
            needs_p  = msg.get("needs_pairing", False)
            self._lbl_fw.config(
                text="Hub fw: {}".format(fw), fg=C_DIM)
            colour = C_GREEN if on > 0 else C_YELLOW
            self._lbl_hub.config(
                text="Hub: READY ({} online)".format(on), fg=colour)
            self._log_event(
                "HUB READY — fw={} online={} offline={} needs_pairing={}".format(
                    fw, on, off, needs_p), "hub")
            if needs_p:
                self._log_event(
                    "⚠  No sensors registered — open pairing to pair sensors",
                    "warn")
            # Refresh sensor list after ready
            self.root.after(1000, lambda: self._tcp.send(
                {"type": "get_sensor_config"}))

        elif t == "sensor_joined":
            idx    = msg.get("index", "?")
            name   = msg.get("name",  "?")
            model  = msg.get("model", "?")
            online = msg.get("online", False)
            self._log_event(
                "SENSOR JOINED  [{}] {}  {}  online={}".format(
                    idx, name, model, online), "hub")

        elif t == "sensor_status":
            idx    = msg.get("index", "?")
            name   = msg.get("name",  "?")
            online = msg.get("online", False)
            tag    = "hub" if online else "health"
            self._log_event(
                "SENSOR STATUS  [{}] {}  online={}{}".format(
                    idx, name, online,
                    "" if online else " — OFFLINE (rejoin failed)"), tag)

        elif t == "sensor_list_complete":
            total   = msg.get("total",   0)
            online  = msg.get("online",  0)
            offline = msg.get("offline", 0)
            self._log_event(
                "SENSOR LIST COMPLETE — total={} online={} offline={}".format(
                    total, online, offline), "hub")
            # Refresh sensor table now that list is final
            self.root.after(500, lambda: self._tcp.send(
                {"type": "get_sensor_config"}))

        elif t == "new_sensor_joined":
            idx   = msg.get("index",  "?")
            name  = msg.get("name",   "?")
            model = msg.get("model",  "?")
            role  = msg.get("role",   "?")
            self._log_event(
                "NEW SENSOR JOINED  [{}] {}  {}  {}".format(
                    idx, name, model, role), "hub")
            self.root.after(500, lambda: self._tcp.send(
                {"type": "get_sensor_config"}))

        elif t == "pairing_complete":
            new_s = msg.get("new_sensors",   0)
            tot   = msg.get("total_sensors", 0)
            self._log_event(
                "PAIRING COMPLETE — {} new sensor(s), {} total".format(
                    new_s, tot), "hub")
            self.root.after(500, lambda: self._tcp.send(
                {"type": "get_sensor_config"}))

        # ── Notification / alerts ─────────────────────────────────────────────
        elif t == "notification":
            level   = msg.get("level",   "info")
            message = msg.get("message", "")
            sensor  = msg.get("sensor",  "")
            tag     = {"error": "error", "warning": "health",
                       "info":  "info"}.get(level, "info")
            prefix  = {"error": "⚠ ERROR",
                       "warning": "⚠ WARN",
                       "info": "ℹ"}.get(level, "ℹ")
            full = "{}: {}{}".format(
                prefix, message,
                "  [{}]".format(sensor) if sensor else "")
            self._log_event(full, tag)
            # Show popup for errors
            if level == "error":
                messagebox.showerror("System Alert", message)

        # ── Force commands ────────────────────────────────────────────────────
        elif t == "force_update":
            active  = msg.get("active", False)
            status  = msg.get("status", "")
            expires = msg.get("expires_utc", "")
            reason  = msg.get("reason", "")
            self._update_force_panel(active, status, expires, reason)
            if active:
                self._log_event(
                    "FORCE SET: {} — expires {} — {}".format(
                        status, expires, reason), "force")
            else:
                self._log_event("FORCE CLEARED — automatic control resumed",
                                "force")

        # ── Occupancy ─────────────────────────────────────────────────────────
        elif t == "unit_occupancy":
            s = msg.get("state", "VACANT")
            self._log_event(
                "UNIT OCCUPANCY → {}".format(s), "unit")
            occ_colour = C_GREEN if s.upper() == "OCCUPIED" else C_DIM
            self._lbl_sensor_occ.config(
                text="Sensor: {}".format(s.upper()), fg=occ_colour)
            self._lbl_occ.config(
                text="Sensor: {}".format(s.upper()), fg=occ_colour)

        elif t == "unit_state_update":
            status = msg.get("status", "Unknown")
            colour = STATUS_COLOURS.get(status, C_DIM)
            self._lbl_unit_big.config(text=status, fg=colour)
            self._lbl_unit.config(text="Unit: {}".format(status), fg=colour)
            self._log_event("UNIT STATE → {}".format(status), "unit")
            occ = "occupied" if status in ("Occupied", "UnSold Occupied") \
                  else "vacant"
            occ_colour = C_GREEN if occ == "occupied" else C_DIM
            self._lbl_sensor_occ.config(
                text="Sensor: {}".format(occ.upper()), fg=occ_colour)
            self._lbl_occ.config(
                text="Sensor: {}".format(occ.upper()), fg=occ_colour)

        elif t == "pending_update":
            pending = msg.get("pending_status")
            if pending:
                txt = "⏳ Pending: {}".format(pending)
                self._lbl_pending.config(text=txt)
                self._lbl_pending2.config(text=txt)
            else:
                self._lbl_pending.config(text="")
                self._lbl_pending2.config(text="")

        # ── Sensor events ─────────────────────────────────────────────────────
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
            sensor = msg.get("sensor", "")
            self._log_event(
                "HEALTH  {}  {}".format(
                    sensor, msg.get("state", "")), "health")
            self._update_sensor_health(sensor, online)
            if not online:
                self._log_event(
                    "⚠  {} is OFFLINE — check battery and range".format(
                        sensor), "warn")

        elif t == "battery":
            self._log_event(
                "BATTERY  {}  {}%".format(
                    msg.get("sensor", ""), msg.get("battery_pct", 0)),
                "battery")
            self._update_sensor_battery(
                msg.get("sensor", ""), msg.get("battery_pct", 0))

        elif t == "heartbeat":
            fw = msg.get("firmware_version", "")
            if fw:
                self._lbl_fw.config(
                    text="Hub fw: {}".format(fw), fg=C_DIM)
            self._log_event(
                "HEARTBEAT  unit={}  fw={}".format(
                    msg.get("unit_state", ""), fw), "dim")
            for s in msg.get("sensors", []):
                self._refresh_sensor_row(s)

        elif t == "config_response":
            sensors = msg.get("sensors", [])
            self._apply_sensor_list(sensors)
            self._apply_hub_config_from_response(msg)
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
            if cmd in ("set_unit_config", "set_hub_config", "set_wifi_config"):
                self._log_event(
                    "Config saved ({})".format(cmd), "info")
            elif cmd == "start_watchdog":
                self._log_event("Watchdog started ✓", "hub")
            elif cmd == "start_pairing":
                self._log_event("Pairing window opened ✓", "hub")
            elif cmd == "cancel_force":
                self._log_event("Force cancelled — automatic control active",
                                "force")

        elif t == "log_response":
            self._log_hub(msg.get("line", ""))

    # -----------------------------------------------------------------------
    # FORCE PANEL MANAGEMENT
    # -----------------------------------------------------------------------

    def _update_force_panel(self, active, status="", expires="", reason=""):
        """Show or hide the force indicator panel."""
        self._force_active  = active
        self._force_status  = status
        self._force_expires = expires
        self._force_reason  = reason

        if active:
            colour = STATUS_COLOURS.get(status, C_ORANGE)
            self._lbl_force_status.config(
                text="Status: {}".format(status), fg=colour)
            self._lbl_force_expires.config(
                text="Expires: {} UTC".format(expires), fg=C_YELLOW)
            self._lbl_force_reason.config(
                text="Reason: {}".format(reason if reason else "Manual force"),
                fg=C_DIM)
            self._force_frame.grid()
        else:
            self._force_frame.grid_remove()

    # -----------------------------------------------------------------------
    # STATE UPDATES
    # -----------------------------------------------------------------------

    def _apply_snapshot(self, snap):
        unit      = snap.get("unit_state",       "Unknown")
        sensor    = snap.get("sensor_occupancy", "vacant")
        pending   = snap.get("pending_status")
        ip        = snap.get("wifi_ip",          "")
        inet_up   = snap.get("internet_up",      False)
        ntp_ok    = snap.get("ntp_synced",       False)
        utc_now   = snap.get("utc_now",          "")
        sched     = snap.get("scheduler_status", "")
        hub_cfg   = snap.get("sensor_hub_config", {})
        hub_state = snap.get("hub_state",        "UNKNOWN")
        hub_fw    = snap.get("hub_firmware_version", "")
        master_fw = snap.get("firmware_version", "")
        hub_fault = snap.get("hub_fault",        False)
        boot_ph   = snap.get("boot_phase",       "")
        mode      = snap.get("mode",             "production")

        # Force fields
        force_active  = snap.get("force_active",  False)
        force_status  = snap.get("force_status",  "")
        force_expires = snap.get("force_expires_utc", "")
        force_reason  = snap.get("force_reason",  "")

        # Unit state label
        colour = STATUS_COLOURS.get(unit, C_DIM)
        self._lbl_unit_big.config(text=unit, fg=colour)
        self._lbl_unit.config(text="Unit: {}".format(unit), fg=colour)

        # Sensor occupancy
        occ_colour = C_GREEN if sensor == "occupied" else C_DIM
        self._lbl_sensor_occ.config(
            text="Sensor: {}".format(sensor.upper()), fg=occ_colour)
        self._lbl_occ.config(
            text="Sensor: {}".format(sensor.upper()), fg=occ_colour)

        # Internet / NTP
        self._update_internet_indicator(inet_up)
        if ntp_ok:
            self._lbl_ntp.config(text="NTP: ✓", fg=C_GREEN)
            self._lbl_utc.config(text="UTC: {}".format(utc_now), fg=C_GREEN)
            self._lbl_utc_big.config(text="UTC: {}".format(utc_now),
                                     fg=C_GREEN)
        else:
            self._lbl_ntp.config(text="NTP: ✗", fg=C_RED)
            self._lbl_utc.config(text="UTC: not synced", fg=C_RED)
            self._lbl_utc_big.config(text="UTC: not synced", fg=C_RED)

        # Hub state
        if hub_fault:
            self._lbl_hub.config(text="Hub: FAULT", fg=C_RED)
        elif hub_state == "READY":
            self._lbl_hub.config(text="Hub: READY", fg=C_GREEN)
        elif hub_state == "BOOTING":
            self._lbl_hub.config(text="Hub: BOOTING", fg=C_YELLOW)
        elif hub_state == "FAULT":
            self._lbl_hub.config(text="Hub: FAULT", fg=C_RED)
        else:
            self._lbl_hub.config(text="Hub: UNKNOWN", fg=C_DIM)

        # Firmware version
        fw_parts = []
        if master_fw: fw_parts.append("Master v{}".format(master_fw))
        if hub_fw:    fw_parts.append("Hub v{}".format(hub_fw))
        self._lbl_fw.config(
            text="  ".join(fw_parts) if fw_parts else "fw: —",
            fg=C_DIM)

        # Boot phase (clear if READY)
        if boot_ph and boot_ph != "READY":
            self._lbl_boot_phase.config(
                text="Phase: {}".format(boot_ph), fg=C_YELLOW)
        else:
            self._lbl_boot_phase.config(text="")

        # Pending
        if pending:
            txt = "⏳ Pending: {}".format(pending)
            self._lbl_pending.config(text=txt)
            self._lbl_pending2.config(text=txt)
        else:
            self._lbl_pending.config(text="")
            self._lbl_pending2.config(text="")

        # IP
        self._lbl_ip.config(
            text="IP: {}".format(ip) if ip else "IP: —")

        # Mode indicator
        if mode == "debug":
            self._lbl_inet_mode.config(
                text="Mode: DEBUG (MQTT paused)", fg=C_ORANGE)
        elif inet_up:
            self._lbl_inet_mode.config(
                text="Mode: 4-state (full booking logic)", fg=C_GREEN)
        else:
            self._lbl_inet_mode.config(
                text="Mode: 2-state fallback (Occupied/Vacant only)",
                fg=C_YELLOW)

        # Force indicator — restore on reconnect
        self._update_force_panel(
            force_active, force_status, force_expires, force_reason)

        # Scheduler
        if sched:
            self._apply_relay_snapshot({}, sched)

        # Config tab entries
        for key, entry in self._cfg_entries.items():
            if key == "wifi_password":
                continue
            val = snap.get(key, "")
            if val is None: val = ""
            entry.delete(0, tk.END)
            entry.insert(0, str(val))

        for key, entry in self._hub_entries.items():
            val = hub_cfg.get(key, "")
            if val is None: val = ""
            entry.delete(0, tk.END)
            entry.insert(0, str(val))

        if "watchdog_enable" in hub_cfg:
            self._wd_enable_var.set(bool(hub_cfg["watchdog_enable"]))

        self._log_event(
            "State snapshot — unit={}  ntp={}  utc={}  mode={}{}".format(
                unit, "✓" if ntp_ok else "✗", utc_now, mode,
                "  FORCE: {}".format(force_status) if force_active else ""),
            "dim")

    def _apply_hub_config_from_response(self, msg):
        """Populate hub config entries from config_response (not snapshot)."""
        for key, entry in self._hub_entries.items():
            val = msg.get(key, "")
            if val is None: val = ""
            entry.delete(0, tk.END)
            entry.insert(0, str(val))
        wd_enable = msg.get("watchdog_enable", True)
        if isinstance(wd_enable, bool):
            self._wd_enable_var.set(wd_enable)

    def _update_internet_indicator(self, is_up):
        if is_up:
            self._lbl_inet.config(text="Internet: ✓ UP", fg=C_GREEN)
        else:
            self._lbl_inet.config(text="Internet: ✗ DOWN", fg=C_RED)
            self._lbl_inet_mode.config(
                text="Mode: 2-state fallback (Occupied/Vacant only)",
                fg=C_YELLOW)

    def _apply_sensor_list(self, sensors):
        # Snapshot live state before rebuild
        live = {}
        for iid in self._sensor_tree.get_children():
            vals = self._sensor_tree.item(iid, "values")
            if vals and len(vals) >= 9:
                live[vals[1]] = {
                    "state":   vals[5],
                    "battery": vals[6],
                    "temp":    vals[7],
                    "hum":     vals[8],
                }

        self._sensors = {}
        for row in self._sensor_tree.get_children():
            self._sensor_tree.delete(row)

        for s in sensors:
            if not isinstance(s, dict): continue
            idx    = s.get("index", "?")
            name   = s.get("name",  "?")
            model  = s.get("model", "?")
            role   = s.get("role",  "?")
            online = "✓" if s.get("online", False) else "✗"
            batt   = "{}%".format(s.get("battery", "?"))

            prev = live.get(name, {})

            if prev.get("state", "—") not in ("—", ""):
                state_v = prev["state"]
            elif role == "DOOR":
                contact = s.get("contact", None)
                state_v = contact if contact else "—"
            else:
                pres    = s.get("presence", None)
                state_v = ("YES" if pres else "NO") if pres is not None else "—"

            prev_batt = prev.get("battery", "—")
            batt_v = prev_batt if (prev_batt not in ("—", "")) else batt

            temp_v = prev.get("temp", "—")
            hum_v  = prev.get("hum",  "—")

            self._sensor_tree.insert(
                "", tk.END,
                iid=str(idx),
                values=(idx, name, model, role,
                        online, state_v, batt_v, temp_v, hum_v))
            self._sensors[str(idx)] = s
            self._sensors[name]     = s

    def _refresh_sensor_row(self, s):
        name = s.get("name", "")
        for iid in self._sensor_tree.get_children():
            vals = list(self._sensor_tree.item(iid, "values"))
            if vals[1] == name:
                vals[4] = "✓" if s.get("online", False) else "✗"
                batt = s.get("battery", None)
                if batt is not None:
                    vals[6] = "{}%".format(batt)
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
                text="Scheduler status: {}".format(sched_status), fg=colour)

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
        if t in ("state_snapshot", "pong", "scheduler_update",
                 "pending_update", "unit_state_update",
                 "boot_phase"):
            return   # suppress high-frequency noise from raw log
        self._log("← RX  {}".format(json.dumps(msg)[:160]), "rx")

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

    def _refresh_all(self):
        self._tcp.send({"type": "get_state"})
        self._tcp.send({"type": "get_sensor_config"})
        self._log("TX → Refresh All", "tx")

    def _ntp_sync(self):
        self._tcp.send({"type": "ntp_sync"})
        self._log("TX → ntp_sync", "tx")

    def _force_status(self, status):
        """
        Open force duration dialog. Sends force_status with duration_hours.
        Minimum 1 hour. Maximum 24 hours.
        """
        dlg = ForceDurationDialog(self.root, status)
        if dlg.result_hours is None:
            return   # cancelled
        payload = {
            "type":           "force_status",
            "status":         status,
            "duration_hours": dlg.result_hours,
            "reason":         dlg.result_reason or "Manual force"
        }
        self._tcp.send(payload)
        self._log(
            "TX → force_status: {} for {}h — {}".format(
                status, dlg.result_hours, dlg.result_reason), "tx")

    def _cancel_force(self):
        if messagebox.askyesno(
            "Cancel Force",
            "Cancel the active force?\n\n"
            "Unit will return to automatic sensor+booking control."
        ):
            self._tcp.send({"type": "cancel_force"})
            self._log("TX → cancel_force", "tx")

    def _cancel_pending(self):
        self._tcp.send({"type": "cancel_pending"})
        self._log("TX → cancel_pending", "tx")

    def _start_pairing(self, dur=120):
        self._tcp.send({"type": "start_pairing", "duration_sec": dur})
        self._log("TX → start_pairing {}s".format(dur), "tx")

    def _stop_pairing(self):
        self._tcp.send({"type": "stop_pairing"})
        self._log("TX → stop_pairing", "tx")

    def _start_watchdog(self):
        """Manually send start_watchdog — useful after pairing new sensors."""
        self._tcp.send({"type": "start_watchdog"})
        self._log("TX → start_watchdog", "tx")

    def _get_sensor_config(self):
        self._tcp.send({"type": "get_sensor_config"})
        self._log("TX → get_sensor_config", "tx")

    def _get_hub_logs(self):
        self._tcp.send({"type": "get_hub_logs"})
        self._log("TX → get_hub_logs", "tx")

    def _restart_hub(self):
        if messagebox.askyesno(
            "Restart Hub",
            "Restart the Sensor Hub?\n\n"
            "The boot sequence will restart.\n"
            "Master will re-send ping/hub_init automatically."
        ):
            self._tcp.send({"type": "hub_restart"})
            self._log("TX → hub_restart", "tx")

    def _hub_factory_reset(self):
        if messagebox.askyesno(
            "Factory Reset Sensor Hub",
            "⚠  Factory reset the Sensor Hub?\n\n"
            "All sensor names and registry will be erased.\n"
            "Sensors must be re-paired after this operation.",
            icon="warning"
        ):
            self._tcp.send({"type": "hub_factory_reset"})
            self._log("TX → hub_factory_reset", "tx")

    def _restart_master(self):
        if messagebox.askyesno(
            "Restart Master",
            "Restart the Master ESP32-S3?\n\n"
            "Boot sequence will re-run."
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
            "All relay schedules will be erased.",
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
                try:    val = int(val)
                except Exception: val = 15
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
                    "heartbeat_interval_min", "presence_fading_time_sec",
                    "door_sensor_max_silence_hours"):
            entry = self._hub_entries.get(key)
            if entry:
                try:   payload[key] = int(entry.get().strip())
                except Exception: pass
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
            messagebox.showinfo("Remove Sensor", "Select a sensor row first.")
            return
        iid  = sel[0]
        vals = self._sensor_tree.item(iid, "values")
        idx  = vals[0]
        name = vals[1]
        if messagebox.askyesno(
            "Remove Sensor",
            "Remove sensor:\n\n  [{}] {}\n\n"
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
        description="Innovatsii EMS Pico 1 Desktop Debugger v0.2.5")
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