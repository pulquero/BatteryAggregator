import os
import sys
from script_utils import SCRIPT_HOME, VERSION
sys.path.insert(1, os.path.join(os.path.dirname(__file__), f"{SCRIPT_HOME}/ext"))

import dbus
import time
import json
import ve_utils


class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)


def main():
    includeNames = tuple(sys.argv[1:]) if len(sys.argv) > 1 else ("com.victronenergy",)
    conn = SystemBus()
    data = {}
    for serviceName in conn.list_names():
        if serviceName.startswith(includeNames):
            rawValues = conn.call_blocking(serviceName, '/', None, 'GetValue', '', [])
            if type(rawValues) == dbus.Array:
                values = []
                for v in rawValues:
                    values.append(ve_utils.unwrap_dbus_value(v))
            else:
                values = {}
                for k,v in rawValues.items():
                    values[k] = ve_utils.unwrap_dbus_value(v)
            data[serviceName] = values
    with open(f"/data/dbus_dump_{int(time.time())}", "w") as fp:
        json.dump(data, fp)


if __name__ == "__main__":
    main()
