"""
Offline / intermittent-internet helpers.

Three things the navigation GUI needs to handle gracefully when the Pi has
no network connection:

  1. **Destination lookup** — Nominatim is unreachable. Fall back to:
       a) a local `favourites.json` file ("Home", "Work", "Mum's place" ...)
       b) raw coordinate parsing ("-31.95, 115.86", "31°57'02"S 115°51'38"E")

  2. **Routing** — OSRM is unreachable. Fall back to "compass mode": draw a
       straight-line on the map and present a single instruction telling the
       driver which compass bearing to head and the great-circle distance.
       Crude but genuinely useful in remote areas where you just need to
       know which direction is right.

  3. **Map tiles** — handled by `tile_prefetch.py` separately. tkintermapview
       already serves cached tiles transparently when offline.

Design intent: the GUI never *fails* to navigate. It just degrades — full
turn-by-turn online, compass-bearing offline, with the user clearly told
which mode they're in.
"""

from __future__ import annotations
import json
import logging
import math
import os
import re
import socket
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


log = logging.getLogger(__name__)


# ── favourites ────────────────────────────────────────────────────────────
@dataclass
class Favourite:
    name: str
    lat: float
    lon: float
    aliases: List[str] = field(default_factory=list)
    note: str = ""

    def matches(self, query: str) -> bool:
        q = query.strip().lower()
        if not q:
            return False
        if q in self.name.lower():
            return True
        return any(q in a.lower() for a in self.aliases)


def load_favourites(path: str) -> List[Favourite]:
    """Load a favourites file. Returns [] if the file is missing or malformed.

    Format (favourites.json):
        [
          {"name": "Home",  "lat": -31.9505, "lon": 115.8605,
           "aliases": ["house", "casa"], "note": "Front door"},
          ...
        ]
    """
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not load favourites from %s: %s", path, e)
        return []
    favs: List[Favourite] = []
    for item in data:
        try:
            favs.append(Favourite(
                name=str(item["name"]),
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                aliases=[str(a) for a in item.get("aliases", [])],
                note=str(item.get("note", "")),
            ))
        except (KeyError, ValueError, TypeError) as e:
            log.warning("Skipping malformed favourite %r: %s", item, e)
    return favs


def search_favourites(favs: List[Favourite], query: str,
                      limit: int = 5) -> List[Favourite]:
    return [f for f in favs if f.matches(query)][:limit]


# ── coordinate parsing ────────────────────────────────────────────────────
_DECIMAL_PAIR_RE = re.compile(
    r"""^\s*
        (?P<lat>[-+]?\d+(?:\.\d+)?)         # lat
        \s*[,\s]\s*                         # separator
        (?P<lon>[-+]?\d+(?:\.\d+)?)         # lon
        \s*$""", re.VERBOSE)

# 31°57'02"S 115°51'38"E   (loose — accepts variants)
_DMS_RE = re.compile(
    r"""(?P<deg>\d+(?:\.\d+)?)\s*°?\s*
        (?:(?P<min>\d+(?:\.\d+)?)\s*[\'’′]\s*)?
        (?:(?P<sec>\d+(?:\.\d+)?)\s*[\"”″]\s*)?
        (?P<hem>[NSEW])""", re.IGNORECASE | re.VERBOSE)


def parse_coordinates(query: str) -> Optional[Tuple[float, float]]:
    """Parse a free-text coordinate string. Returns (lat, lon) or None.

    Accepted forms:
        "-31.95, 115.86"
        "-31.95 115.86"
        "31.95S 115.86E"
        "31°57'02\"S 115°51'38\"E"
    """
    if not query or not query.strip():
        return None

    # Decimal pair
    m = _DECIMAL_PAIR_RE.match(query)
    if m:
        try:
            lat = float(m.group("lat"))
            lon = float(m.group("lon"))
            if -90 <= lat <= 90 and -180 <= lon <= 180:
                return (lat, lon)
        except ValueError:
            pass

    # DMS pair — find two matches and assign by hemisphere
    matches = list(_DMS_RE.finditer(query))
    if len(matches) >= 2:
        try:
            vals = []
            for m in matches[:2]:
                deg = float(m.group("deg"))
                mn = float(m.group("min") or 0)
                sc = float(m.group("sec") or 0)
                hem = m.group("hem").upper()
                v = deg + mn / 60 + sc / 3600
                if hem in ("S", "W"):
                    v = -v
                vals.append((hem, v))
            lat = next((v for h, v in vals if h in ("N", "S")), None)
            lon = next((v for h, v in vals if h in ("E", "W")), None)
            if lat is not None and lon is not None:
                return (lat, lon)
        except (ValueError, AttributeError):
            pass

    return None


