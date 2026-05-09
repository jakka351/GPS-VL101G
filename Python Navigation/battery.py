"""
BLE battery monitor reader for the Leagend / FPV-GS-002-style 12V battery
sensor (the same device targeted by jakka351/Battery-Monitor-Protocol).

Mirrors the threading pattern used by gps.py:
  - Background thread owns an asyncio loop running a Bleak client
  - The UI thread calls snapshot() to get the latest reading
  - Auto-reconnects on disconnect / failure
  - Exposes a `--sim` mode so the GUI can be developed without the device

Wire protocol (preserved from the original monitor.py):
  - GATT characteristic 0000fff4-0000-1000-8000-00805f9b34fb sends notifications
  - Each notification is AES-128-CBC encrypted with a fixed key + zero IV
  - Decrypted payload: hex chars 2..5 = voltage*100, chars 6..8 = charge%

NOTE: this is a one-way passive sniff. We only subscribe to notifications;
we never write to the device. Safe to leave running in the background.
"""

from __future__ import annotations
import asyncio
import binascii
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Deque, Optional

try:
    from bleak import BleakClient
except ImportError:                       # allow --sim mode without bleak installed
    BleakClient = None                    # type: ignore

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None                            # type: ignore


log = logging.getLogger(__name__)

# ── wire protocol constants (do not change without retesting) ─────────────
KEY = bytes((b & 255) for b in
            [108, 101, 97, 103, 101, 110, 100, -1, -2, 49, 56, 56, 50, 52, 54, 54])
CHAR_UUID = "0000fff4-0000-1000-8000-00805f9b34fb"

# ── reconnect / sim tuning ────────────────────────────────────────────────
RECONNECT_DELAY_S       = 3.0
SIM_PACKET_INTERVAL_S   = 0.45
HISTORY_LEN             = 240        # ~2 min at ~2 Hz, used for trend detection

# Lead-acid 12V sanity thresholds (rough; vehicle alternator typically 13.8-14.4V)
V_CHARGING_THRESHOLD    = 13.2       # alternator on / charger connected
V_LOW_WARN              = 12.0       # roughly ~50% SoC at rest
V_CRITICAL              = 11.6       # roughly empty


@dataclass
class BatteryReading:
    """Latest known battery state. All fields atomic-ish under the GIL."""
    voltage:        Optional[float]    = None
    charge_pct:     Optional[int]      = None
    state:          str                = "unknown"   # "charging" | "discharging" | "rest" | "unknown"
    raw_hex:        str                = ""
    last_update:    Optional[datetime] = None
    packet_count:   int                = 0
    error_count:    int                = 0

    def has_data(self) -> bool:
        return self.voltage is not None and self.last_update is not None

    def age_seconds(self) -> Optional[float]:
        if self.last_update is None:
            return None
        return (datetime.now() - self.last_update).total_seconds()

    def is_stale(self, max_age_s: float = 8.0) -> bool:
        age = self.age_seconds()
        return age is None or age > max_age_s


# ── decryption / parsing (preserved from monitor.py) ──────────────────────
def _decrypt(data: bytes) -> bytes:
    if AES is None:
        raise RuntimeError("pycryptodome not installed — pip install pycryptodome")
    if not data or len(data) % 16 != 0:
        raise ValueError(f"AES-CBC payload must be a non-zero multiple of 16; got {len(data)}")
    cipher = AES.new(KEY, AES.MODE_CBC, b"\x00" * 16)
    return cipher.decrypt(data)


def _parse(payload: bytes) -> tuple[float, int, str]:
    raw = binascii.hexlify(payload).decode()
    if len(raw) < 8:
        raise ValueError(f"decrypted packet too short: {raw!r}")
    voltage = int(raw[2:5], 16) / 100.0
    charge = max(0, min(100, int(raw[6:8], 16)))
    return voltage, charge, raw


