"""GTK 4 / libadwaita desktop app. Runs as the normal user; changes go through pkexec."""

import json
import math
import os
import platform
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango  # noqa: E402

from . import APP_ID, VERSION, core, monitor, power, tray, tuning  # noqa: E402
from .cli import fmt_hours, fmt_watts, helper_command  # noqa: E402

GREEN = (0.15, 0.73, 0.42)
AMBER = (0.90, 0.62, 0.05)
RED = (0.88, 0.16, 0.18)
BLUE = (0.21, 0.52, 0.89)
PURPLE = (0.57, 0.38, 0.85)

# Named colours, one shade each for light and dark backgrounds. Plain hex values because
# distributions restyle the named palette (Ubuntu's purple is aubergine, which
# disappears on a dark card). Icons, charts and legends all use these.
PALETTE = {
    "cpu": ("#1c71d8", "#62a0ea"), "mem": ("#9141ac", "#c061cb"),
    "disk": ("#e66100", "#ffa348"), "net": ("#26a269", "#57e389"),
    "battery": ("#e66100", "#ffa348"), "charger": ("#26a269", "#57e389"),
}


def palette_color(name):
    """(r, g, b) of a palette colour for the current colour scheme."""
    value = PALETTE[name][int(Adw.StyleManager.get_default().get_dark())]
    return tuple(int(value[i:i + 2], 16) / 255 for i in (1, 3, 5))

PREFS_PATH = os.path.join(GLib.get_user_config_dir(), "powerkeeper", "prefs.json")
AUTOSTART_PATH = os.path.join(GLib.get_user_config_dir(), "autostart", APP_ID + ".desktop")
DEFAULT_PREFS = {
    "background": True,
    "notify_unplug": True,
    "notify_limit": False,
    "notify_full": True,
    "low_battery": 15,
    "high_temp": 45,
    "reminder_level": 85,
    "monitor_interval": 1,
    "monitor_smooth": True,
}

BEHAVIOUR_NAMES = {
    "auto": "Normal",
    "inhibit-charge": "Pause charging, run on adapter",
    "force-discharge": "Run on battery while plugged in",
}
PRESETS = (60, 70, 80, 85, 90)
# Battery numbers come from App.live (events plus polling while shown); the rest of
# the window (config, service state, power settings) changes rarely.
WINDOW_LIVE_SECONDS = 2
SLOW_REFRESH_SECONDS = 10
FAN_SECONDS = 2               # fan speeds while the Performance tab is on screen
CHART_SECONDS = 60
CHART_PAGES = ("overview", "usage", "health")
RANGES = (("6 hours", 6 * 3600), ("24 hours", 24 * 3600), ("7 days", 7 * 86400))
USAGE_RANGES = (("Today", 1), ("Last 7 days", 7))
USAGE_APPS = 8


def fmt_duration(seconds):
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    if h < 24:
        return f"{h} h {m:02d} min"
    d, h = divmod(h, 24)
    return f"{d} d {h} h"


def load_prefs():
    prefs = dict(DEFAULT_PREFS)
    # Chargekeeper's preferences, the first time Powerkeeper (its new name) starts.
    path = PREFS_PATH if os.path.exists(PREFS_PATH) else PREFS_PATH.replace("/powerkeeper/", "/chargekeeper/")
    try:
        with open(path) as f:
            prefs.update(json.load(f))
    except (OSError, ValueError):
        pass
    return prefs


def save_prefs(prefs):
    os.makedirs(os.path.dirname(PREFS_PATH), exist_ok=True)
    with open(PREFS_PATH, "w") as f:
        json.dump(prefs, f, indent=2)


