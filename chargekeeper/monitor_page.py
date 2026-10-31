"""The Monitor page: live CPU, memory, disk and network with top apps, plus temperatures.

Sampling runs only while the page is on screen, every INTERVAL seconds.
"""

import collections
import os

from gi.repository import Adw, Gdk, Gio, GLib, Graphene, Gsk, Gtk, Pango

from . import core, monitor
from .monitor import app_identity
from .gui import AMBER, BLUE, GREEN, PURPLE, Painter

INTERVAL = 2
HISTORY = 60                 # samples kept for the sparklines (2 minutes)
NETWORK_EVERY = 3            # run `ss` every third tick
TEMPS_EVERY = 2

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
"""

# Category colours: deeper shades on light backgrounds, lighter ones on dark. Plain
# hex values because distributions restyle the named palette (Ubuntu's purple is
# aubergine, which disappears on a dark card).
PALETTE = {
    False: "@define-color ck_cpu #1c71d8; @define-color ck_mem #9141ac;"
           "@define-color ck_disk #e66100; @define-color ck_net #26a269;",
    True: "@define-color ck_cpu #62a0ea; @define-color ck_mem #c061cb;"
          "@define-color ck_disk #ffa348; @define-color ck_net #57e389;",
}

CARD_COLORS = {"cpu": BLUE, "memory": PURPLE, "disk": AMBER, "network": GREEN}
TILE_ICONS = {"cpu": "ck-cpu-symbolic", "battery": "battery-good-symbolic", "board": "ck-board-symbolic",
              "storage": "drive-harddisk-solidstate-symbolic", "gpu": "video-display-symbolic"}


def load_css():
    display = Gdk.Display.get_default()
    if display is None:
        return
    styles = Adw.StyleManager.get_default()
    palette = Gtk.CssProvider()

    def apply_palette(*_):
        palette.load_from_string(PALETTE[styles.get_dark()])

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
    """A minimal area chart of the last HISTORY samples, with an optional second series."""

    def __init__(self, color, fixed_max=None, floor=1.0, height=46):
        super().__init__(hexpand=True)
        self.set_size_request(120, height)
        self.color, self.fixed_max, self.floor = color, fixed_max, floor
        self.a = collections.deque(maxlen=HISTORY)
        self.b = collections.deque(maxlen=HISTORY)

    def push(self, a, b=None):
        self.a.append(a)
        if b is not None:
            self.b.append(b)
        self.queue_draw()

    def do_snapshot(self, snapshot):
        w, h = self.get_width(), self.get_height()
        if w < 4 or h < 4:
            return
        p = Painter(self, snapshot)
        top = self.fixed_max or max(self.floor, max(self.a, default=0) * 1.15, max(self.b, default=0) * 1.15)
        step = w / (HISTORY - 1)

        def points(series):
            start = HISTORY - len(series)
            return [((start + i) * step, h - 1 - (h - 2) * min(v, top) / top) for i, v in enumerate(series)]

        p.stroke([(0, h - 0.5), (w, h - 0.5)], 1, None, 0.08)
        if len(self.b) >= 2:
            p.stroke(points(self.b), 1.5, self.color, 0.55, dash=[3, 3])
        if len(self.a) >= 2:
            line = points(self.a)
            p.fill(line + [(line[-1][0], h), (line[0][0], h)], self.color, 0.16)
            p.stroke(line, 2, self.color)


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

    def set(self, name, value, fraction):
        display, icon = app_identity(name)
        self.icon.set_from_gicon(icon)
        self.name.set_label(display)
        self.name.set_tooltip_text(display if display == name else f"{display} ({name})")
        self.value.set_label(value)
        self.bar.set_value(max(0.0, min(1.0, fraction)))
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{display}: {value}"])


class MetricCard(Gtk.Box):
    def __init__(self, kind, title, icon, legend=None, note=None, fixed_max=None, floor=1.0,
                 empty="Nothing busy right now"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         css_classes=["card", "metric-card", kind])
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
        self.value = Gtk.Label(css_classes=["metric-value"], valign=Gtk.Align.CENTER)
        head.append(self.value)
        self.append(head)

        self.spark = Sparkline(CARD_COLORS[kind], fixed_max=fixed_max, floor=floor)
        self.append(self.spark)
        self.legend = []
        if legend:
            row = Gtk.Box(spacing=16)
            for i, label in enumerate(legend):
                item = Gtk.Box(spacing=6)
                item.append(Gtk.Label(label="●" if i == 0 else "◌", css_classes=["dot"]))
                text = Gtk.Label(xalign=0, css_classes=["caption", "numeric"])
                item.append(text)
                row.append(item)
                self.legend.append((label, text))
            self.append(row)

        self.append(Gtk.Label(label="Top apps", xalign=0, css_classes=["caption-heading", "dim-label"]))
        self.lines = []
        for _ in range(3):
            line = AppLine()
            self.append(line)
            self.lines.append(line)
        self.empty = Gtk.Label(label=empty, xalign=0, css_classes=["dim-label", "caption"])
        self.append(self.empty)
        if note:
            # A wrapping label asks for its full sentence as natural width, which would
            # make every card want a whole row; cap it so two cards fit side by side.
            self.append(Gtk.Label(label=note, xalign=0, wrap=True, max_width_chars=30, width_chars=20,
                                  css_classes=["dim-label", "caption"]))

    def set_legend(self, *values):
        for (label, text), value in zip(self.legend, values):
            text.set_label(f"{label} {value}")

    def set_apps(self, apps):
        """apps: [(name, value text, fraction)]"""
        for i, line in enumerate(self.lines):
            line.set_visible(i < len(apps))
            if i < len(apps):
                line.set(*apps[i])
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

    def update(self, value, source):
        state = monitor.level(self.kind, value)
        for name in ("ok", "warm", "hot"):
            self.value.remove_css_class(f"temp-{name}")
            self.bar.remove_css_class(name)
        if value is None:
            self.value.set_label("—")
            self.bar.set_value(0)
        else:
            self.value.set_label(f"{value:.0f} °C")
            self.value.add_css_class(f"temp-{state}")
            self.bar.add_css_class(state)
            self.bar.set_value(min(1.0, max(0.0, value / 100)))
        self.source.set_label(source)
        self.source.set_tooltip_text(source)
        words = {"ok": "normal", "warm": "warm", "hot": "hot"}[state]
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
        self.ss_busy = False
        self.ss_missing = False
        self.net_apps = []
        self.sensor_rows = {}

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, css_classes=["monitor-page"],
                      margin_top=18, margin_bottom=18)
        self.set_child(MaxWidth(box, 980))

        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_start=4)
        head.append(Gtk.Label(label="System monitor", xalign=0, css_classes=["page-title"]))
        head.append(Gtk.Label(label=f"Live, updated every {INTERVAL} seconds while this page is open",
                              xalign=0, wrap=True, css_classes=["dim-label"]))
        box.append(head)

        grid = card_grid(1, 2)
        self.cpu = MetricCard("cpu", "Processor", "ck-cpu-symbolic", fixed_max=100)
        self.mem = MetricCard("memory", "Memory", "ck-memory-symbolic", fixed_max=100)
        self.disk = MetricCard("disk", "Disk", "drive-harddisk-symbolic", legend=("Read", "Write"),
                               floor=64 * 1024, empty="None of your apps are using the disk",
                               note=None if os.getuid() == 0 else "Apps: only your own processes can be measured.")
        self.net = MetricCard("network", "Network", "network-transmit-receive-symbolic",
                              legend=("Down", "Up"), floor=16 * 1024,
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

        self.update_temps()

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
        self.ticks += 1
        self.update_resources()
        if self.ticks % TEMPS_EVERY == 1:
            self.update_temps()
        if self.ticks % NETWORK_EVERY == 1:
            self._run_ss()
        return GLib.SOURCE_CONTINUE

    # --- resources ---

    def update_resources(self):
        r = self.monitor.sample()
        fmt = monitor.fmt_bytes

        mhz = r["cpu_mhz"]
        self.cpu.value.set_label(f"{r['cpu_pct']:.0f}%")
        self.cpu.subtitle.set_label(f"{r['cpu_count']} threads" + (f" · {mhz / 1000:.1f} GHz" if mhz else ""))
        self.cpu.spark.push(r["cpu_pct"])
        busy = max(r["cpu_pct"], 0.1)
        self.cpu.set_apps([(n, f"{v:.1f}%", v / busy) for n, v in r["top_cpu"] if v >= 0.05])

        total = r["mem_total"] or 1
        self.mem.value.set_label(fmt(r["mem_used"]))
        swap = f" · swap {fmt(r['swap_used'])}" if r["swap_total"] else ""
        self.mem.subtitle.set_label(f"{100 * r['mem_used'] / total:.0f}% of {fmt(total)}{swap}")
        self.mem.spark.push(100 * r["mem_used"] / total)
        self.mem.set_apps([(n, fmt(v), v / total) for n, v in r["top_mem"]])

        disk_total = r["disk_read"] + r["disk_write"]
        self.disk.value.set_label(fmt(disk_total, rate=True))
        self.disk.subtitle.set_label("All physical drives")
        self.disk.spark.push(r["disk_read"], r["disk_write"])
        self.disk.set_legend(fmt(r["disk_read"], rate=True), fmt(r["disk_write"], rate=True))
        top_disk = max([v for _, v in r["top_disk"]] + [disk_total, 1])
        self.disk.set_apps([(n, fmt(v, rate=True), v / top_disk) for n, v in r["top_disk"]])

        net_total = r["net_down"] + r["net_up"]
        self.net.value.set_label(fmt(net_total, rate=True))
        self.net.subtitle.set_label("Wi-Fi and Ethernet")
        self.net.spark.push(r["net_down"], r["net_up"])
        self.net.set_legend(fmt(r["net_down"], rate=True), fmt(r["net_up"], rate=True))
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

    def update_temps(self):
        all_sensors = monitor.sensors()
        bats = core.batteries()
        battery = core.battery_temperature(bats[0]) if bats else None
        for kind, title, value, source in monitor.highlights(all_sensors, battery):
            tile = self.tiles.get(kind)
            if tile is None:
                tile = self.tiles[kind] = TempTile(kind, title)
                self.temp_grid.append(tile)
            tile.update(value, source)

        self.sensors_row.set_subtitle(f"{len(all_sensors)} readings")
        seen = set()
        for s in all_sensors:
            key = (s["chip"], s["label"], s["name"])
            seen.add(key)
            row = self.sensor_rows.get(key)
            if row is None:
                row = Adw.ActionRow(title=GLib.markup_escape_text(s["label"] or s["chip"]),
                                    subtitle=GLib.markup_escape_text(s["chip"] if s["label"] else s["name"]))
                row.value = Gtk.Label(css_classes=["numeric"])
                row.add_suffix(row.value)
                self.sensors_row.add_row(row)
                self.sensor_rows[key] = row
            state = monitor.level(s["kind"], s["value"])
            for name in ("ok", "warm", "hot"):
                row.value.remove_css_class(f"temp-{name}")
            row.value.add_css_class(f"temp-{state}")
            row.value.set_label(f"{s['value']:.0f} °C")
        for key in list(self.sensor_rows):
            if key not in seen:
                self.sensors_row.remove(self.sensor_rows.pop(key))
