#!/usr/bin/env python3
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from urllib import error, request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk, Pango


CONFIG_URL = "http://127.0.0.1:5000/api/config"
GAME_ART_API_URL = "http://127.0.0.1:5000/api/game-art"
ADB_BIN = shutil.which("adb") or "/usr/bin/adb"
DDCUTIL_BIN = shutil.which("ddcutil") or "/usr/bin/ddcutil"
PHONE_ADB_TARGET = "192.168.0.8:5555"
LOCAL_CONFIG_DIR = Path.home() / ".config" / "dashy"
ADMIN_CONFIG_PATH = LOCAL_CONFIG_DIR / "admin.json"
DEFAULTS = {
    "lyrics_font_scale": 1.0,
    "album_art_scale": 1.0,
    "active_lyric_scale": 1.03,
    "stats_bg_blur": 1.0,
    "stats_bg_dim": 0.79,
    "stats_card_opacity": 0.36,
    "control_mode": "buttons",
    "swipe_start_threshold": 6.0,
    "swipe_commit_threshold": 72.0,
    "stats_theme": "slate",
}
DEFAULT_ADMIN_CONFIG = {
    "steamgriddb_api_key": "",
}
CONTROL_MODES = ("buttons", "swipe")
CONTROL_MODE_LABELS = {
    "buttons": "Buttons",
    "swipe": "Swipe Gestures",
}
STATS_THEMES = ("macchiato", "mocha", "graphite", "aurora", "slate")
STATS_THEME_LABELS = {
    "macchiato": "Catppuccin Macchiato",
    "mocha": "Catppuccin Mocha",
    "graphite": "Graphite",
    "aurora": "Aurora",
    "slate": "Slate",
}

APP_CSS = """
.dashy-compact-button {
  min-height: 28px;
  min-width: 28px;
  padding: 2px 10px;
}

.dashy-compact-icon {
  min-height: 26px;
  min-width: 26px;
  padding: 2px;
}

.dashy-card {
  background: alpha(@window_fg_color, 0.04);
  border: 1px solid alpha(@window_fg_color, 0.08);
  border-radius: 14px;
  padding: 10px;
}

.dashy-card-selected {
  background: alpha(@accent_bg_color, 0.12);
  border-color: alpha(@accent_bg_color, 0.55);
}

.dashy-selected-pill {
  background: @accent_bg_color;
  color: @accent_fg_color;
  border-radius: 999px;
  padding: 2px 8px;
  font-size: 0.84em;
  font-weight: 700;
}

.dashy-tile-meta {
  font-size: 0.9em;
}

.dashy-inline-entry {
  min-height: 30px;
}

.dashy-sidebar-row {
  padding: 8px 10px;
  border-radius: 12px;
}

.dashy-sidebar-title {
  font-weight: 600;
}

.dashy-sidebar-subtitle {
  font-size: 0.92em;
  opacity: 0.72;
}
"""


def apply_app_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(APP_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


def http_json(url, method="GET", payload=None, timeout=3):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, method=method, data=data, headers=headers)
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def load_admin_config():
    try:
        with ADMIN_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except FileNotFoundError:
        return dict(DEFAULT_ADMIN_CONFIG)
    except Exception:
        return dict(DEFAULT_ADMIN_CONFIG)

    if not isinstance(loaded, dict):
        return dict(DEFAULT_ADMIN_CONFIG)
    return {
        "steamgriddb_api_key": (loaded.get("steamgriddb_api_key") or "").strip(),
    }


def save_admin_config(config):
    LOCAL_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "steamgriddb_api_key": (config.get("steamgriddb_api_key") or "").strip(),
    }
    temp_path = ADMIN_CONFIG_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(ADMIN_CONFIG_PATH)


def run_command(command, timeout=5):
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def ensure_phone_connection():
    if not shutil.which("adb") and not (ADB_BIN and shutil.which(ADB_BIN)):
        raise RuntimeError("adb is not installed")

    run_command([ADB_BIN, "connect", PHONE_ADB_TARGET], timeout=4)
    state_result = run_command([ADB_BIN, "-s", PHONE_ADB_TARGET, "get-state"], timeout=4)
    if state_result.returncode != 0 or state_result.stdout.strip() != "device":
        message = (state_result.stderr or state_result.stdout or "phone unavailable").strip()
        raise RuntimeError(message)


def adb_shell(*args, timeout=5):
    ensure_phone_connection()
    result = run_command([ADB_BIN, "-s", PHONE_ADB_TARGET, "shell", *args], timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "adb command failed").strip())
    return result.stdout.strip()


def get_phone_brightness():
    brightness = adb_shell("settings", "get", "system", "screen_brightness")
    try:
        value = int(brightness)
    except ValueError as exc:
        raise RuntimeError(f"unexpected brightness value: {brightness}") from exc
    return max(1, min(255, value))


def set_phone_brightness(value):
    brightness = str(max(1, min(255, int(round(value)))))
    adb_shell("settings", "put", "system", "screen_brightness_mode", "0")
    adb_shell("settings", "put", "system", "screen_brightness", brightness)
    return int(brightness)


def get_monitor_brightness():
    result = run_command([DDCUTIL_BIN, "getvcp", "10"], timeout=6)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ddcutil getvcp failed").strip())

    match = re.search(r"current value\s*=\s*(\d+),\s*max value\s*=\s*(\d+)", result.stdout, re.IGNORECASE)
    if not match:
        raise RuntimeError("could not parse monitor brightness")

    current = int(match.group(1))
    maximum = int(match.group(2))
    return current, maximum


