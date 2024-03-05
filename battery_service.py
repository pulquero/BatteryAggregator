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
import functools
import hashlib
import json
from pathlib import Path
import multiprocessing
import signal

DEFAULT_SERVICE_NAME = 'com.victronenergy.battery.aggregator'
DEVICE_INSTANCE_ID = 1024
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

BASE_DEVICE_INSTANCE_ID = DEVICE_INSTANCE_ID + 32

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
    return min(newValue, currentValue) if currentValue is not None else newValue


def _safe_max(newValue, currentValue):
    return max(newValue, currentValue) if currentValue is not None else newValue


def _safe_sum(newValue, currentValue):
    return newValue + currentValue if currentValue is not None else newValue


class Unit:
    def __init__(self, gettextcallback=None):
        self.gettextcallback = gettextcallback


VOLTAGE = Unit(VOLTAGE_TEXT)
CURRENT = Unit(CURRENT_TEXT)
POWER = Unit(POWER_TEXT)
TEMPERATURE = Unit()
AMP_HOURS = Unit(AH_TEXT)
NO_UNIT = Unit()


class Aggregator:
    def __init__(self, op, initial_value=None, store=False, requires=None):
        self.values = [] if store else None
        self.op = op
        self.value = initial_value
        self.requires = requires

    def add(self, x):
        if x is not None:
            self.value = self.op(x, self.value)
        if self.values is not None:
            self.values.append(x)


class MeanAggregator:
    def __init__(self):
        self.sum = 0
        self.count = 0

    def add(self, x):
        if x is not None:
            self.sum += x
            self.count += 1

    @property
    def value(self):
        return self.sum/self.count if self.count > 0 else None


SumAggregator = functools.partial(Aggregator, _safe_sum)
MinAggregator = functools.partial(Aggregator, _safe_min)
MaxAggregator = functools.partial(Aggregator, _safe_max)
AlarmAggregator = functools.partial(Aggregator, max, initial_value=ALARM_OK)
BooleanAggregator = functools.partial(Aggregator, _safe_max)
MaxChargeCurrentAggregator = functools.partial(Aggregator, _safe_sum, store=True, requires={"/Io/AllowToCharge": 1})
MaxChargeVoltageAggregator = functools.partial(Aggregator, _safe_min, store=True)
MaxDischargeCurrentAggregator = functools.partial(Aggregator, _safe_sum, requires={"/Io/AllowToDischarge": 1})


class PathDefinition:
    def __init__(self, unit=NO_UNIT, aggregatorClass=None, defaultValue=None):
        self.unit = unit
        self.aggregatorClass = aggregatorClass
        self.defaultValue = defaultValue


BATTERY_PATHS = {
    '/Dc/0/Current': PathDefinition(CURRENT, defaultValue=0),
    '/Dc/0/Voltage': PathDefinition(VOLTAGE, defaultValue=0),
    '/Dc/0/Power':  PathDefinition(POWER, defaultValue=0),
    '/Dc/0/Temperature':  PathDefinition(TEMPERATURE, MeanAggregator),
    '/Soc':  PathDefinition(NO_UNIT, MeanAggregator, defaultValue=0),
    '/TimeToGo':  PathDefinition(NO_UNIT, MeanAggregator, defaultValue=0),
    '/Capacity' : PathDefinition(AMP_HOURS, SumAggregator, defaultValue=0),
    '/InstalledCapacity' : PathDefinition(AMP_HOURS, SumAggregator, defaultValue=0),
    '/ConsumedAmphours': PathDefinition(AMP_HOURS, SumAggregator, defaultValue=0),
    '/Balancing': PathDefinition(NO_UNIT, BooleanAggregator),
    '/Info/BatteryLowVoltage': PathDefinition(VOLTAGE, MaxAggregator),
    '/Info/MaxChargeCurrent': PathDefinition(CURRENT, MaxChargeCurrentAggregator),
    '/Info/MaxChargeVoltage': PathDefinition(VOLTAGE, MaxChargeVoltageAggregator),
    '/Info/MaxDischargeCurrent': PathDefinition(CURRENT, MaxDischargeCurrentAggregator),
    '/Io/AllowToCharge': PathDefinition(NO_UNIT, BooleanAggregator),
    '/Io/AllowToDischarge': PathDefinition(NO_UNIT, BooleanAggregator),
    '/Io/AllowToBalance': PathDefinition(NO_UNIT, BooleanAggregator),
    '/System/MinCellTemperature': PathDefinition(TEMPERATURE, MinAggregator),
    '/System/MinTemperatureCellId': PathDefinition(NO_UNIT, MinAggregator),
    '/System/MinCellVoltage': PathDefinition(VOLTAGE, MinAggregator),
    '/System/MinVoltageCellId': PathDefinition(NO_UNIT, MinAggregator),
    '/System/MaxCellTemperature': PathDefinition(TEMPERATURE, MaxAggregator),
    '/System/MaxTemperatureCellId': PathDefinition(NO_UNIT, MaxAggregator),
    '/System/MaxCellVoltage': PathDefinition(VOLTAGE, MaxAggregator),
    '/System/MaxVoltageCellId': PathDefinition(NO_UNIT, MaxAggregator),
    '/System/NrOfModulesBlockingCharge': PathDefinition(NO_UNIT, SumAggregator, defaultValue=0),
    '/System/NrOfModulesBlockingDischarge': PathDefinition(NO_UNIT, SumAggregator, defaultValue=0),
    '/System/NrOfModulesOnline': PathDefinition(NO_UNIT, SumAggregator, defaultValue=0),
    '/System/NrOfModulesOffline': PathDefinition(NO_UNIT, SumAggregator, defaultValue=0),
    '/Alarms/CellImbalance': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/LowSoc': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/HighDischargeCurrent': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/LowVoltage': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/HighVoltage': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/LowCellVoltage': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/HighCellVoltage': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/LowTemperature': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/HighTemperature': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/LowChargeTemperature': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
    '/Alarms/HighChargeTemperature': PathDefinition(aggregatorClass=AlarmAggregator, defaultValue=ALARM_OK),
}


