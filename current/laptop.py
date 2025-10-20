import socket
import sys
import termios
import tty
import time

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

# ── main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    sock = connect("192.168.4.1", 80)

    try:
        print("Press p to quit")
        while True:
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
                break
    finally:
        sock.close()
        print("socket closed")
