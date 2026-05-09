#!/usr/bin/env python3
"""
Pre-download OpenStreetMap tiles into tkintermapview's SQLite cache so the
GUI works offline once the Pi leaves the home network.

Run while the Pi has internet (e.g. plugged into home WiFi). Tiles are
cached into the same SQLite DB the GUI uses (default
`/tmp/repatriate_map_cache.db`, override with --db). The GUI then reads
from that cache transparently when offline.

USAGE
    # Cache a 25 km radius around home for zoom levels 12-18
    python3 tile_prefetch.py --center "-31.9505,115.8605" --radius-km 25 \\
                             --zoom 12-18

    # Cache a tight bounding box at high detail
    python3 tile_prefetch.py --bbox "-32.10,115.65,-31.85,116.00" --zoom 14-19

NOTES
  - OpenStreetMap's tile-usage policy says: bulk-download is discouraged,
    "no more than 2 tiles/sec to one server, no more than 250 GB/mo".
    This script defaults to ~1.5 tiles/sec and a custom User-Agent. Don't
    hammer it. For aggressive prefetching, set up your own tile server
    (e.g. https://switch2osm.org/serving-tiles/).
  - At zoom 19 over a city, expect millions of tiles. Use sparingly.
  - SQLite schema matches tkintermapview's internal format; the library
    will pick tiles up automatically with no extra configuration.

ROUGH SIZE BUDGET (PNG, ~15 KB/tile average)
    radius 5 km, z12-17  →     ~600 tiles  ~9 MB
    radius 25 km, z12-17 →   ~12,000 tiles ~180 MB
    radius 25 km, z12-18 →   ~50,000 tiles ~750 MB
    radius 25 km, z12-19 →  ~200,000 tiles ~3 GB
"""

from __future__ import annotations
import argparse
import math
import sqlite3
import sys
import time
from typing import Iterable, Tuple

import requests


USER_AGENT = "ProjectRepatriate-NavGUI-TilePrefetch/0.1 (offline-cache)"
DEFAULT_DB = "/tmp/repatriate_map_cache.db"
DEFAULT_TILE_URL = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
DEFAULT_SERVER_NAME = "https://a.tile.openstreetmap.org"
REQUEST_INTERVAL_S = 0.7      # ~1.5 req/sec, well under the 2/sec policy


# ── tile math ─────────────────────────────────────────────────────────────
def deg_to_tile(lat: float, lon: float, zoom: int) -> Tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    xt = int((lon + 180.0) / 360.0 * n)
    yt = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xt, yt


def bbox_from_center(lat: float, lon: float, radius_km: float) -> Tuple[float, float, float, float]:
    """Approximate bbox around (lat, lon) — fine for prefetch radius purposes."""
    dlat = radius_km / 110.574
    dlon = radius_km / (111.320 * math.cos(math.radians(lat)) or 1.0)
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


def tiles_for_bbox(bbox: Tuple[float, float, float, float],
                   zoom: int) -> Iterable[Tuple[int, int, int]]:
    s, w, n, e = bbox
    x0, y1 = deg_to_tile(s, w, zoom)
    x1, y0 = deg_to_tile(n, e, zoom)
    for x in range(min(x0, x1), max(x0, x1) + 1):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            yield zoom, x, y


