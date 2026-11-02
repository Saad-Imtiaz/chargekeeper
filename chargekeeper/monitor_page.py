"""The Monitor page: live CPU, memory, disk and network with top apps, plus temperatures.

Sampling runs only while the page is on screen, every INTERVAL seconds, in a short-lived
thread: reading sensors can take tens of milliseconds, which would stall scrolling.

The page is built not to move: every card reserves room for its three app rows, numbers
have fixed widths, and displayed values are averaged a little (the sparklines stay raw).
"""

import collections
import os
import threading
import time
import traceback

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango

from . import core, monitor
from .monitor import app_identity
from .gui import PALETTE, Painter, PowerFlow

INTERVAL = 2
HISTORY = 60                 # samples kept for the sparklines (2 minutes)
NETWORK_EVERY = 3            # run `ss` every third tick
TEMPS_EVERY = 2
FIRST_READING = 1.0          # seconds between the two samples a first rate needs
MAX_GAP = 3 * INTERVAL       # away longer than this (other tab, minimized): start over
NUMBER_TAU = 4.0             # seconds; averaging of the large numbers
TEMP_TAU = 8.0               # CPU temperature swings by 15 °C within a second

CSS = """
.monitor-page { padding: 6px 0 24px 0; }
.page-title { font-size: 1.6em; font-weight: 800; }
.cards flowboxchild { padding: 0; background: none; }
.metric-card, .temp-tile { padding: 16px; }
.metric-icon { border-radius: 999px; min-width: 36px; min-height: 36px; }
.metric-icon image { margin: 10px; }
.metric-value { font-size: 1.55em; font-weight: 800; font-feature-settings: "tnum"; }
.temp-value { font-size: 2.1em; font-weight: 800; font-feature-settings: "tnum"; }
.cpu .metric-icon, .temp-tile.cpu .metric-icon { background: alpha(@ck_cpu, 0.16); color: @ck_cpu; }
.memory .metric-icon { background: alpha(@ck_mem, 0.16); color: @ck_mem; }
.disk .metric-icon, .temp-tile.storage .metric-icon { background: alpha(@ck_disk, 0.16); color: @ck_disk; }
.network .metric-icon { background: alpha(@ck_net, 0.16); color: @ck_net; }
.temp-tile.battery .metric-icon { background: alpha(@ck_net, 0.16); color: @ck_net; }
.temp-tile.board .metric-icon, .temp-tile.gpu .metric-icon { background: alpha(@ck_mem, 0.16); color: @ck_mem; }
.temp-warm { color: @warning_color; }
.temp-hot { color: @error_color; }
.dot { font-size: 0.9em; }
.disk .dot { color: @ck_disk; }
.network .dot { color: @ck_net; }
levelbar.thin trough { min-height: 4px; border-radius: 2px; }
levelbar.thin block { min-height: 4px; border-radius: 2px; }
levelbar.thin block.empty { background: alpha(currentColor, 0.08); }
.cpu levelbar.thin block.filled { background: @ck_cpu; }
.memory levelbar.thin block.filled { background: @ck_mem; }
.disk levelbar.thin block.filled { background: @ck_disk; }
.network levelbar.thin block.filled { background: @ck_net; }
levelbar.thin.ok block.filled { background: @success_color; }
levelbar.thin.warm block.filled { background: @warning_color; }
levelbar.thin.hot block.filled { background: @error_color; }
.top-note { opacity: 0.55; }
.power-tile { padding: 12px 14px; }
.power-value { font-size: 1.45em; font-weight: 800; font-feature-settings: "tnum"; min-height: 32px; }
.power-tile.charger.active .power-value, .power-tile.charging .power-value { color: @ck_net; }
.power-tile.battery.active .power-value { color: @ck_disk; }
.power-tile.charger.active image, .power-tile.charging image { color: @ck_net; }
.power-tile.battery.active image { color: @ck_disk; }
.dot-battery { color: @ck_disk; }
.dot-charger { color: @ck_net; }
"""

CARD_COLORS = {"cpu": "cpu", "memory": "mem", "disk": "disk", "network": "net"}
TILE_ICONS = {"cpu": "ck-cpu-symbolic", "battery": "battery-good-symbolic", "board": "ck-board-symbolic",
              "storage": "drive-harddisk-solidstate-symbolic", "gpu": "video-display-symbolic"}


