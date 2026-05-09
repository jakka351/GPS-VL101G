#!/usr/bin/env python3
"""
PROJECT REPATRIATE — In-Vehicle Navigation GUI

Tkinter head-unit-style interface that:
  - Reads live GPS data from a VL101G via Pi serial UART
  - Displays the current position on an OpenStreetMap-tiled live map
  - Lets the driver search for a destination and navigate to it
  - Shows turn-by-turn instructions, ETA, speed, and route distance
  - Designed for car-display use with Bluetooth keyboard / mouse input

Run on the Pi:
    python3 main.py [--port /dev/serial0] [--baud 9600] [--fullscreen]

Keyboard shortcuts:
    F11        toggle fullscreen
    Escape     quit
    Ctrl+L     focus the search bar
    Ctrl+R     re-centre map on current position
    Ctrl+X     cancel current route
"""

from __future__ import annotations
import argparse
import logging
import math
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from typing import Optional, List, Union

import tkintermapview

import theme as T
import gps as gpsmod
import nav as navmod
import offline as offmod
import battery as batmod


log = logging.getLogger(__name__)

# ── update intervals (ms) ─────────────────────────────────────────────────
TICK_GPS_MS         = 500    # poll GPS snapshot
TICK_CLOCK_MS       = 1000   # update wall clock
TICK_OFFROUTE_MS    = 5000   # off-route check interval
TICK_BATTERY_MS     = 1000   # poll BLE battery snapshot
TICK_ONLINE_MS      = T.ONLINE_PROBE_INTERVAL_S * 1000


# ═══════════════════════════════════════════════════════════════════════════
#  CUSTOM WIDGETS
# ═══════════════════════════════════════════════════════════════════════════

class FlatButton(tk.Frame):
    """A flat, dark-themed button with hover and press states. Touch-friendly."""

    def __init__(self, parent, text: str, command=None,
                 bg=T.BG_RAISED, hover_bg=T.BG_PANEL_ALT,
                 fg=T.FG_PRIMARY, accent=T.ACCENT_CYAN,
                 width=T.BUTTON_MIN_WIDTH, height=T.BUTTON_MIN_HEIGHT,
                 font=None):
        super().__init__(parent, bg=bg, height=height, width=width,
                         highlightthickness=0)
        self.pack_propagate(False)
        self._normal_bg = bg
        self._hover_bg = hover_bg
        self._command = command
        self._accent = accent

        self.label = tk.Label(self, text=text, bg=bg, fg=fg,
                              font=font or T.FONT_BUTTON)
        self.label.pack(expand=True, fill="both")

        for w in (self, self.label):
            w.bind("<Enter>", self._on_enter)
            w.bind("<Leave>", self._on_leave)
            w.bind("<Button-1>", self._on_click)

    def _on_enter(self, _):
        self.configure(bg=self._hover_bg)
        self.label.configure(bg=self._hover_bg)

    def _on_leave(self, _):
        self.configure(bg=self._normal_bg)
        self.label.configure(bg=self._normal_bg)

    def _on_click(self, _):
        if self._command:
            self._command()


class StatusBar(tk.Frame):
    """Top status bar — time, GPS status, satellites, signal indicators."""

    def __init__(self, parent):
        super().__init__(parent, bg=T.BG_DEEP, height=T.STATUS_BAR_HEIGHT)
        self.pack_propagate(False)

        # Left: GPS sat count + status
        self.gps_indicator = tk.Label(
            self, text="🛰  --", bg=T.BG_DEEP, fg=T.STATUS_GPS_LOST,
            font=T.FONT_HEADING, padx=T.PAD_PANEL)
        self.gps_indicator.pack(side="left")

        self.gps_status_text = tk.Label(
            self, text="GPS: searching", bg=T.BG_DEEP, fg=T.FG_SECONDARY,
            font=T.FONT_STATUS_BAR)
        self.gps_status_text.pack(side="left")

        # Right: clock, voltage, cellular
        self.clock_label = tk.Label(
            self, text="--:--:--", bg=T.BG_DEEP, fg=T.FG_PRIMARY,
            font=T.FONT_HEADING, padx=T.PAD_PANEL)
        self.clock_label.pack(side="right")

        # Net (online/offline) indicator — replaces the old cellular slot
        self.net_indicator = tk.Label(
            self, text="◌ NET", bg=T.BG_DEEP, fg=T.FG_MUTED,
            font=T.FONT_STATUS_BAR, padx=T.GAP_M)
        self.net_indicator.pack(side="right")

        # Compact battery indicator (voltage only — full readout is in BatteryCard)
        self.batt_indicator = tk.Label(
            self, text="⚡ --", bg=T.BG_DEEP, fg=T.FG_MUTED,
            font=T.FONT_STATUS_BAR, padx=T.GAP_M)
        self.batt_indicator.pack(side="right")

        # Centre: app title
        self.title_label = tk.Label(
            self, text="PROJECT REPATRIATE  ·  Navigation",
            bg=T.BG_DEEP, fg=T.FG_SECONDARY, font=T.FONT_HEADING)
        self.title_label.pack(expand=True)

    def update_gps(self, pos: gpsmod.Position, connected: bool):
        if not connected:
            self.gps_indicator.config(text="🛰  ✕", fg=T.STATUS_GPS_LOST)
            self.gps_status_text.config(text="GPS: serial disconnected", fg=T.STATUS_GPS_LOST)
            return
        if not pos.has_fix():
            self.gps_indicator.config(text=f"🛰  {pos.satellites_used}", fg=T.STATUS_GPS_SEARCH)
            self.gps_status_text.config(text="GPS: searching", fg=T.STATUS_GPS_SEARCH)
            return
        self.gps_indicator.config(text=f"🛰  {pos.satellites_used}", fg=T.STATUS_GPS_LOCK)
        hdop_str = f"HDOP {pos.hdop:.1f}" if pos.hdop else ""
        self.gps_status_text.config(text=f"GPS: locked  {hdop_str}", fg=T.STATUS_GPS_LOCK)

    def update_clock(self):
        self.clock_label.config(text=datetime.now().strftime("%H:%M:%S"))

    def update_online(self, online: bool):
        if online:
            self.net_indicator.config(text="● NET", fg=T.FG_SUCCESS)
        else:
            self.net_indicator.config(text="○ OFFLINE", fg=T.FG_WARNING)

    def update_battery(self, reading: batmod.BatteryReading, connected: bool):
        if not connected and reading.voltage is None:
            self.batt_indicator.config(text="⚡ --", fg=T.FG_MUTED)
            return
        if reading.voltage is None:
            self.batt_indicator.config(text="⚡ ...", fg=T.FG_MUTED)
            return
        v = reading.voltage
        if v < T.BATTERY_V_CRITICAL:
            colour = T.BATTERY_COLOR_CRITICAL
        elif v < T.BATTERY_V_LOW:
            colour = T.BATTERY_COLOR_WARN
        elif v >= T.BATTERY_V_CHARGING:
            colour = T.BATTERY_COLOR_CHARGING
        else:
            colour = T.BATTERY_COLOR_OK
        if reading.is_stale(T.BATTERY_STALE_S):
            colour = T.BATTERY_COLOR_OFFLINE
        self.batt_indicator.config(text=f"⚡ {v:.2f}V", fg=colour)


