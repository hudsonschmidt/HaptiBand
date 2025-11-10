# language.py - Haptic Language Builder
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import socket, threading, time
import json
import os

HOST_DEFAULT = "192.168.4.1"
PORT_DEFAULT = 80

# PORT - SIDE
# 5 - Left
# 18 - Front
# 19 - Right 
# 23 - Back

root = tk.Tk()
root.title("HaptiBand - Haptic Language Builder")
root.geometry("1200x750")

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
    test_btn.configure(state=("normal" if on else "disabled"))

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

# ----------------------- pattern builder -----------------------
# Store loaded patterns
patterns_library = {}
PATTERNS_FILE = "haptic_patterns.json"

def build_sequence_from_settings():
    """Build a sequence based on current UI settings"""
    motors = []
    if motor_left_var.get():
        motors.append(5)
    if motor_front_var.get():
        motors.append(18)
    if motor_right_var.get():
        motors.append(19)
    if motor_back_var.get():
        motors.append(23)

    if not motors:
        return []

    buzz_length = buzz_length_var.get() / 1000.0  # Convert ms to seconds
    two_buzz = two_buzz_var.get()
    gap = 0.05  # Fixed 50ms gap for two-buzz patterns

    sequence = []

    # Turn on all selected motors
    for motor in motors:
        sequence.append((f"1;{motor}:1", 0.00))

    # First buzz duration
    sequence[-1] = (sequence[-1][0], buzz_length)

    # Turn off all motors
    for motor in motors:
        sequence.append((f"1;{motor}:0", 0.00))

    if two_buzz:
        # Gap between buzzes
        sequence[-1] = (sequence[-1][0], gap)

        # Turn on all motors again
        for motor in motors:
            sequence.append((f"1;{motor}:1", 0.00))

        # Second buzz duration
        sequence[-1] = (sequence[-1][0], buzz_length)

        # Turn off all motors
        for motor in motors:
            sequence.append((f"1;{motor}:0", 0.00))

    return sequence

def test_pattern():
    """Test the current pattern"""
    sequence = build_sequence_from_settings()
    if not sequence:
        log("No motors selected!")
        return
    run_sequence(sequence)
    log(f"Testing pattern: {len(sequence)} commands")

def save_pattern():
    """Save the current pattern"""
    name = pattern_name_var.get().strip()
    if not name:
        messagebox.showerror("Error", "Please enter a pattern name")
        return

    motors = []
    if motor_left_var.get():
        motors.append(5)
    if motor_front_var.get():
        motors.append(18)
    if motor_right_var.get():
        motors.append(19)
    if motor_back_var.get():
        motors.append(23)

    if not motors:
        messagebox.showerror("Error", "Please select at least one motor")
        return

    pattern = {
        "name": name,
        "motors": motors,
        "buzz_length_ms": buzz_length_var.get(),
        "two_buzz": two_buzz_var.get()
    }

    patterns_library[name] = pattern
    save_patterns_to_file()
    update_pattern_list()
    generate_code_output()
    log(f"✓ Saved pattern: {name}")
    messagebox.showinfo("Success", f"Pattern '{name}' saved successfully!")

def save_patterns_to_file():
    """Save all patterns to JSON file"""
    try:
        with open(PATTERNS_FILE, 'w') as f:
            json.dump(patterns_library, f, indent=2)
    except Exception as e:
        log(f"Error saving patterns: {e}")

def load_patterns_from_file():
    """Load patterns from JSON file"""
    global patterns_library
    if os.path.exists(PATTERNS_FILE):
        try:
            with open(PATTERNS_FILE, 'r') as f:
                patterns_library = json.load(f)
            update_pattern_list()
            log(f"✓ Loaded {len(patterns_library)} patterns")
        except Exception as e:
            log(f"Error loading patterns: {e}")

def load_selected_pattern():
    """Load the selected pattern from the list"""
    selection = pattern_listbox.curselection()
    if not selection:
        messagebox.showinfo("Info", "Please select a pattern to load")
        return

    name = pattern_listbox.get(selection[0])
    if name in patterns_library:
        pattern = patterns_library[name]
        pattern_name_var.set(pattern["name"])
        buzz_length_var.set(pattern["buzz_length_ms"])
        two_buzz_var.set(pattern["two_buzz"])

        # Reset all motors first
        motor_left_var.set(False)
        motor_front_var.set(False)
        motor_right_var.set(False)
        motor_back_var.set(False)

        # Set selected motors
        for motor in pattern["motors"]:
            if motor == 5:
                motor_left_var.set(True)
            elif motor == 18:
                motor_front_var.set(True)
            elif motor == 19:
                motor_right_var.set(True)
            elif motor == 23:
                motor_back_var.set(True)

        update_buzz_length_label()
        log(f"✓ Loaded pattern: {name}")