def load_css():
    display = Gdk.Display.get_default()
    if display is None:
        return
    styles = Adw.StyleManager.get_default()
    palette = Gtk.CssProvider()

    def apply_palette(*_):
        shade = int(styles.get_dark())
        palette.load_from_string("".join(f"@define-color ck_{name} {shades[shade]};"
                                         for name, shades in PALETTE.items()))

    apply_palette()
    styles.connect("notify::dark", apply_palette)
    provider = Gtk.CssProvider()
    provider.load_from_string(CSS)
    for p in (palette, provider):
        Gtk.StyleContext.add_provider_for_display(display, p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def add_icon_path():
    """Find our symbolic icons when running from the source tree.

    Installed, they live in /usr/share/icons/hicolor/symbolic/apps. From the source
    tree the folder is added as a theme root (not a plain icon folder), so GTK
    treats them as themed symbolic icons and recolors them like the built-in ones.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return
    here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "icons")
    if os.path.isdir(here):
        Gtk.IconTheme.get_for_display(display).add_search_path(here)


# --- widgets ---------------------------------------------------------------------

class Sparkline(Gtk.Widget):
    """An area chart of the last HISTORY samples, with an optional second series.

    Without a fixed top the scale snaps to round numbers (and says which), so it stays put
    instead of rescaling with every sample. None in a series is a gap: the page was closed.
    """

    def __init__(self, color, fixed_max=None, floor=1.0, height=46, scale_text=None):
        super().__init__(hexpand=True, vexpand=True)
        self.set_size_request(120, height)
        self.color, self.fixed_max, self.floor, self.scale_text = color, fixed_max, floor, scale_text
        self.a = collections.deque(maxlen=HISTORY)
        self.b = collections.deque(maxlen=HISTORY)

    def push(self, a, b=None):
        self.a.append(a)
        self.b.append(b)
        self.queue_draw()

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w < 4 or h < 4:
            return
        p = Painter(self, snapshot)
        peak = max((v for series in (self.a, self.b) for v in series if v is not None), default=0)
        top = self.fixed_max or monitor.nice_ceiling(peak, self.floor)
        step = w / (HISTORY - 1)

        def lines(series):
            out, current = [], []
            start = HISTORY - len(series)
            for i, v in enumerate(series):
                if v is None:
                    out.append(current)
                    current = []
                else:
                    current.append(((start + i) * step, h - 1 - (h - 2) * min(v, top) / top))
            out.append(current)
            return [line for line in out if len(line) >= 2]

        p.stroke([(0, h - 0.5), (w, h - 0.5)], 1, None, 0.08)
        for line in lines(self.b):
            p.stroke(line, 1.5, self.color, 0.55, dash=[3, 3])
        for line in lines(self.a):
            p.fill(line + [(line[-1][0], h), (line[0][0], h)], self.color, 0.16)
            p.stroke(line, 2, self.color)
        if self.scale_text:
            layout, _size = p.layout(self.scale_text(top), 8)
            p.text(layout, 0, 0, alpha=0.55)


def thin_bar(extra=None):
    bar = Gtk.LevelBar(min_value=0, max_value=1, css_classes=["thin"] + ([extra] if extra else []))
    for name in ("low", "high", "full"):
        bar.remove_offset_value(name)
    return bar


class AppLine(Gtk.Grid):
    def __init__(self):
        super().__init__(column_spacing=10, row_spacing=4)
        self.icon = Gtk.Image(pixel_size=18)
        self.name = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.value = Gtk.Label(xalign=1, css_classes=["dim-label", "numeric", "caption"])
        self.bar = thin_bar()
        self.attach(self.icon, 0, 0, 1, 2)
        self.attach(self.name, 1, 0, 1, 1)
        self.attach(self.value, 2, 0, 1, 1)
        self.attach(self.bar, 1, 1, 2, 1)
        self.app = None
        self.fraction = 0.0
        self.clear()

    def set(self, name, value, fraction):
        if name != self.app:
            self.app = name
            display, icon = app_identity(name)
            self.icon.set_from_gicon(icon)
            self.name.set_label(display)
            self.name.set_tooltip_text(display if display == name else f"{display} ({name})")
            self.set_opacity(1)
        self.value.set_label(value)
        fraction = max(0.0, min(1.0, fraction))
        if abs(fraction - self.fraction) >= 0.005:     # no redraw for a change nobody can see
            self.fraction = fraction
            self.bar.set_value(fraction)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{self.name.get_label()}: {value}"])

    def clear(self):
        """Blank but still taking up its space, so the card doesn't change height."""
        self.app = None
        self.set_opacity(0)
        self.icon.clear()
        self.name.set_label("")
        self.name.set_tooltip_text(None)
        self.value.set_label("")
        self.update_property([Gtk.AccessibleProperty.LABEL], [""])


class MetricCard(Gtk.Box):
    def __init__(self, kind, title, icon, legend=None, note=None, fixed_max=None, floor=1.0,
                 empty="Nothing busy right now", scale_text=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         css_classes=["card", "metric-card", kind])
        # A steady smallest width: otherwise a longer number could flip the grid's column count.
        self.set_size_request(280, -1)
        head = Gtk.Box(spacing=12)
        badge = Gtk.Box(css_classes=["metric-icon"], valign=Gtk.Align.CENTER)
        badge.append(Gtk.Image(icon_name=icon))
        head.append(badge)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, hexpand=True)
        titles.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"]))
        self.subtitle = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"],
                                  ellipsize=Pango.EllipsizeMode.END)
        titles.append(self.subtitle)
        head.append(titles)
        self.value = Gtk.Label(label="—", css_classes=["metric-value"], valign=Gtk.Align.CENTER,
                               width_chars=9, xalign=1)
        head.append(self.value)
        self.append(head)

        self.spark = Sparkline(CARD_COLORS[kind], fixed_max=fixed_max, floor=floor, scale_text=scale_text)
        self.append(self.spark)
        self.legend = []
        if legend:
            row = Gtk.Box(spacing=16)
            for i, label in enumerate(legend):
                item = Gtk.Box(spacing=6)
                item.append(Gtk.Label(label="●" if i == 0 else "◌", css_classes=["dot"]))
                text = Gtk.Label(label=f"{label} —", xalign=0, width_chars=15,
                                 css_classes=["caption", "numeric"])
                item.append(text)
                row.append(item)
                self.legend.append((label, text))
            self.append(row)

        heading = Gtk.Box(spacing=6)
        heading.append(Gtk.Label(label="Top apps", xalign=0, css_classes=["caption-heading", "dim-label"]))
        if note:
            # In a tooltip: a sentence in the card would make all four cards taller and
            # put their app lists out of line.
            info = Gtk.Image(icon_name="dialog-information-symbolic", pixel_size=12, tooltip_text=note,
                             css_classes=["top-note"])
            info.update_property([Gtk.AccessibleProperty.LABEL], [note])
            heading.append(info)
        self.append(heading)
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.lines = []
        for _ in range(3):
            line = AppLine()
            rows.append(line)
            self.lines.append(line)
        # The rows keep their space even when blank; this message sits on top of them.
        overlay = Gtk.Overlay(child=rows)
        self.empty = Gtk.Label(label=empty, xalign=0, valign=Gtk.Align.START, visible=False,
                               css_classes=["dim-label", "caption"])
        overlay.add_overlay(self.empty)
        self.append(overlay)

    def set_legend(self, *values):
        for (label, text), value in zip(self.legend, values):
            text.set_label(f"{label} {value}")

    def set_apps(self, apps):
        """apps: [(name, value text, fraction)]"""
        for i, line in enumerate(self.lines):
            if i < len(apps):
                line.set(*apps[i])
            elif line.app is not None:
                line.clear()
        self.empty.set_visible(not apps)


