"""
Visual theme for the in-car head-unit GUI.

Designed for:
  - Dark cabin / night driving (high contrast, low blue-light at night)
  - Glanceable readability at arm's length on a 7"-10" car head unit
  - Touch-friendly hit targets (>= 48px)
  - Bluetooth keyboard / mouse navigation
"""

# ── COLOURS ────────────────────────────────────────────────────────────────
# Tesla-ish dark theme with cyan + amber accents. Calibrated for low-glare.
BG_DEEP        = "#0a0e1a"   # deepest background — outer chrome
BG_PANEL       = "#111827"   # panels / sidebars
BG_PANEL_ALT   = "#1a2332"   # secondary panels, hover state
BG_RAISED      = "#1f2937"   # raised buttons / cards
BG_MAP_BORDER  = "#0f172a"

FG_PRIMARY     = "#f9fafb"   # primary text — near white
FG_SECONDARY   = "#9ca3af"   # secondary text — grey
FG_MUTED       = "#4b5563"   # placeholder / disabled
FG_DANGER      = "#ef4444"
FG_WARNING     = "#f59e0b"
FG_SUCCESS     = "#10b981"

# Brand accents — used for current-position marker, route line, key buttons
ACCENT_CYAN    = "#22d3ee"   # electric cyan — current speed, GPS-locked status
ACCENT_AMBER   = "#fbbf24"   # navigation arrows, ETA emphasis
ACCENT_BLUE    = "#3b82f6"   # route polyline
ACCENT_PURPLE  = "#a855f7"   # destination marker

# Status semantics
STATUS_GPS_LOCK    = ACCENT_CYAN
STATUS_GPS_SEARCH  = FG_WARNING
STATUS_GPS_LOST    = FG_DANGER
STATUS_CELL_OK     = FG_SUCCESS
STATUS_CELL_DOWN   = FG_DANGER

# ── TYPOGRAPHY ─────────────────────────────────────────────────────────────
FONT_FAMILY        = "Inter"            # falls back gracefully if missing
FONT_FAMILY_MONO   = "JetBrains Mono"
FONT_FAMILY_FALLBACK = "Helvetica"

FONT_SPEED         = (FONT_FAMILY, 96, "bold")
FONT_SPEED_UNIT    = (FONT_FAMILY, 18, "normal")
FONT_TURN_DIST     = (FONT_FAMILY, 36, "bold")
FONT_TURN_INSTR    = (FONT_FAMILY, 22, "normal")
FONT_TURN_STREET   = (FONT_FAMILY, 18, "normal")
FONT_HEADING       = (FONT_FAMILY, 16, "bold")
FONT_BODY          = (FONT_FAMILY, 14, "normal")
FONT_SMALL         = (FONT_FAMILY, 12, "normal")
FONT_MONO          = (FONT_FAMILY_MONO, 12, "normal")
FONT_BUTTON        = (FONT_FAMILY, 14, "bold")
FONT_STATUS_BAR    = (FONT_FAMILY, 13, "normal")

# ── SPACING / SIZING ───────────────────────────────────────────────────────
GAP_S              = 4
GAP_M              = 8
GAP_L              = 16
GAP_XL             = 24

PAD_PANEL          = 16
PAD_BUTTON_X       = 20
PAD_BUTTON_Y       = 14

CORNER_RADIUS      = 12      # used by tkintermapview where supported

BUTTON_MIN_HEIGHT  = 48      # touch-friendly
BUTTON_MIN_WIDTH   = 120

SIDEBAR_WIDTH      = 360     # left navigation panel
STATUS_BAR_HEIGHT  = 56
SEARCH_BAR_HEIGHT  = 64

# ── MAP ────────────────────────────────────────────────────────────────────
MAP_TILE_SERVER    = "https://a.tile.openstreetmap.org/{z}/{x}/{y}.png"
# Other tile servers worth trying:
#   "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"  # satellite
#   "https://tile.openstreetmap.de/{z}/{x}/{y}.png"  # German server, often faster from AU
#   "https://tile-{s}.openstreetmap.fr/hot/{z}/{x}/{y}.png"  # humanitarian
MAP_DEFAULT_ZOOM   = 16
# Tile cache lives somewhere writeable on every platform.
# Linux: /tmp/repatriate_map_cache.db
# Windows: C:\Users\<you>\AppData\Local\Temp\repatriate_map_cache.db
import tempfile as _tempfile
import os as _os_for_tile
MAP_TILE_CACHE_DB  = _os_for_tile.path.join(_tempfile.gettempdir(), "repatriate_map_cache.db")
MAP_MAX_ZOOM       = 19

# Marker / polyline styling
MARKER_CURRENT_POS_FILL    = ACCENT_CYAN
MARKER_CURRENT_POS_OUTLINE = "#0e7490"
MARKER_DESTINATION_FILL    = ACCENT_PURPLE
MARKER_DESTINATION_OUTLINE = "#6b21a8"
ROUTE_POLYLINE_COLOR       = ACCENT_BLUE
ROUTE_POLYLINE_WIDTH       = 6

# ── BEHAVIOUR ──────────────────────────────────────────────────────────────
GPS_TIMEOUT_S      = 8       # seconds without NMEA before "GPS lost"
MAP_FOLLOW_DEFAULT = True    # auto-recenter on current position
MAP_RECENTER_AFTER_PAN_S = 30  # seconds after manual pan before re-following
NAV_RECALC_OFFROUTE_M    = 80   # metres off-route before recalculating
NAV_COMPASS_AVG_KMH      = 60   # assumed speed for compass-mode ETA estimate
KEYBOARD_FULLSCREEN_KEY  = "F11"
KEYBOARD_QUIT_KEY        = "Escape"

# ── OFFLINE / INTERMITTENT-INTERNET ────────────────────────────────────────
# When the Pi is at home base it can prefetch tiles and warm the route cache.
# When out on the road, the GUI reads from these caches and falls back to
# compass-mode straight-line nav if no cached route is available.
import os as _os
_HOME = _os.path.expanduser("~")
FAVOURITES_PATH    = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                                   "favourites.json")
ROUTE_CACHE_DIR    = _os.path.join(_HOME, ".cache", "repatriate", "routes")
ONLINE_PROBE_INTERVAL_S = 30      # how often to re-check connectivity
ROUTE_CACHE_MAX_AGE_S   = 7 * 24 * 3600   # 7 days

# ── BATTERY MONITOR (BLE) ──────────────────────────────────────────────────
# Voltage thresholds for the colour-coded battery card. These are typical
# 12V lead-acid values; tune to your battery chemistry if different.
BATTERY_V_FULL         = 12.7    # at-rest full
BATTERY_V_LOW          = 12.0    # ~50% SoC at rest
BATTERY_V_CRITICAL     = 11.6    # cranking risk
BATTERY_V_CHARGING     = 13.2    # alternator/charger active above this

BATTERY_COLOR_OK       = ACCENT_CYAN
BATTERY_COLOR_CHARGING = FG_SUCCESS
BATTERY_COLOR_WARN     = FG_WARNING
BATTERY_COLOR_CRITICAL = FG_DANGER
BATTERY_COLOR_OFFLINE  = FG_MUTED

BATTERY_STALE_S        = 8.0     # seconds without packet → show as stale


def font(spec, fallback=True):
    """Return a font tuple, falling back if the requested family isn't installed."""
    if not fallback:
        return spec
    try:
        from tkinter import font as tkfont
        if spec[0] not in tkfont.families():
            return (FONT_FAMILY_FALLBACK,) + spec[1:]
    except Exception:
        pass
    return spec
