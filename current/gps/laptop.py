import socket
import sys
import termios
import tty
import time
import threading
import select
import math
import base64

from rtk import NTRIPClient

# NTRIP Configuration 
NTRIP_ENABLED = True  
NTRIP_CASTER = "rtgpsout.earthscope.org"
NTRIP_PORT = 2101
NTRIP_MOUNTPOINT = "P528_RTCM3P3"
NTRIP_USER = "compassionate_euler"
NTRIP_PASS = "hBv0TuTG0q9CqcZJ"

# Thread-safe shutdown event
shutdown_event = threading.Event()

# Socket write lock — prevents RTCM and control messages from interleaving on the wire
sock_write_lock = threading.Lock()

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
    with sock_write_lock:
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
            print(f"Connected to {ip}:{port}")
            return s
        except OSError as e:
            print(f"[{n}/{retries}] connect error: {e}")
            time.sleep(1)
    raise RuntimeError("Unable to reach hub")


# =============================================================================
# RTCM Relay Thread - Receives NTRIP corrections and sends to hub
# =============================================================================
rtcm_lock = threading.Lock()
rtcm_stats = {"bytes_sent": 0, "packets_sent": 0}


def send_rtcm(data: bytes, sock: socket.socket) -> bool:
    """
    Send RTCM correction data to hub for relay to headbands.

    Format: RTCM:<base64_encoded_data>\n

    Base64 encoding ensures binary RTCM data survives text-based transmission.
    Hub will decode and broadcast via ESP-NOW.
    """
    try:
        encoded = base64.b64encode(data).decode("ascii")
        msg = f"RTCM:{encoded}\n".encode()
        with sock_write_lock:
            sock.sendall(msg)

        with rtcm_lock:
            rtcm_stats["bytes_sent"] += len(data)
            rtcm_stats["packets_sent"] += 1

        return True
    except Exception as e:
        print(f"\rRTCM send error: {e}")
        return False


def rtcm_relay_thread(sock: socket.socket):
    """
    Background thread that receives RTCM data from NTRIP caster
    and relays it to the hub for broadcast to headbands.

    NOTE: This requires internet access. If connected to hub's WiFi only,
    you need a second network interface (ethernet, USB tethering) for internet.
    """
    if not NTRIP_ENABLED:
        return

    print(f"Starting NTRIP client: {NTRIP_CASTER}:{NTRIP_PORT}/{NTRIP_MOUNTPOINT}")
    print("(Requires internet - use ethernet/tethering if on hub WiFi only)")

    client = NTRIPClient(
        caster=NTRIP_CASTER,
        port=NTRIP_PORT,
        mountpoint=NTRIP_MOUNTPOINT,
        username=NTRIP_USER,
        password=NTRIP_PASS,
    )

    # Retry connection with backoff
    retry_delay = 5
    while not shutdown_event.is_set():
        if client.connect():
            break
        print(f"Failed to connect to NTRIP caster. No internet? Retrying in {retry_delay}s...")
        for _ in range(retry_delay):
            if shutdown_event.is_set():
                return
            time.sleep(1)
        retry_delay = min(retry_delay * 2, 60)  # Exponential backoff, max 60s

    if shutdown_event.is_set():
        return

    print("NTRIP connected, relaying RTCM corrections...")

    last_print_time = 0

    while not shutdown_event.is_set():
        data = client.read_data(timeout=1.0)
        if data:
            send_rtcm(data, sock)

        # Print stats periodically (every 2 seconds)
        now = time.time()
        if now - last_print_time >= 2.0:
            with rtcm_lock:
                local_stats = rtcm_stats.copy()
            if local_stats["packets_sent"] > 0:
                print(f"\rRTCM: {local_stats['packets_sent']} pkts, {local_stats['bytes_sent']} bytes relayed", end="", flush=True)
            last_print_time = now

    client.disconnect()
    print("\nRTCM relay stopped")

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

    # Start RTCM relay thread if enabled
    rtcm_thread = None
    if NTRIP_ENABLED:
        rtcm_thread = threading.Thread(target=rtcm_relay_thread, args=(sock,), daemon=True)
        rtcm_thread.start()

    print("Press 'q' to quit, 'r' to show RTCM stats")

    try:
        while not shutdown_event.is_set():
            key = get_char()
            if key == "q":
                shutdown_event.set()
                break
            elif key == "r" and NTRIP_ENABLED:
                with rtcm_lock:
                    print(f"\nRTCM Stats: {rtcm_stats}")
    finally:
        shutdown_event.set()
        listener_thread.join(timeout=1.0)
        if rtcm_thread:
            rtcm_thread.join(timeout=1.0)
        sock.close()
        print("socket closed")
