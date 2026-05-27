#!/usr/bin/env python3
import socket
import base64
import threading
import time
from typing import Optional, Callable

DEFAULT_CASTER = "rtgpsout.earthscope.org"
DEFAULT_PORT = 2101
DEFAULT_MOUNTPOINT = "USLO_RTCM3P3"
DEFAULT_USERNAME = "compassionate_euler"
DEFAULT_PASSWORD = "hBv0TuTG0q9CqcZJ"


class NTRIPClient:
    """NTRIP client for receiving RTCM correction data streams."""

    def __init__(
        self,
        caster: str,
        port: int,
        mountpoint: str,
        username: str = "",
        password: str = "",
        on_data: Optional[Callable[[bytes], None]] = None,
    ):
        self.caster = caster
        self.port = port
        self.mountpoint = mountpoint
        self.username = username
        self.password = password
        self.on_data = on_data  # Callback for received RTCM data

        self.sock: Optional[socket.socket] = None
        self.running = False
        self.connected = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Stats
        self.bytes_received = 0
        self.last_data_time: Optional[float] = None

    def _build_auth_header(self) -> str:
        """Build HTTP Basic Authorization header."""
        if not self.username:
            return ""
        credentials = f"{self.username}:{self.password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Authorization: Basic {encoded}\r\n"

    def _build_request(self, path: str) -> bytes:
        """Build HTTP GET request for NTRIP."""
        request = (
            f"GET {path} HTTP/1.0\r\n"
            f"Host: {self.caster}\r\n"
            f"User-Agent: NTRIP HaptiBand/1.0\r\n"
            f"Ntrip-Version: Ntrip/2.0\r\n"
            f"{self._build_auth_header()}"
            f"\r\n"
        )
        return request.encode()

    def get_sourcetable(self) -> list[dict]:
        """
        Fetch the sourcetable (list of available mountpoints) from the caster.

        Returns a list of dicts with mountpoint info.
        """
        mountpoints = []

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(10.0)
            sock.connect((self.caster, self.port))
            sock.sendall(self._build_request("/"))

            response = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                response += chunk

            sock.close()

            # Parse response
            lines = response.decode("utf-8", errors="ignore").split("\r\n")

            for line in lines:
                if line.startswith("STR;"):
                    # STR record format: STR;mountpoint;identifier;format;...
                    parts = line.split(";")
                    if len(parts) >= 4:
                        mountpoints.append({
                            "mountpoint": parts[1],
                            "identifier": parts[2],
                            "format": parts[3],
                            "raw": line,
                        })

        except Exception as e:
            print(f"Error fetching sourcetable: {e}")

        return mountpoints

    def connect(self) -> bool:
        """Connect to the NTRIP caster and start receiving data."""
        if self.connected:
            return True

        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.settimeout(10.0)
            self.sock.connect((self.caster, self.port))

            # Send request for mountpoint
            request = self._build_request(f"/{self.mountpoint}")
            self.sock.sendall(request)

            # Read response header
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = self.sock.recv(1)
                if not chunk:
                    raise ConnectionError("Connection closed during header read")
                response += chunk

            header = response.decode("utf-8", errors="ignore")

            # Check for success (ICY 200 OK or HTTP/1.x 200)
            if "200" not in header.split("\r\n")[0]:
                print(f"Connection failed: {header.split(chr(13))[0]}")
                self.sock.close()
                return False

            print(f"Connected to {self.caster}:{self.port}/{self.mountpoint}")
            self.connected = True
            self.sock.settimeout(30.0)  # Longer timeout for data stream
            return True

        except Exception as e:
            print(f"Connection error: {e}")
            if self.sock:
                self.sock.close()
            return False

    def disconnect(self):
        """Disconnect from the caster."""
        self.running = False
        self.connected = False
        if self.sock:
            try:
                self.sock.close()
            except:
                pass
            self.sock = None

    def read_data(self, timeout: float = 1.0) -> Optional[bytes]:
        """
        Read available RTCM data from the stream.

        Returns bytes if data available, None on timeout/error.
        """
        if not self.connected or not self.sock:
            return None

        try:
            self.sock.settimeout(timeout)
            data = self.sock.recv(1024)
            if data:
                with self._lock:
                    self.bytes_received += len(data)
                    self.last_data_time = time.time()
                return data
        except socket.timeout:
            return None
        except Exception as e:
            print(f"Read error: {e}")
            self.connected = False
            return None

        return None

    def start_stream(self):
        """Start background thread to continuously receive data."""
        if self.running:
            return

        self.running = True
        self._thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._thread.start()

    def stop_stream(self):
        """Stop the background streaming thread."""
        self.running = False
        if self._thread:
            self._thread.join(timeout=2.0)
        self.disconnect()

    def _stream_loop(self):
        """Background loop that receives RTCM data."""
        while self.running:
            if not self.connected:
                if not self.connect():
                    print("Reconnecting in 5 seconds...")
                    time.sleep(5)
                    continue

            data = self.read_data(timeout=1.0)
            if data and self.on_data:
                self.on_data(data)

    def get_stats(self) -> dict:
        """Get connection statistics."""
        with self._lock:
            return {
                "connected": self.connected,
                "bytes_received": self.bytes_received,
                "last_data_time": self.last_data_time,
            }