def delete_selected_pattern():
    """Delete the selected pattern"""
    selection = pattern_listbox.curselection()
    if not selection:
        messagebox.showinfo("Info", "Please select a pattern to delete")
        return

    name = pattern_listbox.get(selection[0])
    if messagebox.askyesno("Confirm Delete", f"Delete pattern '{name}'?"):
        del patterns_library[name]
        save_patterns_to_file()
        update_pattern_list()
        generate_code_output()
        log(f"✓ Deleted pattern: {name}")

def update_pattern_list():
    """Update the pattern listbox"""
    pattern_listbox.delete(0, tk.END)
    for name in sorted(patterns_library.keys()):
        pattern_listbox.insert(tk.END, name)

def generate_code_output():
    """Generate Python and JSON code for saved patterns"""
    if not patterns_library:
        code_output.configure(state="normal")
        code_output.delete("1.0", "end")
        code_output.insert("1.0", "# No patterns saved yet")
        code_output.configure(state="disabled")
        return

    # Generate Python code
    py_code = "# Haptic Pattern Functions\n\n"
    py_code += "def run_sequence(sequence):\n"
    py_code += "    \"\"\"sequence = list of (msg, delay_after_sec)\"\"\"\n"
    py_code += "    # Implementation depends on your setup\n"
    py_code += "    pass\n\n"

    for name, pattern in sorted(patterns_library.items()):
        func_name = name.lower().replace(" ", "_").replace("-", "_")
        py_code += f"def {func_name}():\n"
        py_code += f"    \"\"\"Pattern: {pattern['name']} - Motors: {pattern['motors']}, "
        py_code += f"Buzz: {pattern['buzz_length_ms']}ms, Two-buzz: {pattern['two_buzz']}\"\"\"\n"

        motors = pattern['motors']
        buzz_len = pattern['buzz_length_ms'] / 1000.0
        two_buzz = pattern['two_buzz']

        py_code += "    run_sequence([\n"

        # First buzz on
        for i, motor in enumerate(motors):
            delay = buzz_len if i == len(motors) - 1 else 0.00
            py_code += f"        (\"1;{motor}:1\", {delay:.2f}),\n"

        # First buzz off
        for i, motor in enumerate(motors):
            delay = 0.05 if two_buzz and i == len(motors) - 1 else 0.00
            if not two_buzz and i == len(motors) - 1:
                delay = 0.00
            py_code += f"        (\"1;{motor}:0\", {delay:.2f}),\n"

        if two_buzz:
            # Second buzz on
            for i, motor in enumerate(motors):
                delay = buzz_len if i == len(motors) - 1 else 0.00
                py_code += f"        (\"1;{motor}:1\", {delay:.2f}),\n"

            # Second buzz off
            for i, motor in enumerate(motors):
                delay = 0.00
                py_code += f"        (\"1;{motor}:0\", {delay:.2f}),\n"

        py_code += "    ])\n\n"

    # Add JSON section
    py_code += "\n# JSON Format\n"
    py_code += "# " + "="*60 + "\n"
    py_code += json.dumps(patterns_library, indent=2)

    code_output.configure(state="normal")
    code_output.delete("1.0", "end")
    code_output.insert("1.0", py_code)
    code_output.configure(state="disabled")

def update_buzz_length_label():
    """Update the buzz length display"""
    buzz_length_label.config(text=f"{buzz_length_var.get()} ms")

def set_buzz_preset(value_ms):
    """Set buzz length to a preset value"""
    buzz_length_var.set(value_ms)
    update_buzz_length_label()

# ----------------------- layout --------------------------
# Connection section
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

# Main content area - split into left and right
main_content = ttk.Frame(root, padding=10)
main_content.pack(fill="both", expand=True, padx=10)

# Left side - Pattern Builder
left_frame = ttk.Frame(main_content)
left_frame.pack(side="left", fill="both", expand=True, padx=(0, 5))

# Pattern Builder
builder_frame = ttk.LabelFrame(left_frame, text="Pattern Builder", padding=10)
builder_frame.pack(fill="x", pady=(0, 10))

# Pattern Name
name_frame = ttk.Frame(builder_frame)
name_frame.pack(fill="x", pady=(0, 10))
ttk.Label(name_frame, text="Pattern Name:").pack(side="left", padx=(0, 5))
pattern_name_var = tk.StringVar()
pattern_name_entry = ttk.Entry(name_frame, textvariable=pattern_name_var, width=30)
pattern_name_entry.pack(side="left", fill="x", expand=True)

# Motor Selection
motor_frame = ttk.LabelFrame(builder_frame, text="Motor Selection", padding=10)
motor_frame.pack(fill="x", pady=(0, 10))

motor_left_var = tk.BooleanVar(value=False)
motor_front_var = tk.BooleanVar(value=False)
motor_right_var = tk.BooleanVar(value=False)
motor_back_var = tk.BooleanVar(value=False)

