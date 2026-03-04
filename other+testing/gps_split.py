import math

# Conversion constants
FEET_PER_DEGREE_LAT = 364567.2  # approximately constant

def feet_to_degrees(feet, latitude):
    """Convert feet to degrees for lat/lon at a given latitude."""
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


if __name__ == "__main__":
    data = 'GPS:35.303276,-120.664299|IMU:3'
    spacing_feet = 3.0 

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
        print(f"Sending to headband: {formatted_msg}")
        i += 1
