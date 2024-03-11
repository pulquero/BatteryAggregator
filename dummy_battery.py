#!/usr/bin/env python

import os
import sys
from script_utils import SCRIPT_HOME, VERSION
sys.path.insert(1, os.path.join(os.path.dirname(__file__), f"{SCRIPT_HOME}/ext"))

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
from vedbus import VeDbusService
from battery_service import BATTERY_PATHS, dbusConnection


def main():
    DBusGMainLoop(set_as_default=True)

    battery = VeDbusService("com.victronenergy.battery.dummy", dbusConnection())
    battery.add_mandatory_paths(__file__, "1.0", 'dbus', 0,
                                 0, "Dummy Battery", 0, 0, 1)
    for path in BATTERY_PATHS:
        battery.add_path(path, None, writeable=True)

    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
