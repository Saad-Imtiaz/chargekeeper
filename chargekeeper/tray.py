"""Top-bar tray icon with a quick menu.

Implements StatusNotifierItem and com.canonical.dbusmenu directly over Gio, so it
works in the GTK 4 process without extra libraries. GNOME shows it through the
AppIndicator extension; KDE, Cinnamon, XFCE and others support it natively.
"""

import os
import time

from gi.repository import Gio, GLib

from . import core, monitor
from .cli import fmt_hours

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"
LIMITS = (60, 70, 80, 85, 90)

# GNOME extension UUIDs that provide a StatusNotifierWatcher.
EXTENSIONS = ("ubuntu-appindicators@ubuntu.com", "appindicatorsupport@rgcjonas.gmail.com")

ITEM_XML = """
<node><interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="AttentionIconName" type="s" access="read"/>
  <property name="OverlayIconName" type="s" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <property name="XAyatanaLabel" type="s" access="read"/>
  <property name="XAyatanaLabelGuide" type="s" access="read"/>
  <method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
  <method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
  <signal name="NewTitle"/>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg name="status" type="s"/></signal>
  <signal name="XAyatanaNewLabel"><arg name="label" type="s"/><arg name="guide" type="s"/></signal>
</interface></node>
"""

MENU_XML = """
<node><interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg name="parentId" type="i" direction="in"/>
    <arg name="recursionDepth" type="i" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="revision" type="u" direction="out"/>
    <arg name="layout" type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="propertyNames" type="as" direction="in"/>
    <arg name="properties" type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg name="id" type="i" direction="in"/>
    <arg name="name" type="s" direction="in"/>
    <arg name="value" type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg name="id" type="i" direction="in"/>
    <arg name="eventId" type="s" direction="in"/>
    <arg name="data" type="v" direction="in"/>
    <arg name="timestamp" type="u" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg name="events" type="a(isvu)" direction="in"/>
    <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <method name="AboutToShow">
    <arg name="id" type="i" direction="in"/>
    <arg name="needUpdate" type="b" direction="out"/>
  </method>
  <method name="AboutToShowGroup">
    <arg name="ids" type="ai" direction="in"/>
    <arg name="updatesNeeded" type="ai" direction="out"/>
    <arg name="idErrors" type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated">
    <arg name="updatedProps" type="a(ia{sv})"/>
    <arg name="removedProps" type="a(ias)"/>
  </signal>
  <signal name="LayoutUpdated"><arg name="revision" type="u"/><arg name="parent" type="i"/></signal>
  <signal name="ItemActivationRequested"><arg name="id" type="i"/><arg name="timestamp" type="u"/></signal>
</interface></node>
"""

# Stable menu item ids. Limit radios use LIMIT_BASE + percent.
ID_STATUS, ID_HEADER, ID_NO_LIMIT, ID_FULL, ID_OPEN, ID_QUIT = 1, 2, 3, 4, 5, 6
ID_SEPARATORS = (90, 91, 92, 93)
LIMIT_BASE = 100

# System monitor timing. Nothing is sampled while the menu is closed. Opening it
# measures straight away when the last reading is recent, otherwise it starts a
# new measurement and fills in the numbers a second later. While the menu stays
# open the numbers refresh every LIVE_SECONDS.
FRESH_SECONDS = 10
FIRST_READING_MS = 1000
LIVE_SECONDS = 2
LIVE_LIMIT_SECONDS = 300      # stop if the host never reports the menu closing
# System monitor section: each row's submenu holds its top apps at row id + 1..3.
ID_MON_HEADER, ID_MEASURING = 300, 301
ID_CPU, ID_MEM, ID_DISK, ID_NET, ID_TEMPS, ID_OPEN_MONITOR = 310, 320, 330, 340, 350, 360
TEMP_ICONS = {"cpu": "ck-cpu-symbolic", "battery": "battery-good-symbolic", "board": "ck-board-symbolic",
              "storage": "drive-harddisk-solidstate-symbolic", "gpu": "video-display-symbolic"}


def menu_text(text):
    """dbusmenu treats '_' as a mnemonic marker."""
    return text.replace("_", "__")