# ── network probe ─────────────────────────────────────────────────────────
def is_online(host: str = "1.1.1.1", port: int = 53, timeout: float = 1.5) -> bool:
    """Quick non-blocking-ish probe to see if we have outbound connectivity.

    Defaults to Cloudflare DNS port — universally reachable when there's any
    real internet, returns fast when there isn't. Don't call this in a tight
    loop; cache the result for a few seconds.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


_LAST_PROBE_T = 0.0
_LAST_PROBE_RESULT = False
_PROBE_CACHE_S = 8.0


def is_online_cached() -> bool:
    """Like is_online() but caches for ~8 seconds to avoid repeated probes."""
    global _LAST_PROBE_T, _LAST_PROBE_RESULT
    now = time.time()
    if now - _LAST_PROBE_T < _PROBE_CACHE_S:
        return _LAST_PROBE_RESULT
    _LAST_PROBE_RESULT = is_online()
    _LAST_PROBE_T = now
    return _LAST_PROBE_RESULT


# ── compass-mode geometry ─────────────────────────────────────────────────
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing (deg, 0=N, clockwise) from point 1 toward point 2."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360.0) % 360.0


def cardinal_for_bearing(deg: float) -> str:
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int((deg + 11.25) % 360 / 22.5)]


def relative_bearing_glyph(bearing: float, heading: Optional[float]) -> str:
    """Pick an arrow glyph showing target direction relative to vehicle heading.

    If heading isn't known yet, just point in the absolute bearing direction.
    """
    rel = bearing if heading is None else (bearing - heading + 360) % 360
    if rel < 22.5 or rel >= 337.5:
        return "↑"
    if rel < 67.5:
        return "↗"
    if rel < 112.5:
        return "→"
    if rel < 157.5:
        return "↘"
    if rel < 202.5:
        return "↓"
    if rel < 247.5:
        return "↙"
    if rel < 292.5:
        return "←"
    return "↖"


def compass_mode_route(from_lat: float, from_lon: float,
                       to_lat: float, to_lon: float,
                       avg_speed_kmh: float = 60.0) -> dict:
    """Return a minimal route dict for offline / no-OSRM navigation.

    Shape mirrors what the GUI's `_on_route_ready` consumes — single straight
    polyline, single instruction, ETA estimated from a flat average speed.

    Returns a plain dict (not a nav.Route) to avoid a circular import; the
    caller adapts it.
    """
    distance_m = haversine_m(from_lat, from_lon, to_lat, to_lon)
    bearing = bearing_deg(from_lat, from_lon, to_lat, to_lon)
    cardinal = cardinal_for_bearing(bearing)
    duration_s = (distance_m / 1000.0) / max(1.0, avg_speed_kmh) * 3600.0

    return {
        "mode": "compass",
        "distance_m": distance_m,
        "duration_s": duration_s,
        "bearing_deg": bearing,
        "cardinal": cardinal,
        "geometry": [(from_lat, from_lon), (to_lat, to_lon)],
        "instruction": f"Head {cardinal} ({bearing:.0f}°) — {distance_m/1000:.1f} km",
    }


# ── route cache (for cached online routes used while offline) ─────────────
def _round_coord(v: float, places: int = 3) -> float:
    """Round to ~100m grid for cache lookups (3 decimal places ≈ 110m)."""
    return round(v, places)


def route_cache_key(from_lat: float, from_lon: float,
                    to_lat: float, to_lon: float) -> str:
    return (f"{_round_coord(from_lat)},{_round_coord(from_lon)}"
            f"->{_round_coord(to_lat)},{_round_coord(to_lon)}.json")


def save_route_cache(cache_dir: str, key: str, payload: dict) -> None:
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, key), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except OSError as e:
        log.warning("Could not write route cache %s: %s", key, e)


def load_route_cache(cache_dir: str, key: str,
                     max_age_s: float = 7 * 24 * 3600) -> Optional[dict]:
    path = os.path.join(cache_dir, key)
    if not os.path.isfile(path):
        return None
    try:
        if time.time() - os.path.getmtime(path) > max_age_s:
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read route cache %s: %s", key, e)
        return None


# ── stand-alone diagnostic ────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Online?", is_online())
    print()
    print("Coord parse tests:")
    for s in [
        "-31.95, 115.86",
        "  -31.95   115.86 ",
        "31.95S 115.86E",
        '31°57\'02"S 115°51\'38"E',
        "garbage",
    ]:
        print(f"  {s!r:<40} → {parse_coordinates(s)}")

    print()
    print("Compass route Perth → Fremantle:")
    r = compass_mode_route(-31.9505, 115.8605, -32.0569, 115.7439)
    for k, v in r.items():
        if k != "geometry":
            print(f"  {k}: {v}")
