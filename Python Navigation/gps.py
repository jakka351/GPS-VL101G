"""
GPS serial reader for the VL101G.

Connects to the device's TTL UART (Blue=RX, Green=TX, 3.3V logic).
Reads NMEA sentences output by the AG3335 GNSS module and exposes the
parsed position data via a thread-safe Position object.

If the VL101G's main MCU does NOT pass through raw NMEA on the TTL UART
and instead emits binary GT06 packets or vendor-specific log lines, set
RAW_LOG_MODE = True at runtime to capture raw bytes for analysis.

Default serial config:
  Port: /dev/serial0       (Pi primary UART; symlink to ttyAMA0 on Pi 4/5)
  Baud: 9600 (try 38400, 57600, 115200 if 9600 produces nothing readable)

Pi UART setup (one-time):
  sudo raspi-config  →  Interface Options  →  Serial Port
    "Login shell over serial?" → No
    "Serial port hardware enabled?" → Yes
  sudo reboot

Wiring:
  VL101G Green (TX)  →  Pi GPIO 15 / pin 10 (RX)
  VL101G Blue  (RX)  →  Pi GPIO 14 / pin 8  (TX)
  Common ground.
"""

from __future__ import annotations
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# pyserial / pynmea2 are only needed for the real reader. Allow the simulator
# (and by extension Windows test mode) to run even when they're not installed.
try:
    import serial
except ImportError:                       # pragma: no cover
    serial = None                          # type: ignore
try:
    import pynmea2
except ImportError:                       # pragma: no cover
    pynmea2 = None                         # type: ignore


log = logging.getLogger(__name__)

DEFAULT_PORT = "/dev/serial0"
DEFAULT_BAUD = 9600
RECONNECT_DELAY_S = 2.0
RAW_LOG_MODE = False        # flip to True to log every raw line read


@dataclass
class Position:
    """Latest known position from the GPS. All fields are atomic-ish under GIL."""
    lat: Optional[float]            = None
    lon: Optional[float]            = None
    altitude_m: Optional[float]     = None
    speed_kmh: Optional[float]      = None
    heading_deg: Optional[float]    = None
    fix_quality: int                = 0          # 0=no fix, 1=GPS, 2=DGPS
    satellites_used: int            = 0
    hdop: Optional[float]           = None
    timestamp_utc: Optional[datetime] = None
    last_update_local: Optional[datetime] = None
    fix_valid: bool                 = False

    def has_fix(self) -> bool:
        return self.fix_valid and self.lat is not None and self.lon is not None

    def age_seconds(self) -> Optional[float]:
        if self.last_update_local is None:
            return None
        return (datetime.now() - self.last_update_local).total_seconds()


