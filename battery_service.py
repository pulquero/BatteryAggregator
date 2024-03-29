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


VOLTAGE_TEXT = lambda path,value: "{:.3f}V".format(value)
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


class AbstractAggregator:
    def __init__(self, initial_value=None):
        self.values = {}
        self.initial_value = initial_value

    def set(self, name, x):
        self.values[name] = x

    def unset(self, name):
        del self.values[name]

    def get_result(self):
        ...


class Aggregator(AbstractAggregator):
    def __init__(self, op, initial_value=None):
        super().__init__(initial_value=initial_value)
        self.op = op

    def get_result(self):
        r = self.initial_value
        for v in self.values.values():
            if v is not None:
                r = self.op(v, r)
        return r


class MeanAggregator(AbstractAggregator):
    def __init__(self, initial_value=None):
        super().__init__(initial_value=initial_value)

    def get_result(self):
        _sum = 0
        _count = 0
        for v in self.values.values():
            if v is not None:
                _sum += v
                _count += 1
        return _sum/_count if _count > 0 else self.initial_value


class NullAggregator(AbstractAggregator):
    def __init__(self):
        super().__init__(initial_value=None)

    def get_result(self):
        return None


SumAggregator = functools.partial(Aggregator, _sum, initial_value=0)
MinAggregator = functools.partial(Aggregator, _safe_min)
MaxAggregator = functools.partial(Aggregator, _safe_max)
AlarmAggregator = functools.partial(Aggregator, max, initial_value=ALARM_OK)
BooleanAggregator = functools.partial(Aggregator, _safe_max)
Mean0Aggregator = functools.partial(MeanAggregator, initial_value=0)


class PathDefinition:
    def __init__(self, unit=NO_UNIT, aggregatorClass=None, triggerPaths=None, action=None):
        self.unit = unit
        self.aggregatorClass = aggregatorClass
        self.triggerPaths = triggerPaths
        self.action = action