def app_icon(name):
    """('icon-name', str) or ('icon-data', PNG bytes) for a menu row."""
    icon = monitor.app_identity(name)[1]
    if isinstance(icon, Gio.ThemedIcon):
        return "icon-name", icon.get_names()[0]
    if isinstance(icon, Gio.FileIcon) and icon.get_file().get_path():
        try:
            from gi.repository import GdkPixbuf
            pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(icon.get_file().get_path(), 16, 16)
            ok, data = pixbuf.save_to_bufferv("png", [], [])
            if ok:
                return "icon-data", bytes(data)
        except (GLib.Error, ImportError, ValueError):
            pass
    return "icon-name", "application-x-executable-symbolic"

def battery_icon(info):
    if not info or info["capacity"] is None:
        return "battery-missing-symbolic"
    level = min(100, max(0, round(info["capacity"] / 10) * 10))
    if info["ac"]:
        return "battery-level-100-charged-symbolic" if level == 100 else f"battery-level-{level}-charging-symbolic"
    return f"battery-level-{level}-symbolic"


def status_line(info, backend, config):
    if not info or info["capacity"] is None:
        return "No battery found"
    cap = info["capacity"]
    limit = backend.limit() if backend.available else None
    if config["calibrating"]:
        return f"{cap}% · Calibrating"
    if info["status"] == "Charging":
        target = 100 if config["top_up"] else (limit or 100)
        return f"{cap}% · Charging to {target}%"
    if info["status"] == "Discharging":
        remaining = core.time_remaining(info)
        return f"{cap}% · On battery" + (f", {fmt_hours(remaining)} left" if remaining else "")
    if info["ac"] and limit and limit < 100:
        if cap > limit + 2:
            return f"{cap}% · Plugged in, above the {limit}% limit"
        return f"{cap}% · Plugged in, held at {limit}%"
    if info["ac"]:
        return f"{cap}% · Plugged in"
    return f"{cap}% · {info['status']}"