class TempTile(Gtk.Box):
    def __init__(self, kind, title):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                         css_classes=["card", "temp-tile", kind], accessible_role=Gtk.AccessibleRole.GROUP)
        self.kind = kind
        head = Gtk.Box(spacing=10)
        badge = Gtk.Box(css_classes=["metric-icon"], valign=Gtk.Align.CENTER)
        badge.append(Gtk.Image(icon_name=TILE_ICONS.get(kind, "ck-temperature-symbolic")))
        head.append(badge)
        head.append(Gtk.Label(label=title, xalign=0, css_classes=["heading"], hexpand=True))
        self.append(head)
        self.value = Gtk.Label(xalign=0, css_classes=["temp-value"])
        self.append(self.value)
        self.bar = thin_bar()
        self.append(self.bar)
        self.source = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
        self.append(self.source)
        self.title = title
        self.state = None
        self.average = monitor.Average(TEMP_TAU)

    def update(self, value, source, dt=0):
        if value is None:
            self.average = monitor.Average(TEMP_TAU)
        else:
            value = self.average.update(value, dt)
        state = monitor.level(self.kind, value) if value is not None else None
        if state != self.state:     # changing classes restyles the widget, so only on a change
            for widget, prefix in ((self.value, "temp-"), (self.bar, "")):
                if self.state:
                    widget.remove_css_class(prefix + self.state)
                if state:
                    widget.add_css_class(prefix + state)
            self.state = state
        if value is None:
            self.value.set_label("—")
            self.bar.set_value(0)
        else:
            self.value.set_label(f"{value:.0f} °C")
            self.bar.set_value(min(1.0, max(0.0, value / 100)))
        self.source.set_label(source)
        self.source.set_tooltip_text(source)
        words = {"ok": "normal", "warm": "warm", "hot": "hot"}.get(state)
        spoken = f"{self.title} {value:.0f} degrees, {words}" if value is not None else f"{self.title}: no reading"
        self.update_property([Gtk.AccessibleProperty.LABEL], [spoken])


