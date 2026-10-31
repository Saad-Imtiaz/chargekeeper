"""GTK 4 / libadwaita desktop app. Runs as the normal user; changes go through pkexec."""

import json
import math
import os
import platform
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import APP_ID, VERSION, core  # noqa: E402
from .cli import fmt_hours, helper_command  # noqa: E402

GREEN = (0.15, 0.73, 0.42)
AMBER = (0.90, 0.62, 0.05)
RED = (0.88, 0.16, 0.18)
BLUE = (0.21, 0.52, 0.89)
PURPLE = (0.57, 0.38, 0.85)

PREFS_PATH = os.path.join(GLib.get_user_config_dir(), "chargekeeper", "prefs.json")
AUTOSTART_PATH = os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")
DEFAULT_PREFS = {
    "background": True,
    "notify_unplug": True,
    "notify_limit": False,
    "notify_full": True,
    "low_battery": 15,
    "high_temp": 45,
    "reminder_level": 85,
}

BEHAVIOUR_NAMES = {
    "auto": "Normal",
    "inhibit-charge": "Pause charging, run on adapter",
    "force-discharge": "Run on battery while plugged in",
}
PRESETS = (60, 70, 80, 85, 90)
RANGES = (("6 hours", 6 * 3600), ("24 hours", 24 * 3600), ("7 days", 7 * 86400))


def load_prefs():
    prefs = dict(DEFAULT_PREFS)
    try:
        with open(PREFS_PATH) as f:
            prefs.update(json.load(f))
    except (OSError, ValueError):
        pass
    return prefs


def save_prefs(prefs):
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f, indent=2)


def sync_autostart(enabled):
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
        with open(AUTOSTART_PATH, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Chargekeeper\n"
                    "Exec=chargekeeper --background\nIcon=" + APP_ID + "\n"
                    "X-GNOME-Autostart-enabled=true\nNoDisplay=true\n")
    elif os.path.exists(AUTOSTART_PATH):
        os.remove(AUTOSTART_PATH)


def level_color(pct):
    if pct is None:
        return BLUE
    if pct < 20:
        return RED
    if pct < 40:
        return AMBER
    return GREEN


class Painter:
    """Thin wrapper over Gtk.Snapshot + GSK paths (GTK 4.14+, no cairo needed)."""

    def __init__(self, widget, snapshot):
        self.widget, self.snap = widget, snapshot
        self.fg = widget.get_color()

    def rgba(self, color, alpha=1.0):
        rgba = Gdk.RGBA()
        if color is None:
            rgba.red, rgba.green, rgba.blue, rgba.alpha = (self.fg.red, self.fg.green, self.fg.blue,
                                                           self.fg.alpha * alpha)
        else:
            rgba.red, rgba.green, rgba.blue, rgba.alpha = (*color, alpha)
        return rgba

    def stroke(self, points, width, color, alpha=1.0, round_cap=False, dash=None):
        if len(points) < 2:
            return
        builder = Gsk.PathBuilder.new()
        builder.move_to(*points[0])
        for p in points[1:]:
            builder.line_to(*p)
        stroke = Gsk.Stroke.new(width)
        stroke.set_line_join(Gsk.LineJoin.ROUND)
        if round_cap:
            stroke.set_line_cap(Gsk.LineCap.ROUND)
        if dash:
            stroke.set_dash(dash)
        self.snap.append_stroke(builder.to_path(), stroke, self.rgba(color, alpha))

    def fill(self, points, color, alpha):
        builder = Gsk.PathBuilder.new()
        builder.move_to(*points[0])
        for p in points[1:]:
            builder.line_to(*p)
        builder.close()
        self.snap.append_fill(builder.to_path(), Gsk.FillRule.WINDING, self.rgba(color, alpha))

    def rect(self, x, y, w, h, color, alpha):
        self.snap.append_color(self.rgba(color, alpha), Graphene.Rect().init(x, y, w, h))

    def layout(self, text, size, bold=False):
        layout = self.widget.create_pango_layout(text)
        layout.set_font_description(Pango.FontDescription.from_string(f"{'Bold ' if bold else ''}{size}"))
        return layout, layout.get_pixel_size()

    def text(self, layout, x, y, alpha=1.0):
        self.snap.save()
        self.snap.translate(Graphene.Point().init(x, y))
        self.snap.append_layout(layout, self.rgba(None, alpha))
        self.snap.restore()


def arc_points(cx, cy, r, start, sweep, steps=96):
    n = max(2, int(steps * abs(sweep) / (2 * math.pi)) + 1)
    return [(cx + r * math.cos(start + sweep * i / (n - 1)), cy + r * math.sin(start + sweep * i / (n - 1)))
            for i in range(n)]


# --- drawing widgets -------------------------------------------------------