class DataMerger:
    def __init__(self, config):
        if isinstance(config, list):
            # convert short-hand format
            self.paths = {serviceName: list(BATTERY_PATHS) for serviceName in reversed(config)}
        elif isinstance(config, dict):
            self.paths = {}
            for k, v in reversed(config.items()):
                if not v:
                    v = BATTERY_PATHS
                self.paths[k] = v
        elif config is None:
            self.paths = {}
        else:
            raise ValueError(f"Unsupported config object: {type(config)}")

    def merge_into(self, service):
        if self.paths:
            for serviceName, includedPaths in self.paths.items():
                for path in includedPaths:
                        v = service._get_value(serviceName, path)
                        if v is not None:
                            service._local_values[path] = v


class BatteryAggregatorService(SettableService):
    def __init__(self, conn, serviceName, config):
        super().__init__()
        if not serviceName.startswith("com.victronenergy.battery."):
            raise ValueError(f"Invalid service name: {serviceName}")

        self._conn = conn
        self._serviceName = serviceName
        self._configuredCapacity = config.get("capacity")
        scanPaths = set(BATTERY_PATHS.keys())
        if self._configuredCapacity:
            scanPaths.remove('/InstalledCapacity')
            scanPaths.remove('/Capacity')

        self._primaryServices = DataMerger(config.get("primaryServices"))
        self._auxiliaryServices = DataMerger(config.get("auxiliaryServices"))

        excludedServices = [serviceName]
        excludedServices.extend(config.get("excludedServices", []))
        virtualBatteryConfigs = config.get("virtualBatteries", {})
        for virtualBatteryConfig in virtualBatteryConfigs.values():
            excludedServices.extend(virtualBatteryConfig)

        options = None  # currently not used afaik
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {path: options for path in scanPaths}
            },
            excludedServiceNames=excludedServices
        )

        self._aggregatePaths = {path: BATTERY_PATHS[path] for path in scanPaths if BATTERY_PATHS[path].aggregatorClass is not None}
        self._previousOvercurrentRatio = None

    def register(self, timeout):
        self.service = VeDbusService(self._serviceName, self._conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', DEVICE_INSTANCE_ID,
                                     0, "Battery Aggregator", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.add_settable_path("/CustomName", "")
        for path, defn in BATTERY_PATHS.items():
            self.service.add_path(path, defn.defaultValue, gettextcallback=defn.unit.gettextcallback)
        if self._configuredCapacity:
            self.service['/InstalledCapacity'] = self._configuredCapacity
            self.service['/Capacity'] = None

        self.service.add_path("/System/Batteries", None)
        self.service.add_path("/System/NrOfBatteries", 0)
        self.service.add_path("/System/BatteriesParallel", 0)
        self.service.add_path("/System/BatteriesSeries", 1)

        self._init_settings(self._conn, timeout=timeout)

        self._local_values = {}
        for path in self.service._dbusobjects:
            self._local_values[path] = self.service[path]

    def _get_value(self, serviceName, path, defaultValue=None):
        return self.monitor.get_value(serviceName, path, defaultValue)

    def get_battery_service_names(self):
        allServiceNames = self.monitor.get_service_list('com.victronenergy.battery')
        serviceNames = [s for s in allServiceNames if s not in self._auxiliaryServices.paths and s not in self._primaryServices.paths]
        return serviceNames

    def update(self):
        totalCurrent = 0
        totalPower = 0
        voltageSum = 0

        # pre-populate with data from aux services
        self._auxiliaryServices.merge_into(self)

        serviceNames = self.get_battery_service_names()

        # minimize delay between time sensitive values
        batteryCurrents = []
        batteryVoltages = []
        batteryPowers = []
        for serviceName in serviceNames:
            current = self._get_value(serviceName, "/Dc/0/Current", 0)
            voltage = self._get_value(serviceName, "/Dc/0/Voltage", 0)
            power = self._get_value(serviceName, "/Dc/0/Power", voltage * current)
            batteryCurrents.append(current)
            batteryVoltages.append(voltage)
            batteryPowers.append(power)

        totalCurrent = sum(batteryCurrents)
        voltageSum = sum(batteryVoltages)
        totalPower = sum(batteryPowers)

        batteryCount = len(serviceNames)

        self._local_values["/System/Batteries"] = json.dumps(serviceNames)
        self._local_values["/System/NrOfBatteries"] = batteryCount
        self._local_values["/System/BatteriesParallel"] = batteryCount
        self._local_values["/Dc/0/Voltage"] = voltageSum/batteryCount if batteryCount > 0 else 0
        self._local_values["/Dc/0/Current"] = totalCurrent if batteryCount > 0 else 0
        self._local_values["/Dc/0/Power"] = totalPower if batteryCount > 0 else 0

        # other values
        aggregators = {}
        for path, defn in self._aggregatePaths.items():
            aggregators[path] = defn.aggregatorClass()

        for serviceName in serviceNames:
            for path, aggr in aggregators.items():
                can_aggregate = True
                if hasattr(aggr, 'requires') and aggr.requires:
                    for requiredPath, requiredValue in aggr.requires.items():
                        actualValue = self._get_value(serviceName, requiredPath, requiredValue)
                        if actualValue != requiredValue:
                            can_aggregate = False
                            break

                v = self._get_value(serviceName, path) if can_aggregate else None
                aggr.add(v)

        # check for over-current and scale back
        maxOvercurrentRatio = 0
        maxOvercurrentBatteryName = -1
        maxChargeCurrentAggr = aggregators["/Info/MaxChargeCurrent"]
        for i in range(batteryCount):
            # charge current limit
            ccl = maxChargeCurrentAggr.values[i]
            if ccl is not None:
                batteryOvercurrentRatio = batteryCurrents[i]/ccl
                if batteryOvercurrentRatio > maxOvercurrentRatio:
                    maxOvercurrentRatio = batteryOvercurrentRatio
                    maxOvercurrentBatteryName = serviceNames[i]
        if maxOvercurrentRatio > 1:
            scaledCCL = maxChargeCurrentAggr.value / maxOvercurrentRatio
            if self._previousOvercurrentRatio != maxOvercurrentRatio:
                logger.info(f"Max charge current is {maxChargeCurrentAggr.value} but scaling back to {scaledCCL} as limit exceeded for battery {maxOvercurrentBatteryName} (overcurrent ratio: {maxOvercurrentRatio})")
                logMsg = "Battery currents:\n"
                for i in range(batteryCount):
                    logMsg += f"{serviceNames[i]}: {batteryCurrents[i]}, limit {maxChargeCurrentAggr.values[i]}\n"
                logger.info(logMsg)
                self._previousOvercurrentRatio = maxOvercurrentRatio
            maxChargeCurrentAggr.value = scaledCCL

        # check for under-voltage
        maxChargeVoltageAggr = aggregators["/Info/MaxChargeVoltage"]
        balancingAggr = aggregators["/Balancing"]
        if balancingAggr.value == 1:
            # use max voltage for balancing
            minCVL = maxChargeVoltageAggr.value
            maxCVL = max([v for v in maxChargeVoltageAggr.values if v is not None])
            if maxCVL > minCVL:
                logger.info(f"Min charge voltage is {minCVL} but using max of {maxCVL} as balancing")
                maxChargeVoltageAggr.value = maxCVL

        for path, aggr in aggregators.items():
            self._local_values[path] = aggr.value if batteryCount > 0 else self._aggregatePaths[path].defaultValue

        # post-populate with data from primary services
        self._primaryServices.merge_into(self)

        return True

    def publish(self):
        self.update()
        for k, v in self._local_values.items():
            self.service[k] = v
        return True

    def __str__(self):
        return self.service.serviceName


class VirtualBatteryService(SettableService):
    def __init__(self, conn, serviceName, config):
        super().__init__()
        mergedServiceNames = list(config)
        for name in [serviceName] + mergedServiceNames:
            if not name.startswith("com.victronenergy.battery."):
                raise ValueError(f"Invalid service name: {name}")

        offset = hashlib.sha1(serviceName.split('.')[-1].encode('utf-8')).digest()[0]
        self.service = VeDbusService(serviceName, conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', BASE_DEVICE_INSTANCE_ID + offset,
                                     0, "Virtual Battery", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.add_settable_path("/CustomName", "")
        for path, defn in BATTERY_PATHS.items():
            self.service.add_path(path, None, gettextcallback=defn.unit.gettextcallback)
        self.service.add_path("/System/Batteries", json.dumps(mergedServiceNames))

        self._mergedServices = DataMerger(config)

        self._init_settings(conn)

        self._local_values = {}
        for path in self.service._dbusobjects:
            self._local_values[path] = self.service[path]

        options = None  # currently not used afaik
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {path: options for path in BATTERY_PATHS}
            },
            includedServiceNames=mergedServiceNames,
            excludedServiceNames=[serviceName]
        )

    def _get_value(self, serviceName, path, defaultValue=None):
        return self.monitor.get_value(serviceName, path, defaultValue)

    def update(self):
        self._mergedServices.merge_into(self)
        return True

    def publish(self):
        self.update()
        for k, v in self._local_values.items():
            self.service[k] = v
        return True

    def __str__(self):
        return self.service.serviceName


