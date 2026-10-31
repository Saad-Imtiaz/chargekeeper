"""Top-bar tray icon with a quick menu.

Implements StatusNotifierItem and com.canonical.dbusmenu directly over Gio, so it
works in the GTK 4 process without extra libraries. GNOME shows it through the
AppIndicator extension; KDE, Cinnamon, XFCE and others support it natively.
"""

import os

from gi.repository import Gio, GLib

from . import core
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
ID_SEPARATORS = (90, 91, 92)
LIMIT_BASE = 100


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
        self.bus = app.get_dbus_connection()
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self.revision = 1
        self.items = {}           # id -> props
        self.children = {0: []}   # id -> child ids
        self.handlers = {}        # id -> callable
        self.icon, self.label, self.tooltip = "battery-missing-symbolic", "", ""
        self.host_available = False
        self.registrations = []

        item_info = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu_info = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.registrations = [
            self.bus.register_object(ITEM_PATH, item_info, self._item_call, self._item_property, None),
            self.bus.register_object(MENU_PATH, menu_info, self._menu_call, self._menu_property, None),
        ]
        self.own_id = Gio.bus_own_name_on_connection(self.bus, self.name, Gio.BusNameOwnerFlags.NONE,
                                                      None, None)
        self.watch_id = Gio.bus_watch_name_on_connection(self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE,
                                                          self._watcher_appeared, self._watcher_vanished)
        self.update()

    def close(self):
        Gio.bus_unwatch_name(self.watch_id)
        Gio.bus_unown_name(self.own_id)
        for reg in self.registrations:
            self.bus.unregister_object(reg)
        self.registrations = []

    # --- watcher ---

    def _watcher_appeared(self, bus, _name, _owner):
        bus.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                 GLib.Variant("(s)", (self.name,)), None, Gio.DBusCallFlags.NONE, -1, None,
                 self._registered)

    def _registered(self, bus, result):
        try:
            bus.call_finish(result)
            self.host_available = True
        except GLib.Error as exc:
            print(f"Tray: cannot register with {WATCHER}: {exc.message}", flush=True)
            self.host_available = False
        self.app.tray_changed()

    def _watcher_vanished(self, _bus, _name):
        self.host_available = False
        self.app.tray_changed()

    # --- state ---

    def update(self):
        """Re-read the battery and rebuild icon, label and menu; emit only what changed."""
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
        if items != self.items or children != self.children:
            self.items, self.children = items, children
            self.revision += 1
            self._emit(MENU_PATH, "com.canonical.dbusmenu", "LayoutUpdated",
                       GLib.Variant("(ui)", (self.revision, 0)))
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
        add(ID_OPEN, self.app.activate, label="Open Chargekeeper")
        add(ID_QUIT, self.app.quit, label="Quit")
        return items, {0: order}, handlers

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

    def _item_property(self, _bus, _sender, _path, _iface, name):
        values = {
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
        return values.get(name)

    def _item_call(self, _bus, _sender, _path, _iface, method, _params, invocation):
        if method in ("Activate", "SecondaryActivate"):
            self.app.activate()
        invocation.return_value(None)

    def _menu_property(self, _bus, _sender, _path, _iface, name):
        return {
            "Version": GLib.Variant("u", 3),
            "TextDirection": GLib.Variant("s", "ltr"),
            "Status": GLib.Variant("s", "normal"),
            "IconThemePath": GLib.Variant("as", []),
        }.get(name)

    def _props(self, item_id):
        if item_id == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        out = {}
        for key, value in self.items.get(item_id, {}).items():
            kind = "b" if isinstance(value, bool) else "i" if isinstance(value, int) else "s"
            out[key] = GLib.Variant(kind, value)
        return out

    def _layout(self, item_id, depth):
        kids = self.children.get(item_id, []) if depth != 0 else []
        return (item_id, self._props(item_id),
                [GLib.Variant("(ia{sv}av)", self._layout(k, depth - 1)) for k in kids])

    def _click(self, item_id, event):
        handler = self.handlers.get(item_id)
        if event == "clicked" and handler:
            # Leave the D-Bus call first; the handler may start pkexec or open a window.
            GLib.idle_add(lambda: handler() and False)

    def _menu_call(self, _bus, _sender, _path, _iface, method, params, invocation):
        args = params.unpack()
        if method == "GetLayout":
            parent, depth, _names = args
            invocation.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, self._layout(parent, depth))))
        elif method == "GetGroupProperties":
            ids = args[0] or [0, *self.items]
            invocation.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._props(i)) for i in ids],)))
        elif method == "GetProperty":
            item_id, name = args
            value = self._props(item_id).get(name, GLib.Variant("s", ""))
            invocation.return_value(GLib.Variant("(v)", (value,)))
        elif method == "Event":
            self._click(args[0], args[1])
            invocation.return_value(None)
        elif method == "EventGroup":
            for item_id, event, _data, _ts in args[0]:
                self._click(item_id, event)
            invocation.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            before = self.revision
            self.update()   # fresh numbers each time the menu opens
            invocation.return_value(GLib.Variant("(b)", (self.revision != before,)))
        elif method == "AboutToShowGroup":
            self.update()
            invocation.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            invocation.return_error_literal(Gio.dbus_error_quark(), Gio.DBusError.UNKNOWN_METHOD, method)


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
