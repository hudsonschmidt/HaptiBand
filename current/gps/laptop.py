import socket
import sys
import termios
import tty
import time
import threading
import select

def get_char() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch

def send_message(msg: str, sock: socket.socket, timeout=2.0) -> None:
    full = f"{msg}\n".encode()
    sock.sendall(full)
    print(f"→ {msg}")

    sock.settimeout(timeout)
    try:
        reply = sock.recv(256).decode().strip()
        if reply:
            print(f"← {reply}")
    except socket.timeout:
        print("Timeout")

def connect(ip: str, port: int, retries=3) -> socket.socket:
    for n in range(1, retries + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip, port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # low‑latency
            print(f"✓ Connected to {ip}:{port}")
            return s
        except OSError as e:
            print(f"[{n}/{retries}] connect error → {e}")
            time.sleep(1)
    raise RuntimeError("Unable to reach hub")

def listen_for_data(sock):
    global running
    sock.setblocking(0)
    
    while running:
        ready = select.select([sock], [], [], 0.1)
        if ready[0]:
            try:
                data = sock.recv(256).decode().strip()
                if data:
                    print(f"\nReceived from hub: {data}")
                    # Parse GPS and IMU data
                    if "GPS:" in data and "|IMU:" in data:
                        # Extract GPS data
                        gps_start = data.find("GPS:") + 4
                        gps_end = data.find("|IMU:")
                        gps_data = data[gps_start:gps_end]
                        
                        # Extract IMU data
                        imu_start = data.find("|IMU:") + 5
                        imu_data = data[imu_start:]
                        
                        # Format data for headband (row;column:gps|imu)
                        formatted_msg = f"1;2:{gps_data}|{imu_data}"
                        print(f"Sending to headband: {formatted_msg}")
                        send_message(formatted_msg, sock)
            except socket.error:
                pass
        time.sleep(0.1)

# ── main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    sock = connect("192.168.4.1", 80)
    running = True
    
    # Start listener thread for incoming data from the hub
    listener_thread = threading.Thread(target=listen_for_data, args=(sock,))
    listener_thread.daemon = True
    listener_thread.start()

    try:
        print("Press p to quit, or use WASD keys for manual control")
        while running:
            key = get_char()
                # 5 - Left Temple
                # 18 - Forehead
                # 19 - Right Temple
                # 23 - Back of head
                
                # FORWARD
            if key == "w":
                send_message("1;18:1", sock)
                time.sleep(0.1)
                send_message("1;18:0", sock)
                time.sleep(0.05)
                send_message("1;18:1", sock)
                time.sleep(0.1)
                send_message("1;18:0", sock)
            
            # LEFT
            elif key == "a":
                send_message("1;5:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)
                time.sleep(0.05)
                send_message("1;5:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)

            # BACKWARD
            elif key == "s":
                send_message("1;23:1", sock)
                time.sleep(0.1)
                send_message("1;23:0", sock)
                time.sleep(0.05)
                send_message("1;23:1", sock)
                time.sleep(0.1)
                send_message("1;23:0", sock)

            # RIGHT
            elif key == "d":
                send_message("1;19:1", sock)
                time.sleep(0.1)
                send_message("1;19:0", sock)
                time.sleep(0.05)
                send_message("1;19:1", sock)
                time.sleep(0.1)
                send_message("1;19:0", sock)
            
            # STOP
            elif key == "q":
                send_message("1;5:1", sock)
                send_message("1;18:1", sock)
                send_message("1;19:1", sock)
                send_message("1;23:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)
                send_message("1;18:0", sock)
                send_message("1;19:0", sock)
                send_message("1;23:0", sock)
                time.sleep(0.05)
                send_message("1;5:1", sock)
                send_message("1;18:1", sock)
                send_message("1;19:1", sock)
                send_message("1;23:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)
                send_message("1;18:0", sock)
                send_message("1;19:0", sock)
                send_message("1;23:0", sock)

            # ROTATE LEFT
            elif key == "z":
                send_message("1;18:1", sock)
                time.sleep(0.1)
                send_message("1;18:0", sock)
                time.sleep(0.05)
                send_message("1;5:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)

            # ROTATE RIGHT
            elif key == "x":
                send_message("1;18:1", sock)
                time.sleep(0.1)
                send_message("1;18:0", sock)
                time.sleep(0.05)
                send_message("1;19:1", sock)
                time.sleep(0.1)
                send_message("1;19:0", sock)

            # START
            elif key == "e":
                send_message("1;5:1", sock)
                send_message("1;19:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)
                send_message("1;19:0", sock)
                time.sleep(0.05)
                send_message("1;5:1", sock)
                send_message("1;19:1", sock)
                time.sleep(0.1)
                send_message("1;5:0", sock)
                send_message("1;19:0", sock)
                time.sleep(0.1)
                send_message("1;18:1", sock)
                time.sleep(0.1)
                send_message("1;18:0", sock)

            elif key == "p":
                running = False
                break
    finally:
        running = False
        sock.close()
        print("socket closed")
