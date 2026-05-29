import tkinter as tk
from tkinter import ttk, filedialog
import socket
import threading
import time
import math
import select
import base64
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "current", "gps"))
from rtk import NTRIPClient

# ----------------------- Constants -----------------------
HOST_DEFAULT = "192.168.4.1"
PORT_DEFAULT = 80
FEET_PER_DEGREE_LAT = 364567.2

PIN_LEFT = 5
PIN_FRONT = 18
PIN_RIGHT = 19
PIN_BACK = 23

GRID_ROWS = 5
GRID_COLS = 5

NTRIP_CASTER = "rtgpsout.earthscope.org"
NTRIP_PORT = 2101
NTRIP_MOUNTPOINT = "P528_RTCM3P3"
NTRIP_USER = "suspicious_panini"
NTRIP_PASS = "QOpijUWXifA5oH93"

FIX_QUALITY = {
    0: ("No Fix",    "#FF4444"),
    1: ("GPS",       "#FF8C00"),
    2: ("DGPS",      "#FFD700"),
    4: ("RTK Fixed", "#00CC00"),
    5: ("RTK Float", "#90EE90"),
}

LOG_GPS    = "Hub GPS"
LOG_RTCM   = "RTK Corrections"
LOG_MOTOR  = "Motor Command"
LOG_ERR    = "Error"
LOG_SYS    = "System"
LOG_HB     = "Headband"

LOG_COLORS = {
    LOG_GPS:    "#2196F3",
    LOG_RTCM:   "#9C27B0",
    LOG_MOTOR:  "#4CAF50",
    LOG_ERR:    "#F44336",
    LOG_SYS:    "#757575",
    LOG_HB:     "#00ACC1",
}

# ----------------------- GPS Functions -----------------------
def feet_to_degrees(feet, latitude):
    lat_degrees = feet / FEET_PER_DEGREE_LAT
    lon_degrees = feet / (FEET_PER_DEGREE_LAT * math.cos(math.radians(latitude)))
    return lat_degrees, lon_degrees

def calculate_column_positions(lat, lon, heading_deg, spacing_feet=3.0):
    theta = math.radians(heading_deg)
    offset_multipliers = [-2.0, -1.0, 0.0, 1.0, 2.0]
    result = []
    for mult in offset_multipliers:
        offset_feet = mult * spacing_feet
        lat_offset, lon_offset = feet_to_degrees(abs(offset_feet), lat)
        perp_angle = theta + math.pi / 2
        dlat = lat_offset * math.cos(perp_angle) * (1 if offset_feet >= 0 else -1)
        dlon = lon_offset * math.sin(perp_angle) * (1 if offset_feet >= 0 else -1)
        result.append(((lat + dlat, lon + dlon), int(heading_deg)))
    return result

# ----------------------- Network Helpers -----------------------
def send_message(msg, sock_obj, timeout=1.0):
    sock_obj.settimeout(timeout)
    sock_obj.sendall((msg + "\n").encode())

