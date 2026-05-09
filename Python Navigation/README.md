# Open Source Offline Navigation from VL101G Serial UART

<img width="1293" height="822" alt="image" src="https://github.com/user-attachments/assets/5e024a28-c6a0-4d12-a4a9-b277182ed8ac" />
A car-head-unit-style navigation GUI written in Python + Tkinter, designed
to run on a Raspberry Pi connected to a VL101G GPS tracker via serial UART.

Displays live GPS position on an OpenStreetMap, lets the driver search
for a destination, computes a driving route via OSRM, and provides
turn-by-turn navigation — all on a dark, glanceable interface designed
for in-vehicle use.

## What it does

- **Live GPS** — reads NMEA sentences from the VL101G via Pi serial UART
  (`/dev/serial0`), parses position / speed / heading / satellite count
- **Live map** — OpenStreetMap tiles with smooth pan/zoom, current-position
  marker, optional auto-recenter on movement
- **Destination search** — geocodes addresses / place names via OpenStreetMap
  Nominatim (Australian results biased; configurable)
- **Routing** — driving routes via the OSRM public demo server
- **Turn-by-turn** — large arrow + distance + instruction + street name
- **ETA / remaining distance** — live updated as you drive
- **Off-route detection** — automatically recalculates if you deviate >80m
- **Right-click on map** — set any point as destination
- **Fullscreen kiosk mode** — designed for permanent car-display install
- **Works offline (gracefully)** — pre-cached tiles, favourites file, raw
  coord entry, route cache, and a compass-bearing fallback when OSRM
  is unreachable. Designed for a Pi with intermittent home-base internet.
- **BLE battery monitor card** — optional integration with the Leagend /
  FPV-GS-002-style 12V battery sensor. Live voltage, charge %, and
  charging/discharging state, all on the sidebar.

## Architecture

```
┌────────────────────────────────────────────────────────────┐
│                          main.py                           │
│  ┌─────────────────────┐  ┌──────────────────────────────┐ │
│  │  Tkinter UI         │  │  Background threads          │ │
│  │  - StatusBar        │  │  - GpsReader     (serial)    │ │
│  │  - SpeedoCard       │  │  - BatteryReader (BLE async) │ │
│  │  - NavCard          │  │  - Geocode       (HTTP)      │ │
│  │  - TripStats        │  │  - Route calc    (HTTP)      │ │
│  │  - BatteryCard      │  │  - Online probe  (TCP)       │ │
│  │  - SearchBar        │  │                              │ │
│  │  - MapView          │  │  ↕ thread-safe snapshots    │ │
│  └─────────────────────┘  └──────────────────────────────┘ │
└────────────────────────────────────────────────────────────┘
   │           │            │            │            │
┌──▼──┐    ┌───▼───┐    ┌───▼───┐    ┌───▼───┐    ┌───▼────┐
│gps. │    │battery│    │ nav.  │    │offline│    │ theme. │
│ py  │    │  .py  │    │  py   │    │  .py  │    │  py    │
│NMEA │    │ Bleak │    │Nomin- │    │favs + │    │colours │
│     │    │ + AES │    │atim + │    │coords │    │fonts + │
│     │    │decrypt│    │ OSRM  │    │compass│    │config  │
└─────┘    └───────┘    └───────┘    └───────┘    └────────┘
                            │            │
                       ┌────▼────────────▼────┐
                       │  Route cache (JSON)   │
                       │  Tile cache (SQLite)  │
                       │  ~/.cache/repatriate/ │
                       │  /tmp/repatriate_*    │
                       └───────────────────────┘
```

## Files

| File | Purpose |
|------|---------|
| `main.py` | Tkinter app — all UI components, event loop, orchestration |
| `gps.py` | Serial reader thread; NMEA parsing; thread-safe `Position` snapshot |
| `nav.py` | Geocoding (Nominatim) + routing (OSRM) + offline-fallback wrapper |
| `battery.py` | BLE reader thread for the Leagend battery sensor; AES decrypt + parse |
| `offline.py` | Favourites loader, coord parser, compass-mode nav, route cache, online probe |
| `tile_prefetch.py` | Standalone script — pre-download OSM tiles for offline use |
| `favourites.json` | Editable file of saved destinations (Home, Work, etc.) |
| `theme.py` | Colours, fonts, sizing constants, cache paths, voltage thresholds |
| `requirements.txt` | Python dependencies |
| `setup_pi.sh` | One-shot Pi setup: packages, UART config, venv, autostart |
| `README.md` | This file |

