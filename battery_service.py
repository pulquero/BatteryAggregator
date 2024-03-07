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


def _sum(newValue, currentValue):
    return newValue + currentValue


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
    def __init__(self, op, initial_value=None, requires=None):
        self.values = {}
        self.op = op
        self.initial_value = initial_value
        self.requires = requires

    def set(self, name, x):
        self.values[name] = x

    def get_result(self):
        r = self.initial_value
        for v in self.values.values():
            if v is not None:
                r = self.op(v, r)
        return r


class MeanAggregator:
    def __init__(self, initial_value=None, requires=None):
        self.values = {}
        self.initial_value = initial_value
        self.requires = requires

    def set(self, name, x):
        self.values[name] = x

    def get_result(self):
        _sum = 0
        _count = 0
        for v in self.values.values():
            if v is not None:
                _sum += v
                _count += 1
        return _sum/_count if _count > 0 else self.initial_value


SumAggregator = functools.partial(Aggregator, _sum, initial_value=0)
MinAggregator = functools.partial(Aggregator, _safe_min)
MaxAggregator = functools.partial(Aggregator, _safe_max)
AlarmAggregator = functools.partial(Aggregator, max, initial_value=ALARM_OK)
BooleanAggregator = functools.partial(Aggregator, _safe_max)
MaxChargeCurrentAggregator = functools.partial(Aggregator, _safe_sum, requires={"/Io/AllowToCharge": 1})
MaxChargeVoltageAggregator = functools.partial(Aggregator, _safe_min)
MaxDischargeCurrentAggregator = functools.partial(Aggregator, _safe_sum, requires={"/Io/AllowToDischarge": 1})
Mean0Aggregator = functools.partial(MeanAggregator, initial_value=0)


class PathDefinition:
    def __init__(self, unit=NO_UNIT, aggregatorClass=None):
        self.unit = unit
        self.aggregatorClass = aggregatorClass


BATTERY_PATHS = {
    '/Dc/0/Current': PathDefinition(CURRENT, SumAggregator),
    '/Dc/0/Voltage': PathDefinition(VOLTAGE, Mean0Aggregator),
    '/Dc/0/Power':  PathDefinition(POWER, SumAggregator),
    '/Dc/0/Temperature':  PathDefinition(TEMPERATURE, MeanAggregator),
    '/Soc':  PathDefinition(NO_UNIT, Mean0Aggregator),
    '/TimeToGo':  PathDefinition(NO_UNIT, Mean0Aggregator),
    '/Capacity' : PathDefinition(AMP_HOURS, SumAggregator),
    '/InstalledCapacity' : PathDefinition(AMP_HOURS, SumAggregator),
    '/ConsumedAmphours': PathDefinition(AMP_HOURS, SumAggregator),
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
    '/System/NrOfModulesBlockingCharge': PathDefinition(NO_UNIT, SumAggregator),
    '/System/NrOfModulesBlockingDischarge': PathDefinition(NO_UNIT, SumAggregator),
    '/System/NrOfModulesOnline': PathDefinition(NO_UNIT, SumAggregator),
    '/System/NrOfModulesOffline': PathDefinition(NO_UNIT, SumAggregator),
    '/Alarms/CellImbalance': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/LowSoc': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/HighDischargeCurrent': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/LowVoltage': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/HighVoltage': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/LowCellVoltage': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/HighCellVoltage': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/LowTemperature': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/HighTemperature': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/LowChargeTemperature': PathDefinition(aggregatorClass=AlarmAggregator),
    '/Alarms/HighChargeTemperature': PathDefinition(aggregatorClass=AlarmAggregator),
}


class DataMerger:
    def __init__(self, config):
        if isinstance(config, list):
            # convert short-hand format
            expanded_config = {serviceName: list(BATTERY_PATHS) for serviceName in config}
        elif isinstance(config, dict):
            expanded_config = {}
            for k, v in config.items():
                if not v:
                    v = BATTERY_PATHS
                expanded_config[k] = v
        elif config is None:
            expanded_config = {}
        else:
            raise ValueError(f"Unsupported config object: {type(config)}")

        self.service_names = list(expanded_config)

        self.data_by_path = {}
        for service_name, path_list in expanded_config.items():
            for p in path_list:
                path_values = self.data_by_path.get(p)
                if path_values is None:
                    path_values = {}
                    self.data_by_path[p] = path_values
                path_values[service_name] = None

    def init_values(self, api):
        for p, path_values in self.data_by_path.items():
            for service_name in self.service_names:
                path_values[service_name] = api._get_value(service_name, p)

    def update_service_value(self, serviceName, path, value):
        path_values = self.data_by_path.get(path)
        if path_values:
            if serviceName in path_values:
                path_values[serviceName] = value

    def get_value(self, path):
        path_values = self.data_by_path.get(path)
        if path_values:
            for service_name in self.service_names:
                v = path_values.get(service_name)
                if v is not None:
                    return v
        return None