class SpeedoCard(tk.Frame):
    """Big speed readout — top of the left navigation panel."""

    def __init__(self, parent):
        super().__init__(parent, bg=T.BG_PANEL, height=200)
        self.pack_propagate(False)

        self.speed_label = tk.Label(
            self, text="—", bg=T.BG_PANEL, fg=T.ACCENT_CYAN,
            font=T.FONT_SPEED)
        self.speed_label.pack(pady=(T.GAP_L, 0))

        self.unit_label = tk.Label(
            self, text="km/h", bg=T.BG_PANEL, fg=T.FG_SECONDARY,
            font=T.FONT_SPEED_UNIT)
        self.unit_label.pack()

        self.heading_label = tk.Label(
            self, text="—", bg=T.BG_PANEL, fg=T.FG_SECONDARY,
            font=T.FONT_BODY)
        self.heading_label.pack(pady=(T.GAP_M, 0))

    def update(self, pos: gpsmod.Position):
        if pos.speed_kmh is None or not pos.has_fix():
            self.speed_label.config(text="—")
            self.heading_label.config(text="No GPS lock")
            return
        kmh = max(0, round(pos.speed_kmh))
        self.speed_label.config(text=str(kmh))
        if pos.heading_deg is not None:
            self.heading_label.config(text=f"{_compass(pos.heading_deg)}  {pos.heading_deg:.0f}°")
        else:
            self.heading_label.config(text="")


class NavCard(tk.Frame):
    """Turn-by-turn instruction card."""

    def __init__(self, parent):
        super().__init__(parent, bg=T.BG_PANEL_ALT)

        self.glyph = tk.Label(
            self, text="▲", bg=T.BG_PANEL_ALT, fg=T.ACCENT_AMBER,
            font=(T.FONT_FAMILY, 64, "bold"))
        self.glyph.pack(pady=(T.GAP_L, T.GAP_S))

        self.distance = tk.Label(
            self, text="", bg=T.BG_PANEL_ALT, fg=T.ACCENT_AMBER,
            font=T.FONT_TURN_DIST)
        self.distance.pack()

        self.instruction = tk.Label(
            self, text="No active route", bg=T.BG_PANEL_ALT, fg=T.FG_PRIMARY,
            font=T.FONT_TURN_INSTR, wraplength=T.SIDEBAR_WIDTH - 32)
        self.instruction.pack(pady=(T.GAP_S, T.GAP_S))

        self.street = tk.Label(
            self, text="Search for a destination below", bg=T.BG_PANEL_ALT, fg=T.FG_SECONDARY,
            font=T.FONT_TURN_STREET, wraplength=T.SIDEBAR_WIDTH - 32)
        self.street.pack(pady=(0, T.GAP_L))

    def show_step(self, step: navmod.RouteStep, distance_to_step_m: float):
        glyph = navmod.maneuver_glyph(step.maneuver_type, step.maneuver_modifier)
        self.glyph.config(text=glyph)
        self.distance.config(text=_fmt_distance(distance_to_step_m))
        if step.maneuver_type == "arrive":
            self.instruction.config(text="Arrive at destination")
            self.street.config(text="")
        else:
            self.instruction.config(text=step.instruction.split(" onto ")[0])
            self.street.config(text=("on " + step.name) if step.name else "")

    def show_idle(self):
        self.glyph.config(text="▲")
        self.distance.config(text="")
        self.instruction.config(text="No active route")
        self.street.config(text="Search for a destination below")

    def show_arrived(self):
        self.glyph.config(text="🏁")
        self.distance.config(text="0 m")
        self.instruction.config(text="You have arrived")
        self.street.config(text="")


