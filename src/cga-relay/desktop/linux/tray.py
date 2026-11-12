"""System Python only: python3-gi, GTK3, AyatanaAppIndicator3/AppIndicator3."""
import json
import sys
import threading

try:
    import gi
    gi.require_version("Gtk", "3.0")
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3 as AppIndicator
    from gi.repository import Gtk, GLib, Gio
except (ImportError, ValueError) as error:
    print("CGA Relay tray dependency missing: install system python3-gi, "
          "gir1.2-gtk-3.0 and gir1.2-ayatanaappindicator3-0.1 "
          "(or gir1.2-appindicator3-0.1). " + str(error), file=sys.stderr)
    sys.exit(2)


def emit(action):
    try:
        print(action, flush=True)
    except (BrokenPipeError, OSError):
        Gtk.main_quit()


class Tray:
    def __init__(self):
        ok, _ = Gtk.init_check([])
        if not ok:
            raise RuntimeError("GTK3 cannot connect to the graphical session")
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if not self.has_watcher():
            raise RuntimeError("No StatusNotifierWatcher tray host. Enable your desktop's "
                               "AppIndicator/KStatusNotifier support (GNOME needs an AppIndicator extension).")
        self.state = {}
        self.failed = False
        self.indicator = AppIndicator.Indicator.new(
            "cga-relay", "network-offline", AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.account = self.item("Account: checking...", sensitive=False)
        self.backend = self.item("CGA Server: checking...", sensitive=False)
        self.error = self.item("", sensitive=False)
        self.signin = self.item("Sign in...", callback=self.account_action)
        self.menu.append(Gtk.SeparatorMenuItem())
        for label, action in [("Open CGA Web", "web"), ("Settings", "settings"),
                              ("Logs", "logs")]:
            self.item(label, callback=lambda _, action=action: emit(action))
        self.item("About", callback=self.about)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item("Relaunch", callback=lambda _: emit("relaunch"))
        self.item("Exit", callback=lambda _: emit("exit"))
        self.menu.show_all()
        self.error.hide()
        self.indicator.set_menu(self.menu)
        self.bus.signal_subscribe(
            "org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
            "/org/freedesktop/DBus", "org.kde.StatusNotifierWatcher",
            Gio.DBusSignalFlags.NONE, self.watcher_changed)

    def has_watcher(self):
        result = self.bus.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "NameHasOwner", GLib.Variant("(s)", ("org.kde.StatusNotifierWatcher",)),
            GLib.VariantType.new("(b)"), Gio.DBusCallFlags.NONE, 3000, None)
        return result.unpack()[0]

    def watcher_changed(self, connection, sender, path, interface, signal, parameters):
        if not parameters.unpack()[2]:
            print("CGA Relay: desktop tray host disappeared; stopping Relay.", file=sys.stderr)
            self.failed = True
            Gtk.main_quit()

    def item(self, label, callback=None, sensitive=True):
        item = Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        if callback:
            item.connect("activate", callback)
        self.menu.append(item)
        return item

    def account_action(self, _):
        emit("logout" if self.state.get("logged_in") else "login")

    def about(self, _):
        dialog = Gtk.MessageDialog(
            modal=True, message_type=Gtk.MessageType.INFO, buttons=Gtk.ButtonsType.CLOSE,
            text="CGA-Relay")
        dialog.format_secondary_text(self.state.get("about", "Checking Relay status..."))
        dialog.connect("response", lambda dialog, _: dialog.destroy())
        dialog.show_all()

    def update(self, state):
        self.state = state
        self.account.set_label(state["account"])
        self.backend.set_label("CGA Server: available" if state["backend"]
                               else "CGA Server: UNAVAILABLE — start the server container")
        self.signin.set_label("Sign out" if state["logged_in"] else "Sign in...")
        self.error.set_label(state.get("error", ""))
        self.error.set_visible(bool(state.get("error")))
        icon = ("dialog-warning" if not state["backend"] else
                "network-transmit-receive" if state["logged_in"] else "avatar-default")
        self.indicator.set_icon_full(icon, state["tooltip"])
        self.indicator.set_title(state["tooltip"])
        return False


def read_parent(tray):
    try:
        for line in sys.stdin:
            state = json.loads(line)
            fields = {"account": str, "logged_in": bool, "backend": bool,
                      "tooltip": str, "about": str, "error": str}
            if not isinstance(state, dict) or any(
                    key not in state or not isinstance(state[key], kind)
                    for key, kind in fields.items()):
                raise ValueError("invalid Relay status fields")
            GLib.idle_add(tray.update, state)
    except (ValueError, OSError) as error:
        print("CGA Relay tray IPC failed: " + str(error), file=sys.stderr)
        tray.failed = True
    finally:
        # Also closes all modal/about windows: no orphaned helper after core death.
        GLib.idle_add(Gtk.main_quit)


try:
    tray = Tray()
    threading.Thread(target=read_parent, args=(tray,), daemon=True).start()
    emit("ready")
    Gtk.main()
    sys.exit(2 if tray.failed else 0)
except Exception as error:
    print("CGA Relay tray failed: " + str(error), file=sys.stderr)
    sys.exit(2)