class GpsReader(threading.Thread):
    """Background thread reading NMEA from the serial port."""

    def __init__(self, port: str = DEFAULT_PORT, baud: int = DEFAULT_BAUD):
        super().__init__(daemon=True, name="GpsReader")
        if serial is None or pynmea2 is None:
            raise RuntimeError(
                "GpsReader requires pyserial + pynmea2. "
                "Install them, or use SimGpsReader for hardware-free testing.")
        self.port = port
        self.baud = baud
        self.position = Position()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._serial: Optional["serial.Serial"] = None
        self.connected = False
        self.last_error: Optional[str] = None

    # ── public API ──────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Position:
        """Return a copy of the current position. Safe to call from the UI thread."""
        with self._lock:
            return Position(
                lat=self.position.lat,
                lon=self.position.lon,
                altitude_m=self.position.altitude_m,
                speed_kmh=self.position.speed_kmh,
                heading_deg=self.position.heading_deg,
                fix_quality=self.position.fix_quality,
                satellites_used=self.position.satellites_used,
                hdop=self.position.hdop,
                timestamp_utc=self.position.timestamp_utc,
                last_update_local=self.position.last_update_local,
                fix_valid=self.position.fix_valid,
            )

    # ── thread loop ─────────────────────────────────────────────────────
    def run(self) -> None:
        log.info("GPS reader started on %s @ %d baud", self.port, self.baud)
        while not self._stop.is_set():
            try:
                self._connect()
                self._read_loop()
            except serial.SerialException as e:
                self.connected = False
                self.last_error = str(e)
                log.warning("Serial error: %s — retrying in %.1fs", e, RECONNECT_DELAY_S)
                self._stop.wait(RECONNECT_DELAY_S)
            except Exception:
                self.connected = False
                log.exception("Unexpected error in GPS reader, retrying")
                self._stop.wait(RECONNECT_DELAY_S)
        self._close()
        log.info("GPS reader stopped")

    def _connect(self) -> None:
        if self._serial and self._serial.is_open:
            return
        self._serial = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1.0,
        )
        self.connected = True
        self.last_error = None
        log.info("Serial opened: %s", self.port)

    def _close(self) -> None:
        if self._serial:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        self.connected = False

    def _read_loop(self) -> None:
        assert self._serial is not None
        buf = b""
        while not self._stop.is_set():
            chunk = self._serial.read(256)
            if not chunk:
                continue
            buf += chunk
            # Split on newlines — NMEA sentences are CR LF terminated
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip(b"\r ")
                if not line:
                    continue
                try:
                    text = line.decode("ascii", errors="replace")
                except Exception:
                    continue
                if RAW_LOG_MODE:
                    log.debug("RAW: %r", text)
                self._handle_sentence(text)

    def _handle_sentence(self, sentence: str) -> None:
        if not sentence.startswith("$"):
            # Not NMEA — could be AT response, GT06 binary fragment, vendor log.
            # Silently ignore unless raw mode is on.
            return
        try:
            msg = pynmea2.parse(sentence)
        except pynmea2.ParseError:
            return

        with self._lock:
            self.position.last_update_local = datetime.now()

            # GGA — Global Positioning System Fix Data (position + altitude + sats)
            if isinstance(msg, pynmea2.GGA):
                if msg.gps_qual in (None, 0):
                    self.position.fix_quality = 0
                    self.position.fix_valid = False
                    return
                try:
                    self.position.lat = float(msg.latitude)
                    self.position.lon = float(msg.longitude)
                    if msg.altitude is not None:
                        self.position.altitude_m = float(msg.altitude)
                    self.position.fix_quality = int(msg.gps_qual)
                    self.position.satellites_used = int(msg.num_sats or 0)
                    if msg.horizontal_dil:
                        self.position.hdop = float(msg.horizontal_dil)
                    self.position.fix_valid = True
                except (ValueError, TypeError):
                    pass

            # RMC — Recommended Minimum (position + speed + heading + UTC time)
            elif isinstance(msg, pynmea2.RMC):
                if msg.status != "A":   # A = active, V = void
                    return
                try:
                    self.position.lat = float(msg.latitude)
                    self.position.lon = float(msg.longitude)
                    if msg.spd_over_grnd is not None:
                        # NMEA gives knots — convert to km/h
                        self.position.speed_kmh = float(msg.spd_over_grnd) * 1.852
                    if msg.true_course is not None:
                        self.position.heading_deg = float(msg.true_course)
                    if msg.datestamp and msg.timestamp:
                        self.position.timestamp_utc = datetime.combine(
                            msg.datestamp, msg.timestamp, tzinfo=timezone.utc)
                    self.position.fix_valid = True
                except (ValueError, TypeError):
                    pass

            # VTG — Track made good and ground speed (independent speed source)
            elif isinstance(msg, pynmea2.VTG):
                try:
                    if msg.spd_over_grnd_kmph is not None:
                        self.position.speed_kmh = float(msg.spd_over_grnd_kmph)
                    if msg.true_track is not None:
                        self.position.heading_deg = float(msg.true_track)
                except (ValueError, TypeError):
                    pass

            # GSA — DOP and active satellites
            elif isinstance(msg, pynmea2.GSA):
                try:
                    if msg.hdop:
                        self.position.hdop = float(msg.hdop)
                except (ValueError, TypeError):
                    pass


