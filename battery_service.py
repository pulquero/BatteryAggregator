#!/usr/bin/env python

import os
import sys
from script_utils import SCRIPT_HOME, VERSION
sys.path.insert(1, os.path.join(os.path.dirname(__file__), f"{SCRIPT_HOME}/ext"))

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import logging
from vedbus import VeDbusService
from dbusmonitor import DbusMonitor

DEVICE_INSTANCE_ID = 1024
PRODUCT_ID = 0
PRODUCT_NAME = "Battery Aggregator"
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

ALARM_OK = 0
ALARM_WARNING = 1
ALARM_ALARM = 2

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("battery")


class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)


class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)


def dbusConnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()


VOLTAGE_TEXT = lambda path,value: "{:.2f}V".format(value)
CURRENT_TEXT = lambda path,value: "{:.3f}A".format(value)
POWER_TEXT = lambda path,value: "{:.2f}W".format(value)


class Statistics:
    def __init__(self):
        self.sum = 0
        self.count = 0

    def add(self, x):
        if x is not None:
            self.sum += x
            self.count += 1

    def mean(self):
        return self.sum/self.count if self.count > 0 else 0


class BatteryService:
    def __init__(self, conn):
        self.service = VeDbusService('com.victronenergy.battery.aggregator', conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', DEVICE_INSTANCE_ID,
                                     PRODUCT_ID, PRODUCT_NAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.service.add_path("/Dc/0/Voltage", 0, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/Dc/0/Current", 0, gettextcallback=CURRENT_TEXT)
        self.service.add_path("/Dc/0/Power", 0, gettextcallback=POWER_TEXT)
        self.service.add_path("/Dc/0/Temperature", 0)
        self.service.add_path("/Soc", 0)
        self.service.add_path("/ConsumedAmphours", 0)
        self.service.add_path("/System/NrOfBatteries", 0)
        self.alarms = [
            "/Alarms/CellImbalance",
            "/Alarms/LowSoc",
            "/Alarms/HighDischargeCurrent",
            "/Alarms/LowVoltage",
            "/Alarms/HighVoltage",
            "/Alarms/LowCellVoltage",
            "/Alarms/HighCellVoltage",
            "/Alarms/LowTemperature",
            "/Alarms/HighTemperature",
            "/Alarms/LowChargeTemperature",
            "/Alarms/HighChargeTemperature",
        ]
        for alarm in self.alarms:
            self.service.add_path(alarm, ALARM_OK)
        self._local_values = {}
        for path in self.service._dbusobjects:
            self._local_values[path] = self.service[path]
        options = None  # currently not used afaik
        self.monitor = DbusMonitor({
            'com.victronenergy.battery': {
                '/Dc/0/Current': options,
                '/Dc/0/Voltage': options,
                '/Dc/0/Power': options,
                '/Dc/0/Temperature': options,
                '/Soc': options,
                '/ConsumedAmphours': options,
            }
        })

    def _get_value(self, serviceName, path, defaultValue=None):
        return self.monitor.get_value(serviceName, path, defaultValue)

    def update(self):
        totalCurrent = 0
        totalPower = 0
        voltageSum = 0
        totalConsumedAh = 0
        temperatureStats = Statistics()
        socStats = Statistics()
        batteryCount = 0
        aggregated_alarms = {}

        for serviceName in self.monitor.get_service_list('com.victronenergy.battery'):
            current = self._get_value(serviceName, "/Dc/0/Current", 0)
            voltage = self._get_value(serviceName, "/Dc/0/Voltage", 0)
            power = self._get_value(serviceName, "/Dc/0/Power", voltage * current)
            temperature = self._get_value(serviceName, "/Dc/0/Temperature")
            soc = self._get_value(serviceName, "/Soc")
            consumedAh = self._get_value(serviceName, "/ConsumedAmphours", 0)
            totalCurrent += current
            voltageSum += voltage
            totalPower += power
            totalConsumedAh += consumedAh
            temperatureStats.add(temperature)
            socStats.add(soc)
            batteryCount += 1

            for alarm in self.alarms:
                aggregated_alarms[alarm] = max(self._get_value(serviceName, alarm, ALARM_OK), aggregated_alarms.get(alarm, ALARM_OK))

        self._local_values["/System/NrOfBatteries"] = batteryCount
        self._local_values["/Dc/0/Voltage"] = voltageSum/batteryCount if batteryCount > 0 else 0
        self._local_values["/Dc/0/Current"] = totalCurrent
        self._local_values["/Dc/0/Power"] = totalPower
        self._local_values["/Dc/0/Temperature"] = temperatureStats.mean()
        self._local_values["/Soc"] = socStats.mean()

        for alarm in self.alarms:
            self._local_values[alarm] = aggregated_alarms[alarm]
        return True

    def publish(self):
        self.update()
        for k,v in self._local_values.items():
            self.service[k] = v
        return True

    def __str__(self):
        return PRODUCT_NAME


def main():
    DBusGMainLoop(set_as_default=True)
    battery = BatteryService(dbusConnection())
    GLib.timeout_add(250, battery.publish)
    logger.info("Registered Battery Aggregator")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