# ── SQLite cache (tkintermapview-compatible schema) ───────────────────────
def open_cache(db_path: str, server_name: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Schema mirrors tkintermapview/utility_functions/osm_offline_loader.py
    cur.execute("""
        CREATE TABLE IF NOT EXISTS server (
            url   VARCHAR(300) PRIMARY KEY NOT NULL,
            max_zoom INTEGER NOT NULL
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sections (
            position_a  VARCHAR(100) NOT NULL,
            position_b  VARCHAR(100) NOT NULL,
            zoom_a      INTEGER NOT NULL,
            zoom_b      INTEGER NOT NULL,
            server      VARCHAR(300) NOT NULL,
            CONSTRAINT fk_server
                FOREIGN KEY (server) REFERENCES server(url)
        )""")
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tiles (
            zoom    INTEGER NOT NULL,
            x       INTEGER NOT NULL,
            y       INTEGER NOT NULL,
            server  VARCHAR(300) NOT NULL,
            tile_image BLOB NOT NULL,
            CONSTRAINT pk_tiles PRIMARY KEY (zoom, x, y, server),
            CONSTRAINT fk_server
                FOREIGN KEY (server) REFERENCES server(url)
        )""")
    cur.execute("INSERT OR IGNORE INTO server VALUES (?, ?)", (server_name, 19))
    conn.commit()
    return conn


def have_tile(conn: sqlite3.Connection, server: str, z: int, x: int, y: int) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM tiles WHERE zoom=? AND x=? AND y=? AND server=? LIMIT 1",
        (z, x, y, server))
    return cur.fetchone() is not None


def store_tile(conn: sqlite3.Connection, server: str,
               z: int, x: int, y: int, blob: bytes) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO tiles (zoom, x, y, server, tile_image) "
        "VALUES (?, ?, ?, ?, ?)",
        (z, x, y, server, blob))
    conn.commit()


# ── fetch loop ────────────────────────────────────────────────────────────
def fetch_tile(url_template: str, z: int, x: int, y: int) -> bytes:
    url = url_template.format(z=z, x=x, y=y)
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=10)
    resp.raise_for_status()
    return resp.content


def parse_zoom_range(spec: str) -> range:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return range(int(a), int(b) + 1)
    z = int(spec)
    return range(z, z + 1)


def parse_bbox(spec: str) -> Tuple[float, float, float, float]:
    parts = [float(p) for p in spec.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bbox must be 'south,west,north,east'")
    return tuple(parts)  # type: ignore


def parse_center(spec: str) -> Tuple[float, float]:
    parts = [float(p) for p in spec.split(",")]
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("center must be 'lat,lon'")
    return tuple(parts)  # type: ignore


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--center", type=parse_center,
                   help="Centre coordinate 'lat,lon' (use with --radius-km)")
    g.add_argument("--bbox", type=parse_bbox,
                   help="Bounding box 'south,west,north,east'")
    p.add_argument("--radius-km", type=float, default=10.0,
                   help="Radius around --center (default 10)")
    p.add_argument("--zoom", default="12-17",
                   help="Zoom range, e.g. 12-17 or just 15 (default 12-17)")
    p.add_argument("--db", default=DEFAULT_DB,
                   help=f"SQLite cache path (default {DEFAULT_DB})")
    p.add_argument("--url", default=DEFAULT_TILE_URL,
                   help="Tile URL template (default OSM)")
    p.add_argument("--server-name", default=DEFAULT_SERVER_NAME,
                   help="Server name to register in cache (default OSM)")
    p.add_argument("--rate", type=float, default=REQUEST_INTERVAL_S,
                   help=f"Seconds between tile requests (default {REQUEST_INTERVAL_S})")
    p.add_argument("--dry-run", action="store_true",
                   help="Just count tiles, don't download")
    args = p.parse_args()

    if args.center:
        bbox = bbox_from_center(args.center[0], args.center[1], args.radius_km)
        print(f"Centre: {args.center[0]:.5f}, {args.center[1]:.5f}  radius {args.radius_km} km")
    else:
        bbox = args.bbox
    print(f"Bounding box: S {bbox[0]:.5f}  W {bbox[1]:.5f}  N {bbox[2]:.5f}  E {bbox[3]:.5f}")

    zooms = parse_zoom_range(args.zoom)
    all_tiles = []
    for z in zooms:
        zt = list(tiles_for_bbox(bbox, z))
        all_tiles.extend(zt)
        print(f"  zoom {z}: {len(zt):>8} tiles")
    print(f"  TOTAL : {len(all_tiles):>8} tiles")
    print(f"  Estimated download (≈15 KB/tile): {len(all_tiles)*15/1024:.1f} MB")
    print(f"  Estimated time @ {args.rate}s/tile : {len(all_tiles)*args.rate/60:.1f} min")

    if args.dry_run:
        return 0

    conn = open_cache(args.db, args.server_name)
    print(f"Cache DB: {args.db}")
    print()

    fetched = 0
    skipped = 0
    failed = 0
    started = time.time()

    try:
        for i, (z, x, y) in enumerate(all_tiles, 1):
            if have_tile(conn, args.server_name, z, x, y):
                skipped += 1
                if i % 50 == 0:
                    _progress(i, len(all_tiles), fetched, skipped, failed, started)
                continue
            try:
                blob = fetch_tile(args.url, z, x, y)
                store_tile(conn, args.server_name, z, x, y, blob)
                fetched += 1
            except requests.RequestException as e:
                failed += 1
                print(f"  [!] {z}/{x}/{y}: {e}")
            time.sleep(args.rate)
            if i % 25 == 0:
                _progress(i, len(all_tiles), fetched, skipped, failed, started)
    except KeyboardInterrupt:
        print("\n  [interrupted]")
    finally:
        conn.close()

    print()
    print(f"Done. fetched={fetched}  skipped={skipped} (already cached)  failed={failed}")
    return 0


def _progress(i: int, total: int, fetched: int, skipped: int, failed: int, started: float) -> None:
    pct = i / total * 100
    elapsed = time.time() - started
    rate = i / elapsed if elapsed > 0 else 0
    eta = (total - i) / rate if rate > 0 else 0
    print(f"  [{i:>6}/{total}] {pct:5.1f}%  "
          f"new={fetched} cached={skipped} fail={failed}  "
          f"{rate:.1f}/s  ETA {eta/60:.1f} min")


if __name__ == "__main__":
    sys.exit(main())