def sync_autostart(enabled):
    old = AUTOSTART_PATH.replace("dev.powerkeeper.Powerkeeper", "dev.chargekeeper.Chargekeeper")
    if os.path.exists(old):     # Chargekeeper's entry would start a program that's gone
        os.remove(old)
    if enabled:
        os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
        with open(AUTOSTART_PATH, "w") as f:
            f.write("[Desktop Entry]\nType=Application\nName=Powerkeeper\n"
                    "Exec=powerkeeper --background\nIcon=" + APP_ID + "\n"
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
            if isinstance(color, str):
                color = palette_color(color)
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
        super().__init__(halign=Gtk.Align.CENTER, accessible_role=Gtk.AccessibleRole.IMG)
        self.set_size_request(size, size)
        self.value = None
        self.text = "—"
        self.caption = ""
        self.color = GREEN
        self.marker = None

    def update(self, value, text, caption, color, marker=None):
        self.value, self.text, self.caption, self.color, self.marker = value, text, caption, color, marker
        spoken = " ".join(p for p in (text, caption) if p and p != "—")
        if marker is not None and marker < 100:
            spoken += f", limit {marker}%"
        self.update_property([Gtk.AccessibleProperty.LABEL], [spoken or "No data"])
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

    LEFT, RIGHT, TOP, BOTTOM = 44, 6, 6, 22

    def __init__(self, color, unit, fixed_max=None, height=150, name="Chart"):
        super().__init__(hexpand=True, margin_top=8, margin_bottom=8, margin_start=8, margin_end=8,
                         accessible_role=Gtk.AccessibleRole.IMG)
        self.set_size_request(200, height)
        self.color, self.unit, self.fixed_max, self.name = color, unit, fixed_max, name
        self.series, self.points, self.t0, self.t1 = [], [], 0, 1
        self.reference = None
        self.strip = []
        self.empty_text = "Collecting data…"
        self.max_gap = None         # seconds between samples before the line breaks; None: by zoom
        self.hover_x = None
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    def _on_motion(self, _ctrl, x, _y):
        self.hover_x = x
        self.queue_draw()

    def _on_leave(self, _ctrl):
        self.hover_x = None
        self.queue_draw()

    def set_data(self, points, t0, t1, reference=None, strip=None, second=None):
        """points and second (an optional series in its own colour) are [(t, value)]; None breaks a line."""
        self.series = [(points, self.color)] + ([second] if second else [])
        self.points = [(t, v, color) for series, color in self.series for t, v in filter(None, series)]
        self.t0, self.t1 = t0, t1
        self.reference = reference
        self.strip = strip or []
        if len(self.points) >= 2:
            values = [v for _, v, _ in self.points]
            summary = (f"{self.name}: now {values[-1]:.0f}{self.unit}, lowest {min(values):.0f}{self.unit}, "
                       f"highest {max(values):.0f}{self.unit}")
        else:
            summary = f"{self.name}: {self.empty_text}"
        self.update_property([Gtk.AccessibleProperty.LABEL], [summary])
        self.queue_draw()

    def _nearest(self, t):
        best = None
        for point in self.points:
            if best is None or abs(point[0] - t) < abs(best[0] - t):
                best = point
        return best

    def _ymax(self):
        peak = max((p[1] for p in self.points), default=0)
        if self.fixed_max:
            # A reading above the usual top (health over 100%): the next round number.
            return max(self.fixed_max, math.ceil(peak / 10) * 10)
        step = 5 if peak <= 20 else 10 if peak <= 60 else 25
        return max(step, math.ceil(peak * 1.1 / step) * step)

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        p = Painter(self, snapshot)
        left, right, top, bottom = self.LEFT, self.RIGHT, self.TOP, self.BOTTOM
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
            layout, (tw, th) = p.layout(f"{round(ymax * frac, 1):g}{self.unit}", 8)
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

        gap = self.max_gap or max(600, span / 80)
        for series, color in self.series:
            for line in core.split_lines(series, gap):
                seg = [(x_of(t), y_of(v)) for t, v in line]
                if len(seg) >= 2:
                    p.fill(seg + [(seg[-1][0], top + ph), (seg[0][0], top + ph)], color, 0.14)
                    p.stroke(seg, 2, color)
                else:       # a lone sample still counts: show it as a dot
                    p.stroke(arc_points(*seg[0], 1.5, 0, 2 * math.pi, 12), 2, color)

        if self.hover_x is not None and left <= self.hover_x <= left + pw:
            point = self._nearest(self.t0 + span * (self.hover_x - left) / pw)
            if point and abs(x_of(point[0]) - self.hover_x) < max(12, pw / 40):
                x, y = x_of(point[0]), y_of(point[1])
                p.stroke([(x, top), (x, top + ph)], 1, None, 0.35)
                p.stroke(arc_points(x, y, 3, 0, 2 * math.pi, 16), 2.5, point[2])
                fmt = "%H:%M" if span <= 86400 else "%a %H:%M"
                decimals = 1 if self.unit.strip() == "W" else 0
                label = f"{point[1]:.{decimals}f}{self.unit} · {time.strftime(fmt, time.localtime(point[0]))}"
                layout, (tw, th) = p.layout(label, 9, bold=True)
                tx = min(max(x - tw / 2, left), left + pw - tw)
                p.rect(tx - 4, top, tw + 8, th + 4, None, 0.08)
                p.text(layout, tx, top + 2)


class PowerTile(Gtk.Box):
    """One power figure: what it is, how much, and how far to trust it."""

    def __init__(self, kind, icon):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True,
                         css_classes=["card", "power-tile", kind], accessible_role=Gtk.AccessibleRole.GROUP)
        head = Gtk.Box(spacing=6)
        self.icon = Gtk.Image(icon_name=icon)
        self.title = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption-heading"])
        head.append(self.icon)
        head.append(self.title)
        self.value = Gtk.Label(xalign=0, css_classes=["power-value"])
        self.detail = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, css_classes=["caption", "dim-label"])
        for widget in (head, self.value, self.detail):
            self.append(widget)
        self.active = None

    def set(self, title, value, detail, active):
        self.title.set_label(title)
        self.value.set_label(value)
        self.detail.set_label(detail)
        if active != self.active:
            self.active = active
            (self.add_css_class if active else self.remove_css_class)("active")
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title}: {value}, {detail}"])


KIND_WORDS = {"measured": "Measured", "estimate": "Estimated", "at_least": "Not measured",
              "unknown": "Not measured", "unplugged": "Not plugged in"}


