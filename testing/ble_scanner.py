import asyncio
from bleak import BleakScanner

async def scan_devices():
    print("Scanning for BLE devices...")
    devices = await BleakScanner.discover()
    for device in devices:
        print(f"Found device: {device.name}, Address: {device.address}")

if __name__ == "__main__":
    asyncio.run(scan_devices())