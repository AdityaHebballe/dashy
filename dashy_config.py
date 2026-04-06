#!/usr/bin/env python3
import json
import sys
from urllib import error, request

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk


CONFIG_URL = "http://127.0.0.1:5000/api/config"
DEFAULTS = {
    "lyrics_font_scale": 1.0,
    "album_art_scale": 1.0,
    "active_lyric_scale": 1.03,
    "control_mode": "buttons",
    "swipe_start_threshold": 6.0,
    "swipe_commit_threshold": 22.0,
}
CONTROL_MODES = ("buttons", "swipe")
CONTROL_MODE_LABELS = {
    "buttons": "Buttons",
    "swipe": "Swipe Gestures",
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


class SliderRow(Adw.ActionRow):
    def __init__(self, title, subtitle, lower, upper, step, digits=2):
        super().__init__(title=title, subtitle=subtitle)
        self.adjustment = Gtk.Adjustment(
            value=lower,
            lower=lower,
            upper=upper,
            step_increment=step,
            page_increment=step * 4,
        )
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_hexpand(True)

        self.scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.adjustment)
        self.scale.set_hexpand(True)
        self.scale.set_digits(digits)
        self.scale.set_draw_value(True)
        self.scale.set_value_pos(Gtk.PositionType.RIGHT)
        box.append(self.scale)

        self.add_suffix(box)
        self.set_activatable(False)

    def get_value(self):
        return self.scale.get_value()

    def set_value(self, value):
        self.scale.set_value(float(value))


class DashyConfigWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Dashy Config")
        self.set_default_size(520, 520)

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
            "Lyrics Size",
            "Scales the lyrics column without changing layout structure.",
            0.8,
            1.5,
            0.02,
        )
        display_group.add(self.lyrics_row)

        self.album_row = SliderRow(
            "Album Art Size",
            "Adjusts the artwork panel size within the existing responsive layout.",
            0.85,
            1.25,
            0.01,
        )
        display_group.add(self.album_row)

        self.highlight_row = SliderRow(
            "Highlight Scale",
            "Controls how much larger the active lyric appears.",
            1.0,
            1.12,
            0.005,
            digits=3,
        )
        display_group.add(self.highlight_row)

        controls_group = Adw.PreferencesGroup(
            title="Controls",
            description="Choose between on-screen transport buttons or swipe interactions on the album art.",
        )
        page.add(controls_group)

        self.control_mode_row = Adw.ComboRow(title="Control Mode")
        self.control_mode_row.set_subtitle("Swipe mode enables tap-to-pause and left/right track gestures on the album art.")
        self.control_mode_model = Gtk.StringList.new([CONTROL_MODE_LABELS[mode] for mode in CONTROL_MODES])
        self.control_mode_row.set_model(self.control_mode_model)
        controls_group.add(self.control_mode_row)

        self.swipe_start_row = SliderRow(
            "Swipe Start Threshold",
            "How far you need to drag before Dashy treats it as a swipe.",
            2.0,
            24.0,
            1.0,
            digits=0,
        )
        controls_group.add(self.swipe_start_row)

        self.swipe_commit_row = SliderRow(
            "Swipe Commit Threshold",
            "How far you need to drag before the previous/next action triggers on release.",
            8.0,
            72.0,
            2.0,
            digits=0,
        )
        controls_group.add(self.swipe_commit_row)

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

    def show_toast(self, message):
        self.toast_overlay.add_toast(Adw.Toast.new(message))

    def read_ui(self):
        selected = self.control_mode_row.get_selected()
        control_mode = CONTROL_MODES[selected] if 0 <= selected < len(CONTROL_MODES) else DEFAULTS["control_mode"]
        return {
            "lyrics_font_scale": self.lyrics_row.get_value(),
            "album_art_scale": self.album_row.get_value(),
            "active_lyric_scale": self.highlight_row.get_value(),
            "control_mode": control_mode,
            "swipe_start_threshold": self.swipe_start_row.get_value(),
            "swipe_commit_threshold": self.swipe_commit_row.get_value(),
        }

    def write_ui(self, config):
        self.lyrics_row.set_value(config.get("lyrics_font_scale", DEFAULTS["lyrics_font_scale"]))
        self.album_row.set_value(config.get("album_art_scale", DEFAULTS["album_art_scale"]))
        self.highlight_row.set_value(config.get("active_lyric_scale", DEFAULTS["active_lyric_scale"]))
        self.swipe_start_row.set_value(config.get("swipe_start_threshold", DEFAULTS["swipe_start_threshold"]))
        self.swipe_commit_row.set_value(config.get("swipe_commit_threshold", DEFAULTS["swipe_commit_threshold"]))
        control_mode = config.get("control_mode", DEFAULTS["control_mode"])
        if control_mode not in CONTROL_MODES:
            control_mode = DEFAULTS["control_mode"]
        self.control_mode_row.set_selected(CONTROL_MODES.index(control_mode))

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


class DashyConfigApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="local.dashy.Config")

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