class TripStats(tk.Frame):
    """ETA / remaining-distance card under the nav card."""

    def __init__(self, parent):
        super().__init__(parent, bg=T.BG_PANEL)

        for col in (0, 1):
            self.grid_columnconfigure(col, weight=1)

        self.eta_label = tk.Label(self, text="ETA", bg=T.BG_PANEL,
                                   fg=T.FG_SECONDARY, font=T.FONT_SMALL)
        self.eta_label.grid(row=0, column=0, sticky="w", padx=T.GAP_L, pady=(T.GAP_M, 0))

        self.eta_value = tk.Label(self, text="—", bg=T.BG_PANEL,
                                   fg=T.FG_PRIMARY, font=T.FONT_HEADING)
        self.eta_value.grid(row=1, column=0, sticky="w", padx=T.GAP_L, pady=(0, T.GAP_M))

        self.dist_label = tk.Label(self, text="DIST", bg=T.BG_PANEL,
                                    fg=T.FG_SECONDARY, font=T.FONT_SMALL)
        self.dist_label.grid(row=0, column=1, sticky="w", padx=T.GAP_L, pady=(T.GAP_M, 0))

        self.dist_value = tk.Label(self, text="—", bg=T.BG_PANEL,
                                    fg=T.FG_PRIMARY, font=T.FONT_HEADING)
        self.dist_value.grid(row=1, column=1, sticky="w", padx=T.GAP_L, pady=(0, T.GAP_M))

    def update(self, remaining_distance_m: Optional[float], remaining_duration_s: Optional[float]):
        if remaining_distance_m is None or remaining_duration_s is None:
            self.eta_value.config(text="—")
            self.dist_value.config(text="—")
            return
        eta_dt = datetime.now().timestamp() + remaining_duration_s
        eta = datetime.fromtimestamp(eta_dt).strftime("%H:%M")
        self.eta_value.config(text=eta)
        self.dist_value.config(text=_fmt_distance(remaining_distance_m))


class BatteryCard(tk.Frame):
    """Compact BLE-battery readout — voltage, charge bar, state, conn dot.

    Sits between TripStats and the action buttons. Designed to be glanceable
    at arm's length: voltage on the left, charge bar full width below, state
    pill on the right.
    """

    BAR_HEIGHT = 8

    def __init__(self, parent):
        super().__init__(parent, bg=T.BG_PANEL_ALT, height=84)
        self.pack_propagate(False)

        top = tk.Frame(self, bg=T.BG_PANEL_ALT)
        top.pack(fill="x", padx=T.GAP_L, pady=(T.GAP_M, 0))

        self.voltage_label = tk.Label(
            top, text="--.-- V", bg=T.BG_PANEL_ALT, fg=T.BATTERY_COLOR_OFFLINE,
            font=(T.FONT_FAMILY, 22, "bold"))
        self.voltage_label.pack(side="left")

        self.state_label = tk.Label(
            top, text="BLE: searching", bg=T.BG_PANEL_ALT,
            fg=T.FG_SECONDARY, font=T.FONT_SMALL)
        self.state_label.pack(side="right")

        self.charge_label = tk.Label(
            top, text="--%", bg=T.BG_PANEL_ALT, fg=T.FG_SECONDARY,
            font=T.FONT_HEADING, padx=T.GAP_M)
        self.charge_label.pack(side="right")

        # Charge bar — drawn on a Canvas so we can colour-code segments
        self.bar_canvas = tk.Canvas(
            self, height=self.BAR_HEIGHT, bg=T.BG_PANEL_ALT,
            highlightthickness=0, bd=0)
        self.bar_canvas.pack(fill="x", padx=T.GAP_L, pady=(T.GAP_M, 0))

        self.detail_label = tk.Label(
            self, text="connect a Bluetooth battery monitor with --battery-mac",
            bg=T.BG_PANEL_ALT, fg=T.FG_MUTED,
            font=T.FONT_SMALL, anchor="w", padx=T.GAP_L)
        self.detail_label.pack(fill="x", pady=(T.GAP_S, 0))

        self.bar_canvas.bind("<Configure>", lambda _: self._redraw_bar())
        self._charge = 0
        self._bar_colour = T.BATTERY_COLOR_OFFLINE

    def update(self, reading: batmod.BatteryReading, connected: bool, enabled: bool):
        if not enabled:
            self.voltage_label.config(text="-- V", fg=T.BATTERY_COLOR_OFFLINE)
            self.charge_label.config(text="--%", fg=T.FG_MUTED)
            self.state_label.config(text="disabled", fg=T.FG_MUTED)
            self.detail_label.config(text="run with --battery-mac AA:BB:.. to enable")
            self._charge = 0
            self._bar_colour = T.BATTERY_COLOR_OFFLINE
            self._redraw_bar()
            return

        if not connected and reading.voltage is None:
            self.voltage_label.config(text="-- V", fg=T.BATTERY_COLOR_OFFLINE)
            self.charge_label.config(text="--%", fg=T.FG_MUTED)
            self.state_label.config(text="BLE: searching", fg=T.FG_WARNING)
            self.detail_label.config(text="waiting for first packet ...")
            self._charge = 0
            self._bar_colour = T.BATTERY_COLOR_OFFLINE
            self._redraw_bar()
            return

        v = reading.voltage if reading.voltage is not None else 0.0
        # voltage colour
        if v < T.BATTERY_V_CRITICAL:
            colour = T.BATTERY_COLOR_CRITICAL
        elif v < T.BATTERY_V_LOW:
            colour = T.BATTERY_COLOR_WARN
        elif v >= T.BATTERY_V_CHARGING:
            colour = T.BATTERY_COLOR_CHARGING
        else:
            colour = T.BATTERY_COLOR_OK
        if reading.is_stale(T.BATTERY_STALE_S):
            colour = T.BATTERY_COLOR_OFFLINE

        self.voltage_label.config(text=f"{v:.2f} V", fg=colour)
        self.charge_label.config(
            text=f"{reading.charge_pct}%" if reading.charge_pct is not None else "--%",
            fg=colour)

        state_label = reading.state.upper() if reading.state else "REST"
        if reading.is_stale(T.BATTERY_STALE_S):
            state_label = "STALE"
        self.state_label.config(text=state_label, fg=colour)

        age = reading.age_seconds()
        age_str = "just now" if (age is not None and age < 1.5) else (
            f"{age:.0f}s ago" if age is not None else "—")
        self.detail_label.config(
            text=f"{reading.packet_count} pkts  •  err {reading.error_count}  •  {age_str}",
            fg=T.FG_MUTED if not reading.is_stale(T.BATTERY_STALE_S) else T.FG_WARNING)

        self._charge = reading.charge_pct or 0
        self._bar_colour = colour
        self._redraw_bar()

    def _redraw_bar(self):
        c = self.bar_canvas
        c.delete("all")
        w = c.winfo_width()
        h = self.BAR_HEIGHT
        if w <= 1:
            return
        # background track
        c.create_rectangle(0, 0, w, h, fill=T.BG_RAISED, outline="")
        # fill
        fw = int(w * max(0, min(100, self._charge)) / 100.0)
        if fw > 0:
            c.create_rectangle(0, 0, fw, h, fill=self._bar_colour, outline="")