## Setup (Raspberry Pi)

```bash
# Clone or copy the gui/ folder to the Pi, then:
cd gui
bash setup_pi.sh
sudo reboot
```

Then after reboot:

```bash
cd gui
source .venv/bin/activate

# Test GPS connection in isolation
python3 gps.py /dev/serial0 9600

# When that's showing position data, launch the full GUI
python3 main.py --fullscreen
```

## Test mode (Windows / Mac / Linux desktop, no hardware)

The whole GUI runs on any desktop OS in **demo mode** with simulated GPS
and simulated BLE battery — useful for development, screenshots, and
designing layout changes without having to plug into the Pi.

You only need three packages (no `pyserial`, no `bleak`):

```powershell
# Windows PowerShell or cmd:
python -m venv .venv
.venv\Scripts\activate
pip install tkintermapview Pillow requests
python main.py --demo
```

```bash
# macOS / Linux:
python3 -m venv .venv
source .venv/bin/activate
pip install tkintermapview Pillow requests
python3 main.py --demo
```

`--demo` is shorthand for `--gps-sim loop --battery-sim discharge`.
The synthetic GPS drives a 1km circular loop around Perth CBD at
~50 km/h; the synthetic battery slowly discharges from a healthy
~12.6V. Search bar, map clicks, OSRM routing, off-route detection
and the battery card all work as they would on the real Pi.

Variations:

```bash
# Different scenarios
python main.py --gps-sim static --battery-sim charge        # parked + charging
python main.py --gps-sim drift  --battery-sim wave          # walking pace, big V swings
python main.py --gps-sim loop   --battery-sim rest          # driving + battery resting

# Centre the simulated drive somewhere else (Melbourne CBD shown)
python main.py --demo --sim-center "-37.8136,144.9631"

# No fullscreen on a desktop is usually nicer
python main.py --demo                                       # windowed
python main.py --demo --fullscreen                          # full kiosk

# Same simulator without the GUI — just print synthetic NMEA-equivalent state
python main.py --gps-sim drift --debug                      # in app
python gps.py --sim drift                                   # standalone
```

Caveats for desktop test mode:
- The map tile cache lives in your OS's temp dir
  (`%TEMP%\repatriate_map_cache.db` on Windows). Tiles download on demand
  from OSM the first time you pan to a new area.
- Right-click → "Set as destination" works the same as on the Pi.
- Routes go to the public OSRM demo server; works fine for a laptop
  with internet.
- The favourites file (`favourites.json`) is read from the same
  directory as `main.py` regardless of platform.

## Wiring (VL101G ↔ Pi)

| VL101G wire | Pi GPIO | Pi physical pin | Note |
|---|---|---|---|
| Green (TTL TX) | GPIO 15 (RX) | 10 | Device transmits → Pi receives |
| Blue (TTL RX) | GPIO 14 (TX) | 8 | Pi transmits → Device receives |
| Black (V−) | GND | 6, 9, 14, 20, 25, 30, 34, 39 | Common ground |
| Red (V+) | — | — | Power separately, NOT from Pi 5V |
| Yellow (Relay) | — | — | **Leave disconnected** (see top-level README) |
| Orange (ACC) | — | — | Optional — connect to vehicle ACC if using ignition sensing |

> **3.3V logic.** Pi GPIO is 3.3V. The VL101G TTL UART is 3.3V. No level
> shifter needed. **Do not** wire the device to the Pi's 5V rail; the
> device wants 9-30V from the vehicle supply.

## Keyboard shortcuts

| Key | Action |
|-----|--------|
| `F11` | Toggle fullscreen |
| `Escape` | Quit |
| `Ctrl+L` | Focus search bar (type destination, press Enter) |
| `Ctrl+R` | Re-centre map on current position |
| `Ctrl+X` | Cancel current route |

Right-click anywhere on the map → "Set as destination".