class MaxWidth(Gtk.Widget):
    """Centers its child and caps its width.

    Adw.Clamp hands a FlowBox only its minimum width (libadwaita 1.5), which
    collapsed the card grid to one narrow column.
    """

    def __init__(self, child, maximum, margin=12):
        super().__init__(hexpand=True)
        self.maximum, self.margin = maximum, margin
        self.child = child
        child.set_parent(self)

    def do_get_request_mode(self):
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def _inner(self, width):
        return max(0, min(width - 2 * self.margin, self.maximum))

    def do_measure(self, orientation, for_size):
        if orientation == Gtk.Orientation.VERTICAL:
            inner = self._inner(for_size) if for_size >= 0 else -1
            minimum, natural, _b1, _b2 = self.child.measure(orientation, inner)
            return minimum, natural, -1, -1
        minimum, natural, _b1, _b2 = self.child.measure(orientation, -1)
        pad = 2 * self.margin
        return minimum + pad, min(natural, self.maximum) + pad, -1, -1

    def do_size_allocate(self, width, height, _baseline):
        inner = self._inner(width)
        point = Graphene.Point().init((width - inner) / 2, 0)
        self.child.allocate(inner, height, -1, Gsk.Transform.new().translate(point))

    def do_dispose(self):
        if self.child is not None:
            self.child.unparent()
            self.child = None


def card_grid(min_per_line, max_per_line):
    return Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                       min_children_per_line=min_per_line, max_children_per_line=max_per_line,
                       column_spacing=12, row_spacing=12, css_classes=["cards"])


# --- page ------------------------------------------------------------------------