class Ring(Gtk.Widget):
    """Gauge arc with a centered value and an optional limit tick."""

    def __init__(self, size):
        super().__init__(halign=Gtk.Align.CENTER)
        self.set_size_request(size, size)
        self.value = None
        self.text = "—"
        self.caption = ""
        self.color = GREEN
        self.marker = None

    def update(self, value, text, caption, color, marker=None):
        self.value, self.text, self.caption, self.color, self.marker = value, text, caption, color, marker
        self.queue_draw()

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        p = Painter(self, snapshot)
        cx, cy = w / 2, h / 2
        width = max(10, min(w, h) * 0.07)
        r = min(w, h) / 2 - width
        start, sweep = 0.75 * math.pi, 1.5 * math.pi

        p.stroke(arc_points(cx, cy, r, start, sweep), width, None, 0.12, round_cap=True)
        if self.value:
            frac = max(0.0, min(1.0, self.value / 100))
            p.stroke(arc_points(cx, cy, r, start, sweep * frac), width, self.color, round_cap=True)
        if self.marker is not None and self.marker < 100:
            angle = start + sweep * self.marker / 100
            inner, outer = r - width * 0.9, r + width * 0.9
            p.stroke([(cx + inner * math.cos(angle), cy + inner * math.sin(angle)),
                      (cx + outer * math.cos(angle), cy + outer * math.sin(angle))], 3, None, 0.85)

        layout, (tw, th) = p.layout(self.text, max(14, int(min(w, h) * 0.15)), bold=True)
        p.text(layout, cx - tw / 2, cy - th / 2 - 6)
        if self.caption:
            layout, (cw, _ch) = p.layout(self.caption, 10)
            p.text(layout, cx - cw / 2, cy + th / 2 - 4, alpha=0.65)


class Chart(Gtk.Widget):
    """Time series line chart with an optional dashed reference line and AC strip."""

    def __init__(self, color, unit, fixed_max=None, height=150):
        super().__init__(hexpand=True, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
        self.set_size_request(200, height)
        self.color, self.unit, self.fixed_max = color, unit, fixed_max
        self.points, self.t0, self.t1 = [], 0, 1
        self.reference = None
        self.strip = []
        self.empty_text = "Collecting data…"

    def set_data(self, points, t0, t1, reference=None, strip=None):
        self.points, self.t0, self.t1 = points, t0, t1
        self.reference = reference
        self.strip = strip or []
        self.queue_draw()

    def _ymax(self):
        if self.fixed_max:
            return max(self.fixed_max, max((v for _, v in self.points), default=0))
        peak = max((v for _, v in self.points), default=0)
        step = 5 if peak <= 20 else 10 if peak <= 60 else 25
        return max(step, math.ceil(peak * 1.1 / step) * step)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        p = Painter(self, snapshot)
        left, right, top, bottom = 44, 6, 6, 22
        pw, ph = w - left - right, h - top - bottom
        if pw <= 10 or ph <= 10:
            return
        ymax = self._ymax()
        span = max(self.t1 - self.t0, 1)

        def x_of(t):
            return left + pw * (t - self.t0) / span

        def y_of(v):
            return top + ph * (1 - min(v, ymax) / ymax)

        for frac in (0, 0.5, 1):
            y = round(top + ph * (1 - frac)) + 0.5
            p.stroke([(left, y), (left + pw, y)], 1, None, 0.10)
            layout, (tw, th) = p.layout(f"{ymax * frac:.0f}{self.unit}", 8)
            p.text(layout, left - tw - 6, y - th / 2, alpha=0.6)

        fmt = "%H:%M" if span <= 86400 else "%a %d"
        for frac in (0, 0.5, 1):
            label = time.strftime(fmt, time.localtime(self.t0 + span * frac))
            layout, (tw, _th) = p.layout(label, 8)
            p.text(layout, left + pw * frac - tw * frac, top + ph + 6, alpha=0.6)

        for t_start, t_end in self.strip:
            p.rect(x_of(t_start), top + ph - 3, max(1, x_of(t_end) - x_of(t_start)), 3, GREEN, 0.45)

        if self.reference is not None and self.reference < ymax:
            y = round(y_of(self.reference)) + 0.5
            p.stroke([(left, y), (left + pw, y)], 1, None, 0.45, dash=[4, 4])

        if len(self.points) < 2:
            layout, (tw, th) = p.layout(self.empty_text, 10)
            p.text(layout, left + pw / 2 - tw / 2, top + ph / 2 - th / 2, alpha=0.5)
            return

        gap = max(600, span / 80)
        segments, current, last_t = [], [], None
        for t, v in self.points:
            if last_t is not None and t - last_t > gap:
                segments.append(current)
                current = []
            current.append((x_of(t), y_of(v)))
            last_t = t
        segments.append(current)

        for seg in segments:
            if len(seg) < 2:
                continue
            p.fill(seg + [(seg[-1][0], top + ph), (seg[0][0], top + ph)], self.color, 0.14)
            p.stroke(seg, 2, self.color)


# --- small builders --------------------------------------------------------

def info_row(group, title, icon=None):
    row = Adw.ActionRow(title=title)
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon))
    label = Gtk.Label(css_classes=["dim-label", "numeric"], xalign=1,
                      ellipsize=Pango.EllipsizeMode.END, max_width_chars=40)
    row.add_suffix(label)
    group.add(row)
    return label


def set_combo(row, values, current, names=None):
    names = names or {}
    labels = [names.get(v, v.replace("_", " ").replace("-", " ").capitalize()) for v in values]
    model = row.get_model()
    if model is None or [model.get_string(i) for i in range(model.get_n_items())] != labels:
        row.set_model(Gtk.StringList.new(labels))
    row.values = values
    if current in values:
        row.set_selected(values.index(current))


def combo_value(row):
    index = row.get_selected()
    values = getattr(row, "values", [])
    return values[index] if 0 <= index < len(values) else None


# --- window ----------------------------------------------------------------

