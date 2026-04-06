#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
import threading
from urllib import error, request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk


CONFIG_URL = "http://127.0.0.1:5000/api/config"
ADB_BIN = shutil.which("adb") or "/usr/bin/adb"
PHONE_ADB_TARGET = "192.168.0.8:5555"
DEFAULTS = {
    "lyrics_font_scale": 1.0,
    "album_art_scale": 1.0,
    "active_lyric_scale": 1.03,
    "control_mode": "buttons",
    "swipe_start_threshold": 6.0,
    "swipe_commit_threshold": 72.0,
    "stats_theme": "slate",
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


def http_json(url, method="GET", payload=None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(url, method=method, data=data, headers=headers)
    with request.urlopen(req, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


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
        self.scale.set_size_request(260, -1)
        self.scale.set_digits(digits)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        box.append(self.scale)

        self.reset_button = Gtk.Button(icon_name="view-refresh-symbolic")
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
        self.scale.set_size_request(260, -1)
        self.scale.set_digits(0)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        box.append(self.scale)

        self.refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
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


class DashyConfigWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Dashy Config")
        self.set_default_size(1000, 1000)
        self.set_resizable(True)
        self.set_size_request(1000, 1000)
        self.phone_brightness_source_id = 0
        self.phone_brightness_programmatic = False
        self.phone_request_in_flight = False
        self.phone_connected = False
        self.pending_phone_brightness = None

        self.toast_overlay = Adw.ToastOverlay()
        self.set_content(self.toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        title_widget = Adw.WindowTitle(title="Dashy Config", subtitle="Control dashboard layout and gestures")
        header.set_title_widget(title_widget)

        refresh_button = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_button.set_tooltip_text("Reload current config")
        refresh_button.connect("clicked", self.on_refresh)
        header.pack_start(refresh_button)

        apply_button = Gtk.Button(label="Apply")
        apply_button.add_css_class("suggested-action")
        apply_button.connect("clicked", self.on_apply)
        header.pack_end(apply_button)

        content = Gtk.ScrolledWindow()
        content.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(content)

        page = Adw.PreferencesPage()
        content.set_child(page)

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
        phone_refresh_button.connect("clicked", self.on_phone_refresh)
        self.phone_status_row.add_suffix(phone_refresh_button)
        self.phone_status_row.set_activatable(False)
        phone_group.add(self.phone_status_row)

        self.phone_brightness_row = PhoneBrightnessRow()
        self.phone_brightness_row.scale.connect("value-changed", self.on_phone_brightness_changed)
        self.phone_brightness_row.refresh_button.connect("clicked", self.on_phone_refresh)
        self.phone_brightness_row.set_sensitive(False)
        phone_group.add(self.phone_brightness_row)

        action_group = Adw.PreferencesGroup()
        page.add(action_group)

        reset_row = Adw.ActionRow(
            title="Reset Defaults",
            subtitle="Restore the default visual sizes and the button-based control scheme.",
        )
        reset_button = Gtk.Button(label="Reset")
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
            "stats_theme": STATS_THEMES[self.stats_theme_row.get_selected()] if 0 <= self.stats_theme_row.get_selected() < len(STATS_THEMES) else DEFAULTS["stats_theme"],
            "control_mode": control_mode,
            "swipe_start_threshold": self.swipe_start_row.get_value(),
            "swipe_commit_threshold": self.swipe_commit_row.get_value(),
        }

    def write_ui(self, config):
        self.lyrics_row.set_value(config.get("lyrics_font_scale", DEFAULTS["lyrics_font_scale"]))
        self.album_row.set_value(config.get("album_art_scale", DEFAULTS["album_art_scale"]))
        self.highlight_row.set_value(config.get("active_lyric_scale", DEFAULTS["active_lyric_scale"]))
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

    def on_refresh(self, _button):
        self.load_current_config()
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


class DashyConfigApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.dashy.config")

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = DashyConfigWindow(self)
        window.present()


def main():
    app = DashyConfigApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
