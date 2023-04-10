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
from settingsdevice import SettingsDevice
from settableservice import SettableService
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


def _safe_min(newValue, currentValue):
    return min(newValue, currentValue) if currentValue else newValue


def _safe_max(newValue, currentValue):
    return max(newValue, currentValue) if currentValue else newValue


def _safe_sum(newValue, currentValue):
    return newValue + currentValue if currentValue else newValue


class Unit:
    def __init__(self, gettextcallback=None):
        self.gettextcallback = gettextcallback


VOLTAGE = Unit(VOLTAGE_TEXT)
CURRENT = Unit(CURRENT_TEXT)
TEMPERATURE = Unit()
AMP_HOURS = Unit(AH_TEXT)


class Aggregator:
    def __init__(self, path, op, unit=None):
        self.path = path
        self.op = op
        self.value = None
        self.unit = unit

    def add(self, getter, serviceName):
        x = getter(serviceName, self.path)
        if x is not None:
            self.value = self.op(x, self.value)


class Statistics:
    def __init__(self):
        self.sum = 0
        self.count = 0

    def add(self, x):
        if x is not None:
            self.sum += x
            self.count += 1

    def mean(self):
        return self.sum/self.count if self.count > 0 else None


class BatteryService(SettableService):
    def __init__(self, conn, config):
        super().__init__()
        configuredCapacity = config.get("capacity")
        self.service = VeDbusService(SERVICE_NAME, conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', DEVICE_INSTANCE_ID,
                                     PRODUCT_ID, PRODUCT_NAME, FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.add_settable_path("/CustomName", "", 0, 0)
        self.service.add_path("/Dc/0/Voltage", 0, gettextcallback=VOLTAGE_TEXT)
        self.service.add_path("/Dc/0/Current", 0, gettextcallback=CURRENT_TEXT)
        self.service.add_path("/Dc/0/Power", 0, gettextcallback=POWER_TEXT)
        self.service.add_path("/Dc/0/Temperature", None)
        self.service.add_path("/Soc", None)
        self.service.add_path("/System/NrOfBatteries", 0)
        self.service.add_path("/System/BatteriesParallel", 0)
        self.service.add_path("/System/BatteriesSeries", 1)
        self.aggregators = [
            Aggregator('/ConsumedAmphours', _safe_sum, AMP_HOURS),
            Aggregator('/Info/BatteryLowVoltage', _safe_max, VOLTAGE),
            Aggregator('/Info/MaxChargeCurrent', _safe_sum, CURRENT),
            Aggregator('/Info/MaxChargeVoltage', _safe_min, VOLTAGE),
            Aggregator('/Info/MaxDischargeCurrent', _safe_sum, CURRENT),
            Aggregator('/System/MinCellTemperature', _safe_min, TEMPERATURE),
            Aggregator('/System/MinCellVoltage', _safe_min, VOLTAGE),
            Aggregator('/System/MaxCellTemperature', _safe_max, TEMPERATURE),
            Aggregator('/System/MaxCellVoltage', _safe_max, VOLTAGE),
        ]
        if configuredCapacity:
            self.service.add_path("/InstalledCapacity", configuredCapacity, gettextcallback=AH_TEXT)
        else:
            self.aggregators.append(Aggregator('/Capacity', _safe_sum, AMP_HOURS))
            self.aggregators.append(Aggregator('/InstalledCapacity', _safe_sum, AMP_HOURS))

        for aggr in self.aggregators:
            self.service.add_path(aggr.path, None, gettextcallback=aggr.unit.gettextcallback)

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

        self._init_settings(conn)

        self._local_values = {}
        for path in self.service._dbusobjects:
            self._local_values[path] = self.service[path]
        options = None  # currently not used afaik
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {
                    **{
                        '/Dc/0/Current': options,
                        '/Dc/0/Voltage': options,
                        '/Dc/0/Power': options,
                        '/Dc/0/Temperature': options,
                        '/Soc': options,
                    },
                    **{aggr.path: options for aggr in self.aggregators}
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
        temperatureStats = Statistics()
        socStats = Statistics()
        batteryCount = 0
        aggregated_alarms = {}

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
        self._local_values["/System/BatteriesParallel"] = batteryCount
        self._local_values["/Dc/0/Voltage"] = voltageSum/batteryCount if batteryCount > 0 else 0
        self._local_values["/Dc/0/Current"] = totalCurrent
        self._local_values["/Dc/0/Power"] = totalPower

        for aggr in self.aggregators:
            aggr.value = None

        for serviceName in serviceNames:
            temperature = self._get_value(serviceName, "/Dc/0/Temperature")
            temperatureStats.add(temperature)
            soc = self._get_value(serviceName, "/Soc")
            socStats.add(soc)

            for aggr in self.aggregators:
                aggr.add(self._get_value, serviceName)

            for alarm in self.alarms:
                aggregated_alarms[alarm] = max(self._get_value(serviceName, alarm, ALARM_OK), aggregated_alarms.get(alarm, ALARM_OK))

        self._local_values["/Dc/0/Temperature"] = temperatureStats.mean()
        self._local_values["/Soc"] = socStats.mean()

        for aggr in self.aggregators:
            self._local_values[aggr.path] = aggr.value

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
