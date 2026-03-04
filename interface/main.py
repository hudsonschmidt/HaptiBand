import tkinter as tk
from tkinter import ttk, messagebox
import socket
import threading
import time
import math
import select

# 5 - Left Temple
# 18 - Forehead
# 19 - Right Temple
# 23 - Back of head

# ----------------------- Constants -----------------------
HOST_DEFAULT = "192.168.4.1"
PORT_DEFAULT = 80
FEET_PER_DEGREE_LAT = 364567.2

# Motor pin mappings
PIN_LEFT = 5
PIN_FRONT = 18
PIN_RIGHT = 19
PIN_BACK = 23

# Grid dimensions
GRID_ROWS = 5
GRID_COLS = 5


# ----------------------- GPS Functions -----------------------
def feet_to_degrees(feet, latitude):
    """Convert feet to lat/lon degrees at given latitude."""
    lat_degrees = feet / FEET_PER_DEGREE_LAT
    lon_degrees = feet / (FEET_PER_DEGREE_LAT * math.cos(math.radians(latitude)))
    return lat_degrees, lon_degrees


def calculate_column_positions(lat, lon, heading_deg, spacing_feet=3.0):
    """
    Calculate GPS positions for all 5 columns based on hub position.
    Returns list of ((lat, lon), heading) tuples for columns 1-5.
    """
    theta = math.radians(heading_deg)
    offset_multipliers = [-2.0, -1.0, 0.0, 1.0, 2.0]  # columns 1-5
    result = []

    for mult in offset_multipliers:
        distance_feet = mult * spacing_feet
        lat_offset, lon_offset = feet_to_degrees(abs(distance_feet), lat)

        if distance_feet >= 0:
            new_lat = lat - (lat_offset * math.cos(theta))
            new_lon = lon + (lon_offset * math.sin(theta))
        else:
            new_lat = lat + (lat_offset * math.cos(theta))
            new_lon = lon - (lon_offset * math.sin(theta))

        result.append(((new_lat, new_lon), heading_deg))

    return result


# ----------------------- Networking -----------------------
def send_message(msg: str, sock_obj: socket.socket, timeout=1.0) -> str:
    """Send message and return reply."""
    full = f"{msg}\n".encode()
    try:
        sock_obj.sendall(full)
        sock_obj.settimeout(timeout)
        try:
            reply = sock_obj.recv(256).decode(errors="ignore").strip()
            return reply
        except socket.timeout:
            return None
    except OSError as e:
        raise e


def connect(ip: str, port: int, retries=3) -> socket.socket:
    """Connect to hub with retries."""
    last = None
    for _ in range(1, retries + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(3.0)
            s.connect((ip, port))
            s.settimeout(None)
            return s
        except OSError as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"Unable to reach hub at {ip}:{port} ({last})")