class MonitorPage(Gtk.ScrolledWindow):
    def __init__(self):
        super().__init__(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.monitor = monitor.Monitor()
        self.timer = 0
        self.ticks = 0
        self.sampling = False
        self.last_sample = 0.0
        self.ss_busy = False
        self.ss_missing = False
        self.net_apps = []
        self.sensor_rows = {}
        self.numbers = {}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["monitor-page"],
                      margin_top=18, margin_bottom=18)
        self.set_child(MaxWidth(box, 980))

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=4)
        head.append(Gtk.Label(label="System monitor", xalign=0, css_classes=["page-title"]))
        head.append(Gtk.Label(label=f"Live, updated every {INTERVAL} seconds while this page is open",
                              xalign=0, wrap=True, css_classes=["dim-label"]))
        box.append(head)
        self.power = PowerFlow()
        box.append(self.power)

        def rate_scale(top):
            return monitor.fmt_bytes(top, rate=True)

        grid = card_grid(1, 2)
        self.cpu = MetricCard("cpu", "Processor", "ck-cpu-symbolic", fixed_max=100)
        self.mem = MetricCard("memory", "Memory", "ck-memory-symbolic", fixed_max=100)
        self.disk = MetricCard("disk", "Disk", "drive-harddisk-symbolic", legend=("Read", "Write"),
                               floor=100 * 1000, scale_text=rate_scale,
                               empty="None of your apps are using the disk",
                               note=None if os.getuid() == 0 else "Apps: only your own processes can be measured.")
        self.net = MetricCard("network", "Network", "network-transmit-receive-symbolic",
                              legend=("Down", "Up"), floor=10 * 1000, scale_text=rate_scale,
                              empty="No app traffic over TCP right now",
                              note="Apps: TCP traffic. Browsers' QUIC (UDP) traffic isn't counted per app.")
        for card in (self.cpu, self.mem, self.disk, self.net):
            grid.append(card)
        box.append(grid)

        box.append(Gtk.Label(label="Temperatures", xalign=0, css_classes=["title-4"], margin_top=10, margin_start=4))
        self.temp_grid = card_grid(1, 5)
        self.tiles = {}
        box.append(self.temp_grid)

        sensors_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.sensors_row = Adw.ExpanderRow(title="All sensors")
        self.sensors_row.add_prefix(Gtk.Image(icon_name="ck-temperature-symbolic"))
        sensors_list.append(self.sensors_row)
        box.append(sensors_list)

    # --- timer ---

    def set_active(self, active):
        if active and not self.timer:
            self._tick()
            self.timer = GLib.timeout_add_seconds(INTERVAL, self._tick)
        elif not active and self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _tick(self):
        root = self.get_root()
        if root is not None and (root.is_suspended() or not root.get_mapped()):
            return GLib.SOURCE_CONTINUE
        if self.sampling:           # the last reading is still running: never queue them up
            return GLib.SOURCE_CONTINUE
        self.ticks += 1
        fresh = time.monotonic() - self.last_sample > MAX_GAP
        temperatures = fresh or self.ticks % TEMPS_EVERY == 0
        self.sampling = True
        threading.Thread(target=self._sample, args=(fresh, temperatures), daemon=True).start()
        if self.ticks % NETWORK_EVERY == 1:
            self._run_ss()
        return GLib.SOURCE_CONTINUE

    def _sample(self, fresh, temperatures):
        """In the worker thread: everything that reads /proc and /sys, nothing that touches GTK."""
        reading = sensors = None
        try:
            if fresh:
                # Rates need two samples, and the time since the last visit would average
                # everything away. Show nothing new for a second rather than a wrong number.
                self.monitor.restart()
                self.monitor.sample()
                time.sleep(FIRST_READING)
            reading = self.monitor.sample()
            for apps in (reading["top_cpu"], reading["top_mem"], reading["top_disk"]):
                for name, _value in apps:
                    app_identity(name)      # the first lookup scans the installed apps
            if temperatures:
                found = monitor.sensors()
                bats = core.batteries()
                battery = core.battery_temperature(bats[0]) if bats else None
                sensors = (found, monitor.highlights(found, battery))
        except Exception:   # a vanished sensor must not stop the page updating
            traceback.print_exc()
        GLib.idle_add(self._show, reading, sensors, fresh)

    def _show(self, reading, sensors, fresh):
        self.sampling = False
        if self.timer:
            now = time.monotonic()
            dt = now - self.last_sample
            self.last_sample = now
            if reading:
                self.update_resources(reading, fresh, dt)
            if sensors:
                self.update_temps(*sensors, dt)
        return GLib.SOURCE_REMOVE

    # --- resources ---

    def _number(self, key, value, dt, fresh):
        """A displayed number, averaged over a few seconds so it reads instead of flickering."""
        if fresh or key not in self.numbers:
            self.numbers[key] = monitor.Average(NUMBER_TAU)
        return self.numbers[key].update(value, dt)

    def update_resources(self, r, fresh, dt):
        fmt = monitor.fmt_bytes
        if fresh:
            for card in (self.cpu, self.mem, self.disk, self.net):
                if card.spark.a:
                    card.spark.push(None)       # a gap in the line: the page wasn't watching

        cpu = self._number("cpu", r["cpu_pct"], dt, fresh)
        mhz = r["cpu_mhz"] and self._number("mhz", r["cpu_mhz"], dt, fresh)
        self.cpu.value.set_label(f"{cpu:.0f}%")
        self.cpu.subtitle.set_label(f"{r['cpu_count']} threads" + (f" · {mhz / 1000:.1f} GHz" if mhz else ""))
        self.cpu.spark.push(r["cpu_pct"])
        # Relative to the whole processor once it's busy; a quiet app on an idle laptop
        # shouldn't fill its bar just because nothing else is running.
        busy = max(cpu, 25)
        self.cpu.set_apps([(n, f"{v:.1f}%", v / busy) for n, v in r["top_cpu"]])

        total = r["mem_total"] or 1
        self.mem.value.set_label(fmt(r["mem_used"]))
        swap = f" · swap {fmt(r['swap_used'])}" if r["swap_total"] else ""
        self.mem.subtitle.set_label(f"{100 * r['mem_used'] / total:.0f}% of {fmt(total)}{swap}")
        self.mem.spark.push(100 * r["mem_used"] / total)
        self.mem.set_apps([(n, fmt(v), v / total) for n, v in r["top_mem"]])

        disk_read = self._number("disk_read", r["disk_read"], dt, fresh)
        disk_write = self._number("disk_write", r["disk_write"], dt, fresh)
        disk_total = disk_read + disk_write
        self.disk.value.set_label(fmt(disk_total, rate=True))
        self.disk.subtitle.set_label("All physical drives")
        self.disk.spark.push(r["disk_read"], r["disk_write"])
        self.disk.set_legend(fmt(disk_read, rate=True), fmt(disk_write, rate=True))
        top_disk = max([v for _, v in r["top_disk"]] + [disk_total, 1])
        self.disk.set_apps([(n, fmt(v, rate=True), v / top_disk) for n, v in r["top_disk"]])

        net_down = self._number("net_down", r["net_down"], dt, fresh)
        net_up = self._number("net_up", r["net_up"], dt, fresh)
        net_total = net_down + net_up
        self.net.value.set_label(fmt(net_total, rate=True))
        self.net.subtitle.set_label("Wi-Fi and Ethernet")
        self.net.spark.push(r["net_down"], r["net_up"])
        self.net.set_legend(fmt(net_down, rate=True), fmt(net_up, rate=True))
        top_net = max([v for _, v in self.net_apps] + [net_total, 1])
        self.net.set_apps([(n, fmt(v, rate=True), v / top_net) for n, v in self.net_apps])

    def _run_ss(self):
        if self.ss_busy or self.ss_missing:
            return
        try:
            proc = Gio.Subprocess.new(monitor.SS_COMMAND,
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            self.ss_missing = True
            return
        self.ss_busy = True
        proc.communicate_utf8_async(None, None, self._ss_done)

    def _ss_done(self, proc, result):
        self.ss_busy = False
        try:
            _ok, out, _err = proc.communicate_utf8_finish(result)
        except GLib.Error:
            return
        self.net_apps = self.monitor.network_apps(out)

    # --- temperatures ---

    def update_temps(self, all_sensors, highlights, dt=0):
        shown = set()
        for kind, title, value, source in highlights:
            tile = self.tiles.get(kind)
            if tile is None:
                tile = self.tiles[kind] = TempTile(kind, title)
                self.temp_grid.append(tile)
            tile.update(value, source, dt)
            shown.add(kind)
        for kind, tile in self.tiles.items():
            if kind not in shown:       # a sensor that went away (a sleeping GPU), not a frozen number
                tile.update(None, "No reading right now")

        self.sensors_row.set_subtitle(f"{len(all_sensors)} readings")
        seen = set()
        for s in all_sensors:
            key = s["path"]     # two identical drives have the same chip name and label
            seen.add(key)
            row = self.sensor_rows.get(key)
            if row is None:
                row = Adw.ActionRow(title=GLib.markup_escape_text(s["label"] or s["chip"]),
                                    subtitle=GLib.markup_escape_text(s["chip"] if s["label"] else s["name"]))
                row.value = Gtk.Label(css_classes=["numeric"], width_chars=6, xalign=1)
                row.state = None
                row.add_suffix(row.value)
                self.sensors_row.add_row(row)
                self.sensor_rows[key] = row
            self._set_row(row, monitor.level(s["kind"], s["value"]), f"{s['value']:.0f} °C")
        # Keep the row of a sensor that stops answering: re-adding it would reorder the list.
        for key, row in self.sensor_rows.items():
            if key not in seen:
                self._set_row(row, None, "—")

    @staticmethod
    def _set_row(row, state, text):
        if state != row.state:
            if row.state:
                row.value.remove_css_class(f"temp-{row.state}")
            if state:
                row.value.add_css_class(f"temp-{state}")
            row.state = state
        row.value.set_label(text)