class Window(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Chargekeeper", default_width=680, default_height=860,
                         width_request=360, height_request=420)
        self.app = app
        self.syncing = False
        self.pending = {}
        self.pending_source = 0
        self.backend = None
        self.history_range = RANGES[1][1]
        self.last_chart = 0

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("About Chargekeeper", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        view.add_top_bar(header)
        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)

        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bottom)
        breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 520sp"))
        breakpoint.add_setter(bottom, "reveal", True)
        breakpoint.add_setter(switcher, "visible", False)
        self.add_breakpoint(breakpoint)

        self._build_overview()
        self._build_charging()
        self._build_health()
        self._build_system()
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.refresh()
        self.refresh_charts()
        self.timer = GLib.timeout_add_seconds(3, self._tick)
        self.connect("close-request", self._on_close)

    def _on_close(self, *_):
        GLib.source_remove(self.timer)
        self.app.window = None
        return False

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _tick(self):
        self.refresh()
        if time.time() - self.last_chart > 60:
            self.refresh_charts()
        return GLib.SOURCE_CONTINUE

    # --- pages ---

    def _build_overview(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "overview", "Overview", "battery-good-symbolic")

        hero = Adw.PreferencesGroup()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6)
        self.ring = Ring(230)
        self.status_title = Gtk.Label(css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER)
        self.status_sub = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        for widget in (self.ring, self.status_title, self.status_sub):
            box.append(widget)
        hero.add(box)
        page.add(hero)

        now = Adw.PreferencesGroup(title="Right now")
        self.limit_row = Adw.ActionRow(title="Charge limit", activatable=True)
        self.limit_row.add_prefix(Gtk.Image(icon_name="battery-full-charging-symbolic"))
        self.limit_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.limit_row.connect("activated", lambda *_: self.stack.set_visible_child_name("charging"))
        now.add(self.limit_row)
        self.v_power = info_row(now, "Power", "power-profile-balanced-symbolic")
        self.v_time = info_row(now, "Time", "preferences-system-time-symbolic")
        self.v_energy = info_row(now, "Energy", "battery-level-70-symbolic")
        self.v_temp = info_row(now, "Battery temperature", "temperature-symbolic")
        page.add(now)

        history = Adw.PreferencesGroup(title="History")
        picker = Gtk.DropDown.new_from_strings([label for label, _ in RANGES])
        picker.set_selected(1)
        picker.set_valign(Gtk.Align.CENTER)
        picker.connect("notify::selected", self._on_range)
        history.set_header_suffix(picker)
        self.chart_level = Chart(GREEN, "%", fixed_max=100)
        self.chart_power = Chart(PURPLE, " W", height=120)
        for title, chart in (("Charge level", self.chart_level), ("Power draw", self.chart_power)):
            row = Adw.PreferencesRow(activatable=False)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            inner.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading"],
                                   margin_top=10, margin_start=12))
            inner.append(chart)
            row.set_child(inner)
            history.add(row)
        page.add(history)

    def _build_charging(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "charging", "Charging", "battery-full-charging-symbolic")

        control = Adw.PreferencesGroup()
        self.backend_row = Adw.ActionRow()
        self.backend_icon = Gtk.Image(icon_name="emblem-ok-symbolic")
        self.backend_row.add_prefix(self.backend_icon)
        control.add(self.backend_row)
        page.add(control)

        self.hint_group = Adw.PreferencesGroup(title="Needs attention")
        page.add(self.hint_group)
        self.hint_rows = []

        self.limit_group = Adw.PreferencesGroup(title="Charge limit")
        self.limit_switch = Adw.SwitchRow(
            title="Limit charging",
            subtitle="Stopping before 100% keeps the cells at a lower voltage and slows wear")
        self.limit_switch.connect("notify::active", self._on_limit_switch)
        self.limit_group.add(self.limit_switch)

        self.scale_row = Adw.PreferencesRow(activatable=False)
        scale_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6,
                            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12)
        head = Gtk.Box(spacing=6)
        self.scale_title = Gtk.Label(label="Stop charging at", xalign=0, hexpand=True)
        self.scale_value = Gtk.Label(css_classes=["title-3", "numeric"])
        head.append(self.scale_title)
        head.append(self.scale_value)
        scale_box.append(head)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 40, 100, 1)
        self.scale.set_draw_value(False)
        self.scale.set_round_digits(0)
        for mark in (50, 60, 70, 80, 90, 100):
            self.scale.add_mark(mark, Gtk.PositionType.BOTTOM, f"{mark}")
        self.scale.connect("value-changed", self._on_scale)
        scale_box.append(self.scale)
        presets = Gtk.Box(css_classes=["linked"], halign=Gtk.Align.CENTER, margin_top=4)
        for value in PRESETS:
            button = Gtk.Button(label=f"{value}%")
            button.connect("clicked", lambda _b, v=value: self.scale.set_value(v))
            presets.append(button)
        scale_box.append(presets)
        self.scale_row.set_child(scale_box)
        self.limit_group.add(self.scale_row)

        self.fixed_row = Adw.ActionRow(title="Limit")
        self.fixed_label = Gtk.Label(css_classes=["title-3", "numeric"])
        self.fixed_row.add_suffix(self.fixed_label)
        self.limit_group.add(self.fixed_row)

        self.start_row = Adw.SpinRow.new_with_range(10, 99, 1)
        self.start_row.set_title("Resume charging below")
        self.start_row.set_subtitle("A gap stops the charger from topping up constantly")
        self.start_row.connect("notify::value", self._on_start)
        self.limit_group.add(self.start_row)
        page.add(self.limit_group)

        self.topup_group = Adw.PreferencesGroup()
        self.topup_row = Adw.SwitchRow(
            title="Charge to 100% once",
            subtitle="For a long day away from a charger. The limit comes back when you unplug.")
        self.topup_row.add_prefix(Gtk.Image(icon_name="airplane-mode-symbolic"))
        self.topup_row.connect("notify::active", self._on_topup)
        self.topup_group.add(self.topup_row)
        page.add(self.topup_group)

        self.adv_group = Adw.PreferencesGroup(title="Advanced")
        self.behaviour_row = Adw.ComboRow(title="While plugged in",
                                          subtitle="Pause charging or drain the battery on the adapter")
        self.behaviour_row.connect("notify::selected", self._on_behaviour)
        self.type_row = Adw.ComboRow(title="Charging mode", subtitle="Firmware charging algorithm")
        self.type_row.connect("notify::selected", self._on_charge_type)
        self.adv_group.add(self.behaviour_row)
        self.adv_group.add(self.type_row)
        page.add(self.adv_group)

    def _build_health(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "health", "Health", "emblem-favorite-symbolic")

        hero = Adw.PreferencesGroup()
        self.health_ring = Ring(180)
        self.health_ring.set_margin_top(6)
        hero.add(self.health_ring)
        self.health_note = Gtk.Label(css_classes=["dim-label"], wrap=True,
                                     justify=Gtk.Justification.CENTER, margin_top=4)
        hero.add(self.health_note)
        page.add(hero)

        cap = Adw.PreferencesGroup(title="Capacity")
        self.v_full = info_row(cap, "Full charge capacity")
        self.v_design = info_row(cap, "Design capacity")
        self.v_wear = info_row(cap, "Wear")
        self.v_cycles = info_row(cap, "Charge cycles")
        page.add(cap)

        trend = Adw.PreferencesGroup(title="Health over time",
                                     description="One sample per day, recorded by the background service.")
        self.chart_health = Chart(GREEN, "%", fixed_max=100, height=130)
        self.chart_health.empty_text = "Needs a few days of data"
        row = Adw.PreferencesRow(activatable=False)
        row.set_child(self.chart_health)
        trend.add(row)
        page.add(trend)

        self.calib_group = Adw.PreferencesGroup(
            title="Calibration",
            description="Charges to 100%, drains to empty and recharges once, so the reported "
                        "capacity is accurate again. Keep the charger plugged in and let it run; "
                        "it takes several hours. Every 2–3 months is plenty.")
        self.calib_row = Adw.SwitchRow(title="Calibrate battery")
        self.calib_row.connect("notify::active", self._on_calibration)
        self.calib_group.add(self.calib_row)
        page.add(self.calib_group)

        about = Adw.PreferencesGroup(title="Battery")
        self.v_model = info_row(about, "Model")
        self.v_maker = info_row(about, "Manufacturer")
        self.v_tech = info_row(about, "Chemistry")
        self.v_voltage = info_row(about, "Voltage")
        page.add(about)

        tips = Adw.PreferencesGroup(title="What wears a battery")
        for title, body in (
            ("Sitting at 100%", "High charge means high cell voltage. A limit of 80–85% roughly "
                                "doubles the cycle life of most lithium-ion packs."),
            ("Heat", "Above about 40 °C wear speeds up. Gaming while charging is the worst case, "
                     "so keep the vents clear."),
            ("Deep discharges", "Running down to 0% often is harder on the cells than shallow cycles. "
                                "Charge when convenient."),
        ):
            tip = Adw.ActionRow(title=title, subtitle=body, subtitle_lines=0)
            tips.add(tip)
        page.add(tips)

    def _build_system(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "system", "System", "preferences-system-symbolic")

        power = Adw.PreferencesGroup(title="Performance")
        self.ppd_row = Adw.ComboRow(title="Power mode")
        self.ppd_row.add_prefix(Gtk.Image(icon_name="power-profile-balanced-symbolic"))
        self.ppd_row.connect("notify::selected", self._on_ppd)
        self.profile_row = Adw.ComboRow(title="Firmware performance profile",
                                        subtitle="Fan and power limits set by the laptop firmware")
        self.profile_row.connect("notify::selected", self._on_profile)
        self.epp_row = Adw.ComboRow(title="CPU energy preference",
                                    subtitle="Lasts until reboot or until TLP changes it")
        self.epp_row.connect("notify::selected", self._on_epp)
        self.tlp_row = Adw.ActionRow(title="TLP is managing power",
                                     subtitle="TLP switches CPU and device settings when you plug in "
                                              "or unplug. Chargekeeper only handles the battery.")
        self.tlp_row.add_prefix(Gtk.Image(icon_name="emblem-system-symbolic"))
        for row in (self.ppd_row, self.profile_row, self.epp_row, self.tlp_row):
            power.add(row)
        self.v_cpu = info_row(power, "CPU", "computer-symbolic")
        page.add(power)

        notify = Adw.PreferencesGroup(title="Notifications")
        prefs = self.app.prefs
        self.bg_row = Adw.SwitchRow(title="Run in background",
                                    subtitle="Start at login and send notifications without a window",
                                    active=prefs["background"])
        self.bg_row.connect("notify::active", lambda r, _p: self.app.set_background(r.get_active()))
        notify.add(self.bg_row)
        for key, title, subtitle in (
            ("notify_unplug", "Unplug reminder",
             "When this laptop can't limit charging itself, remind me at the limit"),
            ("notify_limit", "Limit reached", "Tell me when charging pauses at the limit"),
            ("notify_full", "Top-up finished", "Tell me when a charge to 100% is done"),
        ):
            row = Adw.SwitchRow(title=title, subtitle=subtitle, active=prefs[key])
            row.connect("notify::active", lambda r, _p, k=key: self.app.set_pref(k, r.get_active()))
            notify.add(row)
        for key, title, low, high in (("low_battery", "Low battery warning at (%)", 5, 50),
                                      ("high_temp", "Battery too hot at (°C)", 35, 70)):
            row = Adw.SpinRow.new_with_range(low, high, 1)
            row.set_title(title)
            row.set_value(prefs[key])
            row.connect("notify::value", lambda r, _p, k=key: self.app.set_pref(k, int(r.get_value())))
            notify.add(row)
        page.add(notify)

        diag = Adw.PreferencesGroup(title="Diagnostics")
        copy = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER,
                          tooltip_text="Copy report", css_classes=["flat"])
        copy.connect("clicked", self._copy_report)
        diag.set_header_suffix(copy)
        self.v_laptop = info_row(diag, "Laptop")
        self.v_kernel = info_row(diag, "Kernel")
        self.v_backend = info_row(diag, "Charge control")
        self.v_service = info_row(diag, "Background service")
        page.add(diag)

    # --- refresh ---

    def refresh(self):
        info = core.combined_info()
        self.backend = backend = core.detect()
        desc = backend.describe()
        config = core.load_config()
        state = core.daemon_state()
        limit = backend.limit() if backend.available else None
        effective = limit if limit is not None else 100

        if backend.available and not (state and state["alive"]):
            self.banner.set_title("Background service is not running, so limits won't be re-applied "
                                  "after reboot or resume. Run install.sh.")
            self.banner.set_revealed(True)
        elif state and state.get("error"):
            self.banner.set_title(f"Service error: {state['error']}")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

        self._refresh_overview(info, backend, config, effective)
        if not self.pending:
            self._refresh_charging(backend, desc, config, limit)
        self._refresh_health(info, backend, config)
        self._refresh_system(backend, state)

    def _refresh_overview(self, info, backend, config, limit):
        if not info:
            self.ring.update(None, "—", "", BLUE)
            self.status_title.set_label("No battery found")
            self.status_sub.set_label("This computer doesn't report a system battery.")
            return
        cap = info["capacity"]
        status = info["status"]
        power = info["power"]
        remaining = core.time_remaining(info, limit)
        marker = limit if limit < 100 else None
        if not backend.available and self.app.prefs["notify_unplug"]:
            marker = self.app.prefs["reminder_level"]

        if config["calibrating"]:
            title, sub = "Calibrating", "Keep the charger connected until calibration finishes."
        elif status == "Charging":
            title = "Charging" + (f" · {power:.0f} W" if power else "")
            target = 100 if config["top_up"] else limit
            sub = f"{fmt_hours(remaining)} until {target}%" if remaining else f"Charging to {target}%"
        elif status == "Discharging":
            title = "On battery" + (f" · {power:.1f} W" if power else "")
            sub = f"About {fmt_hours(remaining)} left at this rate" if remaining else ""
        elif info["ac"]:
            if limit < 100 and cap is not None and cap > limit + 2:
                title = "Plugged in · above the limit"
                sub = (f"Not charging. The battery holds {cap}% and will stay at {limit}% once "
                       "it drains below that.")
            elif limit < 100:
                title = f"Plugged in · held at {cap}%"
                sub = "Running on adapter power while the battery rests."
            else:
                title, sub = "Plugged in · fully charged", ""
        else:
            title, sub = status, ""
        self.ring.update(cap, f"{cap}%" if cap is not None else "—",
                         "plugged in" if info["ac"] else "on battery", level_color(cap), marker)
        self.status_title.set_label(title)
        self.status_sub.set_label(sub)
        self.status_sub.set_visible(bool(sub))

        if not backend.available:
            self.limit_row.set_subtitle("Not supported by this laptop's firmware")
        elif config["calibrating"]:
            self.limit_row.set_subtitle("Paused while calibrating")
        elif config["top_up"]:
            self.limit_row.set_subtitle("Off until unplugged (charging to 100%)")
        elif limit < 100:
            self.limit_row.set_subtitle(f"{limit}% · {backend.name}")
        else:
            self.limit_row.set_subtitle("Off: charges to 100%")

        self.v_power.set_label(f"{power:.1f} W" if power is not None else "—")
        if remaining is not None:
            self.v_time.set_label(fmt_hours(remaining) + (" left" if status == "Discharging" else " to target"))
        else:
            self.v_time.set_label("—")
        if info["energy_now"] is not None and info["energy_full"]:
            self.v_energy.set_label(f"{info['energy_now']:.1f} of {info['energy_full']:.1f} Wh")
        else:
            self.v_energy.set_label("—")
        temp = info["temperature"]
        self.v_temp.set_label(f"{temp:.1f} °C" if temp is not None else "Not reported")

    def _refresh_charging(self, backend, desc, config, limit):
        self.syncing = True
        try:
            self.backend_row.set_title(backend.name)
            self.backend_row.set_subtitle(backend.detail)
            self.backend_icon.set_from_icon_name(
                "emblem-ok-symbolic" if backend.available else "dialog-warning-symbolic")

            hints = core.hints()
            for row in self.hint_rows:
                self.hint_group.remove(row)
            self.hint_rows = []
            for hint in hints:
                row = Adw.ActionRow(title=hint["title"], subtitle=hint["body"], subtitle_lines=0)
                row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
                self.hint_group.add(row)
                self.hint_rows.append(row)
            self.hint_group.set_visible(bool(hints))

            available = backend.available
            enabled = config["limit_enabled"]
            self.limit_switch.set_visible(available)
            self.limit_switch.set_active(enabled)
            self.topup_group.set_visible(available)
            self.topup_row.set_active(config["top_up"])
            self.limit_group.set_sensitive(not config["calibrating"])

            if not available:
                # No firmware control: the slider sets the unplug reminder instead.
                self.limit_group.set_title("Unplug reminder")
                self.limit_group.set_description(
                    "This laptop can't stop charging by itself, so Chargekeeper will notify you "
                    "to unplug at this level.")
                self.scale_title.set_label("Remind me at")
                self.scale_row.set_visible(True)
                self.scale.set_value(self.app.prefs["reminder_level"])
                self.fixed_row.set_visible(False)
                self.start_row.set_visible(False)
            elif desc["continuous"]:
                self.limit_group.set_title("Charge limit")
                self.limit_group.set_description(
                    "Enforced by software while the service runs." if desc["software"] else None)
                self.scale_title.set_label("Stop charging at")
                self.scale_row.set_visible(True)
                self.scale_row.set_sensitive(enabled)
                self.scale.set_range(max(40, desc["min_end"]), 100)
                self.scale.set_value(config["end"])
                self.fixed_row.set_visible(False)
                self.start_row.set_visible(desc["supports_start"])
                self.start_row.set_sensitive(enabled)
                self.start_row.set_range(10, max(11, config["end"] - 1))
                self.start_row.set_value(config["start"] if config["start"] is not None
                                         else config["end"] - 5)
            else:
                self.limit_group.set_title("Charge limit")
                fixed = [s for s in desc["steps"] if s < 100]
                self.limit_group.set_description(
                    "This laptop's firmware supports only "
                    + " or ".join(f"{s}%" for s in fixed)
                    + ", not a custom value. For lithium-ion cells 80% is close to ideal.")
                self.scale_row.set_visible(False)
                self.fixed_row.set_visible(True)
                self.fixed_row.set_sensitive(enabled)
                self.fixed_row.set_subtitle("Set by the firmware")
                self.fixed_label.set_label(f"{backend.snap(config['end'] if enabled else fixed[-1])}%")
                self.start_row.set_visible(False)
            self.scale_value.set_label(f"{int(self.scale.get_value())}%")

            behaviours = desc["behaviours"] if not desc["software"] else []
            self.behaviour_row.set_visible(len(behaviours) > 1)
            if behaviours:
                set_combo(self.behaviour_row, behaviours, config["behaviour"], BEHAVIOUR_NAMES)
            types = desc["charge_types"]
            self.type_row.set_visible(bool(types))
            if types:
                set_combo(self.type_row, types, config["charge_type"] or backend.charge_type())
            self.adv_group.set_visible(len(behaviours) > 1 or bool(types))

            self.calib_group.set_visible(desc["calibration"])
            if desc["calibration"]:
                self.calib_row.set_active(backend.calibrating() or config["calibrating"])
        finally:
            self.syncing = False

    def _refresh_health(self, info, backend, config):
        if not info or info["health"] is None:
            self.health_ring.update(None, "—", "health", BLUE)
            self.health_note.set_label("The battery doesn't report its capacity.")
            return
        health = info["health"]
        color = GREEN if health >= 80 else AMBER if health >= 60 else RED
        self.health_ring.update(min(health, 100), f"{health:.0f}%", "health", color)
        if health > 102:
            note = ("The battery reports more than its design capacity. That's normal for a new "
                    "pack or a stale capacity estimate. Calibrating corrects it.")
        elif health >= 80:
            note = "In good shape."
        elif health >= 60:
            note = "Noticeably worn, but still usable."
        else:
            note = "Heavily worn. Consider replacing the battery."
        self.health_note.set_label(note)
        self.v_full.set_label(f"{info['energy_full']:.1f} Wh")
        self.v_design.set_label(f"{info['energy_design']:.1f} Wh")
        self.v_wear.set_label(f"{max(0, 100 - health):.0f}%")
        cycles = info["cycle_count"]
        self.v_cycles.set_label(str(cycles) if cycles else "Not reported by firmware")
        self.v_model.set_label(info["model"] or "—")
        self.v_maker.set_label(info["manufacturer"] or "—")
        self.v_tech.set_label(info["technology"] or "—")
        if info["voltage"]:
            self.v_voltage.set_label(f"{info['voltage']:.2f} V (minimum {info['voltage_design']:.1f} V)")

    def _refresh_system(self, backend, state):
        ppd = self.app.ppd()
        self.syncing = True
        try:
            if ppd:
                profiles, active = ppd
                set_combo(self.ppd_row, profiles, active,
                          {"power-saver": "Power saver", "balanced": "Balanced", "performance": "Performance"})
            self.ppd_row.set_visible(bool(ppd))
            choices, current = core.platform_profiles()
            self.profile_row.set_visible(bool(choices) and not ppd)
            if choices:
                set_combo(self.profile_row, choices, current)
            choices, current = core.epp_choices()
            self.epp_row.set_visible(bool(choices) and not ppd)
            if choices:
                set_combo(self.epp_row, choices, current)
        finally:
            self.syncing = False
        self.tlp_row.set_visible(self.app.tlp_active)

        cpu = core.cpu_info()
        parts = [p for p in (cpu["driver"], cpu["governor"]) if p]
        if cpu["temperature"] is not None:
            parts.append(f"{cpu['temperature']:.0f} °C")
        self.v_cpu.set_label(" · ".join(parts) or "—")

        self.v_laptop.set_label(f"{core.dmi('sys_vendor')} {core.dmi('product_name')}".strip())
        self.v_kernel.set_label(platform.release())
        self.v_backend.set_label(backend.id)
        if state and state["alive"]:
            self.v_service.set_label("Running")
        elif state:
            self.v_service.set_label("Stopped")
        else:
            self.v_service.set_label("Not installed")

    def _on_range(self, dropdown, _pspec):
        self.history_range = RANGES[dropdown.get_selected()][1]
        self.refresh_charts()

    def refresh_charts(self):
        self.last_chart = time.time()
        now = time.time()
        t0 = now - self.history_range
        points = core.load_history(t0)
        step = max(1, len(points) // 600)
        sampled = points[::step]
        strip, run_start, prev = [], None, None
        for p in points:
            if p["ac"] and run_start is None:
                run_start = p["t"]
            if not p["ac"] and run_start is not None:
                strip.append((run_start, prev))
                run_start = None
            prev = p["t"]
        if run_start is not None:
            strip.append((run_start, prev))
        limit = self.backend.limit() if self.backend and self.backend.available else None
        self.chart_level.set_data([(p["t"], p["capacity"]) for p in sampled if p["capacity"] is not None],
                                  t0, now, reference=limit if limit and limit < 100 else None, strip=strip)
        self.chart_power.set_data([(p["t"], p["power"]) for p in sampled if p["power"] is not None], t0, now)

        health = [r for r in core.load_health() if r["health"] is not None]
        h0 = min(health[0]["t"], now - 30 * 86400) if health else now - 30 * 86400
        self.chart_health.set_data([(r["t"], r["health"]) for r in health], h0, now, reference=80)

    # --- changes ---

    def _queue(self, **changes):
        """Collect changes for 0.7 s so dragging the slider asks for authorization once."""
        self.pending.update(changes)
        if self.pending_source:
            GLib.source_remove(self.pending_source)
        self.pending_source = GLib.timeout_add(700, self._flush)

    def _flush(self):
        self.pending_source = 0
        args = []
        for key, value in self.pending.items():
            if isinstance(value, bool):
                value = "on" if value else "off"
            args += [f"--{key}", str(value)]
        self.app.run_helper(["set", *args], self._after_change)
        return GLib.SOURCE_REMOVE

    def _after_change(self, ok):
        self.pending = {}
        self.refresh()
        self.refresh_charts()

    def _on_limit_switch(self, row, _pspec):
        if not self.syncing:
            self.scale_row.set_sensitive(row.get_active())
            self._queue(**{"limit-enabled": row.get_active(), "top-up": False})

    def _on_scale(self, scale, _pspec=None):
        value = int(round(scale.get_value()))
        self.scale_value.set_label(f"{value}%")
        if self.syncing:
            return
        if self.backend and not self.backend.available:
            self.app.set_pref("reminder_level", value)
            return
        self.start_row.set_range(10, max(11, value - 1))
        self._queue(end=value)

    def _on_start(self, row, _pspec):
        if not self.syncing:
            self._queue(start=int(row.get_value()))

    def _on_topup(self, row, _pspec):
        if not self.syncing:
            self._queue(**{"top-up": row.get_active()})

    def _on_behaviour(self, row, _pspec):
        value = combo_value(row)
        if not self.syncing and value:
            self._queue(behaviour=value)

    def _on_charge_type(self, row, _pspec):
        value = combo_value(row)
        if not self.syncing and value:
            self._queue(**{"charge-type": value})

    def _on_calibration(self, row, _pspec):
        if not self.syncing:
            self._queue(calibration=row.get_active())

    def _on_ppd(self, row, _pspec):
        value = combo_value(row)
        if not self.syncing and value:
            self.app.set_ppd(value)

    def _on_profile(self, row, _pspec):
        value = combo_value(row)
        if not self.syncing and value:
            self.app.run_helper(["platform-profile", value], lambda ok: self.refresh())

    def _on_epp(self, row, _pspec):
        value = combo_value(row)
        if not self.syncing and value:
            self.app.run_helper(["epp", value], lambda ok: self.refresh())

    def _copy_report(self, _button):
        info = core.combined_info() or {}
        backend = core.detect()
        lines = [
            f"Chargekeeper {VERSION}",
            f"Laptop: {core.dmi('sys_vendor')} {core.dmi('product_name')} ({core.dmi('product_version')})",
            f"BIOS: {core.dmi('bios_version')}",
            f"Kernel: {platform.release()}",
            f"Backend: {json.dumps(backend.describe())}",
            f"Limit: {backend.limit()}  start: {backend.start()}",
            f"Config: {json.dumps(core.load_config())}",
            f"Service: {json.dumps(core.daemon_state())}",
            f"Hints: {[h['id'] for h in core.hints()]}",
        ]
        for bat in info.get("batteries", []):
            lines.append(f"Battery: {json.dumps(bat)}")
        Gdk.Display.get_default().get_clipboard().set("\n".join(lines))
        self.toast("Report copied")


# --- application -----------------------------------------------------------

class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.add_main_option("background", 0, GLib.OptionFlags.NONE, GLib.OptionArg.NONE,
                             "Start without a window and only send notifications", None)
        self.connect("handle-local-options", self._local_options)
        self.prefs = load_prefs()
        self.window = None
        self.start_hidden = False
        self.held = False
        self.tlp_active = False
        self.ppd_proxy = None
        self.notified = {}

    def _local_options(self, _app, options):
        if options.contains("background"):
            self.start_hidden = True
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        for name, callback, accels in (("about", self._about, []),
                                       ("quit", lambda *_: self.quit(), ["<primary>q"]),
                                       ("close", self._hide, ["<primary>w"])):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            if accels:
                self.set_accels_for_action(f"app.{name}", accels)
        self.tlp_active = core.service_active("tlp")
        try:
            self.ppd_proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SYSTEM, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
                "net.hadess.PowerProfiles", "/net/hadess/PowerProfiles", "net.hadess.PowerProfiles", None)
        except GLib.Error:
            self.ppd_proxy = None
        sync_autostart(self.prefs["background"])
        self.set_background(self.prefs["background"])
        GLib.timeout_add_seconds(30, self._watch)
        GLib.idle_add(self._watch)

    def do_activate(self):
        if self.start_hidden:
            self.start_hidden = False
            if not self.held:
                self.hold()
                self.held = True
            return
        if self.window is None:
            self.window = Window(self)
        self.window.present()

    def _hide(self, *_):
        if self.window:
            self.window.close()
        if not self.prefs["background"]:
            self.quit()

    def set_pref(self, key, value):
        self.prefs[key] = value
        save_prefs(self.prefs)

    def set_background(self, enabled):
        self.set_pref("background", enabled)
        sync_autostart(enabled)
        if enabled and not self.held:
            self.hold()
            self.held = True
        elif not enabled and self.held:
            self.release()
            self.held = False

    def _about(self, *_):
        dialog = Adw.AboutDialog(
            application_name="Chargekeeper", application_icon=APP_ID, version=VERSION,
            developer_name="Saad Imtiaz", license_type=Gtk.License.GPL_3_0,
            comments="Battery charge limits, health tracking and power tools for Linux laptops.")
        dialog.present(self.window)

    # --- privileged changes ---

    def run_helper(self, args, done=None):
        try:
            proc = Gio.Subprocess.new(helper_command(*args),
                                      Gio.SubprocessFlags.STDERR_PIPE | Gio.SubprocessFlags.STDOUT_SILENCE)
        except GLib.Error as exc:
            self._report(exc.message)
            if done:
                done(False)
            return
        proc.communicate_utf8_async(None, None, self._helper_done, done)

    def _helper_done(self, proc, result, done):
        err = ""
        try:
            _ok, _out, err = proc.communicate_utf8_finish(result)
        except GLib.Error as exc:
            err = exc.message
        status = proc.get_exit_status() if proc.get_if_exited() else -1
        if status == 0:
            ok = True
        else:
            ok = False
            if status in (126, 127):
                self._report("Authorization was cancelled")
            else:
                lines = (err or "").strip().splitlines()
                self._report(lines[-1] if lines else "The change failed")
        if done:
            done(ok)

    def _report(self, text):
        if self.window:
            self.window.toast(text)

    # --- power-profiles-daemon ---

    def ppd(self):
        if not self.ppd_proxy or not self.ppd_proxy.get_name_owner():
            return None
        profiles = self.ppd_proxy.get_cached_property("Profiles")
        active = self.ppd_proxy.get_cached_property("ActiveProfile")
        if profiles is None or active is None:
            return None
        return [p["Profile"] for p in profiles.unpack()], active.unpack()

    def set_ppd(self, value):
        try:
            self.ppd_proxy.call_sync(
                "org.freedesktop.DBus.Properties.Set",
                GLib.Variant("(ssv)", ("net.hadess.PowerProfiles", "ActiveProfile", GLib.Variant("s", value))),
                Gio.DBusCallFlags.NONE, -1, None)
        except GLib.Error as exc:
            self._report(exc.message)

    # --- notifications ---

    def _notify(self, key, title, body, priority=Gio.NotificationPriority.NORMAL):
        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_priority(priority)
        notification.set_default_action("app.activate")
        self.send_notification(key, notification)

    def _once(self, key, condition):
        """True the first time condition holds; re-arms when it stops holding."""
        if not condition:
            self.notified.pop(key, None)
            return False
        if key in self.notified:
            return False
        self.notified[key] = time.time()
        return True

    def _watch(self):
        info = core.combined_info()
        if not info or info["capacity"] is None:
            return GLib.SOURCE_CONTINUE
        prefs = self.prefs
        backend = core.detect()
        config = core.load_config()
        cap, ac = info["capacity"], bool(info["ac"])
        limit = backend.limit() if backend.available else None

        if not backend.available:
            level = prefs["reminder_level"]
            if self._once("unplug", prefs["notify_unplug"] and ac and level < 100 and cap >= level):
                self._notify("unplug", f"Battery at {cap}%: unplug now",
                             "This laptop can't stop charging by itself. Unplugging around "
                             f"{level}% helps the battery last longer.", Gio.NotificationPriority.HIGH)
        elif limit and limit < 100 and not config["top_up"]:
            held = ac and info["status"] in ("Not charging", "Full") and cap >= limit - 2
            if self._once("limit", prefs["notify_limit"] and held):
                self._notify("limit", f"Charging paused at {cap}%",
                             f"The battery is held at its {limit}% limit and the laptop runs on the adapter.")

        if self._once("full", prefs["notify_full"] and config["top_up"] and cap >= 99):
            self._notify("full", "Fully charged",
                         "Unplug when you're ready. The charge limit comes back on automatically.")

        if self._once("low", not ac and cap <= prefs["low_battery"]):
            self._notify("low", f"Battery low: {cap}%", "Plug in the charger soon.",
                         Gio.NotificationPriority.HIGH)

        temp = info["temperature"]
        hot = temp is not None and temp >= prefs["high_temp"]
        if self._once("hot", hot):
            self._notify("hot", f"Battery is hot: {temp:.0f} °C",
                         "Heat wears the battery quickly. Give the vents some room or lower the load.",
                         Gio.NotificationPriority.HIGH)
        return GLib.SOURCE_CONTINUE


def main(argv):
    return App().run(argv)