class SearchBar(tk.Frame):
    """Bottom search bar — destination entry + results dropdown.

    Resolution order when the user hits Enter:
      1. Try to parse as raw coordinates ("-31.95, 115.86" / "31°57'02\"S ...")
      2. Match against favourites.json (case-insensitive substring + aliases)
      3. Hit Nominatim (silently fails when offline; covered by 1 & 2)
    """

    def __init__(self, parent, on_destination_selected,
                 favourites: Optional[List[offmod.Favourite]] = None):
        super().__init__(parent, bg=T.BG_PANEL, height=T.SEARCH_BAR_HEIGHT)
        self.pack_propagate(False)
        self._on_destination = on_destination_selected
        self._results: List[navmod.GeocodeResult] = []
        self._dropdown: Optional[tk.Toplevel] = None
        self._favourites: List[offmod.Favourite] = favourites or []

        # Search icon
        tk.Label(self, text="🔍", bg=T.BG_PANEL, fg=T.FG_SECONDARY,
                 font=T.FONT_HEADING, padx=T.PAD_PANEL).pack(side="left")

        # Entry
        self.var = tk.StringVar()
        self.entry = tk.Entry(
            self, textvariable=self.var, bg=T.BG_PANEL_ALT, fg=T.FG_PRIMARY,
            insertbackground=T.ACCENT_CYAN, relief="flat",
            font=T.FONT_HEADING, bd=0, highlightthickness=2,
            highlightbackground=T.BG_PANEL_ALT, highlightcolor=T.ACCENT_CYAN)
        self.entry.pack(side="left", fill="both", expand=True, pady=T.GAP_M)
        self.entry.bind("<Return>", lambda _: self._do_search())
        self.entry.bind("<Escape>", lambda _: self._close_dropdown())

        # Favourites button
        if self._favourites:
            FlatButton(self, text="★ Favs", command=self._show_favourites,
                       width=110, accent=T.ACCENT_AMBER
                       ).pack(side="right", padx=(T.GAP_S, 0), pady=T.GAP_M)

        # Search button
        FlatButton(self, text="Search", command=self._do_search,
                   width=120, accent=T.ACCENT_CYAN
                   ).pack(side="right", padx=T.GAP_M, pady=T.GAP_M)

    def focus_search(self):
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def _do_search(self):
        query = self.var.get().strip()
        if not query:
            return

        # 1) Coordinate parse — instant, no network needed
        coords = offmod.parse_coordinates(query)
        if coords:
            lat, lon = coords
            self._select(navmod.GeocodeResult(
                display_name=f"Coordinates {lat:.5f}, {lon:.5f}",
                lat=lat, lon=lon))
            return

        # 2) Favourites match — also offline, instant
        fav_hits = offmod.search_favourites(self._favourites, query)
        if fav_hits:
            self._show_results([
                navmod.GeocodeResult(
                    display_name=f"★ {f.name}" + (f"  ({f.note})" if f.note else ""),
                    lat=f.lat, lon=f.lon)
                for f in fav_hits
            ])
            return

        # 3) Nominatim — needs internet, will silently fail offline
        self.entry.config(state="disabled")
        navmod.geocode_async(
            query,
            lambda results: self.after(0, self._show_results, results),
        )

    def _show_favourites(self):
        if not self._favourites:
            return
        self._show_results([
            navmod.GeocodeResult(
                display_name=f"★ {f.name}" + (f"  ({f.note})" if f.note else ""),
                lat=f.lat, lon=f.lon)
            for f in self._favourites
        ])

    def _show_results(self, results: List[navmod.GeocodeResult]):
        self.entry.config(state="normal")
        self._results = results
        self._close_dropdown()
        if not results:
            messagebox.showinfo(
                "No results",
                f"No matches for: {self.var.get()}\n\n"
                "Tip: when offline, you can enter raw coordinates "
                "(e.g. '-31.95, 115.86') or use the ★ Favs button.")
            return

        self._dropdown = tk.Toplevel(self)
        self._dropdown.overrideredirect(True)
        self._dropdown.configure(bg=T.BG_PANEL_ALT)
        x = self.entry.winfo_rootx()
        y = self.entry.winfo_rooty() - (len(results) * 60)
        w = self.entry.winfo_width()
        self._dropdown.geometry(f"{w}x{len(results)*60}+{x}+{y}")

        for r in results:
            row = tk.Frame(self._dropdown, bg=T.BG_PANEL_ALT, height=60)
            row.pack(fill="x", padx=2, pady=1)
            row.pack_propagate(False)
            label = tk.Label(
                row, text=r.display_name, bg=T.BG_PANEL_ALT, fg=T.FG_PRIMARY,
                font=T.FONT_BODY, anchor="w", justify="left",
                wraplength=w - 32)
            label.pack(fill="both", expand=True, padx=T.GAP_M, pady=T.GAP_S)
            for w_ in (row, label):
                w_.bind("<Enter>", lambda _, fr=row, lb=label: (
                    fr.config(bg=T.BG_RAISED), lb.config(bg=T.BG_RAISED)))
                w_.bind("<Leave>", lambda _, fr=row, lb=label: (
                    fr.config(bg=T.BG_PANEL_ALT), lb.config(bg=T.BG_PANEL_ALT)))
                w_.bind("<Button-1>", lambda _, res=r: self._select(res))

    def _close_dropdown(self):
        if self._dropdown:
            self._dropdown.destroy()
            self._dropdown = None

    def _select(self, result: navmod.GeocodeResult):
        self._close_dropdown()
        self.var.set(result.display_name[:50])
        self._on_destination(result)


