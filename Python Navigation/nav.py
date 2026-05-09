"""
Navigation: geocoding (find a destination by name) + routing (compute a path
from current position to destination) + turn-by-turn step tracking.

Uses two free public APIs:
  - Nominatim (OpenStreetMap geocoder) — https://nominatim.openstreetmap.org
  - OSRM (Open Source Routing Machine) — https://router.project-osrm.org

Both require internet. Both have a fair-use policy:
  - Nominatim: max 1 req/sec, must include User-Agent identifying your app
  - OSRM public demo: light use only; for production, host your own OSRM

For offline use you'd self-host both. See the README in the parent folder.
"""

from __future__ import annotations
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

import requests

import offline


log = logging.getLogger(__name__)

USER_AGENT = "ProjectRepatriate-NavGUI/0.1 (in-vehicle navigation)"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
HTTP_TIMEOUT_S = 8.0

# Modes a Route can come from — surfaced in the UI so the driver knows
# whether they're getting real turn-by-turn or compass-only fallback.
ROUTE_MODE_OSRM    = "osrm"        # fresh from OSRM
ROUTE_MODE_CACHED  = "cached"      # previously cached OSRM result
ROUTE_MODE_COMPASS = "compass"     # offline straight-line fallback


# ── data types ───────────────────────────────────────────────────────────
@dataclass
class GeocodeResult:
    display_name: str
    lat: float
    lon: float
    importance: float = 0.0


@dataclass
class RouteStep:
    instruction: str             # human-readable instruction
    name: str                    # street name, e.g. "Main St"
    distance_m: float            # length of this step
    duration_s: float            # estimated time on this step
    maneuver_type: str           # "turn", "merge", "depart", "arrive", etc.
    maneuver_modifier: str       # "left", "right", "slight left", etc.
    location: Tuple[float, float]  # (lat, lon) of the maneuver point
    geometry: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class Route:
    distance_m: float
    duration_s: float
    geometry: List[Tuple[float, float]]   # full polyline as (lat, lon) pairs
    steps: List[RouteStep] = field(default_factory=list)
    mode: str = ROUTE_MODE_OSRM           # ROUTE_MODE_*

    def to_cache_dict(self) -> dict:
        return {
            "distance_m": self.distance_m,
            "duration_s": self.duration_s,
            "geometry":   self.geometry,
            "mode":       self.mode,
            "steps": [
                {
                    "instruction": s.instruction,
                    "name": s.name,
                    "distance_m": s.distance_m,
                    "duration_s": s.duration_s,
                    "maneuver_type": s.maneuver_type,
                    "maneuver_modifier": s.maneuver_modifier,
                    "location": list(s.location),
                    "geometry": s.geometry,
                }
                for s in self.steps
            ],
        }

    @classmethod
    def from_cache_dict(cls, d: dict) -> "Route":
        return cls(
            distance_m=float(d["distance_m"]),
            duration_s=float(d["duration_s"]),
            geometry=[tuple(c) for c in d["geometry"]],
            mode=d.get("mode", ROUTE_MODE_CACHED),
            steps=[
                RouteStep(
                    instruction=s["instruction"],
                    name=s["name"],
                    distance_m=float(s["distance_m"]),
                    duration_s=float(s["duration_s"]),
                    maneuver_type=s["maneuver_type"],
                    maneuver_modifier=s["maneuver_modifier"],
                    location=tuple(s["location"]),
                    geometry=[tuple(c) for c in s.get("geometry", [])],
                )
                for s in d.get("steps", [])
            ],
        )


# ── geocoding ────────────────────────────────────────────────────────────
def geocode(query: str, country_codes: str = "au", limit: int = 5) -> List[GeocodeResult]:
    """Look up an address or place name. Returns up to `limit` results.

    `country_codes` is a comma-separated ISO 3166-1 alpha-2 list to bias results.
    Default "au" prefers Australian results; pass "" for global.
    """
    if not query or not query.strip():
        return []
    params = {
        "q": query.strip(),
        "format": "json",
        "limit": limit,
        "addressdetails": 0,
    }
    if country_codes:
        params["countrycodes"] = country_codes
    headers = {"User-Agent": USER_AGENT}
    try:
        r = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Geocode failed: %s", e)
        return []
    results = []
    for item in data:
        try:
            results.append(GeocodeResult(
                display_name=item.get("display_name", ""),
                lat=float(item["lat"]),
                lon=float(item["lon"]),
                importance=float(item.get("importance", 0.0)),
            ))
        except (KeyError, ValueError):
            continue
    return results