def connect(ip, port, retries=3):
    for attempt in range(retries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            s.settimeout(3.0)
            s.connect((ip, port))
            s.settimeout(None)
            return s
        except Exception:
            if attempt < retries - 1:
                time.sleep(1)
    return None

RECONNECT_DELAY_INITIAL = 3
RECONNECT_DELAY_MAX = 30

# ======================= Main Application =======================
class HaptiBandApp:
    def __init__(self, root):
        self.root = root
        self.root.title("HaptiBand Dashboard")
        self.root.geometry("1400x900")
        self.root.minsize(1100, 750)

        self.sock = None
        self.sock_lock = threading.Lock()
        self.connected = False
        self.connect_time = None
        self.shutdown_event = threading.Event()
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.reconnecting = False

        self.selected_rows = {1}

        self.hub_lat = None
        self.hub_lon = None
        self.hub_heading = None
        self.hub_fix_quality = 0
        self.hub_last_update = None
        self.gps_listener_running = False
        self.gps_listener_thread = None

        self.auto_relay = tk.BooleanVar(value=False)
        self.spacing_var = tk.DoubleVar(value=3.0)

        self.ntrip_enabled = tk.BooleanVar(value=True)
        self.ntrip_client = None
        self.ntrip_thread = None
        self.rtcm_lock = threading.Lock()
        self.rtcm_stats = {"bytes_sent": 0, "packets_sent": 0, "last_send_time": None, "errors": 0}

        self.headband_statuses = {}
        self.log_entries = []
        self.log_filter = tk.StringVar(value="ALL")
        self.column_positions = None
        self.hub_rtcm_stats = None

        self.build_ui()
        self.update_status()
        self.root.bind("<Key>", self.on_key)
        self.root.bind("<Escape>", lambda e: self.emergency_stop())

    # ======================= UI Construction =======================
    def build_ui(self):
        self.build_top_bar()
        self.main_pane = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        self.main_pane.pack(fill="both", expand=True, padx=5, pady=(0, 5))

        left = ttk.Frame(self.main_pane, width=380)
        self.main_pane.add(left, weight=1)
        self.build_formation_panel(left)

        center = ttk.Frame(self.main_pane, width=420)
        self.main_pane.add(center, weight=1)
        self.build_center_panel(center)

        right = ttk.Frame(self.main_pane, width=350)
        self.main_pane.add(right, weight=1)
        self.build_log_panel(right)

    def build_top_bar(self):
        bar = ttk.Frame(self.root, padding=8)
        bar.pack(fill="x")
        ttk.Label(bar, text="IP:").pack(side="left")
        self.ip_entry = ttk.Entry(bar, width=14)
        self.ip_entry.insert(0, HOST_DEFAULT)
        self.ip_entry.pack(side="left", padx=(2, 8))
        ttk.Label(bar, text="Port:").pack(side="left")
        self.port_entry = ttk.Entry(bar, width=6)
        self.port_entry.insert(0, str(PORT_DEFAULT))
        self.port_entry.pack(side="left", padx=(2, 8))
        self.connect_btn = ttk.Button(bar, text="Connect", command=self.connect_to_hub)
        self.connect_btn.pack(side="left", padx=2)
        self.disconnect_btn = ttk.Button(bar, text="Disconnect", command=self.disconnect_from_hub, state="disabled")
        self.disconnect_btn.pack(side="left", padx=2)
        self.status_canvas = tk.Canvas(bar, width=20, height=20, highlightthickness=0)
        self.status_canvas.pack(side="left", padx=(10, 2))
        self.status_indicator = self.status_canvas.create_oval(3, 3, 17, 17, fill="red", outline="darkred")
        self.status_label = ttk.Label(bar, text="Disconnected")
        self.status_label.pack(side="left", padx=2)
        estop = tk.Button(bar, text="EMERGENCY STOP", bg="#CC0000", fg="white",
                          font=("Helvetica", 11, "bold"), command=self.emergency_stop,
                          activebackground="#990000", activeforeground="white", padx=12, pady=4)
        estop.pack(side="right", padx=5)

    def build_formation_panel(self, parent):
        grid_frame = ttk.LabelFrame(parent, text="Formation Grid", padding=8)
        grid_frame.pack(fill="both", expand=True, padx=4, pady=4)
        ttk.Label(grid_frame, text="\u2190 Parade Direction", font=("Helvetica", 9, "italic"), foreground="gray").pack(anchor="w")
        hub_frame = ttk.Frame(grid_frame)
        hub_frame.pack(pady=(2, 4))
        hc = tk.Canvas(hub_frame, width=50, height=20, highlightthickness=0)
        hc.pack()
        hc.create_rectangle(2, 2, 48, 18, fill="#4488CC", outline="#336699")
        hc.create_text(25, 10, text="HUB", fill="white", font=("Helvetica", 8, "bold"))

        sel_frame = ttk.Frame(grid_frame)
        self.selection_label = ttk.Label(sel_frame, text="Selected: Row 1")

        self.grid_canvas = tk.Canvas(grid_frame, width=340, height=300, highlightthickness=0, bg="#F5F5F5")
        self.grid_canvas.pack(pady=4)
        self.grid_cells = {}
        self.draw_formation_grid()

        sel_frame.pack(fill="x", pady=(4, 0))
        self.selection_label.pack(side="left")
        ttk.Button(sel_frame, text="Clear", command=self.clear_selection, width=5).pack(side="right", padx=2)
        ttk.Button(sel_frame, text="All", command=self.select_all_rows, width=4).pack(side="right", padx=2)
        for r in range(1, 6):
            ttk.Button(sel_frame, text=str(r), command=lambda row=r: self.select_single_row(row), width=2).pack(side="right", padx=1)

        col_frame = ttk.LabelFrame(parent, text="Column Targets", padding=8)
        col_frame.pack(fill="x", padx=4, pady=4)
        sides = ["Far L", "Left", "Center", "Right", "Far R"]
        self.column_labels = []
        for i in range(5):
            lbl = ttk.Label(col_frame, text=f"Col {i+1} ({sides[i]}): --", font=("Courier", 9))
            lbl.pack(anchor="w")
            self.column_labels.append(lbl)

    def draw_formation_grid(self):
        self.grid_canvas.delete("all")
        self.grid_cells = {}
        cw, ch = 340, 300
        cell_w = cw // (GRID_COLS + 1)
        cell_h = ch // GRID_ROWS
        for c in range(GRID_COLS):
            x = (c + 1) * cell_w + cell_w // 2
            self.grid_canvas.create_text(x, 10, text=f"C{c+1}", font=("Helvetica", 8), fill="gray")
        for r in range(GRID_ROWS):
            y = r * cell_h + 22
            rl = self.grid_canvas.create_text(cell_w // 2, y + cell_h // 2, text=f"R{r+1}", font=("Helvetica", 9, "bold"), fill="#2196F3")
            self.grid_canvas.tag_bind(rl, "<Button-1>", lambda e, row=r+1: self.on_row_click(row, e))
            for c in range(GRID_COLS):
                x = (c + 1) * cell_w
                cell = self.grid_canvas.create_rectangle(x + 2, y + 2, x + cell_w - 2, y + cell_h - 2, fill="#E0E0E0", outline="#BDBDBD")
                text_id = self.grid_canvas.create_text(x + cell_w // 2, y + cell_h // 2, text=f"{r+1};{c+1}", font=("Helvetica", 8))
                dot = self.grid_canvas.create_oval(x + cell_w - 14, y + 5, x + cell_w - 5, y + 14, fill="gray", outline="")
                self.grid_cells[(r + 1, c + 1)] = (cell, text_id, dot)
                self.grid_canvas.tag_bind(cell, "<Button-1>", lambda e, row=r+1: self.on_row_click(row, e))
                self.grid_canvas.tag_bind(text_id, "<Button-1>", lambda e, row=r+1: self.on_row_click(row, e))
        self.update_grid()

    def on_row_click(self, row, event):
        if event.state & 0x4:
            self.selected_rows.symmetric_difference_update({row})
            if not self.selected_rows: self.selected_rows = {row}
        else:
            self.selected_rows = {row}
        self.update_grid()

    def update_grid(self):
        now = time.time()
        for (r, c), (cell, _, dot) in self.grid_cells.items():
            self.grid_canvas.itemconfig(cell, fill="#C8E6C9" if r in self.selected_rows else "#E0E0E0",
                                        outline="#66BB6A" if r in self.selected_rows else "#BDBDBD")
            key = (r, c)
            if key in self.headband_statuses:
                hb = self.headband_statuses[key]
                if now - hb["last_update"] < 15:
                    _, color = FIX_QUALITY.get(hb["fix"], ("?", "#888"))
                    self.grid_canvas.itemconfig(dot, fill=color)
                else:
                    self.grid_canvas.itemconfig(dot, fill="#AAAAAA")
            else:
                self.grid_canvas.itemconfig(dot, fill="gray")
        self.selection_label.config(text=f"Selected: Row {', '.join(str(r) for r in sorted(self.selected_rows))}")

    def select_single_row(self, row): self.selected_rows = {row}; self.update_grid()
    def select_all_rows(self): self.selected_rows = set(range(1, 6)); self.update_grid()
    def clear_selection(self): self.selected_rows = {1}; self.update_grid()

    def build_center_panel(self, parent):
        hub_frame = ttk.LabelFrame(parent, text="Hub Status", padding=8)
        hub_frame.pack(fill="x", padx=4, pady=4)
        self.build_hub_status(hub_frame)

        ntrip_frame = ttk.LabelFrame(parent, text="RTK Corrections (cm-accuracy GPS)", padding=8)
        ntrip_frame.pack(fill="x", padx=4, pady=4)
        self.build_ntrip_panel(ntrip_frame)

        controls_nb = ttk.Notebook(parent)
        controls_nb.pack(fill="both", expand=True, padx=4, pady=4)
        manual_tab = ttk.Frame(controls_nb, padding=8)
        controls_nb.add(manual_tab, text="Manual Haptics")
        self.build_manual_controls(manual_tab)
        gps_tab = ttk.Frame(controls_nb, padding=8)
        controls_nb.add(gps_tab, text="GPS Guidance")
        self.build_gps_controls(gps_tab)

    def build_hub_status(self, parent):
        fix_row = ttk.Frame(parent)
        fix_row.pack(fill="x", pady=(0, 6))
        self.fix_canvas = tk.Canvas(fix_row, width=24, height=24, highlightthickness=0)
        self.fix_canvas.pack(side="left", padx=(0, 8))
        self.fix_dot = self.fix_canvas.create_oval(2, 2, 22, 22, fill="#FF4444", outline="")
        self.fix_label = ttk.Label(fix_row, text="No Fix", font=("Helvetica", 13, "bold"), foreground="#FF4444")
        self.fix_label.pack(side="left")
        info_frame = ttk.Frame(parent)
        info_frame.pack(fill="x")
        self.hub_lat_label = ttk.Label(info_frame, text="Lat: --", font=("Courier", 10))
        self.hub_lat_label.pack(anchor="w")
        self.hub_lon_label = ttk.Label(info_frame, text="Lon: --", font=("Courier", 10))
        self.hub_lon_label.pack(anchor="w")
        heading_row = ttk.Frame(info_frame)
        heading_row.pack(fill="x", pady=(2, 0))
        self.hub_heading_label = ttk.Label(heading_row, text="Heading: --", font=("Courier", 10))
        self.hub_heading_label.pack(side="left")
        self.heading_canvas = tk.Canvas(heading_row, width=30, height=30, highlightthickness=0)
        self.heading_canvas.pack(side="left", padx=8)
        self.draw_heading_arrow(0)
        self.last_update_label = ttk.Label(parent, text="Last update: --", foreground="gray")
        self.last_update_label.pack(anchor="w", pady=(4, 0))

    def draw_heading_arrow(self, heading_deg):
        self.heading_canvas.delete("all")
        cx, cy, r = 15, 15, 12
        angle_rad = math.radians(90 - heading_deg)
        ex = cx + r * math.cos(angle_rad)
        ey = cy - r * math.sin(angle_rad)
        self.heading_canvas.create_line(cx, cy, ex, ey, width=2, arrow=tk.LAST, fill="#333333")
        self.heading_canvas.create_oval(cx - 2, cy - 2, cx + 2, cy + 2, fill="#333333", outline="")

    def build_ntrip_panel(self, parent):
        row1 = ttk.Frame(parent)
        row1.pack(fill="x")
        ttk.Checkbutton(row1, text="Enable RTK relay", variable=self.ntrip_enabled).pack(side="left")
        self.ntrip_status_label = ttk.Label(row1, text="NTRIP: Inactive", foreground="gray")
        self.ntrip_status_label.pack(side="right")
        self.ntrip_stats_label = ttk.Label(parent, text="0 pkts, 0 bytes relayed", font=("Courier", 9))
        self.ntrip_stats_label.pack(anchor="w", pady=(2, 0))
        self.ntrip_last_label = ttk.Label(parent, text="Last RTCM: --", foreground="gray", font=("Courier", 9))
        self.ntrip_last_label.pack(anchor="w")
        self.ntrip_error_label = ttk.Label(parent, text="Errors: 0", font=("Courier", 9))
        self.ntrip_error_label.pack(anchor="w")
        ttk.Separator(parent, orient="horizontal").pack(fill="x", pady=4)
        ttk.Label(parent, text="Hub-side (corrections reaching GPS):", font=("Helvetica", 8, "italic"), foreground="gray").pack(anchor="w")
        self.hub_rtcm_label = ttk.Label(parent, text="Waiting for hub report...", font=("Courier", 9), foreground="gray")
        self.hub_rtcm_label.pack(anchor="w")

    def build_manual_controls(self, parent):
        ttk.Label(parent, text="Directly buzz motors on selected rows.\nUse keyboard shortcuts or buttons below.",
                  font=("Helvetica", 9), foreground="gray", justify="center").pack(pady=(0, 4))
        diagram_frame = ttk.Frame(parent)
        diagram_frame.pack(pady=4)
        self.motor_canvas = tk.Canvas(diagram_frame, width=180, height=150, highlightthickness=0, bg="#FAFAFA")
        self.motor_canvas.pack()
        self.motor_canvas.create_oval(40, 15, 140, 135, outline="#CCCCCC", width=2)
        self.motor_indicators = {}
        for pin, (x, y, label) in {PIN_FRONT: (90, 20, "Front"), PIN_LEFT: (35, 75, "Left"), PIN_RIGHT: (145, 75, "Right"), PIN_BACK: (90, 130, "Back")}.items():
            oval = self.motor_canvas.create_oval(x-10, y-10, x+10, y+10, fill="#E0E0E0", outline="#999999")
            self.motor_canvas.create_text(x, y+18, text=label, font=("Helvetica", 7), fill="gray")
            self.motor_indicators[pin] = oval
        btn_frame = ttk.Frame(parent)
        btn_frame.pack(pady=4)
        ttk.Button(btn_frame, text="W Fwd", command=self.forward, width=6).grid(row=0, column=1, padx=2, pady=2)
        ttk.Button(btn_frame, text="A Left", command=self.left, width=6).grid(row=1, column=0, padx=2, pady=2)
        ttk.Button(btn_frame, text="S Back", command=self.back, width=6).grid(row=1, column=1, padx=2, pady=2)
        ttk.Button(btn_frame, text="D Right", command=self.right, width=6).grid(row=1, column=2, padx=2, pady=2)
        extra = ttk.Frame(parent)
        extra.pack(pady=4)
        ttk.Button(extra, text="Z Turn L", command=self.rotate_left, width=7).pack(side="left", padx=2)
        ttk.Button(extra, text="X Turn R", command=self.rotate_right, width=7).pack(side="left", padx=2)
        ttk.Button(extra, text="E March", command=self.start_seq, width=7).pack(side="left", padx=2)
        ttk.Button(extra, text="Q Stop", command=self.stop_all, width=7).pack(side="left", padx=2)

    def build_gps_controls(self, parent):
        ttk.Label(parent, text="Automatically guide headbands to their\nformation positions using hub GPS data.",
                  font=("Helvetica", 9), foreground="gray", justify="center").pack(pady=(0, 6))
        sf = ttk.Frame(parent)
        sf.pack(fill="x", pady=4)
        ttk.Label(sf, text="Column spacing (ft):").pack(side="left")
        self.spacing_display = ttk.Label(sf, text="3.0")
        self.spacing_display.pack(side="right")
        ttk.Scale(parent, from_=1.0, to=10.0, variable=self.spacing_var, orient="horizontal",
                  command=lambda v: self.spacing_display.config(text=f"{float(v):.1f}")).pack(fill="x", pady=(0, 8))
        ttk.Checkbutton(parent, text="Auto-send positions when hub GPS updates", variable=self.auto_relay).pack(anchor="w", pady=2)
        ttk.Button(parent, text="Send Positions Now", command=self.send_gps_update).pack(fill="x", pady=8)
        self.listener_status = ttk.Label(parent, text="Hub GPS Listener: Inactive", foreground="gray")
        self.listener_status.pack(anchor="w", pady=(8, 0))

    def build_log_panel(self, parent):
        log_frame = ttk.LabelFrame(parent, text="Activity Log", padding=4)
        log_frame.pack(fill="both", expand=True, padx=4, pady=4)
        filter_bar = ttk.Frame(log_frame)
        filter_bar.pack(fill="x", pady=(0, 4))
        for cat, btn_text in [("ALL", "All"), (LOG_GPS, "GPS"), (LOG_RTCM, "RTK"), (LOG_HB, "Bands"),
                               (LOG_ERR, "Errors"), (LOG_MOTOR, "Motors"), (LOG_SYS, "System")]:
            ttk.Button(filter_bar, text=btn_text, width=6, command=lambda c=cat: self.set_log_filter(c)).pack(side="left", padx=1)
        ttk.Button(filter_bar, text="Save", width=4, command=self.save_log).pack(side="right", padx=1)
        ttk.Button(filter_bar, text="Clear", width=4, command=self.clear_log).pack(side="right", padx=1)
        log_container = ttk.Frame(log_frame)
        log_container.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_container, wrap="word", font=("Courier", 9), state="disabled",
                                bg="#1E1E1E", fg="#D4D4D4", insertbackground="white")
        scrollbar = ttk.Scrollbar(log_container, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        for cat, color in LOG_COLORS.items():
            self.log_text.tag_configure(f"tag_{cat}", foreground=color)
        self.log("Dashboard started - click Connect to link to hub", LOG_SYS)

    # ======================= Logging =======================
    def log(self, msg, cat=LOG_SYS):
        ts = time.strftime("%H:%M:%S")
        self.log_entries.append((ts, cat, msg))
        if self.log_filter.get() == "ALL" or self.log_filter.get() == cat:
            self._insert_log_entry(ts, cat, msg)

    def _insert_log_entry(self, ts, cat, msg):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{ts}] [{cat}] {msg}\n", f"tag_{cat}")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def set_log_filter(self, category):
        self.log_filter.set(category)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for ts, cat, msg in self.log_entries:
            if category == "ALL" or cat == category:
                self._insert_log_entry(ts, cat, msg)
        self.log_text.configure(state="disabled")

    def clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def save_log(self):
        filename = filedialog.asksaveasfilename(defaultextension=".log",
            initialfile=f"haptiband_{time.strftime('%Y%m%d_%H%M%S')}.log",
            filetypes=[("Log files", "*.log"), ("All files", "*.*")])
        if filename:
            with open(filename, "w") as f:
                for ts, cat, msg in self.log_entries:
                    f.write(f"[{ts}] [{cat}] {msg}\n")
            self.log(f"Log saved to {filename}", LOG_SYS)

    # ======================= Connection =======================
    def connect_to_hub(self):
        ip = self.ip_entry.get().strip()
        port = int(self.port_entry.get().strip())
        self.connect_btn.configure(state="disabled")
        self.auto_reconnect = True
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.log(f"Connecting to {ip}:{port}...", LOG_SYS)
        def worker():
            sock = connect(ip, port)
            if sock: self.root.after(0, lambda: self.on_connected(sock))
            else: self.root.after(0, self.on_connection_failed)
        threading.Thread(target=worker, daemon=True).start()

    def on_connected(self, sock):
        self.sock = sock
        self.connected = True
        self.connect_time = time.time()
        self.shutdown_event.clear()
        self.reconnecting = False
        self.reconnect_delay = RECONNECT_DELAY_INITIAL
        self.connect_btn.configure(state="disabled")
        self.disconnect_btn.configure(state="normal")
        self.status_canvas.itemconfig(self.status_indicator, fill="#00CC00", outline="#009900")
        self.status_label.config(text="Connected")
        self.log("Connected to hub", LOG_SYS)
        self.start_gps_listener()
        if self.ntrip_enabled.get(): self.start_ntrip_relay()

    def on_connection_failed(self):
        self.connect_btn.configure(state="normal")
        self.log("Connection failed", LOG_ERR)

    def disconnect_from_hub(self):
        self.log("Disconnecting...", LOG_SYS)
        self.auto_reconnect = False
        self.reconnecting = False
        self.connected = False
        self.shutdown_event.set()
        self.stop_ntrip_relay()
        self.stop_gps_listener()
        with self.sock_lock:
            if self.sock:
                try: self.sock.close()
                except Exception: pass
                self.sock = None
        self.connect_time = None
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.status_canvas.itemconfig(self.status_indicator, fill="red", outline="darkred")
        self.status_label.config(text="Disconnected")
        self._reset_hub_status()
        self.log("Disconnected", LOG_SYS)

    def _handle_socket_death(self, error):
        if not self.connected: return
        self.connected = False
        self.connect_time = None
        with self.sock_lock:
            if self.sock:
                try: self.sock.close()
                except Exception: pass
                self.sock = None
        self.shutdown_event.set()
        self.root.after(0, lambda: self._on_socket_death_ui(str(error)))

    def _on_socket_death_ui(self, error_msg):
        self.log(f"Connection lost: {error_msg}", LOG_ERR)
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.status_canvas.itemconfig(self.status_indicator, fill="red", outline="darkred")
        self.status_label.config(text="Disconnected (lost)")
        self._reset_hub_status()
        if self.auto_reconnect and not self.reconnecting:
            self.reconnecting = True
            delay = self.reconnect_delay
            self.log(f"Auto-reconnecting in {delay}s...", LOG_SYS)
            self.status_label.config(text=f"Reconnecting in {delay}s...")
            self.status_canvas.itemconfig(self.status_indicator, fill="orange", outline="#CC8800")
            self.root.after(delay * 1000, self._do_auto_reconnect)

    def _do_auto_reconnect(self):
        if not self.auto_reconnect or self.connected: self.reconnecting = False; return
        ip = self.ip_entry.get().strip()
        port = int(self.port_entry.get().strip())
        self.log(f"Attempting reconnect to {ip}:{port}...", LOG_SYS)
        self.status_label.config(text="Reconnecting...")
        def worker():
            sock = connect(ip, port, retries=1)
            if sock:
                self.reconnect_delay = RECONNECT_DELAY_INITIAL
                self.reconnecting = False
                self.root.after(0, lambda: self.on_connected(sock))
            else:
                self.reconnect_delay = min(self.reconnect_delay * 2, RECONNECT_DELAY_MAX)
                delay = self.reconnect_delay
                self.root.after(0, lambda: self.log(f"Reconnect failed, retrying in {delay}s...", LOG_ERR))
                self.root.after(0, lambda: self.status_label.config(text=f"Reconnecting in {delay}s..."))
                self.root.after(delay * 1000, self._do_auto_reconnect)
        threading.Thread(target=worker, daemon=True).start()

    def _reset_hub_status(self):
        self.hub_lat = self.hub_lon = self.hub_heading = None
        self.hub_fix_quality = 0
        self.hub_last_update = None
        self.fix_canvas.itemconfig(self.fix_dot, fill="#FF4444")
        self.fix_label.config(text="No Fix", foreground="#FF4444")
        self.hub_lat_label.config(text="Lat: --")
        self.hub_lon_label.config(text="Lon: --")
        self.hub_heading_label.config(text="Heading: --")
        self.last_update_label.config(text="Last update: --", foreground="gray")
        self.listener_status.config(text="Hub GPS Listener: Inactive", foreground="gray")

    # ======================= Periodic Status =======================
    def update_status(self):
        now = time.time()
        if self.connected and self.connect_time:
            d = int(now - self.connect_time); m, s = divmod(d, 60)
            self.status_label.config(text=f"Connected ({m}m {s}s)")
        if self.hub_last_update:
            age = int(now - self.hub_last_update)
            color = "green" if age < 10 else "orange" if age < 30 else "red"
            self.last_update_label.config(text=f"Last update: {age}s ago", foreground=color)
        with self.rtcm_lock: stats = self.rtcm_stats.copy()
        self.ntrip_stats_label.config(text=f"{stats['packets_sent']} pkts, {stats['bytes_sent']:,} bytes relayed")
        if stats.get("last_send_time"):
            self.ntrip_last_label.config(text=f"Last RTCM: {int(now - stats['last_send_time'])}s ago")
        err = stats.get("errors", 0)
        self.ntrip_error_label.config(text=f"Errors: {err}", foreground="red" if err > 0 else "gray")
        if self.hub_rtcm_stats:
            hs = self.hub_rtcm_stats
            to_gps = hs.get("to_gps", 0)
            self.hub_rtcm_label.config(text=f"{hs.get('msgs',0)} msgs | {to_gps:,} bytes -> GPS",
                                        foreground="green" if to_gps > 0 else "red")
        self.update_grid()
        self.root.after(1000, self.update_status)

    # ======================= GPS Listener =======================
    def start_gps_listener(self):
        if self.gps_listener_running: return
        self.gps_listener_running = True
        def listener():
            buffer = ""
            while not self.shutdown_event.is_set() and self.connected:
                try:
                    with self.sock_lock: sock = self.sock
                    if not sock: break
                    ready, _, _ = select.select([sock], [], [], 0.5)
                    if not ready: continue
                    data = sock.recv(1024)
                    if not data: self._handle_socket_death("Connection closed by hub"); break
                    buffer += data.decode(errors="ignore")
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line: continue
                        if line.startswith("GPS:"): self.root.after(0, lambda d=line: self.process_gps_data(d))
                        elif line.startswith("HUB:OK"): self.hub_last_update = time.time(); self.root.after(0, lambda d=line: self._process_hub_heartbeat(d))
                        elif line.startswith("HB:"): self.root.after(0, lambda d=line: self.process_headband_status(d))
                        elif line == "OK": pass
                except (BrokenPipeError, ConnectionResetError, OSError) as e: self._handle_socket_death(e); break
                except Exception: break
            self.gps_listener_running = False
            self.root.after(0, lambda: self.listener_status.config(text="Hub GPS Listener: Inactive", foreground="gray"))
        self.gps_listener_thread = threading.Thread(target=listener, daemon=True)
        self.gps_listener_thread.start()
        self.listener_status.config(text="Hub GPS Listener: Active", foreground="green")
        self.log("Listening for hub GPS data", LOG_SYS)

    def stop_gps_listener(self):
        self.shutdown_event.set()
        if self.gps_listener_thread: self.gps_listener_thread.join(timeout=1.0)
        self.gps_listener_running = False

    def process_gps_data(self, data):
        try:
            gps_start = data.find("GPS:") + 4
            imu_marker = data.find("|IMU:")
            gps = data[gps_start:imu_marker]
            imu_start = imu_marker + 5
            fix_marker = data.find("|FIX:", imu_start)
            if fix_marker >= 0: imu = data[imu_start:fix_marker]; fix_val = int(data[fix_marker + 5:])
            else: imu = data[imu_start:]; fix_val = -1
            lat_str, lon_str = gps.split(",")
            lat_val, lon_val = float(lat_str), float(lon_str)
            heading_val = int(float(imu))
            if lat_val == 0.0 and lon_val == 0.0:
                self.hub_fix_quality = 0; self._update_fix_display()
                self.hub_lat_label.config(text="Lat: --"); self.hub_lon_label.config(text="Lon: --"); self.hub_heading_label.config(text="Heading: --")
                self.log("Hub has no GPS fix yet (sent 0,0)", LOG_GPS); return
            self.hub_lat, self.hub_lon, self.hub_heading = lat_val, lon_val, heading_val
            self.hub_fix_quality = fix_val
            self.hub_last_update = time.time()
            self._update_fix_display()
            self.hub_lat_label.config(text=f"Lat: {lat_val:.8f}")
            self.hub_lon_label.config(text=f"Lon: {lon_val:.8f}")
            self.hub_heading_label.config(text=f"Heading: {heading_val}\u00b0")
            self.draw_heading_arrow(heading_val)
            spacing = self.spacing_var.get()
            self.column_positions = calculate_column_positions(lat_val, lon_val, heading_val, spacing)
            sides = ["Far L", "Left", "Center", "Right", "Far R"]
            for i, ((lat, lon), _) in enumerate(self.column_positions):
                self.column_labels[i].config(text=f"Col {i+1} ({sides[i]}): {lat:.8f}, {lon:.8f}")
            fix_str = FIX_QUALITY.get(fix_val, ("Unknown",))[0] if fix_val >= 0 else "Unknown"
            self.log(f"Hub position: {lat_val:.8f}, {lon_val:.8f} heading {heading_val}\u00b0 | Fix: {fix_str}", LOG_GPS)
            if self.auto_relay.get(): self.send_gps_update()
        except Exception as e: self.log(f"GPS parse error: {e}", LOG_ERR)

    def _update_fix_display(self):
        q = self.hub_fix_quality
        label, color = FIX_QUALITY.get(q, ("Unknown", "#888888"))
        if q == -1: label, color = "Unknown", "#888888"
        self.fix_canvas.itemconfig(self.fix_dot, fill=color)
        self.fix_label.config(text=label, foreground=color)

    # ======================= Hub Heartbeat & Headband Status =======================
    def _process_hub_heartbeat(self, data):
        try:
            rtcm_marker = data.find("|RTCM:")
            if rtcm_marker >= 0:
                parts = data[rtcm_marker + 6:].split(",")
                if len(parts) >= 4:
                    self.hub_rtcm_stats = {"msgs": int(parts[0]), "from_laptop": int(parts[1]),
                                           "to_gps": int(parts[2]), "to_espnow": int(parts[3])}
        except Exception: pass

    def process_headband_status(self, data):
        try:
            content = data[3:]
            semi, colon = content.index(";"), content.index(":")
            row, col = int(content[:semi]), int(content[semi+1:colon])
            rest = content[colon+1:]
            rtcm_msgs = rtcm_bytes = 0
            rtcm_marker = rest.find("|RTCM:")
            if rtcm_marker >= 0:
                rp = rest[rtcm_marker+6:].split(",")
                rest = rest[:rtcm_marker]
                if len(rp) >= 2: rtcm_msgs, rtcm_bytes = int(rp[0]), int(rp[1])
            bar1 = rest.index("|"); gps_part = rest[:bar1]; after = rest[bar1+1:]
            bar2 = after.index("|"); heading, fix = int(after[:bar2]), int(after[bar2+1:])
            lat_s, lon_s = gps_part.split(","); lat, lon = float(lat_s), float(lon_s)
            self.headband_statuses[(row, col)] = {"lat": lat, "lon": lon, "heading": heading, "fix": fix,
                                                   "last_update": time.time(), "rtcm_msgs": rtcm_msgs, "rtcm_bytes": rtcm_bytes}
            fix_label = FIX_QUALITY.get(fix, ("?",))[0]
            ri = f" | RTK corrections: {rtcm_msgs} msgs, {rtcm_bytes} bytes to GPS" if rtcm_msgs > 0 else ""
            if lat == 0.0 and lon == 0.0: self.log(f"Headband R{row} C{col}: no GPS fix{ri}", LOG_HB)
            else: self.log(f"Headband R{row} C{col}: {lat:.6f},{lon:.6f} heading {heading}\u00b0 | {fix_label}{ri}", LOG_HB)
        except Exception as e: self.log(f"Failed to parse headband status: {e}", LOG_ERR)

    # ======================= NTRIP Relay =======================
    def start_ntrip_relay(self):
        if not self.ntrip_enabled.get(): return
        if self.ntrip_thread and self.ntrip_thread.is_alive(): return
        self.ntrip_client = NTRIPClient(caster=NTRIP_CASTER, port=NTRIP_PORT, mountpoint=NTRIP_MOUNTPOINT,
                                         username=NTRIP_USER, password=NTRIP_PASS)
        self.ntrip_thread = threading.Thread(target=self._ntrip_relay_loop, daemon=True)
        self.ntrip_thread.start()
        self.root.after(0, lambda: self.log("RTK correction relay started", LOG_RTCM))
        self.root.after(0, lambda: self.ntrip_status_label.config(text="NTRIP: Connecting...", foreground="orange"))

    def stop_ntrip_relay(self):
        if self.ntrip_client: self.ntrip_client.stop_stream(); self.ntrip_client.disconnect(); self.ntrip_client = None
        if self.ntrip_thread: self.ntrip_thread.join(timeout=2.0); self.ntrip_thread = None
        self.root.after(0, lambda: self.ntrip_status_label.config(text="NTRIP: Inactive", foreground="gray"))

    def _ntrip_relay_loop(self):
        client = self.ntrip_client
        if not client: return
        retry_delay = 5
        while not self.shutdown_event.is_set():
            if client.connect(): break
            err = getattr(client, 'last_error', None) or 'unknown'
            self.root.after(0, lambda d=retry_delay, e=err: self.log(f"RTK caster connection failed ({e}), retrying in {d}s...", LOG_RTCM))
            for _ in range(retry_delay):
                if self.shutdown_event.is_set(): return
                time.sleep(1)
            retry_delay = min(retry_delay * 2, 60)
        if self.shutdown_event.is_set(): return
        self.root.after(0, lambda: self.log("Connected to RTK caster, relaying corrections to hub", LOG_RTCM))
        self.root.after(0, lambda: self.ntrip_status_label.config(text="NTRIP: Active", foreground="green"))
        ntrip_backoff = 5
        while not self.shutdown_event.is_set() and self.connected:
            if not client.connected:
                self.root.after(0, lambda: self.ntrip_status_label.config(text="NTRIP: Reconnecting...", foreground="orange"))
                if client.connect():
                    ntrip_backoff = 5
                    self.root.after(0, lambda: self.log("Reconnected to RTK caster", LOG_RTCM))
                    self.root.after(0, lambda: self.ntrip_status_label.config(text="NTRIP: Active", foreground="green"))
                else:
                    err = getattr(client, 'last_error', None) or 'unknown'
                    delay = ntrip_backoff
                    self.root.after(0, lambda e=err, d=delay: self.log(f"RTK caster reconnect failed ({e}), retrying in {d}s...", LOG_RTCM))
                    for _ in range(ntrip_backoff):
                        if self.shutdown_event.is_set() or not self.connected: break
                        time.sleep(1)
                    ntrip_backoff = min(ntrip_backoff * 2, 60)
                    continue
            data = client.read_data(timeout=1.0)
            if data:
                if not self._send_rtcm(data): break
        client.disconnect()
        self.root.after(0, lambda: self.log("RTK correction relay stopped", LOG_RTCM))

    def _send_rtcm(self, data):
        if not self.connected: return False
        try:
            encoded = base64.b64encode(data).decode("ascii")
            msg = f"RTCM:{encoded}\n".encode()
            with self.sock_lock:
                if not self.sock: return False
                self.sock.sendall(msg)
            with self.rtcm_lock:
                self.rtcm_stats["bytes_sent"] += len(data)
                self.rtcm_stats["packets_sent"] += 1
                self.rtcm_stats["last_send_time"] = time.time()
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            with self.rtcm_lock: self.rtcm_stats["errors"] += 1
            self.root.after(0, lambda err=e: self.log(f"Failed to send RTK data to hub: {err}", LOG_ERR))
            self._handle_socket_death(e)
            return False

    # ======================= GPS Relay =======================
    def send_gps_update(self):
        if self.hub_lat is None: self.log("Can't send positions - no hub GPS data yet", LOG_GPS); return
        if not self.selected_rows: self.log("Can't send - no rows selected", LOG_MOTOR); return
        spacing = self.spacing_var.get()
        positions = calculate_column_positions(self.hub_lat, self.hub_lon, self.hub_heading, spacing)
        def worker():
            sent = 0
            for row in sorted(self.selected_rows):
                for col in range(1, 6):
                    (lat, lon), heading = positions[col - 1]
                    try:
                        with self.sock_lock:
                            if not self.sock: return
                            send_message(f"{row};{col}:{lat},{lon}|{heading}", self.sock, timeout=0.5)
                        sent += 1
                    except Exception as e: self.root.after(0, lambda err=e: self.log(f"Send error: {err}", LOG_ERR))
            self.root.after(0, lambda c=sent: self.log(f"Sent formation positions to {c} headbands", LOG_MOTOR))
        threading.Thread(target=worker, daemon=True).start()

    # ======================= Motor Control =======================
    def update_motor_diagram(self, pin, active):
        if pin in self.motor_indicators:
            self.motor_canvas.itemconfig(self.motor_indicators[pin], fill="#FF5722" if active else "#E0E0E0")

    def run_sequence(self, sequence):
        if not self.connected: self.log("Not connected", LOG_ERR); return
        if not self.selected_rows: self.log("Can't send - no rows selected", LOG_MOTOR); return
        def worker():
            for row, pin, state, delay in sequence:
                try:
                    with self.sock_lock:
                        if not self.sock: return
                        send_message(f"{row};{pin}:{state}", self.sock, timeout=0.5)
                    self.root.after(0, lambda p=pin, s=state: self.update_motor_diagram(p, s == 1))
                except Exception as e: self.root.after(0, lambda err=e: self.log(f"Send error: {err}", LOG_ERR))
                if delay > 0: time.sleep(delay)
            self.root.after(300, lambda: [self.update_motor_diagram(p, False) for p in [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]])
        threading.Thread(target=worker, daemon=True).start()

    def build_sequence_for_rows(self, base):
        seq = []
        for row in sorted(self.selected_rows):
            for pin, state, delay in base: seq.append((row, pin, state, delay))
        return seq

    def _rows_str(self): return ", ".join(map(str, sorted(self.selected_rows)))

    def forward(self): self.log(f"Buzzing FRONT motor on row(s) {self._rows_str()}", LOG_MOTOR); self.run_sequence(self.build_sequence_for_rows([(PIN_FRONT,1,0.10),(PIN_FRONT,0,0.05),(PIN_FRONT,1,0.10),(PIN_FRONT,0,0.00)]))
    def left(self): self.log(f"Buzzing LEFT motor on row(s) {self._rows_str()}", LOG_MOTOR); self.run_sequence(self.build_sequence_for_rows([(PIN_LEFT,1,0.10),(PIN_LEFT,0,0.05),(PIN_LEFT,1,0.10),(PIN_LEFT,0,0.00)]))
    def right(self): self.log(f"Buzzing RIGHT motor on row(s) {self._rows_str()}", LOG_MOTOR); self.run_sequence(self.build_sequence_for_rows([(PIN_RIGHT,1,0.10),(PIN_RIGHT,0,0.05),(PIN_RIGHT,1,0.10),(PIN_RIGHT,0,0.00)]))
    def back(self): self.log(f"Buzzing BACK motor on row(s) {self._rows_str()}", LOG_MOTOR); self.run_sequence(self.build_sequence_for_rows([(PIN_BACK,1,0.10),(PIN_BACK,0,0.05),(PIN_BACK,1,0.10),(PIN_BACK,0,0.00)]))
    def rotate_left(self): self.log(f"Turn-left pattern on row(s) {self._rows_str()}", LOG_MOTOR); self.run_sequence(self.build_sequence_for_rows([(PIN_FRONT,1,0.10),(PIN_FRONT,0,0.05),(PIN_LEFT,1,0.10),(PIN_LEFT,0,0.00)]))
    def rotate_right(self): self.log(f"Turn-right pattern on row(s) {self._rows_str()}", LOG_MOTOR); self.run_sequence(self.build_sequence_for_rows([(PIN_FRONT,1,0.10),(PIN_FRONT,0,0.05),(PIN_RIGHT,1,0.10),(PIN_RIGHT,0,0.00)]))

    def stop_all(self):
        self.log(f"All-stop buzz on row(s) {self._rows_str()}", LOG_MOTOR)
        self.run_sequence(self.build_sequence_for_rows([
            (PIN_LEFT,1,0.00),(PIN_FRONT,1,0.00),(PIN_RIGHT,1,0.00),(PIN_BACK,1,0.10),
            (PIN_LEFT,0,0.00),(PIN_FRONT,0,0.00),(PIN_RIGHT,0,0.00),(PIN_BACK,0,0.05),
            (PIN_LEFT,1,0.00),(PIN_FRONT,1,0.00),(PIN_RIGHT,1,0.00),(PIN_BACK,1,0.10),
            (PIN_LEFT,0,0.00),(PIN_FRONT,0,0.00),(PIN_RIGHT,0,0.00),(PIN_BACK,0,0.00)]))

    def start_seq(self):
        self.log(f"Start-march pattern on row(s) {self._rows_str()}", LOG_MOTOR)
        self.run_sequence(self.build_sequence_for_rows([
            (PIN_LEFT,1,0.00),(PIN_RIGHT,1,0.10),(PIN_LEFT,0,0.00),(PIN_RIGHT,0,0.05),
            (PIN_LEFT,1,0.00),(PIN_RIGHT,1,0.10),(PIN_LEFT,0,0.00),(PIN_RIGHT,0,0.10),
            (PIN_FRONT,1,0.10),(PIN_FRONT,0,0.00)]))

    def emergency_stop(self):
        self.log("EMERGENCY STOP - All rows, all motors OFF", LOG_ERR)
        if not self.connected: return
        def worker():
            for row in range(1, 6):
                for pin in [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]:
                    try:
                        with self.sock_lock:
                            if not self.sock: return
                            send_message(f"{row};{pin}:0", self.sock, timeout=0.3)
                    except Exception: pass
            self.root.after(0, lambda: [self.update_motor_diagram(p, False) for p in [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]])
        threading.Thread(target=worker, daemon=True).start()

    # ======================= Keyboard =======================
    def on_key(self, event):
        if event.char in "12345":
            row = int(event.char)
            if event.state & 0x4:
                self.selected_rows.symmetric_difference_update({row})
                if not self.selected_rows: self.selected_rows = {row}
            else: self.selected_rows = {row}
            self.update_grid(); return
        if not self.connected: return
        key = event.char.lower()
        actions = {"w": self.forward, "a": self.left, "s": self.back, "d": self.right,
                   "z": self.rotate_left, "x": self.rotate_right, "e": self.start_seq, "q": self.stop_all}
        if key in actions: actions[key]()

if __name__ == "__main__":
    root = tk.Tk()
    app = HaptiBandApp(root)
    root.mainloop()