## Configuration

Most tunable parameters live in `theme.py` (colours, fonts, sizes,
behaviour timeouts, voltage thresholds, cache paths). Per-launch options
go on the `main.py` command line:

```bash
python3 main.py \
    --port /dev/ttyUSB0 --baud 38400 \
    --battery-mac AA:BB:CC:DD:EE:FF \
    --fullscreen --debug
```

| Flag | Purpose |
|------|---------|
| `--port`, `--baud` | GPS serial port and baud rate |
| `--fullscreen` | Start in fullscreen kiosk mode |
| `--battery-mac MAC` | Enable the BLE battery card, connect to this device |
| `--battery-sim [scenario]` | Use simulated battery data — no BLE needed. Scenarios: `discharge`, `charge`, `rest`, `wave` |
| `--debug` | Verbose logging |

## Offline / intermittent-internet operation

The GUI is designed for a Pi that has internet only at home base and
nothing on the road. Three layers cover the offline case:

### 1. Map tiles — pre-cached SQLite

Map tiles are served by `tkintermapview` from a SQLite cache (default
`/tmp/repatriate_map_cache.db`). Tiles you've already viewed while online
stay cached and work offline. To **pre-warm** the cache for an area
before you head out, run while you have internet:

```bash
# 25 km radius around Perth CBD, zoom levels 12-17 (~180 MB)
python3 tile_prefetch.py --center "-31.9505,115.8605" \
                          --radius-km 25 --zoom 12-17

# Or a tight bounding box at high detail
python3 tile_prefetch.py --bbox "-32.10,115.65,-31.85,116.00" \
                          --zoom 14-19
```

Rough size guide:

| Coverage | Zoom | Tiles | Disk |
|---|---|---|---|
| 5 km radius | 12-17 | ~600 | ~9 MB |
| 25 km radius | 12-17 | ~12,000 | ~180 MB |
| 25 km radius | 12-18 | ~50,000 | ~750 MB |
| 25 km radius | 12-19 | ~200,000 | ~3 GB |

The script respects OSM's tile-usage policy (~1.5 req/sec, custom
User-Agent). Use `--dry-run` to count tiles before committing.

### 2. Destination lookup — favourites + raw coords

Search bar resolution order:

1. **Coordinate parse** — works with `-31.95, 115.86` or
   `31°57'02"S 115°51'38"E`. Instant, no network.
2. **Favourites match** — case-insensitive substring against
   `favourites.json` (name + aliases). Edit that file to add Home,
   Work, parents' place, etc:

   ```json
   [{"name": "Home", "lat": -31.9505, "lon": 115.8605,
     "aliases": ["house", "casa"], "note": "Front gate"}]
   ```

   Click the **★ Favs** button to see them all.
3. **Nominatim** — only tried if the first two miss. Fails silently
   when offline; the favourites/coords path covers you.

### 3. Routing — three-tier fallback

`nav.compute_route_with_fallback()`:

1. **OSRM live** (online) → full turn-by-turn, blue polyline. Cached on
   disk for next time.
2. **Cached OSRM result** (offline, route was previously computed) →
   purple polyline, full turn-by-turn from cache. Routes are keyed by
   ~100m grid on both endpoints, kept for 7 days.
3. **Compass mode** (offline, no cache hit) → straight-line amber
   polyline, single instruction "Head NNE (037°) — 4.2 km", ETA
   estimated from `NAV_COMPASS_AVG_KMH` (default 60 km/h). Crude but
   genuinely useful when you just need to know which way to point the
   car.

The polyline colour tells you which mode you got: **blue** = fresh
OSRM, **purple** = cached, **amber** = compass-only.

The status bar shows **● NET** in green when online, **○ OFFLINE** in
amber otherwise (probed every 30 seconds, non-blocking).

## Battery monitor (BLE)