class BatteryAggregatorService(SettableService):
    def __init__(self, conn, serviceName, config):
        super().__init__()
        if not serviceName.startswith("com.victronenergy.battery."):
            raise ValueError(f"Invalid service name: {serviceName}")

        self.logger = logging.getLogger(serviceName)
        self.service = None
        self._conn = conn
        self._serviceName = serviceName
        self._configuredCapacity = config.get("capacity")
        scanPaths = set(BATTERY_PATHS.keys())
        if self._configuredCapacity:
            scanPaths.remove('/InstalledCapacity')
            scanPaths.remove('/Capacity')

        self.battery_service_names = []

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
            valueChangedCallback=self._battery_value_changed,
            deviceAddedCallback=self._battery_added,
            deviceRemovedCallback=self._battery_removed,
            excludedServiceNames=excludedServices
        )
        self.battery_service_names = list(self.monitor.servicesByName)

        self.aggregators = {}
        self.pathDependencies = {}
        for path in scanPaths:
            aggr = BATTERY_PATHS[path].aggregatorClass()
            self.aggregators[path] = aggr
            if aggr.requires:
                for depPath in aggr.requires:
                    pathList = self.pathDependencies.get(depPath)
                    if pathList is None:
                        pathList = []
                        self.pathDependencies[depPath] = pathList
                    pathList.append(path)

        self._previousOvercurrentCCL = None

    def register(self, timeout):
        self.service = VeDbusService(self._serviceName, self._conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', DEVICE_INSTANCE_ID,
                                     0, "Battery Aggregator", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.add_settable_path("/CustomName", "")
        for path, defn in BATTERY_PATHS.items():
            aggr = self.aggregators[path]
            self.service.add_path(path, aggr.initial_value, gettextcallback=defn.unit.gettextcallback)
        if self._configuredCapacity:
            self.service['/InstalledCapacity'] = self._configuredCapacity
            self.service['/Capacity'] = None
        self.service.add_path("/System/Batteries", None)
        self.service.add_path("/System/NrOfBatteries", 0)
        self.service.add_path("/System/BatteriesParallel", 0)
        self.service.add_path("/System/BatteriesSeries", 1)

        self._init_settings(self._conn, timeout=timeout)

        # initial values
        self._primaryServices.init_values(self)
        self._auxiliaryServices.init_values(self)
        self._batteries_changed()
        for path in BATTERY_PATHS:
            for battery_name in self.battery_service_names:
                aggr = self._update_aggregator(battery_name, path, self._get_value(battery_name, path))
            aggr_result = self._get_aggregated_value(path)
            self._refresh_value(path, aggr_result)

    def _update_aggregator(self, dbusServiceName, dbusPath, value):
        aggr = self.aggregators[dbusPath]
        can_aggregate = True
        if aggr.requires:
            for requiredPath, requiredValue in aggr.requires.items():
                actualValue = self._get_value(dbusServiceName, requiredPath, requiredValue)
                if actualValue != requiredValue:
                    can_aggregate = False
                    break
        aggr.set(dbusServiceName, value if can_aggregate else None)

    def _get_aggregated_value(self, dbusPath):
        aggr = self.aggregators[dbusPath]
        aggr_result = aggr.get_result()

        if dbusPath == "/Info/MaxChargeCurrent":
            # check for over-current and scale back
            maxOvercurrentRatio = 0
            maxOvercurrentBatteryName = -1
            for batteryName, ccl in aggr.values.items():
                batteryCurrent = self.aggregators["/Dc/0/Current"].values.get(batteryName, 0)
                if ccl is not None and batteryCurrent > ccl:
                    batteryOvercurrentRatio = batteryCurrent/ccl if ccl > 0 else float("inf")
                    if batteryOvercurrentRatio > maxOvercurrentRatio:
                        maxOvercurrentRatio = batteryOvercurrentRatio
                        maxOvercurrentBatteryName = batteryName
            if maxOvercurrentRatio > 1:
                scaledCCL = aggr_result / maxOvercurrentRatio
                if self._previousOvercurrentCCL != scaledCCL:
                    self.logger.info(f"Max charge current is {aggr_result} but scaling back to {scaledCCL} as limit exceeded for battery {maxOvercurrentBatteryName} (overcurrent ratio: {maxOvercurrentRatio})")
                    logMsg = "Battery currents:\n"
                    for batteryName, ccl in aggr.values.items():
                        batteryCurrent = self.aggregators["/Dc/0/Current"].values.get(batteryName, 0)
                        logMsg += f"{batteryName}: {batteryCurrent}, limit {ccl}\n"
                    self.logger.info(logMsg)
                    self._previousOvercurrentCCL = scaledCCL
                aggr_result = scaledCCL

        elif dbusPath == "/Info/MaxChargeVoltage":
            # check for under-voltage
            if self.service["/Balancing"] == 1:
                # use max voltage for balancing
                minCVL = aggr_result
                maxCVL = max([v for v in aggr.values.values() if v is not None])
                if maxCVL > minCVL:
                    self.logger.info(f"Min charge voltage is {minCVL} but using max of {maxCVL} as balancing")
                    aggr_result = maxCVL

        return aggr_result

    def _refresh_value(self, dbusPath, aggr_result):
        v = self._primaryServices.get_value(dbusPath)
        if v is None:
            v = aggr_result
            if v is None:
                v = self._auxiliaryServices.get_value(dbusPath)
        self.service[dbusPath] = v

    def _get_value(self, serviceName, path, defaultValue=None):
        return self.monitor.get_value(serviceName, path, defaultValue)

    def _battery_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        # self.logger.info(f"Battery value changed: {dbusServiceName} {dbusPath}")
        value = changes['Value']
        self._update_battery_value(dbusServiceName, dbusPath, value)

    def _update_battery_value(self, dbusServiceName, dbusPath, value):
        if self.service:

            self._primaryServices.update_service_value(dbusServiceName, dbusPath, value)
            self._auxiliaryServices.update_service_value(dbusServiceName, dbusPath, value)
            self._update_aggregator(dbusServiceName, dbusPath, value)
            aggr_result = self._get_aggregated_value(dbusPath)
            self._refresh_value(dbusPath, aggr_result)

            pathDeps = self.pathDependencies.get(dbusPath)
            if pathDeps:
                for p in pathDeps:
                    self._update_battery_path(dbusServiceName, p, self.service[p])

    def _battery_added(self, dbusServiceName, deviceInstance):
        # self.logger.info(f"Battery added: {dbusServiceName}")
        self.battery_service_names.append(dbusServiceName)
        self._batteries_changed()

    def _battery_removed(self, dbusServiceName, deviceInstance):
        # self.logger.info(f"Battery removed: {dbusServiceName}")
        self.battery_service_names.remove(dbusServiceName)
        self._batteries_changed()

    def _batteries_changed(self):
        if self.service:
            batteryCount = len(self.battery_service_names)
            self.service["/System/Batteries"] = json.dumps(self.battery_service_names)
            self.service["/System/NrOfBatteries"] = batteryCount
            self.service["/System/BatteriesParallel"] = batteryCount

    def __str__(self):
        return self._serviceName


class VirtualBatteryService(SettableService):
    def __init__(self, conn, serviceName, config):
        super().__init__()
        self.logger = logging.getLogger(serviceName)
        self.service = None
        self._conn = conn
        self._serviceName = serviceName

        for name in [serviceName] + list(config):
            if not name.startswith("com.victronenergy.battery."):
                raise ValueError(f"Invalid service name: {name}")

        self._mergedServices = DataMerger(config)

        options = None  # currently not used afaik
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {path: options for path in BATTERY_PATHS}
            },
            valueChangedCallback=self._battery_value_changed,
            includedServiceNames=self._mergedServices.service_names,
            excludedServiceNames=[serviceName]
        )

    def register(self, timeout=0):
        self.service = VeDbusService(self._serviceName, self._conn)
        id_offset = hashlib.sha1(self._serviceName.split('.')[-1].encode('utf-8')).digest()[0]
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', BASE_DEVICE_INSTANCE_ID + id_offset,
                                     0, "Virtual Battery", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.add_settable_path("/CustomName", "")
        for path, defn in BATTERY_PATHS.items():
            self.service.add_path(path, None, gettextcallback=defn.unit.gettextcallback)
        self.service.add_path("/System/Batteries", json.dumps(self._mergedServices.service_names))

        self._init_settings(self._conn, timeout=timeout)

        self._mergedServices.init_values(self)
        for path in BATTERY_PATHS:
            v = self._mergedServices.get_value(path)
            self.service[path] = v

    def _get_value(self, serviceName, path, defaultValue=None):
        return self.monitor.get_value(serviceName, path, defaultValue)

    def _battery_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        # self.logger.info(f"Battery value changed: {dbusServiceName} {dbusPath}")
        value = changes['Value']
        self._update_battery_value(dbusServiceName, dbusPath, value)

    def _update_battery_value(self, dbusServiceName, dbusPath, value):
        if self.service:
            self._mergedServices.update_service_value(dbusServiceName, dbusPath, value)
            self.service[dbusPath] = self._mergedServices.get_value(dbusPath)

    def __str__(self):
        return self._serviceName


def main(virtualBatteryName=None):
    logSubName = f"[{virtualBatteryName}]" if virtualBatteryName is not None else ""
    logger = logging.getLogger(f"main{logSubName}")
    logger.info("Starting...")
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
        virtualBattery.register(timeout=15)
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
            if len(batteryAggr.battery_service_names) > 0:
                batteryAggr.register(timeout=15)
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