ttk.Checkbutton(motor_frame, text="Left (Pin 5)", variable=motor_left_var).pack(anchor="w", pady=2)
ttk.Checkbutton(motor_frame, text="Front (Pin 18)", variable=motor_front_var).pack(anchor="w", pady=2)
ttk.Checkbutton(motor_frame, text="Right (Pin 19)", variable=motor_right_var).pack(anchor="w", pady=2)
ttk.Checkbutton(motor_frame, text="Back (Pin 23)", variable=motor_back_var).pack(anchor="w", pady=2)

# Buzz Length
buzz_frame = ttk.LabelFrame(builder_frame, text="Buzz Length", padding=10)
buzz_frame.pack(fill="x", pady=(0, 10))

buzz_length_var = tk.IntVar(value=100)
buzz_length_label = ttk.Label(buzz_frame, text="100 ms")
buzz_length_label.pack(pady=(0, 5))

buzz_slider = ttk.Scale(buzz_frame, from_=50, to=500, variable=buzz_length_var,
                        orient="horizontal", command=lambda v: update_buzz_length_label())
buzz_slider.pack(fill="x", pady=(0, 5))

# Preset buttons
preset_frame = ttk.Frame(buzz_frame)
preset_frame.pack(fill="x")
ttk.Button(preset_frame, text="Short (100ms)", command=lambda: set_buzz_preset(100)).pack(side="left", padx=2)
ttk.Button(preset_frame, text="Medium (250ms)", command=lambda: set_buzz_preset(250)).pack(side="left", padx=2)
ttk.Button(preset_frame, text="Long (400ms)", command=lambda: set_buzz_preset(400)).pack(side="left", padx=2)

# Buzz Count
count_frame = ttk.LabelFrame(builder_frame, text="Buzz Pattern", padding=10)
count_frame.pack(fill="x", pady=(0, 10))

two_buzz_var = tk.BooleanVar(value=False)
ttk.Radiobutton(count_frame, text="One Buzz", variable=two_buzz_var, value=False).pack(anchor="w", pady=2)
ttk.Radiobutton(count_frame, text="Two Buzzes (50ms gap)", variable=two_buzz_var, value=True).pack(anchor="w", pady=2)

# Action Buttons
action_frame = ttk.Frame(builder_frame)
action_frame.pack(fill="x")

test_btn = ttk.Button(action_frame, text="Test Pattern", command=test_pattern, state="disabled")
test_btn.pack(side="left", padx=(0, 5), fill="x", expand=True)

save_btn = ttk.Button(action_frame, text="Save Pattern", command=save_pattern)
save_btn.pack(side="left", fill="x", expand=True)

# Pattern Library
library_frame = ttk.LabelFrame(left_frame, text="Saved Patterns", padding=10)
library_frame.pack(fill="both", expand=True)

# Listbox with scrollbar
list_frame = ttk.Frame(library_frame)
list_frame.pack(fill="both", expand=True, pady=(0, 5))

scrollbar = ttk.Scrollbar(list_frame)
scrollbar.pack(side="right", fill="y")

pattern_listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, height=8)
pattern_listbox.pack(side="left", fill="both", expand=True)
scrollbar.config(command=pattern_listbox.yview)

# Library buttons
lib_btn_frame = ttk.Frame(library_frame)
lib_btn_frame.pack(fill="x")

ttk.Button(lib_btn_frame, text="Load Selected", command=load_selected_pattern).pack(side="left", padx=(0, 5), fill="x", expand=True)
ttk.Button(lib_btn_frame, text="Delete Selected", command=delete_selected_pattern).pack(side="left", fill="x", expand=True)

# Right side - Code Output and Log
right_frame = ttk.Frame(main_content)
right_frame.pack(side="right", fill="both", expand=True, padx=(5, 0))

# Code Output
code_frame = ttk.LabelFrame(right_frame, text="Generated Code (Python + JSON)", padding=6)
code_frame.pack(fill="both", expand=True, pady=(0, 10))

code_scroll = ttk.Scrollbar(code_frame)
code_scroll.pack(side="right", fill="y")

code_output = tk.Text(code_frame, height=20, state="disabled", yscrollcommand=code_scroll.set, wrap="none")
code_output.pack(fill="both", expand=True)
code_scroll.config(command=code_output.yview)

# Log
log_frame = ttk.LabelFrame(right_frame, text="Log", padding=6)
log_frame.pack(fill="both", expand=True)

log_scroll = ttk.Scrollbar(log_frame)
log_scroll.pack(side="right", fill="y")

output = tk.Text(log_frame, height=10, state="disabled", yscrollcommand=log_scroll.set)
output.pack(fill="both", expand=True)
log_scroll.config(command=output.yview)

# Initialize
load_patterns_from_file()
generate_code_output()
set_controls_enabled(False)

if __name__ == "__main__":
    root.mainloop()