def _classify_state(voltage_history: Deque[float], current_v: float) -> str:
    """Heuristic battery state from short voltage history.

    - voltage > 13.2V        → charging (alternator/charger active)
    - falling > 0.05V/min    → discharging
    - otherwise              → rest
    """
    if current_v >= V_CHARGING_THRESHOLD:
        return "charging"
    if len(voltage_history) < 8:
        return "rest"
    # Compare median of oldest quarter vs newest quarter
    n = len(voltage_history)
    old = sorted(list(voltage_history)[: n // 4])
    new = sorted(list(voltage_history)[-n // 4:])
    if not old or not new:
        return "rest"
    delta = new[len(new) // 2] - old[len(old) // 2]
    if delta < -0.03:
        return "discharging"
    if delta > 0.03:
        return "charging"
    return "rest"


# ── reader threads ────────────────────────────────────────────────────────
class _BaseReader(threading.Thread):
    """Shared snapshot + history bookkeeping for both BLE and sim readers."""

    def __init__(self, name: str):
        super().__init__(daemon=True, name=name)
        self.reading        = BatteryReading()
        self._lock          = threading.Lock()
        self._stop          = threading.Event()
        self._history: Deque[float] = deque(maxlen=HISTORY_LEN)
        self.connected      = False
        self.last_error: Optional[str] = None

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> BatteryReading:
        with self._lock:
            r = self.reading
            return BatteryReading(
                voltage=r.voltage,
                charge_pct=r.charge_pct,
                state=r.state,
                raw_hex=r.raw_hex,
                last_update=r.last_update,
                packet_count=r.packet_count,
                error_count=r.error_count,
            )

    def _record(self, voltage: float, charge: int, raw: str) -> None:
        with self._lock:
            self._history.append(voltage)
            self.reading.voltage      = voltage
            self.reading.charge_pct   = charge
            self.reading.raw_hex      = raw
            self.reading.last_update  = datetime.now()
            self.reading.packet_count += 1
            self.reading.state        = _classify_state(self._history, voltage)

    def _record_error(self, msg: str) -> None:
        with self._lock:
            self.reading.error_count += 1
            self.last_error = msg


class BatteryReader(_BaseReader):
    """Real BLE reader — connects to the device by MAC address."""

    def __init__(self, address: str):
        super().__init__(name="BatteryReader")
        self.address = address

    def run(self) -> None:
        if BleakClient is None:
            log.error("bleak not installed — battery reader cannot start")
            self.last_error = "bleak not installed"
            return
        try:
            asyncio.run(self._async_loop())
        except Exception:
            log.exception("Battery reader crashed")

    async def _async_loop(self) -> None:
        log.info("BatteryReader starting on %s", self.address)

        def on_notify(_handle, data: bytearray) -> None:
            try:
                voltage, charge, raw = _parse(_decrypt(bytes(data)))
                self._record(voltage, charge, raw)
            except Exception as exc:
                log.debug("packet decode failed: %s", exc)
                self._record_error(str(exc))

        while not self._stop.is_set():
            try:
                async with BleakClient(self.address) as client:
                    self.connected = True
                    self.last_error = None
                    log.info("BLE connected: %s", self.address)
                    await client.start_notify(CHAR_UUID, on_notify)
                    while not self._stop.is_set() and client.is_connected:
                        await asyncio.sleep(0.25)
                    try:
                        await client.stop_notify(CHAR_UUID)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                log.warning("BLE error: %s — retry in %.1fs", exc, RECONNECT_DELAY_S)
                # interruptible sleep
                for _ in range(int(RECONNECT_DELAY_S * 4)):
                    if self._stop.is_set():
                        return
                    await asyncio.sleep(0.25)
        self.connected = False
        log.info("BatteryReader stopped")


class SimBatteryReader(_BaseReader):
    """Synthetic data source for bench testing without the BLE device."""

    def __init__(self, scenario: str = "discharge"):
        super().__init__(name="SimBatteryReader")
        self.scenario = scenario   # "discharge" | "charge" | "rest" | "wave"

    def run(self) -> None:
        log.info("SimBatteryReader starting (%s)", self.scenario)
        self.connected = True
        self.last_error = None
        charge = 82
        if self.scenario == "charge":
            base = 14.0
        elif self.scenario == "rest":
            base = 12.65
        else:
            base = 12.55
        phase = 0.0
        while not self._stop.is_set():
            phase += 0.07
            if self.scenario == "wave":
                voltage = 12.6 + math.sin(phase) * 1.8
            else:
                voltage = base + math.sin(phase) * 0.10 + random.uniform(-0.03, 0.03)
                if self.scenario == "discharge":
                    base -= 0.0008          # slow drain
                elif self.scenario == "charge":
                    base = min(14.4, base + 0.0006)
            if random.random() < 0.05:
                charge = max(0, min(100, charge + random.choice([-1, 0, 0, 1])))
            self._record(voltage, charge, f"sim-{voltage:.2f}-{charge:02d}")
            time.sleep(SIM_PACKET_INTERVAL_S)
        self.connected = False


# ── stand-alone diagnostic ────────────────────────────────────────────────
if __name__ == "__main__":
    """Quick CLI tester. Usage:
        python3 battery.py AA:BB:CC:DD:EE:FF
        python3 battery.py --sim
        python3 battery.py --sim charge
    """
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args = sys.argv[1:]
    if not args:
        print("usage: battery.py <MAC> | --sim [scenario]")
        sys.exit(2)

    if args[0] == "--sim":
        reader = SimBatteryReader(args[1] if len(args) > 1 else "discharge")
    else:
        reader = BatteryReader(args[0])
    reader.start()
    try:
        while True:
            time.sleep(2)
            r = reader.snapshot()
            print(f"connected={reader.connected:<5}  v={r.voltage}  "
                  f"soc={r.charge_pct}%  state={r.state}  pkts={r.packet_count}  "
                  f"err={r.error_count}  age={r.age_seconds()}")
    except KeyboardInterrupt:
        reader.stop()
        reader.join(timeout=3)