class Tray:
    def __init__(self, app):
        self.app = app
        # A connection of our own: method calls are answered from a message filter on it
        # rather than with register_object(). With GLib < 2.84 (Ubuntu 24.04 has 2.80),
        # PyGObject's register_object leaks every incoming call (about 4 KB each), and
        # the shell calls the menu many times a day.
        self.bus = Gio.DBusConnection.new_for_address_sync(
            Gio.dbus_address_get_for_bus_sync(Gio.BusType.SESSION),
            Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, None)
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.revision = 1
        self.items = {}           # id -> props
        self.children = {0: []}   # id -> child ids
        self.handlers = {}        # id -> callable
        self.icon, self.label, self.tooltip = "battery-missing-symbolic", "", ""
        self.host_available = False
        # Readings for the System monitor section, taken only around menu openings
        # (see FRESH_SECONDS); temperatures likewise, since SSD sensors wake the drive.
        self.monitor = monitor.Monitor()
        self.stats = None
        self.stats_time = 0
        self.net_apps = []
        self.temps = []
        self.app_icons = {}
        self.menu_open = False
        self.first_pending = False    # "Measuring…" is showing; its replacement may rebuild
        self.layout_pending = False
        self.live_source = 0
        self.live_ticks = 0
        self.ss_busy = False

        self.interfaces = {
            ITEM_PATH: Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0],
            MENU_PATH: Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0],
        }
        self.filter_id = self.bus.add_filter(self._filter)
        self.own_id = Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE,
                                                      None, None)
        self.watch_id = Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                          self._watcher_appeared, self._watcher_vanished)
        self.update()

    def close(self):
        self._stop_live()
        Gio.bus_unwatch_name(self.watch_id)
        Gio.bus_unown_name(self.own_id)
        self.bus.remove_filter(self.filter_id)
        self.bus.close(None, None, None)

    # --- watcher ---

    def _watcher_appeared(self, bus, _name, _owner):
        bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                 GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                 self._registered)

    def _registered(self, bus, result):
        try:
            bus.call_finish(result)
            self.host_available = True
            # GNOME's AppIndicator extension only picks the label up from the change
            # signal, never from the initial property read, so announce it again once
            # the host has had a moment to set up its proxy.
            for delay in (1, 4):
                GLib.timeout_add_seconds(delay, self._announce_label)
        except GLib.Error as exc:
            print(f"Tray: cannot register with {WATCHER}: {exc.message}", flush=True)
            self.host_available = False
        self.app.tray_changed()

    def _announce_label(self):
        # The extension caches the label from its first property read but only shows
        # it on a change, and it drops updates equal to the cached value. Blank it and
        # set it again so the label appears even though the text hasn't changed.
        self._emit_item("XAyatanaNewLabel", GLib.Variant("(ss)", ("", "")))
        self._emit_item("XAyatanaNewLabel", GLib.Variant("(ss)", (self.label, "100%")))
        return GLib.SOURCE_REMOVE

    def _watcher_vanished(self, _bus, _name):
        self.host_available = False
        self.app.tray_changed()

    # --- state ---

    def _measure(self, temperatures=False):
        """Take a reading: rates since the previous one (None for the very first)."""
        result = self.monitor.sample()
        self.stats = result if self.stats_time else None
        self.stats_time = time.monotonic()
        self._run_ss()
        if temperatures:
            bats = core.batteries()
            battery = core.battery_temperature(bats[0]) if bats else None
            self.temps = monitor.highlights(monitor.sensors(), battery)

    def _run_ss(self):
        if self.ss_busy:
            return
        try:
            proc = Gio.Subprocess.new(monitor.SS_COMMAND,
                                      Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
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

    def menu_opening(self):
        """Called from AboutToShow on the root menu."""
        age = time.monotonic() - self.stats_time
        if not self.stats_time or age > FRESH_SECONDS:
            # Too old to be meaningful: start a new measurement and fill it in shortly.
            self.stats, self.stats_time = None, 0
            self._measure(temperatures=True)
            self.first_pending = True
            GLib.timeout_add(FIRST_READING_MS, self._first_reading)
        elif age >= 0.5:
            self._measure(temperatures=True)

    def _first_reading(self):
        self._measure()
        self.update()
        self.first_pending = False
        return GLib.SOURCE_REMOVE

    def _start_live(self):
        if not self.live_source:
            self.live_ticks = 0
            self.live_source = GLib.timeout_add_seconds(LIVE_SECONDS, self._live_tick)

    def _stop_live(self):
        if self.live_source:
            GLib.source_remove(self.live_source)
            self.live_source = 0

    def _live_tick(self):
        self.live_ticks += 1
        if not self.menu_open or self.live_ticks * LIVE_SECONDS > LIVE_LIMIT_SECONDS:
            self.live_source = 0
            return GLib.SOURCE_REMOVE
        self._measure(temperatures=self.live_ticks % 2 == 0)
        self.update()
        return GLib.SOURCE_CONTINUE

    def _icon_for(self, name):
        if name not in self.app_icons:
            self.app_icons[name] = app_icon(name)
        key, value = self.app_icons[name]
        return {key: value}

    def update(self, opening=False):
        """Re-read the battery and rebuild icon, label and menu; emit only what changed.

        opening: called from AboutToShow, where the host expects a new layout.
        """
        info = core.combined_info()
        backend = core.detect()
        config = core.load_config()

        icon = battery_icon(info)
        label = f"{info['capacity']}%" if info and info["capacity"] is not None else ""
        tooltip = status_line(info, backend, config)
        if icon != self.icon:
            self.icon = icon
            self._emit_item("NewIcon")
        if label != self.label:
            self.label = label
            self._emit_item("XAyatanaNewLabel", GLib.Variant("(ss)", (label, "100%")))
        if tooltip != self.tooltip:
            self.tooltip = tooltip
            self._emit_item("NewToolTip")

        items, children, handlers = self._build_menu(info, backend, config, tooltip)
        # Replacing "Measuring…" right after opening may rebuild the menu; any later
        # structural change waits for it to close.
        opening = opening or self.first_pending
        if children != self.children and self.menu_open and not opening:
            # A new layout rebuilds the open menu, collapsing submenus and moving rows
            # under the pointer; wait until it closes.
            self.layout_pending = True
            return
        if children != self.children or (items != self.items and (opening or not self.menu_open)):
            self.items, self.children = items, children
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                       GLib.Variant("(ui)", (self.revision, 0)))
        elif items != self.items:
            # Same structure while open: update the changed rows in place.
            changed = [i for i in items if items[i] != self.items.get(i)]
            self.items = items
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "ItemsPropertiesUpdated",
                       GLib.Variant("(a(ia{sv})a(ias))", ([(i, self._props(i)) for i in changed], [])))
        self.handlers = handlers

    def _build_menu(self, info, backend, config, status):
        items, order, handlers = {}, [], {}

        def add(item_id, handler=None, **props):
            items[item_id] = props
            order.append(item_id)
            if handler:
                handlers[item_id] = handler

        add(ID_STATUS, label=status, enabled=False)
        add(ID_SEPARATORS[0], type="separator")

        if backend.available:
            editable = not config["calibrating"]
            enabled = config["limit_enabled"]
            current = backend.snap(config["end"]) if enabled else 100
            add(ID_HEADER, label="Charge limit", enabled=False)
            if backend.continuous:
                values = sorted({v for v in LIMITS if v >= backend.min_end}
                                | ({current} if current < 100 else set()))
                labels = {v: f"{v}%" for v in values}
            else:
                fixed = [s for s in backend.steps if s < 100]
                values = fixed
                labels = {v: (backend.fixed_text or f"{v}%") for v in fixed}
            for value in values:
                add(LIMIT_BASE + value, lambda v=value: self._set_limit(v),
                    label=labels[value] + (" (recommended)" if value == 80 else ""),
                    **{"toggle-type": "radio", "toggle-state": int(enabled and current == value),
                       "enabled": editable})
            add(ID_NO_LIMIT, lambda: self._set_limit(None), label="No limit (100%)",
                **{"toggle-type": "radio", "toggle-state": int(not enabled), "enabled": editable})
            add(ID_SEPARATORS[1], type="separator")
            add(ID_FULL, self._toggle_full, label="Charge to full once",
                **{"toggle-type": "checkmark", "toggle-state": int(config["top_up"]),
                   "enabled": editable and (enabled or config["top_up"])})
        else:
            add(ID_HEADER, label="This laptop can't limit charging", enabled=False)
        add(ID_SEPARATORS[2], type="separator")
        children = {0: order}
        self._monitor_section(add, children)
        add(ID_SEPARATORS[3], type="separator")
        add(ID_OPEN, self.app.activate, label="Open Chargekeeper")
        add(ID_QUIT, self.app.quit, label="Quit")
        # Submenu rows were added in sequence; move them under their parent.
        for parent, kids in list(children.items()):
            if parent:
                for kid in kids:
                    order.remove(kid)
                items[parent]["children-display"] = "submenu"
        return items, children, handlers

    def _monitor_section(self, add, children):
        """System monitor rows; each opens a submenu with the top three apps."""
        fmt = monitor.fmt_bytes
        add(ID_MON_HEADER, label="System monitor", enabled=False)
        r = self.stats
        if r is None:
            add(ID_MEASURING, label="Measuring…", enabled=False)
        else:
            mem_pct = 100 * r["mem_used"] / (r["mem_total"] or 1)
            rows = (
                (ID_CPU, "ck-cpu-symbolic", f"Processor · {r['cpu_pct']:.0f}%",
                 [(n, f"{v:.1f}%") for n, v in r["top_cpu"] if v >= 0.05]),
                (ID_MEM, "ck-memory-symbolic", f"Memory · {fmt(r['mem_used'])} of {fmt(r['mem_total'])} ({mem_pct:.0f}%)",
                 [(n, fmt(v)) for n, v in r["top_mem"]]),
                (ID_DISK, "drive-harddisk-symbolic", f"Disk · {fmt(r['disk_read'] + r['disk_write'], rate=True)}",
                 [(n, fmt(v, rate=True)) for n, v in r["top_disk"]]),
                (ID_NET, "network-transmit-receive-symbolic",
                 f"Network · ↓ {fmt(r['net_down'], rate=True)}  ↑ {fmt(r['net_up'], rate=True)}",
                 [(n, fmt(v, rate=True)) for n, v in self.net_apps]),
            )
            open_monitor = lambda: self.app.show_page("monitor")  # noqa: E731
            for row_id, icon, label, apps in rows:
                if not apps:
                    # GNOME greys out a submenu holding only disabled rows, so an idle
                    # category is a plain row that opens the Monitor tab instead.
                    add(row_id, open_monitor, label=menu_text(label), **{"icon-name": icon})
                    continue
                add(row_id, label=menu_text(label), **{"icon-name": icon})
                kids = []
                for i, (name, value) in enumerate(apps[:3], start=1):
                    display = monitor.app_identity(name)[0]
                    add(row_id + i, open_monitor, label=menu_text(f"{display} · {value}"), **self._icon_for(name))
                    kids.append(row_id + i)
                children[row_id] = kids
        if self.temps:
            readings = [(kind, title, value) for kind, title, value, _src in self.temps if value is not None]
            short = " · ".join(f"{title.replace('Motherboard', 'Board')} {value:.0f}°"
                               for kind, title, value in readings if kind in ("cpu", "battery", "board"))
            add(ID_TEMPS, label=menu_text(f"Temperatures · {short}" if short else "Temperatures"),
                **{"icon-name": "ck-temperature-symbolic"})
            kids = []
            for i, (kind, title, value, source) in enumerate(self.temps, start=1):
                text = f"{title} · {value:.0f} °C" if value is not None else f"{title} · not reported"
                add(ID_TEMPS + i, label=menu_text(text), **{"icon-name": TEMP_ICONS.get(kind, "ck-temperature-symbolic")})
                kids.append(ID_TEMPS + i)
            children[ID_TEMPS] = kids
        add(ID_OPEN_MONITOR, lambda: self.app.show_page("monitor"), label="Open System Monitor…")

    # --- actions ---

    def _set_limit(self, value):
        if value is None:
            args = ["--limit-enabled", "off", "--top-up", "off"]
        else:
            args = ["--limit-enabled", "on", "--top-up", "off", "--end", str(value)]
        self.app.run_helper(["set", *args], lambda ok: self.app.settings_changed())

    def _toggle_full(self):
        on = not core.load_config()["top_up"]
        self.app.run_helper(["set", "--top-up", "on" if on else "off"],
                            lambda ok: self.app.settings_changed())

    # --- D-Bus plumbing ---

    def _emit(self, path, iface, signal, params=None):
        try:
            self.bus.emit_signal(None, path, iface, signal, params)
        except GLib.Error:
            pass

    def _emit_item(self, signal, params=None):
        self._emit(ITEM_PATH, "org.kde.StatusNotifierItem", signal, params)

    def _filter(self, _bus, message, incoming):
        """Runs on GDBus's worker thread: hand our method calls to the main loop."""
        if (incoming and message.get_message_type() == Gio.DBusMessageType.METHOD_CALL
                and message.get_path() in self.interfaces):
            GLib.idle_add(self._dispatch, message)
            return None
        return message

    def _dispatch(self, message):
        path, iface, method = message.get_path(), message.get_interface(), message.get_member()
        body = message.get_body()
        args = body.unpack() if body is not None else ()
        try:
            if iface == "org.freedesktop.DBus.Properties":
                reply = self._properties(path, method, args)
            elif iface == "org.freedesktop.DBus.Introspectable" or method == "Introspect":
                reply = GLib.Variant("(s)", (self._introspect(path),))
            elif iface == "org.freedesktop.DBus.Peer":
                machine = (core.read("/etc/machine-id") or "").strip()
                reply = GLib.Variant("(s)", (machine,)) if method == "GetMachineId" else None
            elif path == ITEM_PATH:
                reply = self._item_method(method)
            else:
                reply = self._menu_method(method, args)
        except LookupError as exc:
            error = Gio.DBusMessage.new_method_error_literal(
                message, "org.freedesktop.DBus.Error.UnknownMethod", str(exc))
            self.bus.send_message(error, Gio.DBusSendMessageFlags.NONE)
            return GLib.SOURCE_REMOVE
        if not message.get_flags() & Gio.DBusMessageFlags.NO_REPLY_EXPECTED:
            out = Gio.DBusMessage.new_method_reply(message)
            if reply is not None:
                out.set_body(reply)
            self.bus.send_message(out, Gio.DBusSendMessageFlags.NONE)
        return GLib.SOURCE_REMOVE

    def _introspect(self, path):
        own = ITEM_XML if path == ITEM_PATH else MENU_XML
        inner = own.strip().removeprefix("<node>").removesuffix("</node>")
        return ("<node>" + inner +
                '<interface name="org.freedesktop.DBus.Properties">'
                '<method name="Get"><arg type="s" direction="in"/><arg type="s" direction="in"/>'
                '<arg type="v" direction="out"/></method>'
                '<method name="GetAll"><arg type="s" direction="in"/><arg type="a{sv}" direction="out"/></method>'
                '</interface><interface name="org.freedesktop.DBus.Introspectable">'
                '<method name="Introspect"><arg type="s" direction="out"/></method></interface></node>')

    def _property_values(self, path):
        if path == MENU_PATH:
            return {
                "Version": GLib.Variant("u", 3),
                "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"),
                "IconThemePath": GLib.Variant("as", []),
            }
        return {
            "Category": GLib.Variant("s", "Hardware"),
            "Id": GLib.Variant("s", "chargekeeper"),
            "Title": GLib.Variant("s", "Chargekeeper"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon),
            "IconThemePath": GLib.Variant("s", ""),
            "AttentionIconName": GLib.Variant("s", ""),
            "OverlayIconName": GLib.Variant("s", ""),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon, [], "Chargekeeper", self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", True),
            "Menu": GLib.Variant("o", MENU_PATH),
            "XAyatanaLabel": GLib.Variant("s", self.label),
            "XAyatanaLabelGuide": GLib.Variant("s", "100%"),
        }

    def _properties(self, path, method, args):
        values = self._property_values(path)
        if method == "GetAll":
            return GLib.Variant("(a{sv})", (values,))
        if method == "Get" and args[1] in values:
            return GLib.Variant("(v)", (values[args[1]],))
        raise LookupError(f"{method} {args[1] if len(args) > 1 else ''}")

    def _item_method(self, method):
        if method in ("Activate", "SecondaryActivate"):
            self.app.activate()
        elif method not in ("ContextMenu", "Scroll"):
            raise LookupError(method)
        return None

    def _props(self, item_id):
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        # Always state enabled/visible: GNOME's extension keeps a stale "enabled: false"
        # when a later layout simply leaves the key out (which means the default).
        props = self.items.get(item_id, {})
        out = {"enabled": GLib.Variant("b", True), "visible": GLib.Variant("b", True)}
        if props.get("type") == "separator":
            out.pop("enabled")
        for key, value in props.items():
            kind = ("b" if isinstance(value, bool) else "i" if isinstance(value, int)
                    else "ay" if isinstance(value, bytes) else "s")
            out[key] = GLib.Variant(kind, value)
        return out

    def _layout(self, item_id, depth):
        kids = self.children.get(item_id, []) if depth != 0 else []
        return (item_id, self._props(item_id),
                [GLib.Variant("(ia{sv}av)", self._layout(k, depth - 1)) for k in kids])

    def _click(self, item_id, event):
        if item_id == 0 and event in ("opened", "closed"):
            self.menu_open = event == "opened"
            if self.menu_open:
                self._start_live()
            else:
                self._stop_live()
            if not self.menu_open and self.layout_pending:
                self.layout_pending = False
                GLib.idle_add(lambda: self.update() and False)
            return
        handler = self.handlers.get(item_id)
        if event == "clicked" and handler:
            # Leave the D-Bus call first; the handler may start pkexec or open a window.
            GLib.idle_add(lambda: handler() and False)

    def _menu_method(self, method, args):
        if method == "GetLayout":
            parent, depth, _names = args
            return GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, depth)))
        if method == "GetGroupProperties":
            ids = args[0] or [0, *self.items]
            return GLib.Variant("(a(ia{sv}))", ([(i, self._props(i)) for i in ids],))
        if method == "GetProperty":
            item_id, name = args
            return GLib.Variant("(v)", (self._props(item_id).get(name, GLib.Variant("s", "")),))
        if method == "Event":
            self._click(args[0], args[1])
            return None
        if method == "EventGroup":
            for item_id, event, _data, _ts in args[0]:
                self._click(item_id, event)
            return GLib.Variant("(ai)", ([],))
        if method == "AboutToShow":
            before = self.revision
            if args[0] == 0:
                self.menu_opening()
            self.update(opening=True)   # fresh numbers each time the menu opens
            return GLib.Variant("(b)", (self.revision != before,))
        if method == "AboutToShowGroup":
            if 0 in args[0]:
                self.menu_opening()
            self.update(opening=True)
            return GLib.Variant("(aiai)", ([], []))
        raise LookupError(method)


def extension_to_enable():
    """UUID of an installed but disabled GNOME tray extension, if any."""
    if "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", ""):
        return None
    dirs = ["/usr/share/gnome-shell/extensions",
            os.path.join(GLib.get_user_data_dir(), "gnome-shell", "extensions")]
    for uuid in EXTENSIONS:
        if any(os.path.isdir(os.path.join(d, uuid)) for d in dirs):
            return uuid
    return None