# ── routing ──────────────────────────────────────────────────────────────
def compute_route(from_lat: float, from_lon: float,
                  to_lat: float, to_lon: float) -> Optional[Route]:
    """Compute a driving route between two points using OSRM.

    Returns None on failure (network error, no route, malformed response).
    """
    coords = f"{from_lon},{from_lat};{to_lon},{to_lat}"
    url = f"{OSRM_URL}/{coords}"
    params = {
        "overview": "full",
        "geometries": "geojson",
        "steps": "true",
    }
    try:
        r = requests.get(url, params=params, timeout=HTTP_TIMEOUT_S,
                         headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Route request failed: %s", e)
        return None

    if data.get("code") != "Ok" or not data.get("routes"):
        log.warning("OSRM returned no route: %s", data.get("code"))
        return None

    route_data = data["routes"][0]
    # OSRM gives [lon, lat] pairs in geometry.coordinates — flip for our (lat, lon) convention
    geometry = [(coord[1], coord[0]) for coord in route_data["geometry"]["coordinates"]]

    steps = []
    for leg in route_data.get("legs", []):
        for s in leg.get("steps", []):
            maneuver = s.get("maneuver", {})
            mtype = maneuver.get("type", "")
            modifier = maneuver.get("modifier", "")
            loc = maneuver.get("location", [0, 0])
            step_geom = [(c[1], c[0]) for c in s.get("geometry", {}).get("coordinates", [])]
            steps.append(RouteStep(
                instruction=_format_instruction(mtype, modifier, s.get("name", "")),
                name=s.get("name", ""),
                distance_m=float(s.get("distance", 0)),
                duration_s=float(s.get("duration", 0)),
                maneuver_type=mtype,
                maneuver_modifier=modifier,
                location=(loc[1], loc[0]),
                geometry=step_geom,
            ))

    return Route(
        distance_m=float(route_data["distance"]),
        duration_s=float(route_data["duration"]),
        geometry=geometry,
        steps=steps,
        mode=ROUTE_MODE_OSRM,
    )


# ── offline-aware wrapper ────────────────────────────────────────────────
def compute_route_with_fallback(from_lat: float, from_lon: float,
                                to_lat: float, to_lon: float,
                                cache_dir: Optional[str] = None,
                                avg_kmh: float = 60.0) -> Optional[Route]:
    """Three-tier routing strategy:

      1. Try OSRM (online, fresh) — full turn-by-turn. Cache on success.
      2. If OSRM unreachable, try the route cache for a similar trip.
      3. If neither works, return a compass-mode Route — straight line +
         single bearing instruction. Drivers know where to go even with
         zero internet.

    Returns None only if even compass mode can't be constructed (which it
    always can — so this should never actually return None in practice).
    """
    cache_key = offline.route_cache_key(from_lat, from_lon, to_lat, to_lon)

    # Tier 1: live OSRM
    online_route = compute_route(from_lat, from_lon, to_lat, to_lon)
    if online_route is not None:
        if cache_dir:
            offline.save_route_cache(cache_dir, cache_key, online_route.to_cache_dict())
        return online_route

    # Tier 2: cached previous OSRM result
    if cache_dir:
        cached = offline.load_route_cache(cache_dir, cache_key)
        if cached:
            log.info("Routing offline — using cached route %s", cache_key)
            r = Route.from_cache_dict(cached)
            r.mode = ROUTE_MODE_CACHED
            return r

    # Tier 3: compass-mode straight line
    log.info("Routing offline — falling back to compass mode")
    cm = offline.compass_mode_route(from_lat, from_lon, to_lat, to_lon, avg_kmh)
    return Route(
        distance_m=cm["distance_m"],
        duration_s=cm["duration_s"],
        geometry=[(from_lat, from_lon), (to_lat, to_lon)],
        steps=[RouteStep(
            instruction=cm["instruction"],
            name="",
            distance_m=cm["distance_m"],
            duration_s=cm["duration_s"],
            maneuver_type="depart",
            maneuver_modifier=cm["cardinal"].lower(),
            location=(to_lat, to_lon),
            geometry=[(from_lat, from_lon), (to_lat, to_lon)],
        )],
        mode=ROUTE_MODE_COMPASS,
    )


def _format_instruction(mtype: str, modifier: str, street: str) -> str:
    """Convert OSRM maneuver type+modifier into a plain-English instruction."""
    table = {
        ("depart", ""):              "Start",
        ("turn", "left"):            "Turn left",
        ("turn", "right"):           "Turn right",
        ("turn", "slight left"):     "Slight left",
        ("turn", "slight right"):    "Slight right",
        ("turn", "sharp left"):      "Sharp left",
        ("turn", "sharp right"):     "Sharp right",
        ("turn", "uturn"):           "Make a U-turn",
        ("turn", "straight"):        "Continue straight",
        ("merge", "left"):           "Merge left",
        ("merge", "right"):          "Merge right",
        ("on ramp", "left"):         "Take the on-ramp on your left",
        ("on ramp", "right"):        "Take the on-ramp on your right",
        ("off ramp", "left"):        "Take the off-ramp on your left",
        ("off ramp", "right"):       "Take the off-ramp on your right",
        ("fork", "left"):            "Bear left at the fork",
        ("fork", "right"):           "Bear right at the fork",
        ("end of road", "left"):     "Turn left at the end of the road",
        ("end of road", "right"):    "Turn right at the end of the road",
        ("continue", ""):            "Continue",
        ("roundabout", ""):          "Enter the roundabout",
        ("rotary", ""):              "Enter the rotary",
        ("arrive", ""):              "Arrive at destination",
    }
    base = table.get((mtype, modifier)) or table.get((mtype, "")) or mtype.capitalize()
    if street and mtype not in ("arrive", "depart"):
        return f"{base} onto {street}"
    return base


# ── glyph helpers for the turn arrow ─────────────────────────────────────
def maneuver_glyph(mtype: str, modifier: str) -> str:
    """Return a Unicode arrow glyph for a maneuver."""
    if mtype == "arrive":
        return "🏁"
    if mtype == "depart":
        return "▲"
    if mtype == "roundabout" or mtype == "rotary":
        return "↻"
    if mtype == "merge":
        return "↱" if modifier == "right" else "↰"
    glyphs = {
        "left":         "↰",
        "right":        "↱",
        "slight left":  "↖",
        "slight right": "↗",
        "sharp left":   "↺",
        "sharp right":  "↻",
        "straight":     "↑",
        "uturn":        "⇲",
    }
    return glyphs.get(modifier, "↑")


# ── geo math ─────────────────────────────────────────────────────────────
EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points, in metres."""
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return EARTH_RADIUS_M * c


def distance_to_polyline_m(lat: float, lon: float,
                           polyline: List[Tuple[float, float]]) -> float:
    """Approximate shortest distance from a point to a polyline (in metres).

    Uses simple per-segment haversine-to-endpoint approximation. Good enough
    for off-route detection at typical road resolutions.
    """
    if not polyline:
        return float("inf")
    return min(haversine_m(lat, lon, p[0], p[1]) for p in polyline)


# ── async wrappers ───────────────────────────────────────────────────────
def geocode_async(query: str, callback: Callable[[List[GeocodeResult]], None],
                  country_codes: str = "au") -> None:
    """Non-blocking geocode. `callback(results)` runs in the worker thread —
    marshal back to the Tk main thread via root.after()."""
    threading.Thread(
        target=lambda: callback(geocode(query, country_codes=country_codes)),
        daemon=True,
    ).start()


def compute_route_async(from_lat: float, from_lon: float,
                        to_lat: float, to_lon: float,
                        callback: Callable[[Optional[Route]], None],
                        cache_dir: Optional[str] = None,
                        avg_kmh: float = 60.0) -> None:
    """Non-blocking route compute with offline fallback. Same callback
    warning as geocode_async — runs in worker thread, marshal to UI thread."""
    threading.Thread(
        target=lambda: callback(
            compute_route_with_fallback(from_lat, from_lon, to_lat, to_lon,
                                        cache_dir=cache_dir, avg_kmh=avg_kmh)),
        daemon=True,
    ).start()


# ── stand-alone test ─────────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Geocoding 'Perth WA':")
    for r in geocode("Perth WA")[:3]:
        print(f"  {r.lat:.5f}, {r.lon:.5f}  {r.display_name}")

    print("\nRoute Perth → Fremantle:")
    route = compute_route(-31.9505, 115.8605, -32.0569, 115.7439)
    if route:
        print(f"  Distance: {route.distance_m/1000:.1f} km")
        print(f"  Duration: {route.duration_s/60:.0f} min")
        print(f"  Steps: {len(route.steps)}")
        for s in route.steps[:5]:
            print(f"    {maneuver_glyph(s.maneuver_type, s.maneuver_modifier)} "
                  f"{s.instruction} ({s.distance_m:.0f}m)")
