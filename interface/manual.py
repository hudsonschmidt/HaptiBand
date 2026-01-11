# manual.py
import tkinter as tk
from tkinter import ttk, messagebox
import socket, threading, time

HOST_DEFAULT = "192.168.4.1"
PORT_DEFAULT = 80

# PORT - SIDE
# 5 - Left
# 18 - Front
# 19 - Right 
# 23 - Back

root = tk.Tk()
root.title("HaptiBand")
root.geometry("1000x650")

sock = None
sock_lock = threading.Lock()

# ----------------------- networking -----------------------
def send_message(msg: str, sock_obj: socket.socket, timeout=1.0) -> None:
    full = f"{msg}\n".encode()
    try:
        sock_obj.sendall(full)
        log(f"→ {msg}")
        sock_obj.settimeout(timeout)
        try:
            reply = sock_obj.recv(256).decode(errors="ignore").strip()
            if reply:
                log(f"← {reply}")
        except socket.timeout:
            log("… timeout")
    except OSError as e:
        log(f"ERR send: {e}")

def connect(ip: str, port: int, retries=3) -> socket.socket:
    last = None
    for n in range(1, retries + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(3.0)
            s.connect((ip, port))
            s.settimeout(None)  # back to blocking for recv
            return s
        except OSError as e:
            last = e
            time.sleep(0.5)
    raise RuntimeError(f"Unable to reach hub at {ip}:{port} ({last})")

# ----------------------- UI helpers -----------------------
def log(msg: str) -> None:
    output.configure(state="normal")
    output.insert("end", msg + "\n")
    output.see("end")
    output.configure(state="disabled")

def set_controls_enabled(on: bool) -> None:
    for b in control_buttons:
        b.configure(state=("normal" if on else "disabled"))

# ----------------------- actions --------------------------
def connect_to_hub():
    global sock
    ip = ip_var.get().strip() or HOST_DEFAULT
    try:
        port = int(port_var.get())
    except ValueError:
        port = PORT_DEFAULT

    def worker():
        global sock
        try:
            s = connect(ip, port)
            with sock_lock:
                # Close any prior socket
                if sock:
                    try:
                        sock.close()
                    except Exception:
                        pass
                sock = s
            log(f"✓ Connected to {ip}:{port}")
            connect_btn.configure(state="disabled")
            disconnect_btn.configure(state="normal")
            set_controls_enabled(True)
        except Exception as e:
            messagebox.showerror("Connection error", str(e))
            log(f"Connection error: {e}")

    threading.Thread(target=worker, daemon=True).start()

def disconnect_from_hub():
    global sock
    with sock_lock:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
            sock = None
    log("⏏ Disconnected")
    connect_btn.configure(state="normal")
    disconnect_btn.configure(state="disabled")
    set_controls_enabled(False)

def run_sequence(sequence):
    """sequence = list of (msg, delay_after_sec)"""
    def worker():
        with sock_lock:
            if not sock:
                log("Not connected")
                return
            local = sock
        for msg, d in sequence:
            send_message(msg, local, timeout=0.5)
            if d > 0:
                time.sleep(d)
    threading.Thread(target=worker, daemon=True).start()

# --- sequences matching laptop.py ---
def forward():
    run_sequence([
        ("1;18:1", 0.10), ("1;18:0", 0.05),
        ("1;18:1", 0.10), ("1;18:0", 0.00),
    ])

def left():
    run_sequence([
        ("1;5:1", 0.10), ("1;5:0", 0.05),
        ("1;5:1", 0.10), ("1;5:0", 0.00),
    ])

def right():
    run_sequence([
        ("1;19:1", 0.10), ("1;19:0", 0.05),
        ("1;19:1", 0.10), ("1;19:0", 0.00),
    ])

def back():
    run_sequence([
        ("1;23:1", 0.10), ("1;23:0", 0.05),
        ("1;23:1", 0.10), ("1;23:0", 0.00),
    ])

def stop_all():
    run_sequence([
        ("1;5:1", 0.00), ("1;18:1", 0.00), ("1;19:1", 0.00), ("1;23:1", 0.10),
        ("1;5:0", 0.00), ("1;18:0", 0.00), ("1;19:0", 0.00), ("1;23:0", 0.05),
        ("1;5:1", 0.00), ("1;18:1", 0.00), ("1;19:1", 0.00), ("1;23:1", 0.10),
        ("1;5:0", 0.00), ("1;18:0", 0.00), ("1;19:0", 0.00), ("1;23:0", 0.00),
    ])

def rotate_left():
    run_sequence([
        ("1;18:1", 0.10), ("1;18:0", 0.05),
        ("1;5:1", 0.10), ("1;5:0", 0.00),
    ])

def rotate_right():
    run_sequence([
        ("1;18:1", 0.10), ("1;18:0", 0.05),
        ("1;19:1", 0.10), ("1;19:0", 0.00),
    ])

def start_seq():
    run_sequence([
        ("1;5:1", 0.00), ("1;19:1", 0.10),
        ("1;5:0", 0.00), ("1;19:0", 0.05),
        ("1;5:1", 0.00), ("1;19:1", 0.10),
        ("1;5:0", 0.00), ("1;19:0", 0.10),
        ("1;18:1", 0.10), ("1;18:0", 0.00),
    ])

# ----------------------- layout --------------------------
top = ttk.Frame(root, padding=10)
top.pack(fill="x")

ip_var = tk.StringVar(value=HOST_DEFAULT)
port_var = tk.StringVar(value=str(PORT_DEFAULT))

ttk.Label(top, text="IP:").pack(side="left")
ip_entry = ttk.Entry(top, textvariable=ip_var, width=16)
ip_entry.pack(side="left", padx=(0, 10))

ttk.Label(top, text="Port:").pack(side="left")
port_entry = ttk.Entry(top, textvariable=port_var, width=6)
port_entry.pack(side="left", padx=(0, 10))

connect_btn = ttk.Button(top, text="Connect", command=lambda: connect_to_hub())
connect_btn.pack(side="left")

disconnect_btn = ttk.Button(top, text="Disconnect", command=disconnect_from_hub, state="disabled")
disconnect_btn.pack(side="left", padx=(6, 0))

controls = ttk.LabelFrame(root, text="Controls", padding=10)
controls.pack(fill="x", padx=10, pady=10)

control_buttons = []
def add_btn(parent, text, fn):
    b = ttk.Button(parent, text=text, command=fn, width=16)
    b.pack(side="left", padx=6, pady=4)
    control_buttons.append(b)

add_btn(controls, "Forward (w)", forward)
add_btn(controls, "Left (a)", left)
add_btn(controls, "Back (s)", back)
add_btn(controls, "Right (d)", right)
add_btn(controls, "Rotate Left (z)", rotate_left)
add_btn(controls, "Rotate Right (x)", rotate_right)
add_btn(controls, "Start (e)", start_seq)
add_btn(controls, "STOP (q)", stop_all)

set_controls_enabled(False)

log_frame = ttk.LabelFrame(root, text="Log", padding=6)
log_frame.pack(fill="both", expand=True, padx=10, pady=(0,10))

output = tk.Text(log_frame, height=18, state="disabled")
output.pack(fill="both", expand=True)

def on_key(event):
    if event.char == "w": forward()
    elif event.char == "a": left()
    elif event.char == "s": back()
    elif event.char == "d": right()
    elif event.char == "z": rotate_left()
    elif event.char == "x": rotate_right()
    elif event.char == "e": start_seq()
    elif event.char == "q": stop_all()

root.bind("<Key>", on_key)

if __name__ == "__main__":
    root.mainloop()