AGGREGATED_BATTERY_PATHS = {
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

ACTIVE_BATTERY_PATHS = {
    '/Info/MaxChargeCurrent': PathDefinition(CURRENT, aggregatorClass=NullAggregator, triggerPaths={'/Info/MaxChargeCurrent', '/Dc/0/Current', '/Dc/0/Voltage'}, action=lambda api: api._updateCCL()),
    '/Info/MaxChargeVoltage': PathDefinition(VOLTAGE, aggregatorClass=NullAggregator, triggerPaths={'/Info/MaxChargeVoltage', '/Balancing'}, action=lambda api: api._updateCVL()),
    '/Info/MaxDischargeCurrent': PathDefinition(CURRENT, aggregatorClass=NullAggregator, triggerPaths={'/Info/MaxDischargeCurrent', '/Dc/0/Current', '/Dc/0/Voltage'}, action=lambda api: api._updateDCL()),
}

BATTERY_PATHS = {**AGGREGATED_BATTERY_PATHS, **ACTIVE_BATTERY_PATHS}


class DataMerger:
    def __init__(self, config):
        if isinstance(config, list):
            # convert short-hand format
            expanded_config = {serviceName: list(BATTERY_PATHS) for serviceName in config}
        elif isinstance(config, dict):
            expanded_config = {}
            for k, v in config.items():
                if not v:
                    v = list(BATTERY_PATHS)
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

    def init_values(self, service_name, api):
        paths_changed = []
        for p, path_values in self.data_by_path.items():
            if service_name in path_values:
                path_values[service_name] = api.get_value(service_name, p)
                paths_changed.append(p)
        return paths_changed

    def clear_values(self, service_name):
        paths_changed = []
        for p, path_values in self.data_by_path.items():
            if service_name in path_values:
                path_values[service_name] = None
                paths_changed.append(p)
        return paths_changed

    def update_service_value(self, service_name, path, value):
        path_values = self.data_by_path.get(path)
        if path_values:
            if service_name in path_values:
                path_values[service_name] = value

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
        self._registered = False
        self._conn = conn
        self._serviceName = serviceName
        self._configuredCapacity = config.get("capacity")
        scanPaths = set(BATTERY_PATHS.keys())
        if self._configuredCapacity:
            scanPaths.remove('/InstalledCapacity')
            scanPaths.remove('/Capacity')

        self._primaryServices = DataMerger(config.get("primaryServices"))
        self._auxiliaryServices = DataMerger(config.get("auxiliaryServices"))
        otherServiceNames = set()
        otherServiceNames.update(self._primaryServices.service_names)
        otherServiceNames.update(self._auxiliaryServices.service_names)

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

        
        self.battery_service_names = [service_name for service_name in self.monitor.servicesByName if service_name not in otherServiceNames]

        self.aggregators = {}
        for path in scanPaths:
            aggr = BATTERY_PATHS[path].aggregatorClass()
            self.aggregators[path] = aggr

    def register(self, timeout):
        self.service = VeDbusService(self._serviceName, self._conn)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', DEVICE_INSTANCE_ID,
                                     0, "Battery Aggregator", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.add_settable_path("/CustomName", "")
        for path, aggr in self.aggregators.items():
            defn = BATTERY_PATHS[path]
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
        paths_changed = set()

        for battery_name in self._primaryServices.service_names:
            if battery_name in self.monitor.servicesByName:
                changed = self._primaryServices.init_values(battery_name, self.monitor)
                paths_changed.update(changed)

        for battery_name in self._auxiliaryServices.service_names:
            if battery_name in self.monitor.servicesByName:
                changed = self._auxiliaryServices.init_values(battery_name, self.monitor)
                paths_changed.update(changed)

        for path in self.aggregators:
            for battery_name in self.battery_service_names:
                self.aggregators[path].set(battery_name, self.monitor.get_value(battery_name, path))
            paths_changed.add(path)

        self._refresh_values(paths_changed)
        self._batteries_changed()

        self._registered = True

    def _get_value(self, dbusPath):
        aggr = self.aggregators.get(dbusPath)
        if aggr:
            return aggr.get_result()
        else:
            return self.service[dbusPath]

    def _refresh_value(self, dbusPath):
        v = self._primaryServices.get_value(dbusPath)
        if v is None:
            v = self._get_value(dbusPath)
            if v is None:
                v = self._auxiliaryServices.get_value(dbusPath)

        # don't bother updating active paths yet
        if dbusPath not in ACTIVE_BATTERY_PATHS:
            self.service[dbusPath] = v

        self._update_active_values(dbusPath)

    def _refresh_values(self, paths_changed):
        for path in paths_changed:
            self._refresh_value(path)

    def _battery_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        # self.logger.info(f"Battery value changed: {dbusServiceName} {dbusPath}")
        value = changes['Value']
        if dbusServiceName in self._primaryServices.service_names:
            if self._registered:
                self._primaryServices.update_service_value(dbusServiceName, dbusPath, value)
        elif dbusServiceName in self._auxiliaryServices.service_names:
            if self._registered:
                self._auxiliaryServices.update_service_value(dbusServiceName, dbusPath, value)
        else:
            if self._registered:
                self.aggregators[dbusPath].set(dbusServiceName, value)

        if self._registered:
            self._refresh_value(dbusPath)

    def _battery_added(self, dbusServiceName, deviceInstance):
        # self.logger.info(f"Battery added: {dbusServiceName}")
        paths_changed = None
        if dbusServiceName in self._primaryServices.service_names:
            if self._registered:
                paths_changed = self._primaryServices.init_values(dbusServiceName, self.monitor)
        elif dbusServiceName in self._auxiliaryServices.service_names:
            if self._registered:
                paths_changed = self._auxiliaryServices.init_values(dbusServiceName, self.monitor)
        else:
            self.battery_service_names.append(dbusServiceName)
            if self._registered:
                for path in self.aggregators:
                    self.aggregators[path].set(dbusServiceName, self.monitor.get_value(dbusServiceName, path))
                paths_changed = self.aggregators
                self._batteries_changed()

        if paths_changed:
            self._refresh_values(paths_changed)


    def _battery_removed(self, dbusServiceName, deviceInstance):
        # self.logger.info(f"Battery removed: {dbusServiceName}")
        paths_changed = None
        if dbusServiceName in self._primaryServices.service_names:
            if self._registered:
                paths_changed = self._primaryServices.clear_values(dbusServiceName)
        elif dbusServiceName in self._auxiliaryServices.service_names:
            if self._registered:
                paths_changed = self._auxiliaryServices.clear_values(dbusServiceName)
        else:
            self.battery_service_names.remove(dbusServiceName)
            if self._registered:
                for path in self.aggregators:
                    self.aggregators[path].unset(dbusServiceName)
                paths_changed = self.aggregators
                self._batteries_changed()

        if paths_changed:
            self._refresh_values(paths_changed)

    def _batteries_changed(self):
        batteryCount = len(self.battery_service_names)
        self.service["/System/Batteries"] = json.dumps(self.battery_service_names)
        self.service["/System/NrOfBatteries"] = batteryCount
        self.service["/System/BatteriesParallel"] = batteryCount

    def _update_active_values(self, dbusPath):
        for defn in ACTIVE_BATTERY_PATHS.values():
            if dbusPath in defn.triggerPaths:
                defn.action(self)

    def _updateCCL(self):
        aggr_current = self.aggregators["/Dc/0/Current"]
        aggr_voltage = self.aggregators["/Dc/0/Voltage"]

        irs = {}  # internal resistances
        for batteryName, current in aggr_current.values.items():
            voltage = aggr_voltage.values.get(batteryName, 0)
            if current is not None and voltage is not None and current > 0:
                irs[batteryName] = voltage/current
        total_ir = 1.0/sum([1.0/ir for ir in irs.values()]) if irs else 0

        aggr_ccl = self.aggregators["/Info/MaxChargeCurrent"]
        battery_count = len(self.battery_service_names)
        ccls = []
        for batteryName, ccl in aggr_ccl.values.items():
            if ccl is not None:
                ir = irs.get(batteryName, 0)
                if ir and total_ir:
                    ccls.append(ccl*ir/total_ir)
                else:
                    ccls.append(ccl*battery_count)

        self.service["/Info/MaxChargeCurrent"] = min(ccls) if ccls else None

    def _updateDCL(self):
        aggr_current = self.aggregators["/Dc/0/Current"]
        aggr_voltage = self.aggregators["/Dc/0/Voltage"]

        irs = {}  # internal resistances
        for batteryName, current in aggr_current.values.items():
            voltage = aggr_voltage.values.get(batteryName, 0)
            if current is not None and voltage is not None and current > 0:
                irs[batteryName] = voltage/current
        total_ir = 1.0/sum([1.0/ir for ir in irs.values()]) if irs else 0

        aggr_dcl = self.aggregators["/Info/MaxDischargeCurrent"]
        battery_count = len(self.battery_service_names)
        dcls = []
        for batteryName, dcl in aggr_dcl.values.items():
            if dcl is not None:
                ir = irs.get(batteryName, 0)
                if ir and total_ir:
                    dcls.append(dcl*ir/total_ir)
                else:
                    dcls.append(dcl*battery_count)

        self.service["/Info/MaxDischargeCurrent"] = min(dcls) if dcls else None

    def _updateCVL(self):
        aggr_cvl = self.aggregators["/Info/MaxChargeVoltage"]
        cvls = []
        for cvl in aggr_cvl.values.values():
            if cvl is not None:
                cvls.append(cvl)
        if self.service["/Balancing"] == 1:
            op = max
        else:
            op = min
        self.service["/Info/MaxChargeVoltage"] = op(cvls) if cvls else None

    def __str__(self):
        return self._serviceName


class VirtualBatteryService(SettableService):
    def __init__(self, conn, serviceName, config):
        super().__init__()
        self.logger = logging.getLogger(serviceName)
        self.service = None
        self._registered = False
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
            deviceAddedCallback=self._battery_added,
            deviceRemovedCallback=self._battery_removed,
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
        self.service.add_path("/System/Batteries", json.dumps(list(self.monitor.servicesByName)))

        self._init_settings(self._conn, timeout=timeout)

        paths_changed = set()
        for batteryName in self.monitor.servicesByName:
            changed = self._mergedServices.init_values(batteryName, self.monitor)
            paths_changed.update(changed)
        self._refresh_values(paths_changed)

        self._registered = True

    def _refresh_values(self, paths_changed):
        for path in paths_changed:
            self.service[path] = self._mergedServices.get_value(path)

    def _battery_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        # self.logger.info(f"Battery value changed: {dbusServiceName} {dbusPath}")
        if self._registered:
            value = changes['Value']
            self._mergedServices.update_service_value(dbusServiceName, dbusPath, value)
            self.service[dbusPath] = self._mergedServices.get_value(dbusPath)

    def _battery_added(self, dbusServiceName, deviceInstance):
        # self.logger.info(f"Battery added: {dbusServiceName}")
        if self._registered:
            paths_changed = self._mergedServices.init_values(dbusServiceName, self.monitor)
            self._refresh_values(paths_changed)
            self.service["/System/Batteries"] = json.dumps(list(self.monitor.servicesByName))

    def _battery_removed(self, dbusServiceName, deviceInstance):
        # self.logger.info(f"Battery removed: {dbusServiceName}")
        if self._registered:
            paths_changed = self._mergedServices.clear_values(dbusServiceName)
            self._refresh_values(paths_changed)
            self.service["/System/Batteries"] = json.dumps(list(self.monitor.servicesByName))

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