# ======================= Main Application =======================
class HaptiBandApp:
    def __init__(self, root):
        self.root = root
        self.root.title("HaptiBand Control")
        self.root.geometry("1200x800")
        self.root.minsize(1000, 700)

        # State
        self.sock = None
        self.sock_lock = threading.Lock()
        self.selected_rows = {1}  # Default to row 1
        self.connected = False
        self.connect_time = None

        # GPS state
        self.hub_lat = None
        self.hub_lon = None
        self.hub_heading = None
        self.gps_listener_running = False
        self.gps_listener_thread = None
        self.shutdown_event = threading.Event()
        self.auto_relay = tk.BooleanVar(value=False)
        self.spacing_var = tk.DoubleVar(value=3.0)

        # Grid cell references (shared between tabs)
        self.grid_cells = {}
        self.gps_grid_cells = {}

        # Control buttons to enable/disable
        self.control_widgets = []

        # Build UI
        self.build_ui()

        # Keyboard bindings
        self.root.bind("<Key>", self.on_key)
        self.root.bind("<Escape>", lambda _: self.emergency_stop())
        self.root.bind("<Return>", lambda _: self.connect_to_hub() if not self.connected else None)

        # Update status periodically
        self.update_status()

    def build_ui(self):
        """Build the main UI layout."""
        # Connection bar
        self.build_connection_bar()

        # Notebook for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(0, 5))

        # Manual Control Tab
        self.manual_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.manual_frame, text="Manual Control")
        self.build_manual_tab()

        # GPS Mode Tab
        self.gps_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.gps_frame, text="GPS Mode")
        self.build_gps_tab()

        # Log panel at bottom
        self.build_log_panel()

        # Initially disable controls
        self.set_controls_enabled(False)

    def build_connection_bar(self):
        """Build the connection status bar."""
        conn_frame = ttk.Frame(self.root, padding=10)
        conn_frame.pack(fill="x")

        # IP Entry
        ttk.Label(conn_frame, text="IP:").pack(side="left")
        self.ip_var = tk.StringVar(value=HOST_DEFAULT)
        self.ip_entry = ttk.Entry(conn_frame, textvariable=self.ip_var, width=16)
        self.ip_entry.pack(side="left", padx=(0, 10))

        # Port Entry
        ttk.Label(conn_frame, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=str(PORT_DEFAULT))
        self.port_entry = ttk.Entry(conn_frame, textvariable=self.port_var, width=6)
        self.port_entry.pack(side="left", padx=(0, 10))

        # Connect/Disconnect buttons
        self.connect_btn = ttk.Button(conn_frame, text="Connect", command=self.connect_to_hub)
        self.connect_btn.pack(side="left", padx=(0, 5))

        self.disconnect_btn = ttk.Button(conn_frame, text="Disconnect", command=self.disconnect_from_hub, state="disabled")
        self.disconnect_btn.pack(side="left", padx=(0, 15))

        # Status indicator
        self.status_canvas = tk.Canvas(conn_frame, width=20, height=20, highlightthickness=0)
        self.status_canvas.pack(side="left", padx=(0, 5))
        self.status_indicator = self.status_canvas.create_oval(2, 2, 18, 18, fill="red", outline="darkred")

        self.status_label = ttk.Label(conn_frame, text="Disconnected")
        self.status_label.pack(side="left")

        # Emergency Stop button (always enabled)
        self.emergency_btn = tk.Button(
            conn_frame, text="EMERGENCY STOP (Esc)", bg="red", fg="white",
            font=("TkDefaultFont", 10, "bold"), command=self.emergency_stop,
            activebackground="darkred", activeforeground="white"
        )
        self.emergency_btn.pack(side="right", padx=10)

    def build_manual_tab(self):
        """Build the manual control tab."""
        # Split into left and right panels
        left_panel = ttk.Frame(self.manual_frame)
        left_panel.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        right_panel = ttk.Frame(self.manual_frame)
        right_panel.pack(side="right", fill="both", padx=10, pady=10)

        # Left: Formation Grid
        self.build_formation_grid(left_panel, is_gps_tab=False)

        # Right: Headband diagram and controls
        self.build_headband_diagram(right_panel)
        self.build_wasd_controls(right_panel)

    def build_formation_grid(self, parent, is_gps_tab=False):
        """Build the 5x5 formation grid."""
        grid_frame = ttk.LabelFrame(parent, text="Formation - Select Target Rows", padding=10)
        grid_frame.pack(fill="both", expand=True)

        # Info label
        info_text = "Click row number to select. Ctrl+click for multi-select."
        ttk.Label(grid_frame, text=info_text, font=("TkDefaultFont", 9, "italic"), foreground="gray").pack(anchor="w")

        # Grid canvas
        canvas = tk.Canvas(grid_frame, width=400, height=380, bg="white", highlightthickness=1, highlightbackground="gray")
        canvas.pack(pady=10)

        # Store reference
        if is_gps_tab:
            self.gps_grid_canvas = canvas
        else:
            self.grid_canvas = canvas

        # Draw grid
        self.draw_formation_grid(canvas, is_gps_tab)

        # Selection summary
        summary_frame = ttk.Frame(grid_frame)
        summary_frame.pack(fill="x", pady=5)

        if is_gps_tab:
            self.gps_selection_label = ttk.Label(summary_frame, text="Selected: Row 1", font=("TkDefaultFont", 10, "bold"))
            self.gps_selection_label.pack(side="left")
        else:
            self.selection_label = ttk.Label(summary_frame, text="Selected: Row 1", font=("TkDefaultFont", 10, "bold"))
            self.selection_label.pack(side="left")

        # Quick select buttons
        quick_frame = ttk.Frame(grid_frame)
        quick_frame.pack(fill="x", pady=5)

        btn1 = ttk.Button(quick_frame, text="Row 1 (Front)", width=12, command=lambda: self.select_single_row(1))
        btn1.pack(side="left", padx=2)
        self.control_widgets.append(btn1)

        btn2 = ttk.Button(quick_frame, text="All Rows", width=10, command=self.select_all_rows)
        btn2.pack(side="left", padx=2)
        self.control_widgets.append(btn2)

        btn3 = ttk.Button(quick_frame, text="Clear", width=8, command=self.clear_selection)
        btn3.pack(side="left", padx=2)
        self.control_widgets.append(btn3)

    def draw_formation_grid(self, canvas, is_gps_tab=False):
        """Draw the formation grid on canvas."""
        canvas.delete("all")
        cells = {}

        cell_width = 55
        cell_height = 55
        left_margin = 50  # Space for row labels
        top_margin = 40

        # Title showing parade direction
        canvas.create_text(225, 12, text="<-- Parade Direction (Hub is ahead)",
                          font=("TkDefaultFont", 9, "italic"), fill="gray")

        # Column headers
        for col in range(1, GRID_COLS + 1):
            x = left_margin + (col - 1) * cell_width + cell_width // 2
            canvas.create_text(x, top_margin - 15, text=f"Col {col}", font=("TkDefaultFont", 9))

        # Draw rows with labels
        for row in range(1, GRID_ROWS + 1):
            y_center = top_margin + (row - 1) * cell_height + cell_height // 2

            # Row label (clickable)
            row_label = f"Row {row}"
            if row == 1:
                row_label = "Row 1 (Front)"
            elif row == 5:
                row_label = "Row 5 (Back)"

            label_id = canvas.create_text(25, y_center, text=f"{row}", font=("TkDefaultFont", 11, "bold"),
                                         fill="blue" if row in self.selected_rows else "black")
            canvas.tag_bind(label_id, "<Button-1>", lambda e, r=row: self.on_row_label_click(r, e))

            # Draw cells for this row
            for col in range(1, GRID_COLS + 1):
                x1 = left_margin + (col - 1) * cell_width
                y1 = top_margin + (row - 1) * cell_height
                x2 = x1 + cell_width - 4
                y2 = y1 + cell_height - 4

                # Color based on row selection
                if row in self.selected_rows:
                    fill = "#90EE90"  # Light green
                else:
                    fill = "#E0E0E0"  # Gray

                cell = canvas.create_rectangle(x1, y1, x2, y2, fill=fill, outline="black", width=1)
                text_id = canvas.create_text((x1 + x2) / 2, (y1 + y2) / 2,
                                            text=f"{row};{col}", font=("TkDefaultFont", 9))

                cells[(row, col)] = (cell, text_id, label_id)

                # Clicking a cell selects that row
                canvas.tag_bind(cell, "<Button-1>", lambda e, r=row: self.on_row_label_click(r, e))
                canvas.tag_bind(text_id, "<Button-1>", lambda e, r=row: self.on_row_label_click(r, e))

        if is_gps_tab:
            self.gps_grid_cells = cells
        else:
            self.grid_cells = cells

    def on_row_label_click(self, row, event):
        """Handle row selection click."""
        ctrl_held = event.state & 0x4  # Ctrl key on most systems

        if ctrl_held:
            # Toggle this row
            if row in self.selected_rows:
                self.selected_rows.discard(row)
            else:
                self.selected_rows.add(row)
        else:
            # Select only this row
            self.selected_rows = {row}

        # Ensure at least one row is selected for safety
        if not self.selected_rows:
            self.selected_rows = {row}

        self.update_all_grids()

    def update_all_grids(self):
        """Update both grid displays and selection labels."""
        # Update manual tab grid
        if hasattr(self, 'grid_canvas') and self.grid_cells:
            self._update_grid(self.grid_canvas, self.grid_cells)

        # Update GPS tab grid
        if hasattr(self, 'gps_grid_canvas') and self.gps_grid_cells:
            self._update_grid(self.gps_grid_canvas, self.gps_grid_cells)

        # Update selection labels
        self._update_selection_labels()

    def _update_grid(self, canvas, cells):
        """Update a single grid's colors."""
        for (row, col), (cell, text_id, label_id) in cells.items():
            if row in self.selected_rows:
                canvas.itemconfig(cell, fill="#90EE90")
                canvas.itemconfig(label_id, fill="blue")
            else:
                canvas.itemconfig(cell, fill="#E0E0E0")
                canvas.itemconfig(label_id, fill="black")

    def _update_selection_labels(self):
        """Update the selection summary labels."""
        if not self.selected_rows:
            text = "Selected: None"
        elif len(self.selected_rows) == 5:
            text = "Selected: All Rows (1-5)"
        elif len(self.selected_rows) == 1:
            row = list(self.selected_rows)[0]
            text = f"Selected: Row {row}"
        else:
            rows = sorted(self.selected_rows)
            text = f"Selected: Rows {', '.join(map(str, rows))}"

        if hasattr(self, 'selection_label'):
            self.selection_label.config(text=text)
        if hasattr(self, 'gps_selection_label'):
            self.gps_selection_label.config(text=text)

    def select_single_row(self, row):
        """Select a single row."""
        self.selected_rows = {row}
        self.update_all_grids()

    def select_all_rows(self):
        """Select all rows."""
        self.selected_rows = set(range(1, 6))
        self.update_all_grids()

    def clear_selection(self):
        """Clear selection (defaults to row 1 for safety)."""
        self.selected_rows = {1}
        self.update_all_grids()
        self.log("Selection cleared, defaulting to Row 1")

    def build_headband_diagram(self, parent):
        """Build the headband motor visualization."""
        diagram_frame = ttk.LabelFrame(parent, text="Motor Feedback Preview", padding=10)
        diagram_frame.pack(fill="x", pady=(0, 10))

        self.motor_canvas = tk.Canvas(diagram_frame, width=200, height=180, bg="white",
                                      highlightthickness=1, highlightbackground="gray")
        self.motor_canvas.pack()

        # Draw head outline (top-down view)
        self.motor_canvas.create_oval(50, 30, 150, 130, outline="black", width=2)
        self.motor_canvas.create_text(100, 150, text="(Top-down view)", font=("TkDefaultFont", 8, "italic"), fill="gray")

        # Motor indicators with labels
        self.motor_indicators = {}

        # Front (forehead - top of circle)
        self.motor_indicators[PIN_FRONT] = self.motor_canvas.create_oval(90, 15, 110, 35, fill="gray", outline="black")
        self.motor_canvas.create_text(100, 8, text="Front", font=("TkDefaultFont", 8))

        # Left (left temple)
        self.motor_indicators[PIN_LEFT] = self.motor_canvas.create_oval(35, 70, 55, 90, fill="gray", outline="black")
        self.motor_canvas.create_text(30, 80, text="L", font=("TkDefaultFont", 9, "bold"), anchor="e")

        # Right (right temple)
        self.motor_indicators[PIN_RIGHT] = self.motor_canvas.create_oval(145, 70, 165, 90, fill="gray", outline="black")
        self.motor_canvas.create_text(170, 80, text="R", font=("TkDefaultFont", 9, "bold"), anchor="w")

        # Back (back of head)
        self.motor_indicators[PIN_BACK] = self.motor_canvas.create_oval(90, 125, 110, 145, fill="gray", outline="black")
        self.motor_canvas.create_text(100, 165, text="Back", font=("TkDefaultFont", 8))

    def update_motor_diagram(self, pin, state):
        """Update motor indicator color."""
        if pin in self.motor_indicators:
            color = "#00FF00" if state else "gray"
            self.motor_canvas.itemconfig(self.motor_indicators[pin], fill=color)

    def build_wasd_controls(self, parent):
        """Build WASD-style control buttons."""
        ctrl_frame = ttk.LabelFrame(parent, text="Haptic Commands", padding=10)
        ctrl_frame.pack(fill="x")

        # Direction label
        ttk.Label(ctrl_frame, text="Direction signals:", font=("TkDefaultFont", 9)).pack(anchor="w")

        # Top row - Forward (W)
        top_row = ttk.Frame(ctrl_frame)
        top_row.pack(pady=(5, 0))
        btn_w = ttk.Button(top_row, text="W - Forward", width=14, command=self.forward)
        btn_w.pack()
        self.control_widgets.append(btn_w)

        # Middle row - Left (A), Back (S), Right (D)
        mid_row = ttk.Frame(ctrl_frame)
        mid_row.pack(pady=5)

        btn_a = ttk.Button(mid_row, text="A - Left", width=10, command=self.left)
        btn_a.pack(side="left", padx=3)
        self.control_widgets.append(btn_a)

        btn_s = ttk.Button(mid_row, text="S - Back", width=10, command=self.back)
        btn_s.pack(side="left", padx=3)
        self.control_widgets.append(btn_s)

        btn_d = ttk.Button(mid_row, text="D - Right", width=10, command=self.right)
        btn_d.pack(side="left", padx=3)
        self.control_widgets.append(btn_d)

        # Separator
        ttk.Separator(ctrl_frame, orient="horizontal").pack(fill="x", pady=10)

        # Rotation label
        ttk.Label(ctrl_frame, text="Rotation signals:", font=("TkDefaultFont", 9)).pack(anchor="w")

        # Rotation row
        rot_row = ttk.Frame(ctrl_frame)
        rot_row.pack(pady=5)

        btn_z = ttk.Button(rot_row, text="Z - Rotate Left", width=14, command=self.rotate_left)
        btn_z.pack(side="left", padx=3)
        self.control_widgets.append(btn_z)

        btn_x = ttk.Button(rot_row, text="X - Rotate Right", width=14, command=self.rotate_right)
        btn_x.pack(side="left", padx=3)
        self.control_widgets.append(btn_x)

        # Separator
        ttk.Separator(ctrl_frame, orient="horizontal").pack(fill="x", pady=10)

        # Special commands
        ttk.Label(ctrl_frame, text="Sequences:", font=("TkDefaultFont", 9)).pack(anchor="w")

        seq_row = ttk.Frame(ctrl_frame)
        seq_row.pack(pady=5)

        btn_e = ttk.Button(seq_row, text="E - Start March", width=14, command=self.start_seq)
        btn_e.pack(side="left", padx=3)
        self.control_widgets.append(btn_e)

        btn_q = ttk.Button(seq_row, text="Q - All Stop", width=14, command=self.stop_all)
        btn_q.pack(side="left", padx=3)
        self.control_widgets.append(btn_q)

    def build_gps_tab(self):
        """Build the GPS mode tab."""
        # Main container with three columns
        main_container = ttk.Frame(self.gps_frame)
        main_container.pack(fill="both", expand=True, padx=10, pady=10)

        # Left: Formation Grid (reuse same selection)
        left_panel = ttk.Frame(main_container)
        left_panel.pack(side="left", fill="both", expand=True)
        self.build_formation_grid(left_panel, is_gps_tab=True)

        # Middle: GPS Settings
        middle_panel = ttk.Frame(main_container)
        middle_panel.pack(side="left", fill="both", padx=10)

        settings_frame = ttk.LabelFrame(middle_panel, text="GPS Settings", padding=10)
        settings_frame.pack(fill="x", pady=(0, 10))

        # Spacing slider
        ttk.Label(settings_frame, text="Column Spacing:").pack(anchor="w")
        spacing_frame = ttk.Frame(settings_frame)
        spacing_frame.pack(fill="x", pady=5)

        self.spacing_slider = ttk.Scale(spacing_frame, from_=1, to=10, variable=self.spacing_var,
                                        orient="horizontal", length=150)
        self.spacing_slider.pack(side="left")

        self.spacing_label = ttk.Label(spacing_frame, text="3.0 ft", width=6)
        self.spacing_label.pack(side="left", padx=(5, 0))

        self.spacing_var.trace_add("write", self.on_spacing_change)

        # Auto-relay toggle
        auto_check = ttk.Checkbutton(settings_frame, text="Auto-relay to headbands", variable=self.auto_relay)
        auto_check.pack(anchor="w", pady=10)
        self.control_widgets.append(auto_check)

        # Manual trigger button
        send_btn = ttk.Button(settings_frame, text="Send GPS Now", command=self.send_gps_update)
        send_btn.pack(fill="x", pady=5)
        self.control_widgets.append(send_btn)

        # Hub GPS/IMU Display
        gps_display_frame = ttk.LabelFrame(middle_panel, text="Hub Position (from Hub)", padding=10)
        gps_display_frame.pack(fill="x", pady=(0, 10))

        self.gps_status_label = ttk.Label(gps_display_frame, text="Waiting for data...",
                                          font=("TkDefaultFont", 9), foreground="gray")
        self.gps_status_label.pack(anchor="w")

        self.hub_lat_label = ttk.Label(gps_display_frame, text="Lat: --")
        self.hub_lat_label.pack(anchor="w")

        self.hub_lon_label = ttk.Label(gps_display_frame, text="Lon: --")
        self.hub_lon_label.pack(anchor="w")

        self.hub_heading_label = ttk.Label(gps_display_frame, text="Heading: --")
        self.hub_heading_label.pack(anchor="w")

        # GPS Listener status
        self.listener_status = ttk.Label(middle_panel, text="GPS Listener: Inactive", foreground="gray")
        self.listener_status.pack(anchor="w")

        # Right: Column Positions Preview
        right_panel = ttk.Frame(main_container)
        right_panel.pack(side="left", fill="both", expand=True)

        preview_frame = ttk.LabelFrame(right_panel, text="Calculated Column Targets", padding=10)
        preview_frame.pack(fill="both", expand=True)

        ttk.Label(preview_frame, text="Positions sent to each column:",
                  font=("TkDefaultFont", 9, "italic"), foreground="gray").pack(anchor="w")

        self.column_labels = []
        for i in range(1, 6):
            frame = ttk.Frame(preview_frame)
            frame.pack(fill="x", pady=3)

            offset = (i - 3) * 3  # Default 3ft spacing
            side = "center" if i == 3 else ("left" if i < 3 else "right")
            ttk.Label(frame, text=f"Col {i} ({side}):", width=12, font=("TkDefaultFont", 9, "bold")).pack(side="left")
            label = ttk.Label(frame, text="--", font=("TkDefaultFont", 9))
            label.pack(side="left", fill="x", expand=True)
            self.column_labels.append(label)

    def on_spacing_change(self, *_args):
        """Update spacing label when slider changes."""
        val = self.spacing_var.get()
        self.spacing_label.config(text=f"{val:.1f} ft")

    def build_log_panel(self):
        """Build the log output panel."""
        log_frame = ttk.LabelFrame(self.root, text="Activity Log", padding=6)
        log_frame.pack(fill="both", expand=False, padx=10, pady=(0, 10))

        # Scrollbar
        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")

        self.log_output = tk.Text(log_frame, height=6, state="disabled",
                                  yscrollcommand=scrollbar.set, font=("TkFixedFont", 9))
        self.log_output.pack(fill="both", expand=True)
        scrollbar.config(command=self.log_output.yview)

    def log(self, msg: str):
        """Add message to log."""
        timestamp = time.strftime("%H:%M:%S")
        self.log_output.configure(state="normal")
        self.log_output.insert("end", f"[{timestamp}] {msg}\n")
        self.log_output.see("end")
        self.log_output.configure(state="disabled")

    def set_controls_enabled(self, enabled: bool):
        """Enable or disable all control widgets."""
        state = "normal" if enabled else "disabled"
        for widget in self.control_widgets:
            try:
                widget.configure(state=state)
            except tk.TclError:
                pass  # Widget might not support state

    # ----------------------- Connection Methods -----------------------
    def connect_to_hub(self):
        """Connect to the hub in background thread."""
        if self.connected:
            return

        ip = self.ip_var.get().strip() or HOST_DEFAULT
        try:
            port = int(self.port_var.get())
        except ValueError:
            port = PORT_DEFAULT

        self.log(f"Connecting to {ip}:{port}...")
        self.connect_btn.configure(state="disabled")

        def worker():
            try:
                s = connect(ip, port)
                with self.sock_lock:
                    if self.sock:
                        try:
                            self.sock.close()
                        except Exception:
                            pass
                    self.sock = s

                self.connected = True
                self.connect_time = time.time()

                # Update UI on main thread
                self.root.after(0, self.on_connected)

                # Start GPS listener
                self.start_gps_listener()

            except Exception as e:
                self.root.after(0, lambda: self.on_connection_failed(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def on_connected(self):
        """Update UI after successful connection."""
        self.log("Connected successfully")
        self.disconnect_btn.configure(state="normal")
        self.status_canvas.itemconfig(self.status_indicator, fill="green", outline="darkgreen")
        self.status_label.config(text="Connected")
        self.set_controls_enabled(True)

    def on_connection_failed(self, error: str):
        """Handle connection failure."""
        self.log(f"Connection failed: {error}")
        messagebox.showerror("Connection Error", error)
        self.connect_btn.configure(state="normal")

    def disconnect_from_hub(self):
        """Disconnect from hub."""
        self.stop_gps_listener()

        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None

        self.connected = False
        self.connect_time = None
        self.log("Disconnected")

        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.status_canvas.itemconfig(self.status_indicator, fill="red", outline="darkred")
        self.status_label.config(text="Disconnected")
        self.set_controls_enabled(False)

    def update_status(self):
        """Update status indicator periodically."""
        if self.connected and self.connect_time:
            duration = int(time.time() - self.connect_time)
            mins, secs = divmod(duration, 60)
            self.status_label.config(text=f"Connected ({mins}m {secs}s)")

        self.root.after(1000, self.update_status)

    # ----------------------- GPS Listener -----------------------
    def start_gps_listener(self):
        """Start background GPS listener thread."""
        if self.gps_listener_running:
            return

        self.shutdown_event.clear()
        self.gps_listener_running = True

        def listener():
            while not self.shutdown_event.is_set():
                # Get socket reference under lock (quick operation)
                with self.sock_lock:
                    if not self.sock:
                        break
                    local_sock = self.sock

                try:
                    # select() is safe to call without lock - it's read-only
                    ready = select.select([local_sock], [], [], 0.2)
                    if ready[0]:
                        # Hold lock during recv to prevent conflicts with send
                        with self.sock_lock:
                            if not self.sock:
                                break
                            local_sock.settimeout(0.5)
                            data = local_sock.recv(256).decode(errors="ignore").strip()
                            local_sock.settimeout(None)
                        if data and "GPS:" in data and "|IMU:" in data:
                            self.root.after(0, lambda d=data: self.process_gps_data(d))
                except socket.timeout:
                    pass
                except Exception:
                    pass

                time.sleep(0.05)

            self.gps_listener_running = False
            self.root.after(0, lambda: self.listener_status.config(text="GPS Listener: Inactive", foreground="gray"))

        self.gps_listener_thread = threading.Thread(target=listener, daemon=True)
        self.gps_listener_thread.start()
        self.listener_status.config(text="GPS Listener: Active", foreground="green")
        self.log("GPS listener started")

    def stop_gps_listener(self):
        """Stop the GPS listener thread."""
        self.shutdown_event.set()
        if self.gps_listener_thread:
            self.gps_listener_thread.join(timeout=1.0)
        self.gps_listener_running = False

    def process_gps_data(self, data):
        """Process received GPS/IMU data from hub."""
        try:
            # Parse GPS:lat,lon|IMU:heading
            gps_start = data.find("GPS:") + 4
            gps_end = data.find("|IMU:")
            gps = data[gps_start:gps_end]

            imu_start = data.find("|IMU:") + 5
            imu = data[imu_start:]

            lat_str, lon_str = gps.split(",")
            self.hub_lat = float(lat_str)
            self.hub_lon = float(lon_str)
            self.hub_heading = int(imu)

            # Update UI
            self.gps_status_label.config(text="Receiving data", foreground="green")
            self.hub_lat_label.config(text=f"Lat: {self.hub_lat:.6f}")
            self.hub_lon_label.config(text=f"Lon: {self.hub_lon:.6f}")
            self.hub_heading_label.config(text=f"Heading: {self.hub_heading}\u00b0")

            # Calculate column positions
            spacing = self.spacing_var.get()
            positions = calculate_column_positions(self.hub_lat, self.hub_lon, self.hub_heading, spacing)

            for i, ((lat, lon), heading) in enumerate(positions):
                self.column_labels[i].config(text=f"{lat:.6f}, {lon:.6f}")

            # Flash grid cells yellow briefly
            for (row, col), (cell, _, _) in self.gps_grid_cells.items():
                if row in self.selected_rows:
                    self.gps_grid_canvas.itemconfig(cell, fill="#FFFF99")

            self.root.after(300, self.update_all_grids)

            self.log(f"Hub GPS: {self.hub_lat:.6f}, {self.hub_lon:.6f} @ {self.hub_heading}\u00b0")

            # Auto-relay if enabled
            if self.auto_relay.get():
                self.send_gps_update()

        except Exception as e:
            self.log(f"GPS parse error: {e}")

    def send_gps_update(self):
        """Send GPS positions to selected headbands."""
        if self.hub_lat is None or self.hub_lon is None or self.hub_heading is None:
            self.log("No GPS data available yet")
            return

        if not self.selected_rows:
            self.log("No rows selected")
            return

        spacing = self.spacing_var.get()
        positions = calculate_column_positions(self.hub_lat, self.hub_lon, self.hub_heading, spacing)

        def worker():
            sent_count = 0
            for row in sorted(self.selected_rows):
                for col in range(1, 6):
                    (lat, lon), heading = positions[col - 1]
                    msg = f"{row};{col}:{lat},{lon}|{heading}"
                    try:
                        with self.sock_lock:
                            if not self.sock:
                                self.root.after(0, lambda: self.log("Not connected"))
                                return
                            send_message(msg, self.sock, timeout=0.5)
                        sent_count += 1
                    except Exception as e:
                        self.root.after(0, lambda err=e: self.log(f"Send error: {err}"))

            self.root.after(0, lambda: self.log(f"Sent GPS to {sent_count} targets"))

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------- Motor Control Methods -----------------------
    def run_sequence(self, sequence):
        """Run a sequence of motor commands. sequence = list of (row, pin, state, delay)"""
        if not self.connected:
            self.log("Not connected")
            return

        if not self.selected_rows:
            self.log("No rows selected")
            return

        def worker():
            for row, pin, state, delay in sequence:
                msg = f"{row};{pin}:{state}"
                try:
                    with self.sock_lock:
                        if not self.sock:
                            return
                        send_message(msg, self.sock, timeout=0.5)
                    self.root.after(0, lambda p=pin, s=state: self.update_motor_diagram(p, s == 1))
                except Exception as e:
                    self.root.after(0, lambda err=e: self.log(f"Send error: {err}"))
                if delay > 0:
                    time.sleep(delay)

            # Reset motor diagram after sequence
            self.root.after(300, lambda: [self.update_motor_diagram(p, False)
                                          for p in [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]])

        threading.Thread(target=worker, daemon=True).start()

    def build_sequence_for_rows(self, base_sequence):
        """Build sequence for all selected rows."""
        full_sequence = []
        for row in sorted(self.selected_rows):
            for pin, state, delay in base_sequence:
                full_sequence.append((row, pin, state, delay))
        return full_sequence

    def forward(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Forward -> Row(s) {rows}")
        base = [(PIN_FRONT, 1, 0.10), (PIN_FRONT, 0, 0.05), (PIN_FRONT, 1, 0.10), (PIN_FRONT, 0, 0.00)]
        self.run_sequence(self.build_sequence_for_rows(base))

    def left(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Left -> Row(s) {rows}")
        base = [(PIN_LEFT, 1, 0.10), (PIN_LEFT, 0, 0.05), (PIN_LEFT, 1, 0.10), (PIN_LEFT, 0, 0.00)]
        self.run_sequence(self.build_sequence_for_rows(base))

    def right(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Right -> Row(s) {rows}")
        base = [(PIN_RIGHT, 1, 0.10), (PIN_RIGHT, 0, 0.05), (PIN_RIGHT, 1, 0.10), (PIN_RIGHT, 0, 0.00)]
        self.run_sequence(self.build_sequence_for_rows(base))

    def back(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Back -> Row(s) {rows}")
        base = [(PIN_BACK, 1, 0.10), (PIN_BACK, 0, 0.05), (PIN_BACK, 1, 0.10), (PIN_BACK, 0, 0.00)]
        self.run_sequence(self.build_sequence_for_rows(base))

    def stop_all(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"All Stop -> Row(s) {rows}")
        base = [
            (PIN_LEFT, 1, 0.00), (PIN_FRONT, 1, 0.00), (PIN_RIGHT, 1, 0.00), (PIN_BACK, 1, 0.10),
            (PIN_LEFT, 0, 0.00), (PIN_FRONT, 0, 0.00), (PIN_RIGHT, 0, 0.00), (PIN_BACK, 0, 0.05),
            (PIN_LEFT, 1, 0.00), (PIN_FRONT, 1, 0.00), (PIN_RIGHT, 1, 0.00), (PIN_BACK, 1, 0.10),
            (PIN_LEFT, 0, 0.00), (PIN_FRONT, 0, 0.00), (PIN_RIGHT, 0, 0.00), (PIN_BACK, 0, 0.00),
        ]
        self.run_sequence(self.build_sequence_for_rows(base))

    def rotate_left(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Rotate Left -> Row(s) {rows}")
        base = [(PIN_FRONT, 1, 0.10), (PIN_FRONT, 0, 0.05), (PIN_LEFT, 1, 0.10), (PIN_LEFT, 0, 0.00)]
        self.run_sequence(self.build_sequence_for_rows(base))

    def rotate_right(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Rotate Right -> Row(s) {rows}")
        base = [(PIN_FRONT, 1, 0.10), (PIN_FRONT, 0, 0.05), (PIN_RIGHT, 1, 0.10), (PIN_RIGHT, 0, 0.00)]
        self.run_sequence(self.build_sequence_for_rows(base))

    def start_seq(self):
        rows = ", ".join(map(str, sorted(self.selected_rows)))
        self.log(f"Start March -> Row(s) {rows}")
        base = [
            (PIN_LEFT, 1, 0.00), (PIN_RIGHT, 1, 0.10),
            (PIN_LEFT, 0, 0.00), (PIN_RIGHT, 0, 0.05),
            (PIN_LEFT, 1, 0.00), (PIN_RIGHT, 1, 0.10),
            (PIN_LEFT, 0, 0.00), (PIN_RIGHT, 0, 0.10),
            (PIN_FRONT, 1, 0.10), (PIN_FRONT, 0, 0.00),
        ]
        self.run_sequence(self.build_sequence_for_rows(base))

    def emergency_stop(self):
        """Emergency stop - immediately turn off all motors on ALL rows."""
        self.log("EMERGENCY STOP - All rows, all motors OFF")

        if not self.connected:
            return

        def worker():
            # Send OFF to all motors on all rows
            for row in range(1, 6):
                for pin in [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]:
                    msg = f"{row};{pin}:0"
                    try:
                        with self.sock_lock:
                            if not self.sock:
                                return
                            send_message(msg, self.sock, timeout=0.3)
                    except Exception:
                        pass

            self.root.after(0, lambda: [self.update_motor_diagram(p, False)
                                        for p in [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]])

        threading.Thread(target=worker, daemon=True).start()

    # ----------------------- Keyboard Handling -----------------------
    def on_key(self, event):
        """Handle keyboard shortcuts."""
        # Number keys 1-5 for quick row selection (always available)
        if event.char in "12345":
            row = int(event.char)
            if event.state & 0x4:  # Ctrl held
                if row in self.selected_rows:
                    self.selected_rows.discard(row)
                else:
                    self.selected_rows.add(row)
                if not self.selected_rows:
                    self.selected_rows = {row}
            else:
                self.selected_rows = {row}
            self.update_all_grids()
            return

        # Control commands require connection
        if not self.connected:
            return

        key = event.char.lower()
        if key == "w":
            self.forward()
        elif key == "a":
            self.left()
        elif key == "s":
            self.back()
        elif key == "d":
            self.right()
        elif key == "z":
            self.rotate_left()
        elif key == "x":
            self.rotate_right()
        elif key == "e":
            self.start_seq()
        elif key == "q":
            self.stop_all()


# ======================= Main Entry Point =======================
if __name__ == "__main__":
    root = tk.Tk()
    app = HaptiBandApp(root)
    root.mainloop()