def set_monitor_brightness(value):
    result = run_command([DDCUTIL_BIN, "setvcp", "10", str(int(round(value)))], timeout=8)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ddcutil setvcp failed").strip())


class SliderRow(Adw.ActionRow):
    def __init__(self, key, title, subtitle, lower, upper, step, default_value, digits=2):
        super().__init__(title=title, subtitle=subtitle)
        self.key = key
        self.default_value = default_value
        self.adjustment = Gtk.Adjustment(
            value=default_value,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=step * 4,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.FILL)

        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adjustment)
        self.scale.set_hexpand(True)
        self.scale.set_size_request(320, -1)
        self.scale.set_digits(digits)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        box.append(self.scale)

        self.reset_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.reset_button.add_css_class("flat")
        self.reset_button.add_css_class("dashy-compact-icon")
        self.reset_button.set_tooltip_text("Reset this value")
        self.reset_button.set_valign(Gtk.Align.CENTER)
        self.reset_button.connect("clicked", self.on_reset_clicked)
        box.append(self.reset_button)

        self.add_suffix(box)
        self.set_activatable(False)

    def get_value(self):
        return self.scale.get_value()

    def set_value(self, value):
        self.scale.set_value(float(value))

    def on_reset_clicked(self, _button):
        self.set_value(self.default_value)

    def set_sensitive(self, sensitive):
        super().set_sensitive(sensitive)
        self.scale.set_sensitive(sensitive)
        self.reset_button.set_sensitive(sensitive)


class PhoneBrightnessRow(Adw.ActionRow):
    def __init__(self):
        super().__init__(
            title="Phone Brightness",
            subtitle=f"Controls Android brightness over ADB for {PHONE_ADB_TARGET}.",
        )
        self.adjustment = Gtk.Adjustment(
            value=128,
            lower=1,
            upper=255,
            step_increment=1,
            page_increment=16,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        box.set_hexpand(True)
        box.set_halign(Gtk.Align.FILL)

        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adjustment)
        self.scale.set_hexpand(True)
        self.scale.set_size_request(320, -1)
        self.scale.set_digits(0)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        box.append(self.scale)

        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        self.refresh_button.add_css_class("flat")
        self.refresh_button.add_css_class("dashy-compact-icon")
        self.refresh_button.set_tooltip_text("Read the current phone brightness")
        self.refresh_button.set_valign(Gtk.Align.CENTER)
        box.append(self.refresh_button)

        self.add_suffix(box)
        self.set_activatable(False)

    def get_value(self):
        return self.scale.get_value()

    def set_value(self, value):
        self.scale.set_value(float(value))

    def set_sensitive(self, sensitive):
        super().set_sensitive(sensitive)
        self.scale.set_sensitive(sensitive)
        self.refresh_button.set_sensitive(sensitive)


class AssetTile(Gtk.Button):
    def __init__(self, asset, width=220, height=130):
        super().__init__()
        self.asset = asset
        self.set_valign(Gtk.Align.START)
        self.add_css_class("flat")
        self.add_css_class("dashy-card")
        if asset.get("selected"):
            self.add_css_class("dashy-card-selected")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_halign(Gtk.Align.FILL)
        if asset.get("selected"):
            selected_badge = Gtk.Label(label="Selected")
            selected_badge.add_css_class("dashy-selected-pill")
            selected_badge.set_halign(Gtk.Align.START)
            header.append(selected_badge)
        else:
            spacer = Gtk.Box()
            spacer.set_hexpand(True)
            header.append(spacer)
        box.append(header)

        picture = Gtk.Picture()
        picture.set_size_request(width, height)
        picture.set_can_shrink(True)
        asset_width = max(1, int(asset.get("width", 0) or 1))
        asset_height = max(1, int(asset.get("height", 0) or 1))
        aspect = asset_width / asset_height
        picture.set_content_fit(Gtk.ContentFit.CONTAIN if aspect > 1.6 or abs(aspect - 1.0) < 0.2 else Gtk.ContentFit.COVER)
        thumb_path = asset.get("thumb_path")
        if thumb_path and Path(thumb_path).exists():
            picture.set_filename(thumb_path)
        box.append(picture)

        meta = Gtk.Label(
            label=f"{asset.get('width', 0)}x{asset.get('height', 0)}",
            xalign=0,
        )
        meta.add_css_class("dim-label")
        meta.add_css_class("dashy-tile-meta")
        box.append(meta)
        self.set_child(box)


class GameListRow(Gtk.ListBoxRow):
    def __init__(self, game):
        super().__init__()
        self.game_payload = game

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        outer.add_css_class("dashy-sidebar-row")
        outer.set_margin_top(2)
        outer.set_margin_bottom(2)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        thumb_path = (
            game.get("selected_logo_thumb_path")
            or game.get("selected_thumb_path")
        )
        if thumb_path:
            thumb = Gtk.Picture()
            thumb.set_size_request(56, 40)
            thumb.set_can_shrink(True)
            thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
            thumb.set_filename(thumb_path)
            outer.append(thumb)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_hexpand(True)
        text_box.set_valign(Gtk.Align.CENTER)

        title = Gtk.Label(xalign=0)
        title.add_css_class("dashy-sidebar-title")
        title.set_text(game.get("display_name") or game.get("game_key"))
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_single_line_mode(True)
        title.set_hexpand(True)
        text_box.append(title)

        subtitle_text = game.get("matched_game_name") or "No SteamGridDB match yet"
        if game.get("active"):
            subtitle_text = f"Active now • {subtitle_text}"
        subtitle = Gtk.Label(xalign=0)
        subtitle.add_css_class("dashy-sidebar-subtitle")
        subtitle.set_text(subtitle_text)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        subtitle.set_single_line_mode(True)
        subtitle.set_hexpand(True)
        text_box.append(subtitle)

        outer.append(text_box)
        self.set_child(outer)