# ── simulated reader (for Windows test mode / bench dev) ──────────────────
class SimGpsReader(threading.Thread):
    """Drop-in replacement for GpsReader that produces synthetic position
    data. Same `snapshot()` API, same `connected` flag, same `stop()` semantics.

    Drives a smooth circular path around (center_lat, center_lon) at the
    configured speed. Heading + speed are derived from the velocity vector
    so the speedo and compass rose update believably.

    Scenarios:
        "loop"     — circles a 2km loop around the centre at ~50 km/h
        "static"   — sits at the centre (good for testing UI without motion)
        "drift"    — slow random walk (good for testing the off-route alarm)
    """

    SCENARIOS = ("loop", "static", "drift")

    def __init__(self,
                 center_lat: float = -31.9505,
                 center_lon: float = 115.8605,
                 scenario: str = "loop",
                 speed_kmh: float = 50.0,
                 radius_m: float = 1000.0):
        super().__init__(daemon=True, name="SimGpsReader")
        if scenario not in self.SCENARIOS:
            raise ValueError(f"scenario must be one of {self.SCENARIOS}")
        self.center_lat = center_lat
        self.center_lon = center_lon
        self.scenario = scenario
        self.speed_kmh = speed_kmh
        self.radius_m = radius_m
        self.position = Position()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = True
        self.last_error: Optional[str] = None
        # Pretend we have a GNSS-like sat count
        self._fake_sats = 11
        self._t0 = time.time()

    # ── public API (matches GpsReader) ──────────────────────────────────
    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> Position:
        with self._lock:
            return Position(
                lat=self.position.lat,
                lon=self.position.lon,
                altitude_m=self.position.altitude_m,
                speed_kmh=self.position.speed_kmh,
                heading_deg=self.position.heading_deg,
                fix_quality=self.position.fix_quality,
                satellites_used=self.position.satellites_used,
                hdop=self.position.hdop,
                timestamp_utc=self.position.timestamp_utc,
                last_update_local=self.position.last_update_local,
                fix_valid=self.position.fix_valid,
            )

    # ── thread loop ─────────────────────────────────────────────────────
    def run(self) -> None:
        log.info("SimGpsReader running (%s, %.0f km/h, %.0fm radius)",
                 self.scenario, self.speed_kmh, self.radius_m)
        # Very rough deg-per-metre at this latitude
        m_per_deg_lat = 110_574.0
        m_per_deg_lon = 111_320.0 * max(0.01, math.cos(math.radians(self.center_lat)))

        prev_lat: Optional[float] = None
        prev_lon: Optional[float] = None
        prev_t: Optional[float] = None

        # Circle period (seconds) for the loop scenario, derived from circumference / speed
        circumference_m = 2 * math.pi * self.radius_m
        period_s = circumference_m / max(0.1, self.speed_kmh / 3.6)

        # Random-walk state for "drift"
        import random
        drift_dx = 0.0
        drift_dy = 0.0

        while not self._stop.is_set():
            now = time.time()
            elapsed = now - self._t0

            if self.scenario == "static":
                lat = self.center_lat
                lon = self.center_lon
                heading = 0.0
                speed = 0.0

            elif self.scenario == "drift":
                # Wander up to ~150m from center with smooth small steps
                drift_dx += random.uniform(-0.5, 0.5)
                drift_dy += random.uniform(-0.5, 0.5)
                drift_dx = max(-150, min(150, drift_dx))
                drift_dy = max(-150, min(150, drift_dy))
                lat = self.center_lat + drift_dy / m_per_deg_lat
                lon = self.center_lon + drift_dx / m_per_deg_lon
                heading = None  # unknown when drifting at walking pace
                speed = 0.0

            else:  # "loop"
                phase = (elapsed % period_s) / period_s * 2 * math.pi
                # parametric circle: x east, y north
                dx_m = self.radius_m * math.sin(phase)
                dy_m = self.radius_m * math.cos(phase)
                lat = self.center_lat + dy_m / m_per_deg_lat
                lon = self.center_lon + dx_m / m_per_deg_lon
                # Heading derived from instantaneous velocity (clockwise loop)
                vx = math.cos(phase)        # east component
                vy = -math.sin(phase)       # north component
                heading = (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0
                speed = self.speed_kmh

            # If we have a previous fix, also derive empirical speed (sanity)
            if prev_lat is not None and prev_t is not None and now > prev_t:
                dlat_m = (lat - prev_lat) * m_per_deg_lat
                dlon_m = (lon - prev_lon) * m_per_deg_lon
                dist_m = math.sqrt(dlat_m * dlat_m + dlon_m * dlon_m)
                empirical_kmh = (dist_m / (now - prev_t)) * 3.6
                # Blend planned & empirical so it looks slightly noisy
                if speed > 0:
                    speed = (speed * 0.7) + (empirical_kmh * 0.3)

            with self._lock:
                self.position.lat = lat
                self.position.lon = lon
                self.position.altitude_m = 25.0
                self.position.speed_kmh = speed
                self.position.heading_deg = heading
                self.position.fix_quality = 1
                self.position.satellites_used = self._fake_sats
                self.position.hdop = 0.9
                self.position.timestamp_utc = datetime.now(timezone.utc)
                self.position.last_update_local = datetime.now()
                self.position.fix_valid = True

            prev_lat, prev_lon, prev_t = lat, lon, now
            # ~2 Hz update rate, like a real GNSS module
            self._stop.wait(0.5)
        self.connected = False
        log.info("SimGpsReader stopped")


# ── stand-alone test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    """Run gps.py directly to verify your serial connection or test the
    simulator.

    Usage:
        python3 gps.py /dev/serial0 9600       # real device
        python3 gps.py --sim                   # simulated (loop)
        python3 gps.py --sim drift             # simulated (random walk)
        python3 gps.py --sim static            # simulated (stationary)
    """
    import sys
    logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if len(sys.argv) > 1 and sys.argv[1] == "--sim":
        scenario = sys.argv[2] if len(sys.argv) > 2 else "loop"
        reader: "threading.Thread" = SimGpsReader(scenario=scenario)
    else:
        port = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_PORT
        baud = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_BAUD
        reader = GpsReader(port=port, baud=baud)
    reader.start()
    try:
        while True:
            time.sleep(2)
            p = reader.snapshot()
            print(f"connected={reader.connected} fix={p.fix_valid} sats={p.satellites_used} "
                  f"lat={p.lat} lon={p.lon} kmh={p.speed_kmh} hdg={p.heading_deg} hdop={p.hdop}")
    except KeyboardInterrupt:
        reader.stop()
        reader.join(timeout=3)
