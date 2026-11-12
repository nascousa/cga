"""Run: dbus-run-session -- xvfb-run -a /usr/bin/python3 test_tray.py

Requires the same system GI/GTK3/AppIndicator packages as tray.py, plus dbus-x11
and xvfb. The private session bus and mock watcher need no desktop services.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import threading
import time
import unittest

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib  # noqa: E402


WATCHER = "org.kde.StatusNotifierWatcher"
WATCHER_XML = """<node><interface name="org.kde.StatusNotifierWatcher">
<method name="RegisterStatusNotifierItem"><arg type="s" direction="in"/></method>
<method name="RegisterStatusNotifierHost"><arg type="s" direction="in"/></method>
<property name="RegisteredStatusNotifierItems" type="as" access="read"/>
<property name="IsStatusNotifierHostRegistered" type="b" access="read"/>
<property name="ProtocolVersion" type="i" access="read"/>
<signal name="StatusNotifierItemRegistered"><arg type="s"/></signal>
<signal name="StatusNotifierHostRegistered"/>
</interface></node>"""


class Watcher:
    def __init__(self):
        self.connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self.items = []
        self.condition = threading.Condition()
        info = Gio.DBusNodeInfo.new_for_xml(WATCHER_XML).interfaces[0]
        self.object_id = self.connection.register_object(
            "/StatusNotifierWatcher", info, self.method, self.property, None)
        self.request_name()
        self.loop = GLib.MainLoop()
        self.thread = threading.Thread(target=self.loop.run, daemon=True)
        self.thread.start()

    def request_name(self):
        result = self.connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "RequestName", GLib.Variant("(su)", (WATCHER, 4)),
            GLib.VariantType.new("(u)"), Gio.DBusCallFlags.NONE, 3000, None)
        if result.unpack()[0] != 1:
            raise RuntimeError("Tests require a private session bus without a real tray watcher")

    def method(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "RegisterStatusNotifierItem":
            service = parameters.unpack()[0]
            item = (sender, service) if service.startswith("/") else (service, "/StatusNotifierItem")
            with self.condition:
                self.items.append(item)
                self.condition.notify_all()
            invocation.return_value(None)
            connection.emit_signal(
                None, "/StatusNotifierWatcher", WATCHER, "StatusNotifierItemRegistered",
                GLib.Variant("(s)", (item[0] + item[1],)))
        else:
            invocation.return_value(None)

    def property(self, connection, sender, path, interface, name):
        if name == "RegisteredStatusNotifierItems":
            return GLib.Variant("as", [service + path for service, path in self.items])
        if name == "IsStatusNotifierHostRegistered":
            return GLib.Variant("b", True)
        return GLib.Variant("i", 0)

    def wait_for_item(self, previous_count):
        with self.condition:
            if not self.condition.wait_for(lambda: len(self.items) > previous_count, timeout=5):
                raise AssertionError("AppIndicator did not register with the mock watcher")
            return self.items[-1]

    def close(self):
        self.connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "ReleaseName", GLib.Variant("(s)", (WATCHER,)), None,
            Gio.DBusCallFlags.NONE, 3000, None)
        self.connection.unregister_object(self.object_id)
        self.loop.quit()
        self.thread.join(timeout=3)


class TraySmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not os.environ.get("DBUS_SESSION_BUS_ADDRESS") or not os.environ.get("DISPLAY"):
            raise RuntimeError("Run under dbus-run-session -- xvfb-run -a")
        cls.watcher = Watcher()

    @classmethod
    def tearDownClass(cls):
        cls.watcher.close()

    def setUp(self):
        self.helpers = []

    def tearDown(self):
        for helper in self.helpers:
            if helper.poll() is None:
                helper.kill()
                helper.wait(timeout=5)
            for stream in (helper.stdin, helper.stdout, helper.stderr):
                if stream:
                    stream.close()

    def start_helper(self):
        previous_count = len(self.watcher.items)
        environment = dict(os.environ, NO_AT_BRIDGE="1", GSETTINGS_BACKEND="memory")
        helper = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).with_name("tray.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=environment)
        self.helpers.append(helper)
        self.assertEqual(self.action(helper), "ready")
        service, path = self.watcher.wait_for_item(previous_count)
        result = self.watcher.connection.call_sync(
            service, path, "org.freedesktop.DBus.Properties", "Get",
            GLib.Variant("(ss)", ("org.kde.StatusNotifierItem", "Menu")),
            GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 3000, None)
        return helper, service, result.unpack()[0]

    def action(self, helper):
        ready, _, _ = select.select([helper.stdout], [], [], 5)
        self.assertTrue(ready, "helper did not send the expected IPC action")
        line = helper.stdout.readline()
        self.assertTrue(line, "helper closed its IPC unexpectedly")
        return line.rstrip("\r\n")

    def status(self, helper, **overrides):
        state = dict(account="Not signed in", logged_in=False, backend=False,
                     tooltip="CGA-Relay unavailable", about="Test relay account", error="")
        state.update(overrides)
        helper.stdin.write(json.dumps(state) + "\n")
        helper.stdin.flush()

    def layout(self, service, menu):
        result = self.watcher.connection.call_sync(
            service, menu, "com.canonical.dbusmenu", "GetLayout",
            GLib.Variant("(iias)", (0, -1, [])),
            GLib.VariantType.new("(u(ia{sv}av))"),
            Gio.DBusCallFlags.NONE, 3000, None)
        labels = {}

        def visit(node):
            item_id, properties, children = node
            if "label" in properties:
                labels[properties["label"]] = item_id
            for child in children:
                visit(child)

        visit(result.unpack()[1])
        return labels

    def wait_label(self, service, menu, label):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            labels = self.layout(service, menu)
            if label in labels:
                return labels
            time.sleep(0.05)
        self.fail(f"menu did not refresh to contain {label!r}: {labels!r}")

    def click(self, service, menu, item_id):
        self.watcher.connection.call_sync(
            service, menu, "com.canonical.dbusmenu", "Event",
            GLib.Variant("(isvu)", (item_id, "clicked", GLib.Variant("i", 0), 0)),
            None, Gio.DBusCallFlags.NONE, 3000, None)

    def close_input(self, helper):
        helper.stdin.close()
        helper.stdin = None
        helper.wait(timeout=5)
        return helper.stderr.read()

    def test_native_menu_status_and_core_actions(self):
        helper, service, menu = self.start_helper()
        self.status(helper)
        labels = self.wait_label(service, menu, "Not signed in")
        self.assertIn("CGA Server: UNAVAILABLE — start the server container", labels)
        for label, action in [("Sign in...", "login"), ("Open CGA Web", "web"),
                              ("Settings", "settings"), ("Logs", "logs")]:
            self.click(service, menu, labels[label])
            self.assertEqual(self.action(helper), action)
        self.status(helper, account="Signed in: smoke@example.com", logged_in=True,
                    backend=True, error="Test credential warning")
        labels = self.wait_label(service, menu, "Signed in: smoke@example.com")
        self.assertIn("CGA Server: available", labels)
        self.assertIn("Test credential warning", labels)
        for label, action in [("Sign out", "logout"), ("Relaunch", "relaunch"), ("Exit", "exit")]:
            self.click(service, menu, labels[label])
            self.assertEqual(self.action(helper), action)
        # Exit/relaunch are requests to the core, which then closes the helper pipe.
        self.close_input(helper)
        self.assertEqual(helper.returncode, 0)

    def test_parent_eof_closes_helper_even_with_about_open(self):
        helper, service, menu = self.start_helper()
        self.status(helper)
        labels = self.wait_label(service, menu, "Not signed in")
        self.click(service, menu, labels["About"])
        self.close_input(helper)
        self.assertEqual(helper.returncode, 0)

    def test_bad_status_stops_helper_actionably(self):
        for value in ["not-json", "{}", '{"account":"test","backend":"not-a-bool"}']:
            with self.subTest(value=value):
                helper, _, _ = self.start_helper()
                helper.stdin.write(value + "\n")
                helper.stdin.flush()
                helper.wait(timeout=5)
                self.assertEqual(helper.returncode, 2)
                self.assertIn("CGA Relay tray IPC failed:", helper.stderr.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)
