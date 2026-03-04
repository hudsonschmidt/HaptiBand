import socket
import sys
import termios
import tty
import time
import threading
import select
import math

# Thread-safe shutdown event
shutdown_event = threading.Event()

def get_char() -> str:
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch

def send(msg: str, sock: socket.socket, timeout=2.0) -> None:
    full = f"{msg}\n".encode()
    sock.sendall(full)
    print(f"\r→ {msg}")

    sock.settimeout(timeout)
    try:
        reply = sock.recv(256).decode().strip()
        if reply:
            print(f"\r← {reply}")
    except socket.timeout:
        print("Timeout")

def connect(ip: str, port: int, retries=3) -> socket.socket:
    for n in range(1, retries + 1):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip, port))
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"✓ Connected to {ip}:{port}")
            return s
        except OSError as e:
            print(f"[{n}/{retries}] connect error → {e}")
            time.sleep(1)
    raise RuntimeError("Unable to reach hub")

def listen(sock):
    sock.setblocking(0)

    while not shutdown_event.is_set():
        ready = select.select([sock], [], [], 0.1)
        if ready[0]:
            try:
                data = sock.recv(256).decode().strip()
                spacing_feet = 3.0 
                
                if data:
                    print(f"\nReceived from hub: {data}")
                    print(f"Spacing: {spacing_feet} feet")
                    
                    # Parse GPS and IMU data
                    if "GPS:" in data and "|IMU:" in data:
                        # Extract GPS data
                        gps_start = data.find("GPS:") + 4
                        gps_end = data.find("|IMU:")
                        gps = data[gps_start:gps_end]

                        # Extract IMU data
                        imu_start = data.find("|IMU:") + 5
                        imu = data[imu_start:]

                        print(f'GPS: {gps}')
                        print(f'IMU: {imu}')
                        coords = split_coordinates(gps, imu, spacing_feet)
                        print(coords)

                        i = 1
                        for (lat, lon), theta in coords:
                            gps_str = f"{lat},{lon}"
                            imu_str = f"{theta}"

                            formatted_msg = f"1;{i}:{gps_str}|{imu_str}"
                            print(f"\rSending to headband: {formatted_msg}")
                            send(formatted_msg, sock)
                            i += 1
                        
            except socket.error:
                pass
        time.sleep(0.1)

# Conversion constants
FEET_PER_DEGREE_LAT = 364567.2  # approximately constant

def feet_to_degrees(feet, latitude):
    lat_degrees = feet / FEET_PER_DEGREE_LAT
    lon_degrees = feet / (FEET_PER_DEGREE_LAT * math.cos(math.radians(latitude)))
    return lat_degrees, lon_degrees

def split_coordinates(gps: str, imu: str, spacing_feet: float = 3.0):
    lat_str, lon_str = gps.split(",")
    lat = float(lat_str)
    lon = float(lon_str)

    theta_deg = int(imu)
    theta = math.radians(theta_deg)

    # Offsets in multiples of spacing_feet: columns 1-5 left to right
    offset_multipliers = [-2.0, -1.0, 0.0, 1.0, 2.0]
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

        result.append(((new_lat, new_lon), theta_deg))

    return result

# main ───────────────────────────────────────────────────────────
if __name__ == "__main__":
    sock = connect("192.168.4.1", 80)

    # Start listener thread for incoming data from the hub
    listener_thread = threading.Thread(target=listen, args=(sock,), daemon=True)
    listener_thread.start()

    try:
        while not shutdown_event.is_set():
            key = get_char()
            if key == "q":
                shutdown_event.set()
                break
    finally:
        shutdown_event.set()
        listener_thread.join(timeout=1.0)
        sock.close()
        print("socket closed")