# ═══════════════════════════════════════════════════════════════════════════
#  MAIN APP
# ═══════════════════════════════════════════════════════════════════════════

class App(tk.Tk):

    def __init__(self, gps_port: str, gps_baud: int, fullscreen: bool,
                 battery_mac: Optional[str] = None,
                 battery_sim: Optional[str] = None,
                 gps_sim: Optional[str] = None,
                 sim_center: Optional[tuple] = None):
        super().__init__()
        self.title("Project Repatriate — Navigation")
        self.configure(bg=T.BG_DEEP)
        self.geometry("1280x800")
        self.minsize(1024, 600)

        if fullscreen:
            self.attributes("-fullscreen", True)
        self.bind(f"<{T.KEYBOARD_FULLSCREEN_KEY}>", self._toggle_fullscreen)
        self.bind(f"<{T.KEYBOARD_QUIT_KEY}>", lambda _: self._on_quit())
        self.bind("<Control-l>", lambda _: self.search_bar.focus_search())
        self.bind("<Control-r>", lambda _: self._recenter_map())
        self.bind("<Control-x>", lambda _: self._cancel_route())
        self.protocol("WM_DELETE_WINDOW", self._on_quit)

        # ── state ────────────────────────────────────────────────────────
        # GPS reader: real serial reader, or a synthetic one for Windows /
        # bench testing. The two share the snapshot() API exactly.
        if gps_sim:
            clat, clon = sim_center if sim_center else (-31.9505, 115.8605)
            self.gps_reader = gpsmod.SimGpsReader(
                center_lat=clat, center_lon=clon, scenario=gps_sim)
            log.info("GPS in SIMULATION mode (%s) at (%.4f, %.4f)",
                     gps_sim, clat, clon)
        else:
            self.gps_reader = gpsmod.GpsReader(port=gps_port, baud=gps_baud)
        self.current_route: Optional[navmod.Route] = None
        self.current_destination: Optional[navmod.GeocodeResult] = None
        self.current_step_index: int = 0
        self.position_marker = None
        self.destination_marker = None
        self.route_path = None
        self._auto_follow = True
        self._last_manual_pan = 0.0
        self._online = False

        # Favourites — loaded from disk; safe if file missing
        self.favourites = offmod.load_favourites(T.FAVOURITES_PATH)
        log.info("Loaded %d favourite(s) from %s", len(self.favourites), T.FAVOURITES_PATH)

        # Battery reader — sim, real, or disabled
        self.battery_reader: Optional[Union[batmod.BatteryReader, batmod.SimBatteryReader]] = None
        self.battery_enabled = False
        if battery_sim:
            self.battery_reader = batmod.SimBatteryReader(battery_sim)
            self.battery_enabled = True
        elif battery_mac:
            self.battery_reader = batmod.BatteryReader(battery_mac)
            self.battery_enabled = True

        # ── layout ───────────────────────────────────────────────────────
        self._build_ui()

        # ── start background services ────────────────────────────────────
        self.gps_reader.start()
        if self.battery_reader is not None:
            self.battery_reader.start()
        self.after(TICK_GPS_MS, self._tick_gps)
        self.after(TICK_CLOCK_MS, self._tick_clock)
        self.after(TICK_OFFROUTE_MS, self._tick_offroute_check)
        self.after(TICK_BATTERY_MS, self._tick_battery)
        self.after(500, self._tick_online)        # initial probe quickly

    # ── UI assembly ─────────────────────────────────────────────────────
    def _build_ui(self):
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_columnconfigure(0, weight=1)

        # row 0: status bar
        self.status_bar = StatusBar(self)
        self.status_bar.grid(row=0, column=0, sticky="ew")

        # row 1: main area (sidebar + map)
        main = tk.Frame(self, bg=T.BG_DEEP)
        main.grid(row=1, column=0, sticky="nsew")
        main.grid_columnconfigure(0, weight=0, minsize=T.SIDEBAR_WIDTH)
        main.grid_columnconfigure(1, weight=1)
        main.grid_rowconfigure(0, weight=1)

        # sidebar
        sidebar = tk.Frame(main, bg=T.BG_PANEL, width=T.SIDEBAR_WIDTH)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)

        self.speedo = SpeedoCard(sidebar)
        self.speedo.pack(fill="x", padx=T.GAP_M, pady=(T.GAP_M, T.GAP_S))

        self.nav_card = NavCard(sidebar)
        self.nav_card.pack(fill="x", padx=T.GAP_M, pady=T.GAP_S)

        self.trip_stats = TripStats(sidebar)
        self.trip_stats.pack(fill="x", padx=T.GAP_M, pady=T.GAP_S)

        # battery monitor card
        self.battery_card = BatteryCard(sidebar)
        self.battery_card.pack(fill="x", padx=T.GAP_M, pady=T.GAP_S)

        # action buttons
        actions = tk.Frame(sidebar, bg=T.BG_PANEL)
        actions.pack(fill="x", padx=T.GAP_M, pady=T.GAP_M)
        FlatButton(actions, text="Re-centre", command=self._recenter_map,
                   width=140).pack(side="left", expand=True, fill="x", padx=(0, T.GAP_S))
        FlatButton(actions, text="Cancel Route", command=self._cancel_route,
                   width=140, accent=T.FG_DANGER).pack(side="right", expand=True, fill="x", padx=(T.GAP_S, 0))

        # map
        map_frame = tk.Frame(main, bg=T.BG_MAP_BORDER, padx=2, pady=2)
        map_frame.grid(row=0, column=1, sticky="nsew")
        self.map_widget = tkintermapview.TkinterMapView(
            map_frame, corner_radius=0,
            database_path=T.MAP_TILE_CACHE_DB,
            max_zoom=T.MAP_MAX_ZOOM)
        self.map_widget.set_tile_server(T.MAP_TILE_SERVER, max_zoom=T.MAP_MAX_ZOOM)
        self.map_widget.pack(fill="both", expand=True)
        # Default position — Perth, will jump on first GPS fix
        self.map_widget.set_position(-31.9505, 115.8605)
        self.map_widget.set_zoom(T.MAP_DEFAULT_ZOOM)
        # Right-click on map to set destination
        self.map_widget.add_right_click_menu_command(
            label="Set as destination",
            command=self._set_destination_from_map_click,
            pass_coords=True)

        # row 2: search bar (favourites injected so it works offline)
        self.search_bar = SearchBar(self, on_destination_selected=self._set_destination,
                                    favourites=self.favourites)
        self.search_bar.grid(row=2, column=0, sticky="ew")

    # ── tick handlers ───────────────────────────────────────────────────
    def _tick_gps(self):
        try:
            pos = self.gps_reader.snapshot()
            self.status_bar.update_gps(pos, self.gps_reader.connected)
            self.speedo.update(pos)

            if pos.has_fix():
                self._update_position_marker(pos.lat, pos.lon, pos.heading_deg)
                if self._auto_follow:
                    self.map_widget.set_position(pos.lat, pos.lon)

                if self.current_route:
                    self._update_nav_progress(pos)
        except Exception:
            log.exception("Error during GPS tick")
        finally:
            self.after(TICK_GPS_MS, self._tick_gps)

    def _tick_clock(self):
        self.status_bar.update_clock()
        self.after(TICK_CLOCK_MS, self._tick_clock)

    def _tick_battery(self):
        try:
            if self.battery_reader is not None:
                reading = self.battery_reader.snapshot()
                connected = self.battery_reader.connected
            else:
                reading = batmod.BatteryReading()
                connected = False
            self.battery_card.update(reading, connected, self.battery_enabled)
            self.status_bar.update_battery(reading, connected)
        except Exception:
            log.exception("Error during battery tick")
        finally:
            self.after(TICK_BATTERY_MS, self._tick_battery)

    def _tick_online(self):
        # Probe runs in a thread so it never stalls the UI
        def probe():
            online = offmod.is_online()
            self.after(0, self._on_online_result, online)
        threading.Thread(target=probe, daemon=True).start()
        self.after(int(TICK_ONLINE_MS), self._tick_online)

    def _on_online_result(self, online: bool):
        if online != self._online:
            log.info("Connectivity changed: %s", "ONLINE" if online else "OFFLINE")
        self._online = online
        self.status_bar.update_online(online)

    def _tick_offroute_check(self):
        try:
            if self.current_route:
                pos = self.gps_reader.snapshot()
                if pos.has_fix():
                    dist = navmod.distance_to_polyline_m(
                        pos.lat, pos.lon, self.current_route.geometry)
                    if dist > T.NAV_RECALC_OFFROUTE_M:
                        log.info("Off-route by %.0fm — recalculating", dist)
                        self._recalculate_route(pos.lat, pos.lon)
        except Exception:
            log.exception("Error during off-route check")
        finally:
            self.after(TICK_OFFROUTE_MS, self._tick_offroute_check)

    # ── map operations ──────────────────────────────────────────────────
    def _update_position_marker(self, lat: float, lon: float,
                                 heading: Optional[float]):
        if self.position_marker is None:
            self.position_marker = self.map_widget.set_marker(
                lat, lon, text="",
                marker_color_circle=T.MARKER_CURRENT_POS_OUTLINE,
                marker_color_outside=T.MARKER_CURRENT_POS_FILL)
        else:
            self.position_marker.set_position(lat, lon)

    def _recenter_map(self):
        pos = self.gps_reader.snapshot()
        if pos.has_fix():
            self.map_widget.set_position(pos.lat, pos.lon)
            self.map_widget.set_zoom(T.MAP_DEFAULT_ZOOM)
            self._auto_follow = True
        else:
            messagebox.showinfo("No GPS lock", "Waiting for first GPS fix.")

    # ── destination + route ─────────────────────────────────────────────
    def _set_destination(self, result: navmod.GeocodeResult):
        log.info("Destination: %s (%.5f, %.5f)",
                 result.display_name, result.lat, result.lon)
        self.current_destination = result

        # Drop a marker
        if self.destination_marker:
            self.destination_marker.delete()
        self.destination_marker = self.map_widget.set_marker(
            result.lat, result.lon,
            text=result.display_name.split(",")[0][:40],
            marker_color_circle=T.MARKER_DESTINATION_OUTLINE,
            marker_color_outside=T.MARKER_DESTINATION_FILL,
            text_color=T.FG_PRIMARY)
        self.map_widget.set_position(result.lat, result.lon)

        # Compute the route
        pos = self.gps_reader.snapshot()
        if not pos.has_fix():
            messagebox.showinfo("Waiting for GPS",
                                "Need a GPS fix before computing a route. "
                                "Destination saved — route will compute as soon "
                                "as the GPS locks on.")
            return
        self._recalculate_route(pos.lat, pos.lon)

    def _set_destination_from_map_click(self, coords):
        lat, lon = coords
        self._set_destination(navmod.GeocodeResult(
            display_name=f"Map point  {lat:.5f}, {lon:.5f}",
            lat=lat, lon=lon))

    def _recalculate_route(self, from_lat: float, from_lon: float):
        if not self.current_destination:
            return
        navmod.compute_route_async(
            from_lat, from_lon,
            self.current_destination.lat, self.current_destination.lon,
            lambda route: self.after(0, self._on_route_ready, route),
            cache_dir=T.ROUTE_CACHE_DIR,
            avg_kmh=T.NAV_COMPASS_AVG_KMH)

    def _on_route_ready(self, route: Optional[navmod.Route]):
        # compute_route_with_fallback always returns *something* — None means
        # an unexpected total failure (e.g. invalid coords).
        if route is None:
            messagebox.showerror("Routing failed",
                                 "Could not compute a route — even the offline "
                                 "compass fallback failed. Check the destination.")
            return
        self.current_route = route
        self.current_step_index = 0

        # Style the polyline differently for non-OSRM routes so the driver
        # immediately knows what kind of guidance they're getting.
        polyline_colour = T.ROUTE_POLYLINE_COLOR
        polyline_width  = T.ROUTE_POLYLINE_WIDTH
        if route.mode == navmod.ROUTE_MODE_COMPASS:
            polyline_colour = T.ACCENT_AMBER     # amber = "no road data, bearing only"
            polyline_width  = 4
        elif route.mode == navmod.ROUTE_MODE_CACHED:
            polyline_colour = T.ACCENT_PURPLE    # purple = previously cached

        if self.route_path:
            self.route_path.delete()
        self.route_path = self.map_widget.set_path(
            route.geometry,
            color=polyline_colour,
            width=polyline_width)

        log.info("Route ready (%s): %.1f km, %.0f min, %d steps",
                 route.mode, route.distance_m / 1000,
                 route.duration_s / 60, len(route.steps))
        if route.steps:
            self.nav_card.show_step(route.steps[0], route.steps[0].distance_m)
        self.trip_stats.update(route.distance_m, route.duration_s)

        # Friendlier-than-a-dialog notice when we've degraded to compass mode
        if route.mode == navmod.ROUTE_MODE_COMPASS:
            messagebox.showinfo(
                "Compass mode",
                "No internet — showing straight-line bearing only.\n"
                "Cached tiles will still display, but turn-by-turn isn't "
                "available until OSRM is reachable again.")

    def _cancel_route(self):
        self.current_route = None
        self.current_destination = None
        self.current_step_index = 0
        if self.route_path:
            self.route_path.delete()
            self.route_path = None
        if self.destination_marker:
            self.destination_marker.delete()
            self.destination_marker = None
        self.nav_card.show_idle()
        self.trip_stats.update(None, None)

    def _update_nav_progress(self, pos: gpsmod.Position):
        if not self.current_route or self.current_step_index >= len(self.current_route.steps):
            return

        step = self.current_route.steps[self.current_step_index]
        dist_to_maneuver = navmod.haversine_m(
            pos.lat, pos.lon, step.location[0], step.location[1])

        # Advance to next step when within 30m of the current maneuver point
        if dist_to_maneuver < 30 and self.current_step_index < len(self.current_route.steps) - 1:
            self.current_step_index += 1
            step = self.current_route.steps[self.current_step_index]
            dist_to_maneuver = navmod.haversine_m(
                pos.lat, pos.lon, step.location[0], step.location[1])

        # Arrived check — within 25m of destination
        dest = self.current_destination
        if dest and navmod.haversine_m(pos.lat, pos.lon, dest.lat, dest.lon) < 25:
            self.nav_card.show_arrived()
            self.trip_stats.update(0, 0)
            return

        self.nav_card.show_step(step, dist_to_maneuver)

        # Roughly recompute remaining distance & duration
        remaining_dist = sum(s.distance_m for s in self.current_route.steps[self.current_step_index:])
        remaining_dur = sum(s.duration_s for s in self.current_route.steps[self.current_step_index:])
        self.trip_stats.update(remaining_dist, remaining_dur)

    # ── window / shutdown ───────────────────────────────────────────────
    def _toggle_fullscreen(self, _event=None):
        is_fs = bool(self.attributes("-fullscreen"))
        self.attributes("-fullscreen", not is_fs)

    def _on_quit(self):
        log.info("Shutting down")
        try:
            self.gps_reader.stop()
            self.gps_reader.join(timeout=2)
        except Exception:
            pass
        try:
            if self.battery_reader is not None:
                self.battery_reader.stop()
                self.battery_reader.join(timeout=2)
        except Exception:
            pass
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═══════════════════════════════════════════════════════════════════════════