Optional integration with a Leagend / FPV-GS-002-style 12V battery
sensor (the same target as
[jakka351/Battery-Monitor-Protocol](https://github.com/jakka351/Battery-Monitor-Protocol)).
Subscribes to GATT characteristic `0000fff4-…`, decrypts each
notification with AES-128-CBC (zero IV, fixed 16-byte key), parses
voltage and charge percentage.

Pair the sensor first using `bluetoothctl`:

```bash
bluetoothctl
[bluetooth]# scan on
   ... wait until you see your device's MAC ...
[bluetooth]# scan off
[bluetooth]# pair AA:BB:CC:DD:EE:FF
[bluetooth]# trust AA:BB:CC:DD:EE:FF
[bluetooth]# exit
```

Then launch the GUI with the MAC:

```bash
python3 main.py --fullscreen --battery-mac AA:BB:CC:DD:EE:FF
```

The **BatteryCard** (sidebar, between trip stats and action buttons)
shows live voltage + charge bar + state (`CHARGING`, `DISCHARGING`,
`REST`, `STALE`) plus a top-right `⚡ 12.62V` strip in the status bar.
Voltage colour-codes:

| Voltage | Colour | Meaning |
|---|---|---|
| ≥ 13.2 V | green | Charging — alternator/charger active |
| 12.0–13.2 V | cyan | Healthy, at-rest range |
| 11.6–12.0 V | amber | Low — recommend a top-up |
| < 11.6 V | red | Critical — cranking risk |

Tune the thresholds in `theme.py` (`BATTERY_V_*`) to match your
battery chemistry — defaults are generic 12V lead-acid.

To bench-test without the BLE device:

```bash
python3 main.py --battery-sim            # synthetic discharge
python3 main.py --battery-sim charge     # synthetic charging
python3 main.py --battery-sim wave       # cycles through full range
```

You can also test the BLE module standalone:

```bash
python3 battery.py AA:BB:CC:DD:EE:FF
python3 battery.py --sim charge
```

## Troubleshooting

### "GPS: serial disconnected"

Means the serial port can't be opened. Check:

```bash
ls -l /dev/serial0
# Should show a symlink to ttyAMA0 or ttyS0
groups
# Should include 'dialout'
```

If `dialout` isn't in your groups, run `sudo usermod -aG dialout $USER`
and log out / log in.

### "GPS: searching" forever

The serial port is connected but no NMEA fix is being parsed. Causes:

- **Antenna** — GNSS needs sky view. Test outdoors or near a window.
- **First-fix delay** — cold start can take 30-60 seconds. Be patient.
- **Wrong baud rate** — try `--baud 38400`, `--baud 57600`, `--baud 115200`.
- **Wiring** — confirm Green ↔ GPIO 15 (RX) and ground common.
- **Device is outputting GT06 binary, not NMEA** — flip
  `RAW_LOG_MODE = True` in `gps.py` and watch what comes through with
  `--debug`. If you see binary, the device's TTL UART carries the
  cellular protocol, not raw GNSS — would need protocol-level decoding.

### Map tiles don't load

If the status bar shows **○ OFFLINE**, your tiles must be pre-cached
(see the "Offline operation" section above). Run `tile_prefetch.py`
once while you have internet to seed the cache.

If you're online but tiles still don't load:

```bash
curl -I https://a.tile.openstreetmap.org/0/0/0.png
```

Should return `HTTP/2 200`. If not, your Pi can't reach OSM. Tiles are
cached on first load to `/tmp/repatriate_map_cache.db`.

For a self-hosted tile source (e.g. for genuinely remote ops), point
`MAP_TILE_SERVER` in `theme.py` at a TileServer GL / Maplibre instance.

### Search returns no results

- **When offline**, type raw coordinates (e.g. `-31.95, 115.86`) or use
  the **★ Favs** button. Nominatim won't work without internet.
- Nominatim has a fair-use rate limit (1 req/sec). Don't hammer it.
- Default `country_codes="au"` biases toward Australian results. For
  global, edit `geocode_async()` calls to pass `country_codes=""`.
- Ambiguous names — try adding state/country, e.g. "Fremantle WA".

### Routing falls back to compass mode

That's by design — when OSRM is unreachable and there's no cached route
for your trip, the GUI shows a straight-line bearing instead of failing.
The amber polyline + the "Compass mode" dialog is your cue. Pre-warm
the cache by visiting common destinations once while online.

The OSRM public demo (`router.project-osrm.org`) is rate-limited and
occasionally goes down. For production, host your own OSRM on the Pi or
a home server: https://github.com/Project-OSRM/osrm-backend

### Battery card stuck on "BLE: searching"

- **Pair the device first.** `bleak` will *not* prompt for pairing on
  the fly — pair via `bluetoothctl` once before launching the GUI.
- **MAC address** — `bluetoothctl scan on` and watch for your device's
  advertised name. Confirm the MAC matches your `--battery-mac`.
- **Permissions** — your user needs to be in the `bluetooth` group
  (added by `setup_pi.sh`). Log out / log back in if you just added it.
- **AES key mismatch** — if you see lots of "packet decode failed"
  messages in `--debug`, the `KEY` constant in `battery.py` doesn't
  match your device's firmware. Compare against monitor.py from
  Battery-Monitor-Protocol; some firmware revisions use a different key.
- **Adapter conflict** — if you have a paired Bluetooth keyboard *and*
  a battery sensor, an old BlueZ on Pi 3 can struggle to maintain both
  connections. Pi 4/5 generally fine.

## Display calibration for car head units

Most car displays are 1024×600 (7"), 1280×720 (8"), or 1280×800 (10").
The GUI is laid out for 1280×800 and degrades gracefully down to 1024×600.

For a car install, also consider:

- **Touchscreen calibration** — `xinput_calibrator` on Pi OS
- **Disable screen blanking** — `sudo raspi-config` → Display → Screen Blanking → Disable
- **Boot directly to GUI** — autostart service installed by `setup_pi.sh`
- **Hide cursor when idle** — install `unclutter`: `sudo apt install unclutter`
- **Splash screen** — `sudo apt install plymouth plymouth-themes`

## Bluetooth keyboard / mouse

Pair via the Pi's Bluetooth admin (`bluetoothctl` or the desktop GUI).
Once paired, both work transparently with Tkinter — no app changes needed.
The keyboard shortcuts above all work with a paired Bluetooth keyboard.

## Limitations & known issues

- **Compass-mode routing is just a straight line** — useful in remote
  areas where you know the road network roughly, useless in dense
  cities. Pre-warm the route cache by visiting common destinations
  online if you anticipate going offline.
- **Tile prefetch obeys OSM's fair-use policy (~1.5 req/sec)** — caching
  a large region takes hours. Self-host a tile server if you need
  fast bulk-prefetch (and to be a good OSM citizen at scale).
- **No speed-limit display** — would require a speed-limit data source
  (OSM `maxspeed` tag scraping or a paid API).
- **No traffic data** — OSRM doesn't provide live traffic.
- **No voice guidance** — visual only. Adding TTS is straightforward
  (e.g., `espeak-ng`) but not implemented yet.
- **Single GPS device** — designed for one VL101G connected via UART;
  not for multi-tracker fleet visualisation. (Use the parent project's
  Traccar setup for multi-device.)
- **Battery monitor key is hardcoded** — works for the Leagend / FPV-GS-002
  but not other BLE battery sensors. Edit `KEY` in `battery.py` if your
  device uses a different one.
- **Tkinter is not GPU-accelerated** — pan/zoom on the map is fine on
  Pi 4/5; older Pis will feel sluggish.

## Future work

- [x] ~~Offline tile bundle (download a region for cached use)~~ — done via `tile_prefetch.py`
- [x] ~~Battery monitor integration~~ — done via `battery.py` + `BatteryCard`
- [x] ~~Favourites / coord entry for offline destination lookup~~ — done via `offline.py` + `favourites.json`
- [ ] Voice guidance (TTS via `espeak-ng` or Piper)
- [ ] Day / night auto theme switch based on local sunset
- [ ] Speed-limit overlay from OSM `maxspeed` tags
- [ ] Lane guidance for motorway interchanges
- [ ] Customisable POI overlay (fuel, charging, etc.)
- [ ] Self-hosted OSRM on the Pi for genuine offline turn-by-turn
- [ ] Integration with Traccar so the same Pi can both navigate AND host
      the fleet server
- [ ] Battery card history graph (sparkline like the original `monitor.py`)
- [ ] CarPlay / Android Auto bridge (probably out of scope, requires
      proprietary protocols)

## License

MIT, same as the parent project.