def main(virtualBatteryName=None):
    DBusGMainLoop(set_as_default=True)
    setupOptions = Path("/data/setupOptions/BatteryAggregator")
    configFile = setupOptions/"config.json"
    config = {}
    try:
        with configFile.open() as f:
            config = json.load(f)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError:
        logger.warning("Ignoring invalid JSON file")

    virtualBatteryConfigs = config.get("virtualBatteries", {})
    if virtualBatteryName:
        virtualBatteryConfig = virtualBatteryConfigs[virtualBatteryName]
        virtualBattery = VirtualBatteryService(dbusConnection(), virtualBatteryName, virtualBatteryConfig)
        GLib.timeout_add(250, virtualBattery.publish)
        logger.info(f"Registered Virtual Battery {virtualBattery.service.serviceName}")
    else:
        virtualBatteryConfigs = config.get("virtualBatteries", {})
        processes = []
        for virtualBatteryName in virtualBatteryConfigs:
            p = multiprocessing.Process(target=main, name=virtualBatteryName, args=(virtualBatteryName,), daemon=True)
            processes.append(p)
            p.start()

        def kill_handler(signum, frame):
            for p in processes:
                p.terminate()
                p.join()
                p.close()
                logger.info(f"Stopped child process {p.name}")
            logger.info("Exit")
            exit(0)

        signal.signal(signal.SIGTERM, kill_handler)

        batteryAggr = BatteryAggregatorService(dbusConnection(), DEFAULT_SERVICE_NAME, config)

        max_attempts = config.get("startupBatteryWait", 30)
        attempts = 0

        def wait_for_batteries():
            nonlocal attempts
            logger.info(f"Waiting for batteries (attempt {attempts+1} of {max_attempts})...")
            if len(batteryAggr.get_battery_service_names()) > 0:
                batteryAggr.register(timeout=15)
                GLib.timeout_add(250, batteryAggr.publish)
                logger.info(f"Registered Battery Aggregator {batteryAggr.service.serviceName}")
                return False
            else:
                attempts += 1
                if attempts < max_attempts:
                    return True
                else:
                    logger.warning("No batteries discovered!")
                    signal.raise_signal(signal.SIGTERM)
                    return False

        GLib.timeout_add_seconds(1, wait_for_batteries)

    mainloop = GLib.MainLoop()
    mainloop.run()


if __name__ == "__main__":
    main()