class GameArtManagerWindow(Adw.Window):
    def __init__(self, parent_window, show_toast):
        super().__init__(transient_for=parent_window, modal=False, title="Game Wallpapers")
        self.set_default_size(1260, 860)
        self.set_size_request(860, 640)
        self.show_toast = show_toast
        self.current_games = []
        self.current_game = None
        self.busy_count = 0

        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)
        header.set_title_widget(Adw.WindowTitle(title="Game Wallpapers", subtitle="Match detected MangoHud games to SteamGridDB background art and FPS logos"))

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.add_css_class("flat")
        refresh_button.add_css_class("dashy-compact-icon")
        refresh_button.set_tooltip_text("Reload detected games and cached hero art")
        refresh_button.connect("clicked", self.on_refresh_clicked)
        header.pack_start(refresh_button)
        self.refresh_button = refresh_button

        self.spinner = Gtk.Spinner()
        self.spinner.set_spinning(False)
        header.pack_end(self.spinner)

        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)
        paned.set_position(340)
        toolbar_view.set_content(paned)

        left_scroll = Gtk.ScrolledWindow()
        left_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        paned.set_start_child(left_scroll)

        self.games_list = Gtk.ListBox()
        self.games_list.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.games_list.add_css_class("boxed-list")
        self.games_list.connect("row-selected", self.on_game_selected)
        left_scroll.set_child(self.games_list)

        right_scroll = Gtk.ScrolledWindow()
        right_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        paned.set_end_child(right_scroll)

        self.detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.detail_box.set_margin_top(18)
        self.detail_box.set_margin_bottom(18)
        self.detail_box.set_margin_start(18)
        self.detail_box.set_margin_end(18)
        right_scroll.set_child(self.detail_box)

        self.title_label = Gtk.Label(xalign=0)
        self.title_label.add_css_class("title-2")
        self.detail_box.append(self.title_label)

        self.match_label = Gtk.Label(xalign=0, wrap=True)
        self.match_label.add_css_class("dim-label")
        self.detail_box.append(self.match_label)

        query_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.detail_box.append(query_row)

        self.query_entry = Gtk.Entry()
        self.query_entry.set_hexpand(True)
        self.query_entry.set_width_chars(28)
        self.query_entry.add_css_class("dashy-inline-entry")
        self.query_entry.set_placeholder_text("SteamGridDB lookup query")
        query_row.append(self.query_entry)

        self.refetch_button = Gtk.Button(label="Match")
        self.refetch_button.add_css_class("dashy-compact-button")
        self.refetch_button.connect("clicked", self.on_refetch_clicked)
        query_row.append(self.refetch_button)

        self.asset_stack = Gtk.Stack()
        self.asset_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.asset_stack.set_vexpand(True)

        self.asset_switcher = Gtk.StackSwitcher()
        self.asset_switcher.set_stack(self.asset_stack)
        self.asset_switcher.set_halign(Gtk.Align.START)
        self.detail_box.append(self.asset_switcher)
        self.detail_box.append(self.asset_stack)

        wallpaper_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.asset_stack.add_titled(wallpaper_page, "wallpaper", "Background Image")

        self.selected_preview = Gtk.Picture()
        self.selected_preview.set_size_request(720, 232)
        self.selected_preview.set_can_shrink(True)
        self.selected_preview.set_content_fit(Gtk.ContentFit.COVER)
        wallpaper_page.append(self.selected_preview)

        self.heroes_flow = Gtk.FlowBox()
        self.heroes_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.heroes_flow.set_max_children_per_line(3)
        self.heroes_flow.set_column_spacing(12)
        self.heroes_flow.set_row_spacing(12)
        wallpaper_page.append(self.heroes_flow)

        fps_page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.asset_stack.add_titled(fps_page, "fps", "FPS Card")

        self.logo_preview = Gtk.Picture()
        self.logo_preview.set_size_request(340, 140)
        self.logo_preview.set_can_shrink(True)
        self.logo_preview.set_content_fit(Gtk.ContentFit.CONTAIN)
        fps_page.append(self.logo_preview)

        logos_heading = Gtk.Label(label="Logos", xalign=0)
        logos_heading.add_css_class("heading")
        fps_page.append(logos_heading)

        self.logos_flow = Gtk.FlowBox()
        self.logos_flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.logos_flow.set_max_children_per_line(4)
        self.logos_flow.set_column_spacing(12)
        self.logos_flow.set_row_spacing(12)
        fps_page.append(self.logos_flow)

        self.load_games_async()

    def set_busy(self, busy, subtitle=None):
        self.busy_count = max(0, self.busy_count + (1 if busy else -1))
        active = self.busy_count > 0
        self.spinner.set_spinning(active)
        self.refresh_button.set_sensitive(not active)
        self.refetch_button.set_sensitive(not active and self.current_game is not None)
        self.query_entry.set_sensitive(not active)
        if subtitle is not None:
            self.match_label.set_text(subtitle)

    def show_placeholder(self, title="No game selected", subtitle="Pick a detected game from the list to manage its wallpaper."):
        self.title_label.set_text(title)
        self.match_label.set_text(subtitle)
        self.query_entry.set_text("")
        self.selected_preview.set_visible(False)
        self.logo_preview.set_visible(False)
        self.asset_stack.set_visible_child_name("wallpaper")
        for flow in (self.heroes_flow, self.logos_flow):
            child = flow.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                flow.remove(child)
                child = next_child

    def load_games_async(self):
        self.set_busy(True, "Reading detected MangoHud games and cached artwork.")
        self.show_placeholder("Loading games…", "Reading detected MangoHud games and cached artwork.")

        def worker():
            try:
                payload = http_json(f"{GAME_ART_API_URL}/games", timeout=10)
            except Exception as exc:
                GLib.idle_add(self.on_games_loaded, None, str(exc))
                return
            GLib.idle_add(self.on_games_loaded, payload, None)

        threading.Thread(target=worker, daemon=True).start()

    def on_games_loaded(self, payload, error_message):
        self.set_busy(False)
        child = self.games_list.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.games_list.remove(child)
            child = next_child

        if error_message:
            self.show_placeholder("Could not load games", error_message)
            return False

        self.current_games = payload.get("games", [])
        active_key = payload.get("active_game_key")

        for game in self.current_games:
            self.games_list.append(GameListRow(game))

        if not self.current_games:
            self.show_placeholder("No games yet", "Launch a MangoHud-enabled game to populate the wallpaper cache.")
            return False

        selected_row = None
        row = self.games_list.get_first_child()
        while row:
            if getattr(row, "game_payload", {}).get("game_key") == active_key:
                selected_row = row
                break
            row = row.get_next_sibling()
        if selected_row is None:
            selected_row = self.games_list.get_first_child()
        self.games_list.select_row(selected_row)
        return False

    def on_game_selected(self, _listbox, row):
        if row is None:
            self.current_game = None
            self.show_placeholder()
            return

        game = row.game_payload
        self.current_game = game
        self.title_label.set_text(game.get("display_name") or game.get("game_key"))
        matched = game.get("matched_game_name") or "No SteamGridDB match yet"
        self.match_label.set_text(f"Current match: {matched}")
        self.query_entry.set_text(game.get("lookup_query") or game.get("display_name") or "")

        wallpaper_path = game.get("selected_image_path")
        if wallpaper_path and Path(wallpaper_path).exists():
            self.selected_preview.set_filename(wallpaper_path)
            self.selected_preview.set_visible(True)
        else:
            self.selected_preview.set_visible(False)

        logo_path = game.get("selected_logo_path")
        if logo_path and Path(logo_path).exists():
            self.logo_preview.set_filename(logo_path)
            self.logo_preview.set_visible(True)
        else:
            self.logo_preview.set_visible(False)

        for flow in (self.heroes_flow, self.logos_flow):
            child = flow.get_first_child()
            while child:
                next_child = child.get_next_sibling()
                flow.remove(child)
                child = next_child

        for hero in game.get("heroes", []):
            tile = AssetTile(hero, width=220, height=130)
            tile.connect("clicked", self.on_asset_clicked, "hero", hero.get("id"))
            self.heroes_flow.insert(tile, -1)

        for logo in game.get("logos", []):
            tile = AssetTile(logo, width=140, height=140)
            tile.connect("clicked", self.on_asset_clicked, "logo", logo.get("id"))
            self.logos_flow.insert(tile, -1)

    def on_refresh_clicked(self, _button):
        self.load_games_async()

    def on_refetch_clicked(self, _button):
        if not self.current_game:
            return
        query = self.query_entry.get_text().strip()
        game_key = self.current_game.get("game_key")
        self.set_busy(True, "Refreshing SteamGridDB match…")

        def worker():
            try:
                http_json(f"{GAME_ART_API_URL}/refresh", method="POST", payload={"game_key": game_key, "query": query}, timeout=30)
            except Exception as exc:
                GLib.idle_add(self.on_refresh_failed, str(exc))
                return
            GLib.idle_add(self.on_refresh_success)

        threading.Thread(target=worker, daemon=True).start()

    def on_refresh_success(self):
        self.set_busy(False)
        self.show_toast("Wallpaper candidates refreshed.")
        self.load_games_async()
        return False

    def on_refresh_failed(self, message):
        self.set_busy(False)
        self.show_toast(f"Game-art refresh failed: {message}")
        self.match_label.set_text(f"Refresh failed: {message}")
        return False

    def on_asset_clicked(self, _button, asset_kind, asset_id):
        if not self.current_game:
            return
        game_key = self.current_game.get("game_key")
        self.set_busy(True, f"Applying {asset_kind} selection…")

        def worker():
            try:
                http_json(
                    f"{GAME_ART_API_URL}/select",
                    method="POST",
                    payload={"game_key": game_key, "hero_id": asset_id, "asset_kind": asset_kind},
                    timeout=20,
                )
            except Exception as exc:
                GLib.idle_add(self.on_select_failed, str(exc))
                return
            GLib.idle_add(self.on_select_success)

        threading.Thread(target=worker, daemon=True).start()

    def on_select_success(self):
        self.set_busy(False)
        self.show_toast("Wallpaper updated.")
        self.load_games_async()
        return False

    def on_select_failed(self, message):
        self.set_busy(False)
        self.show_toast(f"Could not apply wallpaper: {message}")
        return False


class DashyConfigWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Dashy Config")
        self.set_default_size(900, 760)
        self.set_resizable(True)
        self.set_size_request(760, 620)
        self.phone_brightness_source_id = 0
        self.phone_brightness_programmatic = False
        self.phone_request_in_flight = False
        self.phone_connected = False
        self.pending_phone_brightness = None
        self.monitor_match_in_flight = False
        self.admin_config = load_admin_config()
        self.game_art_window = None

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        title_widget = Adw.WindowTitle(title="Dashy Config", subtitle="Control dashboard layout and gestures")
        header.set_title_widget(title_widget)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.add_css_class("flat")
        refresh_button.add_css_class("dashy-compact-icon")
        refresh_button.set_tooltip_text("Reload current config")
        refresh_button.connect("clicked", self.on_refresh)
        header.pack_start(refresh_button)

        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("dashy-compact-button")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", self.on_apply)
        header.pack_end(apply_button)

        content = Gtk.ScrolledWindow()
        content.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(content)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(940)
        clamp.set_tightening_threshold(760)
        content.set_child(clamp)

        page = Adw.PreferencesPage()
        clamp.set_child(page)

        display_group = Adw.PreferencesGroup(
            title="Display",
            description="Tune the relative sizing of the main dashboard elements.",
        )
        page.add(display_group)

        self.lyrics_row = SliderRow(
            "lyrics_font_scale",
            "Lyrics Size",
            "Scales the lyrics column without changing layout structure.",
            0.8,
            1.5,
            0.02,
            DEFAULTS["lyrics_font_scale"],
        )
        display_group.add(self.lyrics_row)

        self.album_row = SliderRow(
            "album_art_scale",
            "Album Art Size",
            "Adjusts the artwork panel size within the existing responsive layout.",
            0.85,
            1.25,
            0.01,
            DEFAULTS["album_art_scale"],
        )
        display_group.add(self.album_row)

        self.highlight_row = SliderRow(
            "active_lyric_scale",
            "Highlight Scale",
            "Controls how much larger the active lyric appears.",
            1.0,
            1.12,
            0.005,
            DEFAULTS["active_lyric_scale"],
            digits=3,
        )
        display_group.add(self.highlight_row)

        stats_group = Adw.PreferencesGroup(
            title="Stats View",
            description="Control the appearance of the system stats dashboard without affecting music mode.",
        )
        page.add(stats_group)

        self.stats_theme_row = Adw.ComboRow(title="Stats Theme")
        self.stats_theme_row.set_subtitle("Slate is the default. Themes affect only the stats page.")
        self.stats_theme_model = Gtk.StringList.new([STATS_THEME_LABELS[theme] for theme in STATS_THEMES])
        self.stats_theme_row.set_model(self.stats_theme_model)
        stats_group.add(self.stats_theme_row)

        self.stats_blur_row = SliderRow(
            "stats_bg_blur",
            "Background Blur",
            "Controls how soft the game-art background looks in the stats view.",
            0.0,
            12.0,
            0.5,
            DEFAULTS["stats_bg_blur"],
            digits=1,
        )
        stats_group.add(self.stats_blur_row)

        self.stats_dim_row = SliderRow(
            "stats_bg_dim",
            "Background Brightness",
            "Higher values brighten the game-art background; lower values darken it.",
            0.45,
            1.0,
            0.01,
            DEFAULTS["stats_bg_dim"],
            digits=2,
        )
        stats_group.add(self.stats_dim_row)

        self.stats_card_opacity_row = SliderRow(
            "stats_card_opacity",
            "Card Opacity",
            "Controls how strongly the stats cards cover the game-art background.",
            0.12,
            0.72,
            0.01,
            DEFAULTS["stats_card_opacity"],
            digits=2,
        )
        stats_group.add(self.stats_card_opacity_row)

        game_art_group = Adw.PreferencesGroup(
            title="Game Art",
            description="SteamGridDB-backed game wallpaper matching for the stats page and FPS card.",
        )
        page.add(game_art_group)

        self.sgdb_key_row = Adw.ActionRow(
            title="SteamGridDB API Key",
            subtitle="Stored locally on this PC only. Dashy uses it to match MangoHud-detected games to hero artwork.",
        )
        sgdb_key_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        sgdb_key_box.set_halign(Gtk.Align.END)
        sgdb_key_box.set_valign(Gtk.Align.CENTER)
        self.sgdb_key_entry = Gtk.Entry()
        self.sgdb_key_entry.set_hexpand(False)
        self.sgdb_key_entry.set_width_chars(26)
        self.sgdb_key_entry.set_max_width_chars(32)
        self.sgdb_key_entry.add_css_class("dashy-inline-entry")
        self.sgdb_key_entry.set_placeholder_text("Enter SteamGridDB API key")
        self.sgdb_key_entry.set_text(self.admin_config.get("steamgriddb_api_key", ""))
        sgdb_key_box.append(self.sgdb_key_entry)
        sgdb_save_button = Gtk.Button(label="Save")
        sgdb_save_button.add_css_class("dashy-compact-button")
        sgdb_save_button.connect("clicked", self.on_save_sgdb_key)
        sgdb_key_box.append(sgdb_save_button)
        self.sgdb_key_row.add_suffix(sgdb_key_box)
        self.sgdb_key_row.set_activatable(False)
        game_art_group.add(self.sgdb_key_row)

        manage_art_row = Adw.ActionRow(
            title="Manage Game Wallpapers",
            subtitle="Review detected games, fix bad matches, and choose a different cached hero image per game.",
        )
        manage_art_button = Gtk.Button(label="Open")
        manage_art_button.add_css_class("dashy-compact-button")
        manage_art_button.set_halign(Gtk.Align.END)
        manage_art_button.set_valign(Gtk.Align.CENTER)
        manage_art_button.connect("clicked", self.on_open_game_art_manager)
        manage_art_row.add_suffix(manage_art_button)
        manage_art_row.set_activatable(False)
        game_art_group.add(manage_art_row)

        controls_group = Adw.PreferencesGroup(
            title="Controls",
            description="Choose between on-screen transport buttons or swipe interactions on the album art.",
        )
        page.add(controls_group)

        self.control_mode_row = Adw.ComboRow(title="Control Mode")
        self.control_mode_row.set_subtitle("Swipe mode enables tap-to-pause and left/right track gestures on the album art.")
        self.control_mode_model = Gtk.StringList.new([CONTROL_MODE_LABELS[mode] for mode in CONTROL_MODES])
        self.control_mode_row.set_model(self.control_mode_model)
        self.control_mode_row.connect("notify::selected", self.on_control_mode_changed)
        controls_group.add(self.control_mode_row)

        self.swipe_start_row = SliderRow(
            "swipe_start_threshold",
            "Swipe Start Threshold",
            "How far you need to drag before Dashy treats it as a swipe.",
            2.0,
            24.0,
            1.0,
            DEFAULTS["swipe_start_threshold"],
            digits=0,
        )
        controls_group.add(self.swipe_start_row)

        self.swipe_commit_row = SliderRow(
            "swipe_commit_threshold",
            "Swipe Commit Threshold",
            "How far you need to drag before the previous/next action triggers on release.",
            8.0,
            72.0,
            2.0,
            DEFAULTS["swipe_commit_threshold"],
            digits=0,
        )
        controls_group.add(self.swipe_commit_row)

        phone_group = Adw.PreferencesGroup(
            title="Phone",
            description="Reads and updates Android brightness over ADB when the phone is reachable.",
        )
        page.add(phone_group)

        self.phone_status_row = Adw.ActionRow(
            title="Phone Status",
            subtitle=f"Checking {PHONE_ADB_TARGET}…",
        )
        phone_refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        phone_refresh_button.set_tooltip_text("Reconnect and refresh the phone brightness")
        phone_refresh_button.set_halign(Gtk.Align.END)
        phone_refresh_button.set_valign(Gtk.Align.CENTER)
        phone_refresh_button.add_css_class("dashy-compact-icon")
        phone_refresh_button.connect("clicked", self.on_phone_refresh)
        self.phone_status_row.add_suffix(phone_refresh_button)
        self.phone_status_row.set_activatable(False)
        phone_group.add(self.phone_status_row)

        self.phone_brightness_row = PhoneBrightnessRow()
        self.phone_brightness_row.scale.connect("value-changed", self.on_phone_brightness_changed)
        self.phone_brightness_row.refresh_button.connect("clicked", self.on_phone_refresh)
        self.phone_brightness_row.set_sensitive(False)
        phone_group.add(self.phone_brightness_row)

        self.monitor_match_row = Adw.ActionRow(
            title="Match Phone To Monitor",
            subtitle="Reads monitor brightness with ddcutil and applies the equivalent brightness to the phone.",
        )
        self.monitor_match_button = Gtk.Button(label="Match")
        self.monitor_match_button.add_css_class("dashy-compact-button")
        self.monitor_match_button.set_halign(Gtk.Align.END)
        self.monitor_match_button.set_valign(Gtk.Align.CENTER)
        self.monitor_match_button.connect("clicked", self.on_match_monitor_brightness)
        self.monitor_match_row.add_suffix(self.monitor_match_button)
        self.monitor_match_row.set_activatable(False)
        phone_group.add(self.monitor_match_row)

        action_group = Adw.PreferencesGroup()
        page.add(action_group)

        reset_row = Adw.ActionRow(
            title="Reset Defaults",
            subtitle="Restore the default visual sizes and the button-based control scheme.",
        )
        reset_button = Gtk.Button(label="Reset")
        reset_button.add_css_class("dashy-compact-button")
        reset_button.set_halign(Gtk.Align.END)
        reset_button.set_valign(Gtk.Align.CENTER)
        reset_button.connect("clicked", self.on_reset)
        reset_row.add_suffix(reset_button)
        reset_row.set_activatable(False)
        action_group.add(reset_row)

        self.load_current_config()
        self.refresh_phone_brightness()

    def show_toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def read_ui(self):
        selected = self.control_mode_row.get_selected()
        control_mode = CONTROL_MODES[selected] if 0 <= selected < len(CONTROL_MODES) else DEFAULTS["control_mode"]
        return {
            "lyrics_font_scale": self.lyrics_row.get_value(),
            "album_art_scale": self.album_row.get_value(),
            "active_lyric_scale": self.highlight_row.get_value(),
            "stats_bg_blur": self.stats_blur_row.get_value(),
            "stats_bg_dim": self.stats_dim_row.get_value(),
            "stats_card_opacity": self.stats_card_opacity_row.get_value(),
            "stats_theme": STATS_THEMES[self.stats_theme_row.get_selected()] if 0 <= self.stats_theme_row.get_selected() < len(STATS_THEMES) else DEFAULTS["stats_theme"],
            "control_mode": control_mode,
            "swipe_start_threshold": self.swipe_start_row.get_value(),
            "swipe_commit_threshold": self.swipe_commit_row.get_value(),
        }

    def write_ui(self, config):
        self.lyrics_row.set_value(config.get("lyrics_font_scale", DEFAULTS["lyrics_font_scale"]))
        self.album_row.set_value(config.get("album_art_scale", DEFAULTS["album_art_scale"]))
        self.highlight_row.set_value(config.get("active_lyric_scale", DEFAULTS["active_lyric_scale"]))
        self.stats_blur_row.set_value(config.get("stats_bg_blur", DEFAULTS["stats_bg_blur"]))
        self.stats_dim_row.set_value(config.get("stats_bg_dim", DEFAULTS["stats_bg_dim"]))
        self.stats_card_opacity_row.set_value(config.get("stats_card_opacity", DEFAULTS["stats_card_opacity"]))
        stats_theme = config.get("stats_theme", DEFAULTS["stats_theme"])
        if stats_theme not in STATS_THEMES:
            stats_theme = DEFAULTS["stats_theme"]
        self.stats_theme_row.set_selected(STATS_THEMES.index(stats_theme))
        self.swipe_start_row.set_value(config.get("swipe_start_threshold", DEFAULTS["swipe_start_threshold"]))
        self.swipe_commit_row.set_value(config.get("swipe_commit_threshold", DEFAULTS["swipe_commit_threshold"]))
        control_mode = config.get("control_mode", DEFAULTS["control_mode"])
        if control_mode not in CONTROL_MODES:
            control_mode = DEFAULTS["control_mode"]
        self.control_mode_row.set_selected(CONTROL_MODES.index(control_mode))
        self.update_swipe_row_state()

    def load_current_config(self):
        try:
            config = http_json(CONFIG_URL)
        except Exception as exc:
            self.write_ui(DEFAULTS)
            self.show_toast(f"Could not load config: {exc}")
            return

        self.write_ui(config)
        self.show_toast("Loaded current config.")

    def on_apply(self, _button):
        try:
            response = http_json(CONFIG_URL, method="POST", payload=self.read_ui())
        except error.URLError as exc:
            self.show_toast(f"Apply failed: {exc.reason}")
            return
        except Exception as exc:
            self.show_toast(f"Apply failed: {exc}")
            return

        config = response.get("config", DEFAULTS)
        self.write_ui(config)
        self.show_toast("Config applied.")

    def on_reset(self, _button):
        self.write_ui(DEFAULTS)
        self.on_apply(_button)

    def on_save_sgdb_key(self, _button):
        self.admin_config["steamgriddb_api_key"] = self.sgdb_key_entry.get_text().strip()
        try:
            save_admin_config(self.admin_config)
        except Exception as exc:
            self.show_toast(f"Could not save SteamGridDB key: {exc}")
            return
        self.show_toast("SteamGridDB key saved locally.")

    def on_open_game_art_manager(self, _button):
        if self.game_art_window is None:
            self.game_art_window = GameArtManagerWindow(self, self.show_toast)
            self.game_art_window.connect("close-request", self.on_game_art_window_closed)
        self.game_art_window.present()

    def on_game_art_window_closed(self, _window):
        self.game_art_window = None
        return False

    def on_refresh(self, _button):
        self.load_current_config()
        self.admin_config = load_admin_config()
        self.sgdb_key_entry.set_text(self.admin_config.get("steamgriddb_api_key", ""))
        self.refresh_phone_brightness()

    def on_control_mode_changed(self, _row, _pspec):
        self.update_swipe_row_state()

    def update_swipe_row_state(self):
        selected = self.control_mode_row.get_selected()
        control_mode = CONTROL_MODES[selected] if 0 <= selected < len(CONTROL_MODES) else DEFAULTS["control_mode"]
        swipe_enabled = control_mode == "swipe"
        self.swipe_start_row.set_sensitive(swipe_enabled)
        self.swipe_commit_row.set_sensitive(swipe_enabled)

    def set_phone_status(self, title, subtitle, available):
        self.phone_connected = available
        self.phone_status_row.set_title(title)
        self.phone_status_row.set_subtitle(subtitle)
        self.phone_brightness_row.set_sensitive(available or self.phone_request_in_flight)
        self.monitor_match_button.set_sensitive(available and not self.monitor_match_in_flight)

    def refresh_phone_brightness(self):
        if self.phone_request_in_flight:
            return

        self.phone_request_in_flight = True
        self.set_phone_status("Phone Status", f"Connecting to {PHONE_ADB_TARGET}…", False)

        def worker():
            try:
                brightness = get_phone_brightness()
            except Exception as exc:
                GLib.idle_add(self.on_phone_refresh_failed, str(exc))
                return
            GLib.idle_add(self.on_phone_refresh_success, brightness)

        threading.Thread(target=worker, daemon=True).start()

    def on_phone_refresh_success(self, brightness):
        self.phone_request_in_flight = False
        self.phone_brightness_programmatic = True
        self.phone_brightness_row.set_value(brightness)
        self.phone_brightness_programmatic = False
        percent = round((brightness / 255) * 100)
        self.set_phone_status("Phone Status", f"Connected to {PHONE_ADB_TARGET} • Current brightness: {brightness}/255 ({percent}%)", True)
        self.phone_brightness_row.set_subtitle("Live updates are sent while you drag the slider.")
        self.flush_pending_phone_brightness()
        return False

    def on_phone_refresh_failed(self, message):
        self.phone_request_in_flight = False
        self.set_phone_status("Phone Status", f"Unavailable: {message}", False)
        self.phone_brightness_row.set_subtitle(f"Could not reach {PHONE_ADB_TARGET}.")
        return False

    def on_phone_refresh(self, _button):
        self.refresh_phone_brightness()

    def on_phone_brightness_changed(self, _scale):
        if self.phone_brightness_programmatic or not self.phone_connected:
            return
        if self.phone_brightness_source_id:
            GLib.source_remove(self.phone_brightness_source_id)
        self.phone_brightness_source_id = GLib.timeout_add(70, self.commit_phone_brightness)

    def commit_phone_brightness(self):
        self.phone_brightness_source_id = 0
        target_value = int(round(self.phone_brightness_row.get_value()))
        self.pending_phone_brightness = target_value

        if self.phone_request_in_flight:
            return False

        self.flush_pending_phone_brightness()
        return False

    def flush_pending_phone_brightness(self):
        if self.phone_request_in_flight or self.pending_phone_brightness is None or not self.phone_connected:
            return

        target_value = self.pending_phone_brightness
        self.pending_phone_brightness = None
        self.phone_request_in_flight = True
        self.phone_brightness_row.set_sensitive(True)
        self.set_phone_status("Phone Status", f"Setting brightness to {target_value}/255…", True)

        def worker():
            try:
                applied = set_phone_brightness(target_value)
            except Exception as exc:
                GLib.idle_add(self.on_phone_set_failed, str(exc))
                return
            GLib.idle_add(self.on_phone_set_success, applied)

        threading.Thread(target=worker, daemon=True).start()

    def on_phone_set_success(self, brightness):
        self.phone_request_in_flight = False
        self.phone_brightness_programmatic = True
        self.phone_brightness_row.set_value(brightness)
        self.phone_brightness_programmatic = False
        percent = round((brightness / 255) * 100)
        self.set_phone_status("Phone Status", f"Connected to {PHONE_ADB_TARGET} • Brightness set to {brightness}/255 ({percent}%)", True)
        self.flush_pending_phone_brightness()
        return False

    def on_phone_set_failed(self, message):
        self.phone_request_in_flight = False
        self.set_phone_status("Phone Status", f"Brightness update failed: {message}", False)
        self.show_toast(f"Phone brightness failed: {message}")
        return False

    def on_match_monitor_brightness(self, _button):
        if self.monitor_match_in_flight or not self.phone_connected:
            return

        self.monitor_match_in_flight = True
        self.monitor_match_button.set_sensitive(False)
        self.monitor_match_row.set_subtitle("Reading monitor brightness and updating the phone…")

        def worker():
            try:
                current_monitor, max_monitor = get_monitor_brightness()
                target_phone = max(1, min(255, round((current_monitor / max_monitor) * 255))) if max_monitor else 1
                applied_phone = set_phone_brightness(target_phone)
            except Exception as exc:
                GLib.idle_add(self.on_match_monitor_failed, str(exc))
                return
            GLib.idle_add(self.on_match_monitor_success, current_monitor, max_monitor, applied_phone)

        threading.Thread(target=worker, daemon=True).start()

    def on_match_monitor_success(self, current_monitor, max_monitor, applied_phone):
        self.monitor_match_in_flight = False
        self.monitor_match_button.set_sensitive(self.phone_connected)
        monitor_percent = round((current_monitor / max_monitor) * 100) if max_monitor else 0
        phone_percent = round((applied_phone / 255) * 100)
        self.monitor_match_row.set_subtitle(
            f"Matched monitor {current_monitor}/{max_monitor} ({monitor_percent}%) to phone {applied_phone}/255 ({phone_percent}%)."
        )
        self.phone_brightness_programmatic = True
        self.phone_brightness_row.set_value(applied_phone)
        self.phone_brightness_programmatic = False
        self.show_toast("Phone brightness matched to monitor.")
        return False

    def on_match_monitor_failed(self, message):
        self.monitor_match_in_flight = False
        self.monitor_match_button.set_sensitive(self.phone_connected)
        self.monitor_match_row.set_subtitle(f"Monitor match failed: {message}")
        self.show_toast(f"Monitor match failed: {message}")
        return False


class DashyConfigApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.dashy.config")
        self.main_window = None

    def do_activate(self):
        apply_app_css()
        if self.main_window is None:
            self.main_window = DashyConfigWindow(self)
            self.main_window.connect("close-request", self.on_main_window_close_request)
        self.main_window.present()

    def on_main_window_close_request(self, _window):
        self.main_window = None
        return False


def main():
    app = DashyConfigApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