class PowerFlow(Gtk.Box):
    """From the charger, from (or into) the battery, and what the laptop uses."""

    def __init__(self):
        super().__init__(spacing=10, homogeneous=True)
        self.charger = PowerTile("charger", "ac-adapter-symbolic")
        self.battery = PowerTile("battery", "battery-good-symbolic")
        self.laptop = PowerTile("laptop", "computer-symbolic")
        for tile in (self.charger, self.battery, self.laptop):
            self.append(tile)

    def update(self, info, typical):
        """Show a battery reading; returns the explanation for any figure that isn't measured."""
        if not info:
            for tile, title in ((self.charger, "From charger"), (self.battery, "From battery"),
                                (self.laptop, "Laptop uses")):
                tile.set(title, "—", "No battery found", False)
            self.set_tooltip_text(None)
            return ""
        flow = core.power_flow(info, typical)
        self.charger.set("From charger", fmt_watts(flow["from_charger"], flow["charger_kind"]),
                         KIND_WORDS[flow["charger_kind"]], flow["source"] in ("charger", "both"))
        if flow["into_battery"] is not None or info["status"] == "Charging":
            self.battery.remove_css_class("battery")
            self.battery.add_css_class("charging")
            self.battery.set("Into battery", fmt_watts(flow["into_battery"]), "Charging", True)
        else:
            self.battery.remove_css_class("charging")
            self.battery.add_css_class("battery")
            drawing = flow["source"] in ("battery", "both")
            self.battery.set("From battery", fmt_watts(flow["from_battery"]),
                             "Measured" if drawing else "Resting", drawing)
        self.laptop.set("Laptop uses", fmt_watts(flow["laptop"], flow["laptop_kind"]),
                        KIND_WORDS[flow["laptop_kind"]], False)

        note = ""
        if flow["charger_kind"] == "estimate":
            note = (f"This laptop has no sensor for charger power. ≈ figures use your typical draw on "
                    f"battery ({typical:.0f} W over the last week), so they won't follow a heavy load.")
        elif flow["charger_kind"] in ("at_least", "unknown"):
            note = ("This laptop has no sensor for charger power. After about 10 minutes on battery, "
                    "Powerkeeper can estimate it from your typical draw.")
        self.set_tooltip_text(note or None)
        return note


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
        super().__init__(application=app, title="Powerkeeper", default_width=860, default_height=880,
                         width_request=360, height_request=420)
        self.app = app
        self.syncing = False
        self.pending = {}
        self.pending_source = 0
        self.inflight = False
        self.hint_ids = None
        self.start_range = (10, 1)
        self.backend = None
        self.history_range = RANGES[1][1]
        self.usage_days = USAGE_RANGES[0][1]
        self.last_chart = 0
        self.typical = None
        self.notice_seen = 0

        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("About Powerkeeper", "app.about")
        menu.append("Quit", "app.quit")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu"))
        view.add_top_bar(header)
        self.banner = Adw.Banner()
        view.add_top_bar(self.banner)

        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        view.add_bottom_bar(bottom)
        # The version on every tab, so a screenshot or a bug report always shows it.
        footer = Gtk.Label(label=f"Powerkeeper {VERSION}", css_classes=["dim-label", "caption"],
                           margin_top=4, margin_bottom=6)
        view.add_bottom_bar(footer)
        self._build_overview()
        self._build_usage()
        self._build_monitor()
        self._build_performance()
        self._build_charging()
        self._build_health()
        self._build_system()
        # Seven tabs side by side need about 1000sp; below that the header switcher stacks
        # each tab's icon above its label, and below 720sp the tabs move to the bottom.
        compact = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 1000sp"))
        compact.add_setter(switcher, "policy", Adw.ViewSwitcherPolicy.NARROW)
        self.add_breakpoint(compact)
        # Only one breakpoint applies at a time, so the narrower one repeats the first's setters.
        for width, tile_icons in ((720, True), (480, False)):
            breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse(f"max-width: {width}sp"))
            breakpoint.add_setter(bottom, "reveal", True)
            breakpoint.add_setter(switcher, "visible", False)
            if not tile_icons:      # three tiles side by side: their titles need the room
                for flow in (self.power_flow, self.monitor_page.power):
                    for tile in (flow.charger, flow.battery, flow.laptop):
                        breakpoint.add_setter(tile.icon, "visible", False)
            self.add_breakpoint(breakpoint)
        view.set_content(self.stack)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.config = core.load_config()
        self.effective = 100
        self.refresh()
        self.refresh_charts()
        self.timer = GLib.timeout_add_seconds(SLOW_REFRESH_SECONDS, self._tick)
        self.app.live.subscribe(self._on_battery)
        self.connect("close-request", self._on_close)
        self.connect("notify::visible", self._on_visible)
        self.connect("notify::suspended", self._on_page)
        self.stack.connect("notify::visible-child-name", self._on_page)

    def _on_page(self, *_):
        page = self.stack.get_visible_child_name()
        seen = self.get_visible() and not self.is_suspended()
        # The monitor keeps sampling /proc, so it only runs while it's on screen.
        self.monitor_page.set_active(seen and page == "monitor")
        self._fans_live(seen and page == "performance")
        # Only these tabs show live battery numbers; the others have no use for polling.
        if seen and page in ("overview", "monitor"):
            self.app.live.want("window", WINDOW_LIVE_SECONDS)
            self._show_battery(self.app.live.info)
        else:
            self.app.live.release("window")
        if seen and page in CHART_PAGES and time.time() - self.last_chart > CHART_SECONDS:
            self.refresh_charts()

    def _on_visible(self, *_):
        if self.get_visible():
            self.refresh()
            self.refresh_charts()
        self._on_page()

    def _on_battery(self, info):
        """A new live reading: redraw only the battery numbers."""
        if self.get_mapped() and not self.is_suspended():
            self._show_battery(info)

    def _show_battery(self, info):
        page = self.stack.get_visible_child_name()
        if page == "overview":
            self._refresh_overview(info, self.backend, self.config, self.effective)
        elif page == "monitor":
            self.monitor_page.power.update(info, self.typical)

    def _on_close(self, *_):
        if self.app.prefs["background"]:
            # Hide and reuse. A destroyed window is never freed: its widgets' signal
            # handlers refer back to it through GTK, a cycle Python's collector can't
            # see, so every reopen from the tray leaked about 7 MB.
            self.set_visible(False)
            return True
        GLib.source_remove(self.timer)
        self.monitor_page.set_active(False)
        self._fans_live(False)
        self.app.live.release("window")
        self.app.live.listeners.remove(self._on_battery)
        self.app.window = None
        return False

    def toast(self, text):
        self.toasts.add_toast(Adw.Toast(title=text, timeout=4))

    def _tick(self):
        # Minimized or hidden windows don't need live numbers; skip the sysfs reads.
        if self.is_suspended() or not self.get_mapped():
            return GLib.SOURCE_CONTINUE
        self.refresh()
        if self.stack.get_visible_child_name() in CHART_PAGES and time.time() - self.last_chart > CHART_SECONDS:
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
        self.full_button = Gtk.Button(halign=Gtk.Align.CENTER, margin_top=8, css_classes=["pill"])
        self.full_button.connect("clicked", self._on_full_button)
        for widget in (self.ring, self.status_title, self.status_sub, self.full_button):
            box.append(widget)
        hero.add(box)
        page.add(hero)

        flow_group = Adw.PreferencesGroup(title="Power")
        self.power_flow = PowerFlow()
        flow_group.add(self.power_flow)
        self.power_note = Gtk.Label(xalign=0, wrap=True, visible=False, margin_top=8, margin_start=2,
                                    css_classes=["caption", "dim-label"])
        flow_group.add(self.power_note)
        page.add(flow_group)

        now = Adw.PreferencesGroup(title="Right now")
        self.limit_row = Adw.ActionRow(title="Charge limit", activatable=True)
        self.limit_row.add_prefix(Gtk.Image(icon_name="battery-full-charging-symbolic"))
        self.limit_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        self.limit_row.connect("activated", lambda *_: self.stack.set_visible_child_name("charging"))
        now.add(self.limit_row)
        self.v_time = info_row(now, "Time", "preferences-system-time-symbolic")
        self.v_energy = info_row(now, "Energy", "battery-level-70-symbolic")
        self.v_temp = info_row(now, "Battery temperature", "pk-temperature-symbolic")
        page.add(now)

        history = Adw.PreferencesGroup(title="History")
        picker = Gtk.DropDown.new_from_strings([label for label, _ in RANGES])
        picker.set_selected(1)
        picker.set_valign(Gtk.Align.CENTER)
        picker.connect("notify::selected", self._on_range)
        history.set_header_suffix(picker)
        self.chart_level = Chart(GREEN, "%", fixed_max=100, name="Charge level")
        self.chart_power = Chart("battery", " W", height=120, name="Power")
        for title, chart, legend in (("Charge level", self.chart_level, ()),
                                     ("Battery power", self.chart_power,
                                      (("battery", "Drawn from it"), ("charger", "Charging it")))):
            row = Adw.PreferencesRow(activatable=False)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            head = Gtk.Box(spacing=6, margin_top=10, margin_start=12, margin_end=12)
            head.append(Gtk.Label(label=title, xalign=0, hexpand=True, css_classes=["caption-heading"]))
            for kind, text in legend:
                head.append(Gtk.Label(label="●", css_classes=["caption", "dot", f"dot-{kind}"]))
                head.append(Gtk.Label(label=text, css_classes=["caption", "dim-label"], margin_end=6))
            inner.append(head)
            inner.append(chart)
            row.set_child(inner)
            history.add(row)
        page.add(history)

    def _build_usage(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "usage", "Usage", "document-open-recent-symbolic")

        self.usage_battery = Adw.PreferencesGroup(title="On battery")
        picker = Gtk.DropDown.new_from_strings([label for label, _ in USAGE_RANGES])
        picker.set_valign(Gtk.Align.CENTER)
        picker.connect("notify::selected", self._on_usage_range)
        self.usage_battery.set_header_suffix(picker)
        self.u_time = info_row(self.usage_battery, "Time on battery", "preferences-system-time-symbolic")
        self.u_used = info_row(self.usage_battery, "Battery used", "battery-level-30-symbolic")
        self.u_energy = info_row(self.usage_battery, "Energy used", "battery-caution-symbolic")
        self.u_average = info_row(self.usage_battery, "Average power", "power-profile-balanced-symbolic")
        self.u_runtime = info_row(self.usage_battery, "A full charge lasts about", "battery-full-symbolic")
        page.add(self.usage_battery)

        charger = Adw.PreferencesGroup(title="On the charger")
        self.u_plugged = info_row(charger, "Time plugged in", "ac-adapter-symbolic")
        self.u_charged = info_row(charger, "Battery charged", "battery-full-charging-symbolic")
        self.u_charged_wh = info_row(charger, "Energy into the battery", "battery-full-charged-symbolic")
        page.add(charger)

        self.apps_group = Adw.PreferencesGroup(
            title="Apps on battery",
            description="Share of processor time while running on battery. After the screen, the "
                        "processor is usually what drains the battery most.")
        self.apps_empty = Adw.ActionRow(title="No time on battery yet",
                                        subtitle="Apps show up here after the laptop runs unplugged.")
        self.apps_group.add(self.apps_empty)
        self.app_rows = []
        for _ in range(USAGE_APPS):
            row = Adw.ActionRow(visible=False)
            row.icon = Gtk.Image(pixel_size=24)
            row.add_prefix(row.icon)
            bar = Gtk.LevelBar(valign=Gtk.Align.CENTER, width_request=90, max_value=1)
            label = Gtk.Label(css_classes=["dim-label", "numeric"], width_chars=4, xalign=1)
            row.add_suffix(bar)
            row.add_suffix(label)
            self.apps_group.add(row)
            self.app_rows.append((row, bar, label))
        page.add(self.apps_group)

    def _on_usage_range(self, dropdown, _pspec):
        self.usage_days = USAGE_RANGES[dropdown.get_selected()][1]
        self.refresh_usage()

    def refresh_usage(self):
        now = time.time()
        if self.usage_days == 1:
            today = time.localtime(now)
            since = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
        else:
            since = now - self.usage_days * 86400
        summary = core.usage_summary(core.load_history(since))
        on_battery = summary["battery_seconds"]
        self.u_time.set_label(fmt_duration(on_battery) if on_battery else "—")
        self.u_used.set_label(f"{summary['used_pct']}%" if on_battery else "—")
        self.u_energy.set_label(f"{summary['energy_wh']:.1f} Wh" if summary["energy_wh"] else "—")
        average = summary["average_w"]
        self.u_average.set_label(f"{average:.1f} W" if average else "—")
        info = core.combined_info()
        if average and info and info["energy_full"]:
            self.u_runtime.set_label(fmt_hours(info["energy_full"] / average) + " at this rate")
        else:
            self.u_runtime.set_label("—")
        plugged = summary["plugged_seconds"]
        self.u_plugged.set_label(fmt_duration(plugged) if plugged else "—")
        self.u_charged.set_label(f"{summary['charged_pct']}%" if plugged else "—")
        self.u_charged_wh.set_label(f"{summary['charged_wh']:.1f} Wh" if plugged else "—")

        apps = core.app_usage(self.usage_days)
        total = sum(cpu for _, cpu in apps) or 1
        self.apps_empty.set_visible(not apps)
        for i, (row, bar, label) in enumerate(self.app_rows):
            if i >= len(apps):
                row.set_visible(False)
                continue
            name, cpu = apps[i]
            share = cpu / total
            display, icon = monitor.app_identity(name)
            row.set_title(GLib.markup_escape_text(display))
            row.icon.set_from_gicon(icon)
            row.set_subtitle(f"{fmt_duration(cpu)} of processor time" if cpu >= 60
                             else f"{cpu:.0f} s of processor time")
            bar.set_value(share)
            label.set_label(f"{share * 100:.0f}%")
            row.set_visible(True)

    def _build_monitor(self):
        from .monitor_page import MonitorPage
        self.monitor_page = MonitorPage(self.app)
        self.stack.add_titled_with_icon(self.monitor_page, "monitor", "Monitor",
                                        "utilities-system-monitor-symbolic")

    def _build_performance(self):
        page = Adw.PreferencesPage()
        self.stack.add_titled_with_icon(page, "performance", "Performance",
                                        "power-profile-performance-symbolic")
        self.fan_list = []
        self.fan_rows = []
        self.fan_timer = 0
        self.fan_reading = False
        self.parts = None

        self.unlock_group = Adw.PreferencesGroup(visible=False)
        page.add(self.unlock_group)
        self.unlock_rows = []

        power = Adw.PreferencesGroup(title="Power")
        self.max_row = Adw.SwitchRow(title="Maximum performance",
                                     subtitle="Highest firmware profile, CPU and graphics power")
        self.max_row.add_prefix(Gtk.Image(icon_name="power-profile-performance-symbolic"))
        self.max_row.connect("notify::active", self._on_max)
        self.max_ac_row = Adw.SwitchRow(title="Only while plugged in",
                                        subtitle="Back to the usual settings on battery, which "
                                                 "also limits the graphics power")
        self.max_ac_row.add_prefix(Gtk.Image(icon_name="ac-adapter-symbolic"))
        self.max_ac_row.connect("notify::active", self._on_max)
        power.add(self.max_row)
        power.add(self.max_ac_row)
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
                                              "or unplug, and may undo maximum performance.")
        self.tlp_row.add_prefix(Gtk.Image(icon_name="emblem-system-symbolic"))
        for row in (self.ppd_row, self.profile_row, self.epp_row, self.tlp_row):
            power.add(row)
        self.v_cpu = info_row(power, "CPU", "pk-cpu-symbolic")
        page.add(power)

        self.fan_group = Adw.PreferencesGroup(title="Fans")
        self.fan_mode_row = Adw.ComboRow(title="Fan control")
        self.fan_mode_row.add_prefix(Gtk.Image(icon_name="pk-fan-symbolic"))
        self.fan_mode_row.connect("notify::selected", self._on_fan_mode)
        self.fan_speed_row = Adw.SpinRow.new_with_range(tuning.FAN_MIN_PERCENT, 100, 5)
        self.fan_speed_row.set_title("Fixed speed (%)")
        self.fan_speed_row.set_subtitle(f"Back to automatic if the processor reaches "
                                        f"{tuning.FAN_SAFE_TEMP} °C")
        self.fan_speed_row.connect("notify::value", self._on_fan_speed)
        self.fan_none_row = Adw.ActionRow(title="No fan control")
        self.fan_none_row.add_prefix(Gtk.Image(icon_name="pk-fan-symbolic"))
        for row in (self.fan_mode_row, self.fan_speed_row, self.fan_none_row):
            self.fan_group.add(row)
        page.add(self.fan_group)

    def _refresh_performance(self, config):
        self.syncing = True
        try:
            self.max_row.set_active(config["performance"] != "off")
            self.max_ac_row.set_active(config["performance"] != "on")
            self.max_ac_row.set_sensitive(config["performance"] != "off")
            self.fan_list = tuning.fans()
            modes = tuning.fan_modes(self.fan_list)
            names = {"auto": "Automatic", "max": "Full speed", "fixed": "Fixed speed"}
            if modes:
                set_combo(self.fan_mode_row, modes,
                          config["fan_mode"] if config["fan_mode"] in modes else "auto", names)
            self.fan_mode_row.set_visible(bool(modes))
            self.fan_speed_row.set_value(config["fan_percent"])
            self.fan_speed_row.set_visible(config["fan_mode"] == "fixed" and "fixed" in modes)
            self.fan_none_row.set_visible(not modes)
            self.fan_none_row.set_subtitle(
                ("Linux can read this laptop's fan speeds but not set them. " if self.fan_list
                 else "Linux has no fan controls for this laptop. ")
                + "The firmware runs the fans; maximum performance usually speeds them up.")
        finally:
            self.syncing = False

        labels = [f["label"] for f in self.fan_list if f["rpm"]]
        if [r.get_title() for r, _ in self.fan_rows] != labels:
            for row, _ in self.fan_rows:
                self.fan_group.remove(row)
            self.fan_rows = []
            for label in labels:
                row = Adw.ActionRow(title=label)
                value = Gtk.Label(css_classes=["dim-label", "numeric"], width_chars=9,
                                  xalign=1, label="—")
                row.add_suffix(value)
                self.fan_group.add(row)
                self.fan_rows.append((row, value))

        unlocks = tuning.unlocks()
        if [u["id"] for u in unlocks] != [r.unlock_id for r in self.unlock_rows]:
            for row in self.unlock_rows:
                self.unlock_group.remove(row)
            self.unlock_rows = []
            for unlock in unlocks:
                row = Adw.ActionRow(title=unlock["title"], subtitle=unlock["body"])
                row.unlock_id = unlock["id"]
                row.add_prefix(Gtk.Image(icon_name="dialog-information-symbolic"))
                button = Gtk.Button(label="Turn on", valign=Gtk.Align.CENTER,
                                    css_classes=["suggested-action"])
                button.connect("clicked", self._on_unlock, unlock["id"])
                row.add_suffix(button)
                self.unlock_group.add(row)
                self.unlock_rows.append(row)
        self.unlock_group.set_visible(bool(unlocks))

        if self.parts is None:
            self.parts = []
            # systemctl and busctl calls: never on the main loop (rule 6).
            threading.Thread(target=self._read_parts, daemon=True).start()

    def _read_parts(self):
        parts = tuning.performance_parts(ppd=self.app.ppd() is not None)
        GLib.idle_add(self._show_parts, parts)

    def _show_parts(self, parts):
        self.parts = parts
        if parts:
            self.max_row.set_subtitle(", ".join(text for _, text in parts))
        else:
            self.max_row.set_subtitle("Nothing on this laptop can be raised from Linux")
        return GLib.SOURCE_REMOVE

    def _fans_live(self, active):
        if active and not self.fan_timer:
            self._fan_tick()
            self.fan_timer = GLib.timeout_add_seconds(FAN_SECONDS, self._fan_tick)
        elif not active and self.fan_timer:
            GLib.source_remove(self.fan_timer)
            self.fan_timer = 0

    def _fan_tick(self):
        # Some fan speeds are firmware calls (Acer: WMI), so read them in a short-lived thread.
        if self.fan_list and not self.fan_reading:
            self.fan_reading = True
            fans = list(self.fan_list)
            threading.Thread(target=lambda: GLib.idle_add(self._show_fans, tuning.fan_speeds(fans)),
                             daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _show_fans(self, speeds):
        self.fan_reading = False
        rpms = [rpm for label, rpm in speeds if label in {r.get_title() for r, _ in self.fan_rows}]
        for (_row, value), rpm in zip(self.fan_rows, rpms):
            text = "—" if rpm is None else f"{rpm} RPM"
            if value.get_label() != text:
                value.set_label(text)
        return GLib.SOURCE_REMOVE

    def _on_max(self, _row, _pspec):
        if self.syncing:
            return
        on = self.max_row.get_active()
        self.max_ac_row.set_sensitive(on)
        self._queue(performance=("ac" if self.max_ac_row.get_active() else "on") if on else "off")

    def _on_fan_mode(self, row, _pspec):
        value = combo_value(row)
        if not self.syncing and value:
            self.fan_speed_row.set_visible(value == "fixed")
            self._queue(**{"fan-mode": value})

    def _on_fan_speed(self, row, _pspec):
        if not self.syncing:
            self._queue(**{"fan-percent": int(row.get_value())})

    def _on_unlock(self, button, unlock_id):
        button.set_sensitive(False)

        def done(ok):
            button.set_sensitive(True)
            self.parts = None
            self.refresh()
            if ok:
                self.toast("Driver option turned on" if not tuning.unlocks()
                           else "Restart the laptop to finish")
        self.app.run_helper(["unlock", unlock_id], done)

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
            title="Charge to full once",
            subtitle="For a long day away from a charger. The limit comes back when you unplug, "
                     f"or {core.TOP_UP_FULL_HOURS} hours after the battery is full.")
        self.topup_row.add_prefix(Gtk.Image(icon_name="battery-full-charged-symbolic"))
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
        self.chart_health = Chart(GREEN, "%", fixed_max=100, height=130, name="Battery health")
        self.chart_health.empty_text = "Needs a few days of data"
        self.chart_health.max_gap = 14 * 86400      # daily samples: a line through days the laptop was off
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
        self.stack.add_titled_with_icon(page, "system", "Settings", "emblem-system-symbolic")

        background = Adw.PreferencesGroup(title="Tray")
        prefs = self.app.prefs
        self.bg_row = Adw.SwitchRow(title="Tray icon",
                                    subtitle="Show the battery in the top bar with a quick limit menu. "
                                             "Starts at login and sends notifications.",
                                    active=prefs["background"])
        self.bg_row.connect("notify::active", lambda r, _p: self.app.set_background(r.get_active()))
        background.add(self.bg_row)
        self.tray_row = Adw.ActionRow(title="The top bar can't show tray icons",
                                      subtitle="GNOME needs the AppIndicator extension for this.",
                                      visible=False)
        self.tray_row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        self.tray_button = Gtk.Button(label="Enable", valign=Gtk.Align.CENTER)
        self.tray_button.connect("clicked", self._enable_tray_extension)
        self.tray_row.add_suffix(self.tray_button)
        background.add(self.tray_row)
        page.add(background)

        notify = Adw.PreferencesGroup(title="Notifications")
        for key, title, subtitle in (
            ("notify_unplug", "Unplug reminder",
             "When this laptop can't limit charging itself, remind me at the limit"),
            ("notify_limit", "Limit reached", "Tell me when charging pauses at the limit"),
            ("notify_full", "Charge to full finished", "Tell me when a charge to 100% is done"),
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
        info = self.app.live.info
        self.backend = backend = core.detect()
        desc = backend.describe()
        self.config = config = core.load_config()
        state = core.daemon_state()
        limit = backend.limit() if backend.available else None
        self.effective = effective = limit if limit is not None else 100

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
        if not self.pending and not self.inflight:
            self._refresh_charging(backend, desc, config, limit)
        self._refresh_health(info, backend, config)
        self._refresh_system(backend, state)
        self._refresh_performance(config)
        notice = (state or {}).get("notice")
        if notice and notice.get("time", 0) > self.notice_seen:
            self.notice_seen = notice["time"]
            if time.time() - notice["time"] < 600:
                self.toast(notice["text"])

    def _refresh_overview(self, info, backend, config, limit):
        if not info:
            self.ring.update(None, "—", "", BLUE)
            self.status_title.set_label("No battery found")
            self.status_sub.set_label("This computer doesn't report a system battery.")
            self.status_sub.set_visible(True)
            self.full_button.set_visible(False)
            self.limit_row.set_subtitle("—")
            for label in (self.v_time, self.v_energy, self.v_temp):
                label.set_label("—")
            self._show_flow(info)
            return
        cap = info["capacity"]
        status = info["status"]
        remaining = core.time_remaining(info, 100 if config["top_up"] else limit)
        marker = limit if limit < 100 else None
        if not backend.available and self.app.prefs["notify_unplug"]:
            marker = self.app.prefs["reminder_level"]

        if config["calibrating"]:
            title, sub = "Calibrating", "Keep the charger connected until calibration finishes."
        elif status == "Charging":
            title = "Charging"
            target = 100 if config["top_up"] else limit
            sub = f"{fmt_hours(remaining)} until {target}%" if remaining else f"Charging to {target}%"
        elif status == "Discharging":
            title = "Plugged in · running on battery" if info["ac"] else "On battery"
            sub = f"About {fmt_hours(remaining)} left" if remaining else ""
            # Tell people when what they're doing right now would change that noticeably.
            at_load = core.time_remaining(info, power=info.get("power_fast"))
            if remaining and at_load and abs(at_load - remaining) / remaining > 0.15:
                sub += f" · {fmt_hours(at_load)} at the current load"
        elif info["ac"]:
            if limit < 100 and cap is not None and cap > limit + 2:
                title = "Plugged in · above the limit"
                sub = (f"Not charging. The battery holds {cap}% and will stay at {limit}% once "
                       "it drains below that.")
            elif limit < 100:
                title = f"Plugged in · held at {cap}%"
                sub = "Running on adapter power while the battery rests."
            elif status == "Full" or (cap is not None and cap >= 99):
                title, sub = "Plugged in · fully charged", ""
            else:
                title, sub = "Plugged in · not charging", "Running on adapter power."
        else:
            title, sub = status, ""
        self.ring.update(cap, f"{cap}%" if cap is not None else "—",
                         "plugged in" if info["ac"] else "on battery", level_color(cap), marker)
        self.status_title.set_label(title)
        self.status_sub.set_label(sub)
        self.status_sub.set_visible(bool(sub))

        # Only worth offering while a limit is actually holding the charge back.
        limited = backend.available and config["limit_enabled"] and not config["calibrating"]
        self.full_button.set_visible(limited and (config["top_up"] or limit < 100))
        if config["top_up"]:
            self.full_button.set_label("Stop and restore the limit")
            self.full_button.remove_css_class("suggested-action")
        else:
            self.full_button.set_label("Charge to full once")
            # A call to action only when pressing it starts charging right away.
            (self.full_button.add_css_class if info["ac"] else self.full_button.remove_css_class)(
                "suggested-action")
        self.full_button.set_tooltip_text(
            f"Charge to 100% once. The {limit}% limit comes back when you unplug."
            if not config["top_up"] else "Go back to the charge limit now")

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

        self._show_flow(info)
        if remaining:
            self.v_time.set_label(fmt_hours(remaining) + (" left" if status == "Discharging" else " to target"))
        else:
            self.v_time.set_label("—")
        if info["energy_now"] is not None and info["energy_full"]:
            self.v_energy.set_label(f"{info['energy_now']:.1f} of {info['energy_full']:.1f} Wh")
        else:
            self.v_energy.set_label("—")
        temp = info["temperature"]
        self.v_temp.set_label(f"{temp:.1f} °C" if temp is not None else "Not reported")

    def _show_flow(self, info):
        note = self.power_flow.update(info, self.typical)
        self.power_note.set_label(note)
        self.power_note.set_visible(bool(note))

    def _refresh_charging(self, backend, desc, config, limit):
        self.syncing = True
        try:
            self.backend_row.set_title(backend.name)
            self.backend_row.set_subtitle(backend.detail)
            self.backend_icon.set_from_icon_name(
                "emblem-ok-symbolic" if backend.available else "dialog-warning-symbolic")

            # Rebuild only on change; recreating rows every tick forced a relayout each time.
            hints = core.hints()
            ids = [h["id"] for h in hints]
            if ids != self.hint_ids:
                self.hint_ids = ids
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
                    "This laptop can't stop charging by itself, so Powerkeeper will notify you "
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
                # MSI derives the resume level from the limit itself.
                self.start_row.set_visible(desc["supports_start"] and not desc["fixed_gap"])
                self.start_row.set_sensitive(enabled)
                self.start_range = (max(10, desc["min_start"]), desc["min_gap"])
                self._set_start_range(config["end"])
                wanted = core.target(dict(config, limit_enabled=True, top_up=False), backend)
                self.start_row.set_value(wanted[1] if wanted and wanted[1] is not None
                                         else config["end"] - 5)
            else:
                self.limit_group.set_title("Charge limit")
                fixed = [s for s in desc["steps"] if s < 100]
                levels = desc["fixed_text"] or " or ".join(f"{s}%" for s in fixed)
                self.limit_group.set_description(
                    f"This laptop's firmware supports only {levels}, not a custom value. "
                    "For lithium-ion cells 80% is close to ideal.")
                self.scale_row.set_visible(False)
                self.fixed_row.set_visible(True)
                self.fixed_row.set_sensitive(enabled)
                self.fixed_row.set_subtitle("Set by the firmware; the exact level depends on the model"
                                            if desc["fixed_text"] else "Set by the firmware")
                self.fixed_label.set_label(desc["fixed_text"] or f"{backend.snap(min(config['end'], fixed[-1]))}%")
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
        if info:
            self.v_model.set_label(info["model"] or "—")
            self.v_maker.set_label(info["manufacturer"] or "—")
            self.v_tech.set_label(info["technology"] or "—")
            if info["voltage"]:
                design = f" (minimum {info['voltage_design']:.1f} V)" if info["voltage_design"] else ""
                self.v_voltage.set_label(f"{info['voltage']:.2f} V{design}")
            else:
                self.v_voltage.set_label("—")
            cycles = info["cycle_count"]
            self.v_cycles.set_label(str(cycles) if cycles else "Not reported by firmware")
        if not info or info["health"] is None:
            self.health_ring.update(None, "—", "health", BLUE)
            self.health_note.set_label("The battery doesn't report its capacity.")
            for label in (self.v_full, self.v_design, self.v_wear):
                label.set_label("—")
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
        self.refresh_tray_row()

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
        week = core.load_history(now - core.TYPICAL_DAYS * 86400)
        self.typical = core.typical_draw(week)
        self._show_battery(self.app.live.info)     # the estimate may have just become available
        points = [p for p in week if p["t"] >= t0]
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
        drawn, charged = core.power_series(sampled)
        self.chart_power.set_data(drawn, t0, now, second=(charged, "charger"))

        health = [r for r in core.load_health() if r["health"] is not None]
        h0 = min(health[0]["t"], now - 30 * 86400) if health else now - 30 * 86400
        self.chart_health.set_data([(r["t"], r["health"]) for r in health], h0, now, reference=80)
        self.refresh_usage()

    # --- changes ---

    def _queue(self, **changes):
        """Collect changes for 0.7 s so dragging the slider asks for authorization once."""
        self.pending.update(changes)
        if self.pending_source:
            GLib.source_remove(self.pending_source)
        self.pending_source = GLib.timeout_add(700, self._flush)

    def _flush(self):
        self.pending_source = 0
        if self.inflight:
            # One pkexec at a time; _after_change sends what queued up meanwhile.
            return GLib.SOURCE_REMOVE
        changes, self.pending = self.pending, {}
        args = []
        for key, value in changes.items():
            if isinstance(value, bool):
                value = "on" if value else "off"
            args += [f"--{key}", str(value)]
        self.inflight = True
        self.app.run_helper(["set", *args], self._after_change)
        return GLib.SOURCE_REMOVE

    def _after_change(self, ok):
        self.inflight = False
        if self.pending and not self.pending_source:
            self._flush()
            return
        self.refresh()
        self.refresh_charts()
        if self.app.tray:
            self.app.tray.update()

    def _on_limit_switch(self, row, _pspec):
        if not self.syncing:
            self.scale_row.set_sensitive(row.get_active())
            self.start_row.set_sensitive(row.get_active())
            self.fixed_row.set_sensitive(row.get_active())
            self._queue(**{"limit-enabled": row.get_active(), "top-up": False})

    def _on_scale(self, scale, _pspec=None):
        value = int(round(scale.get_value()))
        self.scale_value.set_label(f"{value}%")
        if self.syncing:
            return
        if self.backend and not self.backend.available:
            self.app.set_pref("reminder_level", value)
            return
        self._set_start_range(value)
        self._queue(end=value)

    def _set_start_range(self, end):
        low, gap = self.start_range
        self.start_row.set_range(low, max(low + 1, end - gap))

    def _on_start(self, row, _pspec):
        if not self.syncing:
            self._queue(start=int(row.get_value()))

    def _on_full_button(self, _button):
        self._queue(**{"top-up": not core.load_config()["top_up"]})

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

    def refresh_tray_row(self):
        missing = bool(self.app.tray) and not self.app.tray.host_available
        uuid = tray.extension_to_enable()
        self.tray_row.set_visible(missing)
        self.tray_button.set_visible(uuid is not None)
        self.tray_row.set_subtitle("GNOME needs the AppIndicator extension for this." if uuid else
                                   "Install a tray or AppIndicator extension for your desktop.")

    def _enable_tray_extension(self, _button):
        uuid = tray.extension_to_enable()
        if not uuid:
            return
        try:
            proc = Gio.Subprocess.new(["gnome-extensions", "enable", uuid], Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as exc:
            self.toast(exc.message)
            return

        def done(p, result):
            try:
                ok = p.wait_check_finish(result)
            except GLib.Error:      # a failed command raises here rather than returning False
                ok = False
            self.toast("Tray extension enabled" if ok else "Couldn't enable the extension")
        proc.wait_check_async(None, done)

    def _copy_report(self, _button):
        info = core.combined_info() or {}
        backend = core.detect()
        lines = [
            f"Powerkeeper {VERSION}",
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
        self.tray = None

    def _local_options(self, _app, options):
        if options.contains("background"):
            self.start_hidden = True
        return -1

    def do_startup(self):
        Adw.Application.do_startup(self)
        from . import monitor_page
        monitor_page.load_css()
        monitor_page.add_icon_path()
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
        # One shared live reading for the window, the tray and notifications: kernel
        # events plus polling only while something on screen asks for it.
        self.live = power.LiveBattery()
        self.live.subscribe(self._on_battery)
        sync_autostart(self.prefs["background"])
        self.set_background(self.prefs["background"])
        GLib.idle_add(self._first_reading)

    def _first_reading(self):
        self._on_battery(self.live.info)
        return GLib.SOURCE_REMOVE

    def _on_battery(self, info):
        if self.tray:
            self.tray.update(info=info)
        self._watch(info)

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
        if enabled and self.tray is None and self.get_dbus_connection():
            self.tray = tray.Tray(self)
        elif not enabled and self.tray is not None:
            self.tray.close()
            self.tray = None
        self.tray_changed()

    def tray_changed(self):
        if self.window:
            self.window.refresh_tray_row()

    def settings_changed(self):
        """A change from the tray: bring the tray and the window up to date."""
        if self.tray:
            self.tray.update()
        if self.window and not self.window.pending and not self.window.inflight:
            self.window.refresh()
            self.window.refresh_charts()

    def _about(self, *_):
        dialog = Adw.AboutDialog(
            application_name="Powerkeeper", application_icon=APP_ID, version=VERSION,
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
        if self.window and self.window.get_visible():
            self.window.toast(text)
        else:
            self._notify("error", "Powerkeeper", text)

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
        def done(proxy, result):
            try:
                proxy.call_finish(result)
            except GLib.Error as exc:
                self._report(exc.message)

        self.ppd_proxy.call(
            "org.freedesktop.DBus.Properties.Set",
            GLib.Variant("(ssv)", ("net.hadess.PowerProfiles", "ActiveProfile", GLib.Variant("s", value))),
            Gio.DBusCallFlags.NONE, -1, None, done)

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

    def show_page(self, name):
        self.activate()
        if self.window:
            self.window.stack.set_visible_child_name(name)

    def _watch(self, info):
        """Notifications, evaluated on every changed battery reading."""
        if not info or info["capacity"] is None:
            return
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


def main(argv):
    return App().run(argv)
