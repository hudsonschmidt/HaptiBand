import tkinter as tk
from tkinter import ttk, messagebox
import socket
import threading
import time

# ----------------------- Constants -----------------------
HOST_DEFAULT = "192.168.4.1"
PORT_DEFAULT = 80

# Motor pin mappings
PIN_LEFT = 5
PIN_FRONT = 18
PIN_RIGHT = 19
PIN_BACK = 23

MOTOR_MAP = {
    PIN_LEFT: "Left Temple",
    PIN_FRONT: "Forehead",
    PIN_RIGHT: "Right Temple",
    PIN_BACK: "Back of Head",
}

ROW = 1  # Test target row


# ----------------------- Networking -----------------------
def send_message(msg, sock, timeout=1.0):
    sock.sendall(f"{msg}\n".encode())
    sock.settimeout(timeout)
    try:
        return sock.recv(256).decode(errors="ignore").strip()
    except socket.timeout:
        return None


def connect(ip, port, retries=3):
    last = None
    for _ in range(retries):
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


# ======================= App =======================
class MotorTestApp:
    def __init__(self, root):
        self.root = root
        self.root.title("HaptiBand Motor Tester")
        self.root.geometry("500x540")
        self.root.resizable(True, True)
        self.root.minsize(500, 540)

        self.sock = None
        self.sock_lock = threading.Lock()
        self.connected = False
        self.loop_running = False
        self.loop_stop = threading.Event()

        self.build_ui()

    def build_ui(self):
        # --- Connection ---
        conn = ttk.LabelFrame(self.root, text="Connection", padding=10)
        conn.pack(fill="x", padx=10, pady=(10, 5))

        addr_row = ttk.Frame(conn)
        addr_row.pack(fill="x")

        ttk.Label(addr_row, text="IP:").pack(side="left")
        self.ip_var = tk.StringVar(value=HOST_DEFAULT)
        ttk.Entry(addr_row, textvariable=self.ip_var, width=16).pack(side="left", padx=(0, 10))

        ttk.Label(addr_row, text="Port:").pack(side="left")
        self.port_var = tk.StringVar(value=str(PORT_DEFAULT))
        ttk.Entry(addr_row, textvariable=self.port_var, width=6).pack(side="left")

        btn_row = ttk.Frame(conn)
        btn_row.pack(fill="x", pady=(5, 0))

        self.connect_btn = ttk.Button(btn_row, text="Connect", command=self.connect_hub)
        self.connect_btn.pack(side="left", padx=(0, 5))
        self.disconnect_btn = ttk.Button(btn_row, text="Disconnect", command=self.disconnect_hub, state="disabled")
        self.disconnect_btn.pack(side="left", padx=(0, 15))

        self.status_label = ttk.Label(btn_row, text="Disconnected", foreground="red")
        self.status_label.pack(side="left")

        # --- Pin Mappings ---
        pins = ttk.LabelFrame(self.root, text="Pin Mappings", padding=10)
        pins.pack(fill="x", padx=10, pady=5)

        for pin, name in MOTOR_MAP.items():
            ttk.Label(pins, text=f"GPIO {pin:>2}  ->  {name}", font=("TkFixedFont", 11)).pack(anchor="w")

        # --- Single Motor Controls ---
        single = ttk.LabelFrame(self.root, text="Single Motor (tap)", padding=10)
        single.pack(fill="x", padx=10, pady=5)

        self.motor_btns = []
        for pin, name in MOTOR_MAP.items():
            btn = ttk.Button(single, text=f"{name} (GPIO {pin})",
                             command=lambda p=pin: self.tap_motor(p))
            btn.pack(fill="x", pady=2)
            self.motor_btns.append(btn)

        # --- Loop Controls ---
        loop_frame = ttk.LabelFrame(self.root, text="Loop All Motors", padding=10)
        loop_frame.pack(fill="x", padx=10, pady=5)

        btn_row = ttk.Frame(loop_frame)
        btn_row.pack(fill="x")

        self.start_loop_btn = ttk.Button(btn_row, text="Start Loop", command=self.start_loop)
        self.start_loop_btn.pack(side="left", padx=(0, 5))
        self.stop_loop_btn = ttk.Button(btn_row, text="Stop Loop", command=self.stop_loop, state="disabled")
        self.stop_loop_btn.pack(side="left")

        self.loop_label = ttk.Label(loop_frame, text="Cycles through each motor with a 200ms buzz", foreground="gray")
        self.loop_label.pack(anchor="w", pady=(5, 0))

        # --- Log ---
        log_frame = ttk.LabelFrame(self.root, text="Log", padding=6)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(5, 10))

        self.log_output = tk.Text(log_frame, height=5, state="disabled", font=("TkFixedFont", 9))
        self.log_output.pack(fill="both", expand=True)

        self.set_controls_enabled(False)

    # ---- helpers ----
    def log(self, msg):
        ts = time.strftime("%H:%M:%S")
        self.log_output.configure(state="normal")
        self.log_output.insert("end", f"[{ts}] {msg}\n")
        self.log_output.see("end")
        self.log_output.configure(state="disabled")

    def set_controls_enabled(self, enabled):
        state = "normal" if enabled else "disabled"
        for btn in self.motor_btns:
            btn.configure(state=state)
        self.start_loop_btn.configure(state=state)

    def _send(self, pin, state):
        msg = f"{ROW};{pin}:{state}"
        with self.sock_lock:
            if self.sock:
                send_message(msg, self.sock, timeout=0.5)

    # ---- connection ----
    def connect_hub(self):
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
                    self.sock = s
                self.connected = True
                self.root.after(0, self._on_connected)
            except Exception as e:
                self.root.after(0, lambda: self._on_connect_fail(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _on_connected(self):
        self.log("Connected")
        self.status_label.config(text="Connected", foreground="green")
        self.disconnect_btn.configure(state="normal")
        self.set_controls_enabled(True)

    def _on_connect_fail(self, err):
        self.log(f"Failed: {err}")
        messagebox.showerror("Connection Error", err)
        self.connect_btn.configure(state="normal")

    def disconnect_hub(self):
        self.stop_loop()
        with self.sock_lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
        self.connected = False
        self.log("Disconnected")
        self.status_label.config(text="Disconnected", foreground="red")
        self.connect_btn.configure(state="normal")
        self.disconnect_btn.configure(state="disabled")
        self.set_controls_enabled(False)

    # ---- motor actions ----
    def tap_motor(self, pin):
        if not self.connected:
            return
        self.log(f"Tap {MOTOR_MAP[pin]} (GPIO {pin})")

        def worker():
            try:
                self._send(pin, 1)
                time.sleep(0.2)
                self._send(pin, 0)
            except Exception as e:
                self.root.after(0, lambda: self.log(f"Error: {e}"))

        threading.Thread(target=worker, daemon=True).start()

    def start_loop(self):
        if not self.connected or self.loop_running:
            return
        self.loop_running = True
        self.loop_stop.clear()
        self.start_loop_btn.configure(state="disabled")
        self.stop_loop_btn.configure(state="normal")
        self.log("Loop started")

        def worker():
            pins = [PIN_LEFT, PIN_FRONT, PIN_RIGHT, PIN_BACK]
            while not self.loop_stop.is_set():
                for pin in pins:
                    if self.loop_stop.is_set():
                        break
                    try:
                        self._send(pin, 1)
                        self.root.after(0, lambda p=pin: self.log(f"  -> {MOTOR_MAP[p]}"))
                        time.sleep(0.2)
                        self._send(pin, 0)
                        time.sleep(0.15)
                    except Exception:
                        self.loop_stop.set()
                        break
            # make sure all off
            for pin in pins:
                try:
                    self._send(pin, 0)
                except Exception:
                    pass
            self.loop_running = False
            self.root.after(0, self._on_loop_stopped)

        threading.Thread(target=worker, daemon=True).start()

    def stop_loop(self):
        if not self.loop_running:
            return
        self.loop_stop.set()

    def _on_loop_stopped(self):
        self.log("Loop stopped")
        if self.connected:
            self.start_loop_btn.configure(state="normal")
        self.stop_loop_btn.configure(state="disabled")


if __name__ == "__main__":
    root = tk.Tk()
    app = MotorTestApp(root)
    root.mainloop()
