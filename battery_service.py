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
from pathlib import Path
import json

SERVICE_NAME = 'com.victronenergy.battery.aggregator'
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
AH_TEXT = lambda path,value: "{:.3f}Ah".format(value)


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


def _safe_min(newValue, currentValue):
    return min(newValue, currentValue) if currentValue else newValue


def _safe_max(newValue, currentValue):
    return max(newValue, currentValue) if currentValue else newValue


def _safe_sum(newValue, currentValue):
    return newValue + currentValue if currentValue else newValue


class BatteryService:
    def __init__(self, conn, config):
        self.service = VeDbusService(SERVICE_NAME, conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', DEVICE_INSTANCE_ID,
                                     PRODUCT_ID, PRODUCT_NAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.service.add_path("/Dc/0/Voltage", 0, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/Dc/0/Current", 0, gettextcallback=CURRENT_TEXT)
        self.service.add_path("/Dc/0/Power", 0, gettextcallback=POWER_TEXT)
        self.service.add_path("/Dc/0/Temperature", None)
        self.service.add_path("/Soc", None)
        self.service.add_path("/ConsumedAmphours", 0, gettextcallback=AH_TEXT)
        self.service.add_path("/Capacity", None, gettextcallback=AH_TEXT)
        self.service.add_path("/InstalledCapacity", None, gettextcallback=AH_TEXT)
        self.service.add_path("/System/NrOfBatteries", 0)
        self.service.add_path("/Info/BatteryLowVoltage", None)
        self.service.add_path("/Info/MaxChargeCurrent", None)
        self.service.add_path("/Info/MaxChargeVoltage", None)
        self.service.add_path("/Info/MaxDischargeCurrent", None)
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
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {
                    '/Dc/0/Current': options,
                    '/Dc/0/Voltage': options,
                    '/Dc/0/Power': options,
                    '/Dc/0/Temperature': options,
                    '/Soc': options,
                    '/ConsumedAmphours': options,
                    '/Capacity': options,
                    '/InstalledCapacity': options,
                    '/Info/BatteryLowVoltage': options,
                    '/Info/MaxChargeCurrent': options,
                    '/Info/MaxChargeVoltage': options,
                    '/Info/MaxDischargeCurrent': options,
                }
            },
            excludedServiceNames=[SERVICE_NAME] + config.get("excludedServices", [])
        )

    def _get_value(self, serviceName, path, defaultValue=None):
        return self.monitor.get_value(serviceName, path, defaultValue)

    def update(self):
        totalCurrent = 0
        totalPower = 0
        voltageSum = 0
        totalConsumedAh = 0
        temperatureStats = Statistics()
        socStats = Statistics()
        totalCapacity = 0
        totalInstalledCapacity = 0
        batteryCount = 0
        aggregated_alarms = {}
        maxLowVoltage = None
        totalMaxChargeCurrent = None
        minMaxChargeVoltage = None
        totalMaxDischargeCurrent = None

        serviceNames = self.monitor.get_service_list('com.victronenergy.battery')

        # minimize delay between time sensitive values
        for serviceName in serviceNames:
            current = self._get_value(serviceName, "/Dc/0/Current", 0)
            voltage = self._get_value(serviceName, "/Dc/0/Voltage", 0)
            power = self._get_value(serviceName, "/Dc/0/Power", voltage * current)
            totalCurrent += current
            voltageSum += voltage
            totalPower += power
            batteryCount += 1

        self._local_values["/System/NrOfBatteries"] = batteryCount
        self._local_values["/Dc/0/Voltage"] = voltageSum/batteryCount if batteryCount > 0 else 0
        self._local_values["/Dc/0/Current"] = totalCurrent
        self._local_values["/Dc/0/Power"] = totalPower

        for serviceName in serviceNames:
            temperature = self._get_value(serviceName, "/Dc/0/Temperature")
            soc = self._get_value(serviceName, "/Soc")
            consumedAh = self._get_value(serviceName, "/ConsumedAmphours", 0)
            capacity = self._get_value(serviceName, "/Capacity" ,0)
            installedCapacity = self._get_value(serviceName, "/InstalledCapacity" ,0)
            lowVoltage = self._get_value(serviceName, "/Info/BatteryLowVoltage")
            maxChargeCurrent = self._get_value(serviceName, "/Info/MaxChargeCurrent")
            maxChargeVoltage = self._get_value(serviceName, "/Info/MaxChargeVoltage")
            maxDischargeCurrent = self._get_value(serviceName, "/Info/MaxDischargeCurrent")
            totalConsumedAh += consumedAh
            totalCapacity += capacity
            totalInstalledCapacity += installedCapacity
            temperatureStats.add(temperature)
            socStats.add(soc)

            if lowVoltage is not None:
                maxLowVoltage = _safe_max(lowVoltage, maxLowVoltage)
            if maxChargeCurrent is not None:
                totalMaxChargeCurrent = _safe_sum(maxChargeCurrent, totalMaxChargeCurrent)
            if maxChargeVoltage is not None:
                minMaxChargeVoltage = _safe_min(maxChargeVoltage, minMaxChargeVoltage)
            if maxDischargeCurrent is not None:
                totalMaxDischargeCurrent = _safe_sum(maxDischargeCurrent, totalMaxDischargeCurrent)

            for alarm in self.alarms:
                aggregated_alarms[alarm] = max(self._get_value(serviceName, alarm, ALARM_OK), aggregated_alarms.get(alarm, ALARM_OK))

        if temperatureStats.count > 0:
            self._local_values["/Dc/0/Temperature"] = temperatureStats.mean()
        if socStats.count > 0:
            self._local_values["/Soc"] = socStats.mean()
        self._local_values["/ConsumedAmphours"] = totalConsumedAh

        self._local_values["/Capacity"] = totalCapacity
        self._local_values["/InstalledCapacity"] = totalInstalledCapacity

        if maxLowVoltage is not None:
            self._local_values["/Info/BatteryLowVoltage"] = maxLowVoltage
        if totalMaxChargeCurrent is not None:
            self._local_values["/Info/MaxChargeCurrent"] = totalMaxChargeCurrent
        if minMaxChargeVoltage is not None:
            self._local_values["/Info/MaxChargeVoltage"] = minMaxChargeVoltage
        if totalMaxDischargeCurrent is not None:
            self._local_values["/Info/MaxDischargeCurrent"] = totalMaxDischargeCurrent

        if aggregated_alarms:
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
    setupOptions = Path("/data/setupOptions/BatteryAggregator")
    configFile = setupOptions/"config.json"
    try:
        with configFile.open() as f:
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    battery = BatteryService(dbusConnection(), config)
    GLib.timeout_add(250, battery.publish)
    logger.info("Registered Battery Aggregator")
    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