def _fmt_distance(m: float) -> str:
    if m is None:
        return "—"
    if m < 100:
        return f"{int(round(m / 10) * 10)} m"
    if m < 1000:
        return f"{int(round(m / 50) * 50)} m"
    if m < 10_000:
        return f"{m/1000:.1f} km"
    return f"{int(round(m/1000))} km"


def _compass(deg: float) -> str:
    if deg is None:
        return ""
    dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int((deg + 22.5) % 360 / 45)
    return dirs[idx]


# ═══════════════════════════════════════════════════════════════════════════
#  ENTRY
# ═══════════════════════════════════════════════════════════════════════════

def _parse_center(spec: str) -> tuple:
    parts = spec.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("--sim-center must be 'lat,lon'")
    return (float(parts[0]), float(parts[1]))


def main():
    parser = argparse.ArgumentParser(
        description="Project Repatriate Navigation GUI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples
  Real Pi with VL101G + battery monitor:
    python3 main.py --fullscreen --battery-mac AA:BB:CC:DD:EE:FF

  Windows / Mac / Linux desktop test mode (no hardware needed):
    python main.py --demo
    python main.py --gps-sim loop --battery-sim charge
    python main.py --gps-sim drift --sim-center "-37.81,144.96"
""")
    parser.add_argument("--port", default=gpsmod.DEFAULT_PORT,
                        help=f"Serial port for GPS (default: {gpsmod.DEFAULT_PORT})")
    parser.add_argument("--baud", type=int, default=gpsmod.DEFAULT_BAUD,
                        help=f"Baud rate (default: {gpsmod.DEFAULT_BAUD})")
    parser.add_argument("--fullscreen", action="store_true",
                        help="Start in fullscreen mode")
    parser.add_argument("--battery-mac", default=None,
                        help="BLE MAC of the battery monitor (e.g. AA:BB:CC:DD:EE:FF). "
                             "Omit to disable the battery card.")
    parser.add_argument("--battery-sim", default=None, nargs="?", const="discharge",
                        choices=["discharge", "charge", "rest", "wave"],
                        help="Use simulated battery data for bench testing. "
                             "Defaults to 'discharge' if flag given without value.")
    parser.add_argument("--gps-sim", default=None, nargs="?", const="loop",
                        choices=list(gpsmod.SimGpsReader.SCENARIOS),
                        help="Use a simulated GPS instead of opening a real serial "
                             "port — required for Windows test runs and any machine "
                             "without a GNSS device. Default scenario: 'loop'.")
    parser.add_argument("--sim-center", type=_parse_center, default=None,
                        help="Centre coordinate for the simulated GPS as 'lat,lon' "
                             "(default: Perth CBD -31.9505,115.8605).")
    parser.add_argument("--demo", action="store_true",
                        help="Convenience flag: equivalent to "
                             "'--gps-sim loop --battery-sim discharge'. "
                             "Use this to drive the full GUI on a Windows / Mac "
                             "dev box with no hardware connected.")
    parser.add_argument("--debug", action="store_true",
                        help="Verbose logging")
    args = parser.parse_args()

    # --demo turns on both simulators unless the user overrode them
    if args.demo:
        if not args.gps_sim:
            args.gps_sim = "loop"
        if not args.battery_sim:
            args.battery_sim = "discharge"

    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    app = App(gps_port=args.port, gps_baud=args.baud,
              fullscreen=args.fullscreen,
              battery_mac=args.battery_mac,
              battery_sim=args.battery_sim,
              gps_sim=args.gps_sim,
              sim_center=args.sim_center)
    app.mainloop()


if __name__ == "__main__":
    main()