def format_rtcm_preview(data: bytes, max_bytes: int = 32) -> str:
    """Format RTCM binary data for display."""
    preview = data[:max_bytes].hex()
    if len(data) > max_bytes:
        preview += "..."
    return preview


def main():
    """Interactive test mode for NTRIP client."""
    import argparse

    parser = argparse.ArgumentParser(description="NTRIP Client for RTK corrections")
    parser.add_argument("--caster", default=DEFAULT_CASTER, help="NTRIP caster hostname")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Caster port")
    parser.add_argument("--mountpoint", default=DEFAULT_MOUNTPOINT, help="Mountpoint name")
    parser.add_argument("--user", default=DEFAULT_USERNAME, help="Username")
    parser.add_argument("--password", default=DEFAULT_PASSWORD, help="Password")
    parser.add_argument("--list", action="store_true", help="List available mountpoints")
    args = parser.parse_args()

    client = NTRIPClient(
        caster=args.caster,
        port=args.port,
        mountpoint=args.mountpoint,
        username=args.user,
        password=args.password,
    )

    if args.list:
        print(f"Fetching sourcetable from {args.caster}:{args.port}...")
        mountpoints = client.get_sourcetable()

        if not mountpoints:
            print("No mountpoints found or error fetching sourcetable.")
            return

        print(f"\nFound {len(mountpoints)} mountpoints:\n")
        print(f"{'Mountpoint':<20} {'Format':<15} {'Identifier'}")
        print("-" * 60)
        for mp in mountpoints:
            print(f"{mp['mountpoint']:<20} {mp['format']:<15} {mp['identifier']}")
        return

    if not args.mountpoint:
        print("Error: --mountpoint is required. Use --list to see available mountpoints.")
        return

    # Data callback - just print for testing
    def on_rtcm_data(data: bytes):
        stats = client.get_stats()
        print(f"\rRTCM: {len(data):4d} bytes | Total: {stats['bytes_received']:>8d} bytes | {format_rtcm_preview(data, 16)}", end="")

    client.on_data = on_rtcm_data

    print(f"Connecting to {args.caster}:{args.port}/{args.mountpoint}...")
    print("Press Ctrl+C to stop.\n")

    try:
        client.start_stream()
        while True:
            time.sleep(1)
            stats = client.get_stats()
            if not stats["connected"]:
                print("\nDisconnected, attempting reconnect...")
    except KeyboardInterrupt:
        print("\n\nStopping...")
    finally:
        client.stop_stream()
        stats = client.get_stats()
        print(f"Total bytes received: {stats['bytes_received']}")


if __name__ == "__main__":
    main()
