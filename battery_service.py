#!/usr/bin/env python

import os
import sys
from script_utils import SCRIPT_HOME, VERSION
sys.path.insert(1, os.path.join(os.path.dirname(__file__), f"{SCRIPT_HOME}/ext"))

from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib
import logging
from vedbus import VeDbusService
from ve_utils import unwrap_dbus_value
from dbus_utils import dbusConnection
from dbusmonitor import DbusMonitor
from settableservice import SettableService
from data_merger import DataMerger
from service_name_resolver import ServiceNameResolver
from hooks import Hook
from collections import deque, namedtuple
import math
import functools
import hashlib
import json
from pathlib import Path
import multiprocessing
import signal
from pydoc import locate

DEFAULT_SERVICE_NAME = 'com.victronenergy.battery.aggregator'
DEVICE_INSTANCE_ID = 1024
FIRMWARE_VERSION = 0
HARDWARE_VERSION = 0
CONNECTED = 1

BASE_DEVICE_INSTANCE_ID = DEVICE_INSTANCE_ID + 32

ALARM_OK = 0
ALARM_WARNING = 1
ALARM_ALARM = 2

VOLTAGE_HISTORY_SIZE = 10
MIN_VOLTAGE_DELTA = 0.02
MAX_IR_ERROR_PERCENTAGE = 0.1

logging.basicConfig(level=logging.INFO)


def is_battery_service_name(service_name):
    return service_name.startswith("com.victronenergy.battery.")


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

    def has_values(self):
        for v in self.values.values():
            if v is not None:
                return True
        return False

    def get_value_count(self):
        _count = 0
        for v in self.values.values():
            if v is not None:
                _count += 1
        return _count

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


class AvailableAggregator(AbstractAggregator):
    def __init__(self):
        super().__init__(initial_value=None)

    def get_result(self):
        return self.get_value_count()


SumAggregator = functools.partial(Aggregator, _sum, initial_value=0)
MinAggregator = functools.partial(Aggregator, _safe_min)
MaxAggregator = functools.partial(Aggregator, _safe_max)
AlarmAggregator = functools.partial(Aggregator, max, initial_value=ALARM_OK)
BooleanAggregator = functools.partial(Aggregator, _safe_max)
Mean0Aggregator = functools.partial(MeanAggregator, initial_value=0)


class PathDefinition:
    def __init__(self, unit, aggregatorClass):
        self.unit = unit
        self.aggregatorClass = aggregatorClass


class ActivePathDefinition(PathDefinition):
    def __init__(self, unit, triggerPaths=None, action=None):
        super().__init__(unit, AvailableAggregator)
        self.triggerPaths = triggerPaths
        self.action = action


AGGREGATED_BATTERY_PATHS = {
    '/Dc/0/Current': PathDefinition(CURRENT, SumAggregator),
    '/Dc/0/Voltage': PathDefinition(VOLTAGE, Mean0Aggregator),
    '/Dc/0/Power':  PathDefinition(POWER, SumAggregator),
    '/Dc/0/Temperature':  PathDefinition(TEMPERATURE, MaxAggregator),
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
    '/Alarms/CellImbalance': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/LowSoc': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/HighDischargeCurrent': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/LowVoltage': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/HighVoltage': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/LowCellVoltage': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/HighCellVoltage': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/LowTemperature': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/HighTemperature': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/LowChargeTemperature': PathDefinition(NO_UNIT, AlarmAggregator),
    '/Alarms/HighChargeTemperature': PathDefinition(NO_UNIT, AlarmAggregator),
}

ACTIVE_BATTERY_PATHS = {
    '/Info/MaxChargeCurrent': ActivePathDefinition(CURRENT, triggerPaths={'/Info/MaxChargeCurrent', '/Io/AllowToCharge'}, action=lambda api: api._updateCCL()),
    '/Info/MaxChargeVoltage': ActivePathDefinition(VOLTAGE, triggerPaths={'/Info/MaxChargeVoltage', '/Balancing'}, action=lambda api: api._updateCVL()),
    '/Info/MaxDischargeCurrent': ActivePathDefinition(CURRENT, triggerPaths={'/Info/MaxDischargeCurrent', '/Io/AllowToDischarge'}, action=lambda api: api._updateDCL()),
}

BATTERY_PATHS = {**AGGREGATED_BATTERY_PATHS, **ACTIVE_BATTERY_PATHS}


VoltageSample = namedtuple("VoltageSample", ["voltage", "current"])


class IRData:
    def __init__(self, percentage_error, min_voltage_change):
        self._measured_ir = 0
        self.correction = 1
        self.emf = 0
        self.err = 0
        self.percentage_error = percentage_error
        self.min_voltage_change = min_voltage_change
        self.history = deque()

    @property
    def ir(self):
        return self.correction * self._measured_ir

    def append_sample(self, voltage, current):
        if voltage <= 0:
            return False, False

        if not self.history or abs(voltage - self.history[-1].voltage) >= self.min_voltage_change:  # check for a significant change in voltage
            self.history.append(VoltageSample(voltage, current))
            if len(self.history) > VOLTAGE_HISTORY_SIZE:
                self.history.popleft()

                # total least squares
                N = len(self.history)
                sum_v = 0
                sum_i = 0
                for sample in self.history:
                    sum_v += sample.voltage
                    sum_i += sample.current
                mean_v = sum_v/N
                mean_i = sum_i/N

                var_v = 0
                var_i = 0
                var_iv = 0
                for sample in self.history:
                    var_v += (sample.voltage - mean_v)**2
                    var_i += (sample.current - mean_i)**2
                    var_iv += 2 * (sample.voltage - mean_v) * (sample.current - mean_i)

                if var_iv:
                    k = (var_v - var_i)/var_iv

                    ir = k + math.sqrt(k**2 + 1)
                    err = math.sqrt((ir**2 * var_i - ir * var_iv + var_v)/(N-2)) * (1 + ir**2)/math.sqrt(ir**2 * var_v + ir * var_iv + var_i)

                    if ir > 0 and err/ir <= self.percentage_error:
                        has_changed = abs(ir - self._measured_ir) > math.hypot(err, self.err)
                        self._measured_ir = ir
                        self.emf = mean_v + mean_i * ir
                        self.err = err
                        return True, has_changed

        return False, False


class BatteryAggregatorService(SettableService):
    def __init__(self, conn, serviceName, config):
        super().__init__()
        if not is_battery_service_name(serviceName):
            raise ValueError(f"Invalid service name: {serviceName}")

        self.logger = logging.getLogger(serviceName)
        self.service = None
        self._registered = False
        self._conn = conn
        self._serviceName = serviceName
        self._configuredCapacity = config.get("capacity")

        self._cvlMode = config.get("cvlMode", "max_when_balancing")
        self._currentRatioMethod = config.get("currentRatioMethod", "ir")
        ir_config = config.get("ir", {})
        self._ir_percentage_error = ir_config.get("maxPercentageError", MAX_IR_ERROR_PERCENTAGE)
        self._ir_min_voltage_change = ir_config.get("minVoltageChange", MIN_VOLTAGE_DELTA)

        self._irs = {}

        scanPaths = set(BATTERY_PATHS.keys())
        if self._configuredCapacity:
            scanPaths.remove('/InstalledCapacity')
            scanPaths.remove('/Capacity')

        serviceNameResolver = ServiceNameResolver(conn)
        default_config_paths = list(BATTERY_PATHS)
        self._primaryServices = DataMerger(config.get("primaryServices"), default_config_paths, serviceNameResolver)
        self._auxiliaryServices = DataMerger(config.get("auxiliaryServices"), default_config_paths, serviceNameResolver)
        otherServiceNames = set()
        otherServiceNames.add("com.victronenergy.system")
        otherServiceNames.add("com.victronenergy.settings")
        otherServiceNames.update(self._primaryServices.service_names)
        otherServiceNames.update(self._auxiliaryServices.service_names)

        excludedServices = [serviceName]
        for service_name in config.get("excludedServices", []):
            excludedServices.append(serviceNameResolver.resolve_service_name(service_name))
        virtualBatteryConfigs = config.get("virtualBatteries", {})
        for virtualBatteryConfig in virtualBatteryConfigs.values():
            excludedServices.extend(virtualBatteryConfig)

        options = None  # currently not used afaik
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {path: options for path in scanPaths},
                'com.victronenergy.system': {'/Control/Dvcc': options},
                'com.victronenergy.settings': {
                    '/Settings/SystemSetup/MaxChargeCurrent': options,
                    '/Settings/SystemSetup/MaxChargeVoltage': options
                }
            },
            valueChangedCallback=self._service_value_changed,
            deviceAddedCallback=self._battery_added,
            deviceRemovedCallback=self._battery_removed,
            ignoreServices=excludedServices
        )

        self.battery_service_names = [service_name for service_name in self.monitor.servicesByName if is_battery_service_name(service_name) and service_name not in otherServiceNames]

        self.aggregators = {}
        for path in scanPaths:
            aggr = BATTERY_PATHS[path].aggregatorClass()
            self.aggregators[path] = aggr

    def _create_IRData(self):
        return IRData(self._ir_percentage_error, self._ir_min_voltage_change)

    def _is_available(self, service_name):
        return service_name in self.monitor.servicesByName

    def has_valid_batteries(self):
        detected_voltage = None
        for battery_name in self.battery_service_names:
            value = self.monitor.get_value(battery_name, "/Dc/0/Voltage")
            if value:
                detected_voltage = value
        return bool(detected_voltage)

    def register(self, timeout):
        self.service = VeDbusService(self._serviceName, self._conn, register=False)
        self.add_settable_path("/CustomName", "")
        self._init_settings(self._conn, timeout=timeout)
        di = self.register_device_instance("battery", "BatteryAggregator", DEVICE_INSTANCE_ID)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', di,
                                     0, "Battery Aggregator", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        self.service.add_path("/LogLevel", "INFO", writeable=True, onchangecallback=lambda path, newValue: self._change_log_level(newValue))
        for path, aggr in self.aggregators.items():
            defn = BATTERY_PATHS[path]
            self.service.add_path(path, aggr.initial_value, gettextcallback=defn.unit.gettextcallback)
        if self._configuredCapacity:
            self.service.add_path("/InstalledCapacity", self._configuredCapacity, AMP_HOURS.gettextcallback)
        self.service.add_path("/System/Batteries", None)
        self.service.add_path("/System/InternalResistances", None)
        self.service.add_path("/System/EMFs", None)
        self.service.add_path("/System/NrOfBatteries", 0)
        self.service.add_path("/System/BatteriesParallel", 0)
        self.service.add_path("/System/BatteriesSeries", 1)

        # initial values
        paths_changed = set()

        for battery_name in self._primaryServices.service_names:
            if self._is_available(battery_name):
                changed = self._primaryServices.init_values(battery_name, self.monitor)
                paths_changed.update(changed)

        for battery_name in self._auxiliaryServices.service_names:
            if self._is_available(battery_name):
                changed = self._auxiliaryServices.init_values(battery_name, self.monitor)
                paths_changed.update(changed)

        for path in self.aggregators:
            for battery_name in self.battery_service_names:
                value = self.monitor.get_value(battery_name, path)
                self._set_aggregator_value(path, battery_name, value)
            paths_changed.add(path)

        for battery_name in self.battery_service_names:
            self._irs[battery_name] = self._create_IRData()

        self._batteries_changed()
        self._refresh_values(paths_changed)

        self.service.register()
        self._registered = True

    def _change_log_level(self, value):
        if value in ("DEBUG", "INFO", "WARNING", "ERROR", "FATAL", "CRITICAL"):
            self.logger.setLevel(value)
            return True
        else:
            return False

    def _set_aggregator_value(self, dbusPath, dbusServiceName, value):
        aggr = self.aggregators[dbusPath]
        aggr.set(dbusServiceName, value)
        if self.logger.isEnabledFor(logging.DEBUG):
            self.logger.debug(f"Aggregator for {dbusPath} updated with {{{dbusServiceName}: {value}}} now has values {aggr.values} with result {aggr.get_result()}")

    def _add_vi_sample(self, dbusServiceName, voltage, current):
        if voltage is not None and current is not None:
            irdata = self._irs[dbusServiceName]
            updated, changed = irdata.append_sample(voltage, current)
            if updated:
                self.logger.info(f"Internal resistance for {dbusServiceName} @ {voltage}V is {irdata.ir}+-{irdata.err}")
                self._refresh_internal_resistances()
                if changed:
                    self._updateCLs()

    def _refresh_internal_resistances(self):
        irs = []
        emfs = []
        for batteryName in self.battery_service_names:
            irdata = self._irs[batteryName]
            ir = irdata.ir if irdata else None
            irs.append(ir)
            emf = irdata.emf if irdata else None
            emfs.append(emf)
        self.service["/System/InternalResistances"] = json.dumps(irs)
        self.service["/System/EMFs"] = json.dumps(emfs)

    def _refresh_value(self, dbusPath):
        v = self._primaryServices.get_value(dbusPath)
        if v is None:
            aggr = self.aggregators.get(dbusPath)
            if aggr:
                v = aggr.get_result()
            else:
                v = self.service[dbusPath]
            if v is None or (aggr is not None and not aggr.has_values()):
                aux_v = self._auxiliaryServices.get_value(dbusPath)
                if aux_v is not None:
                    v = aux_v

        # don't bother updating active paths yet
        if dbusPath not in ACTIVE_BATTERY_PATHS:
            self.service[dbusPath] = v

        self._update_active_values(dbusPath)

    def _refresh_values(self, paths_changed):
        for path in paths_changed:
            self._refresh_value(path)

    def _service_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        if is_battery_service_name(dbusServiceName):
            self._battery_value_changed(dbusServiceName, dbusPath, options, changes, deviceInstance)
        elif dbusServiceName == "com.victronenergy.system":
            if dbusPath == "/Control/Dvcc":
                self._updateCVL()
                self._updateCCL()
        elif dbusServiceName == "com.victronenergy.settings":
            if dbusPath == "/Settings/SystemSetup/MaxChargeCurrent":
                self._updateCCL()
            elif dbusPath == "/Settings/SystemSetup/MaxChargeVoltage":
                self._updateCVL()

    def _battery_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        value = changes['Value']
        self.logger.debug(f"Battery value changed: {dbusServiceName} {dbusPath} {value}")
        if dbusServiceName in self._primaryServices.service_names:
            if self._registered:
                self._primaryServices.update_service_value(dbusServiceName, dbusPath, value)
        elif dbusServiceName in self._auxiliaryServices.service_names:
            if self._registered:
                self._auxiliaryServices.update_service_value(dbusServiceName, dbusPath, value)
        else:
            if self._registered:
                self._set_aggregator_value(dbusPath, dbusServiceName, value)
                if dbusPath == "/Dc/0/Voltage":
                    voltage = value
                    current = self.monitor.get_value(dbusServiceName, "/Dc/0/Current")
                    self._add_vi_sample(dbusServiceName, voltage, current)
                elif dbusPath == "/Dc/0/Current" and self._currentRatioMethod == "ir":
                    current = value
                    self._adjust_ir(dbusServiceName, current)

        if self._registered:
            self._refresh_value(dbusPath)

    def _battery_added(self, dbusServiceName, deviceInstance):
        self.logger.debug(f"Battery added: {dbusServiceName}")
        paths_changed = None
        if dbusServiceName in self._primaryServices.service_names:
            if self._registered:
                paths_changed = self._primaryServices.init_values(dbusServiceName, self.monitor)
        elif dbusServiceName in self._auxiliaryServices.service_names:
            if self._registered:
                paths_changed = self._auxiliaryServices.init_values(dbusServiceName, self.monitor)
        elif is_battery_service_name(dbusServiceName):
            self.battery_service_names.append(dbusServiceName)
            self._irs[dbusServiceName] = self._create_IRData()
            if self._registered:
                for path in self.aggregators:
                    self.aggregators[path].set(dbusServiceName, self.monitor.get_value(dbusServiceName, path))
                paths_changed = self.aggregators
                self._batteries_changed()

        if paths_changed:
            self._refresh_values(paths_changed)


    def _battery_removed(self, dbusServiceName, deviceInstance):
        self.logger.debug(f"Battery removed: {dbusServiceName}")
        paths_changed = None
        if dbusServiceName in self._primaryServices.service_names:
            if self._registered:
                paths_changed = self._primaryServices.clear_values(dbusServiceName)
        elif dbusServiceName in self._auxiliaryServices.service_names:
            if self._registered:
                paths_changed = self._auxiliaryServices.clear_values(dbusServiceName)
        elif is_battery_service_name(dbusServiceName):
            self.battery_service_names.remove(dbusServiceName)
            del self._irs[dbusServiceName]
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
        self._refresh_internal_resistances()
        self._updateCLs()

    def _update_active_values(self, dbusPath):
        for defn in ACTIVE_BATTERY_PATHS.values():
            if dbusPath in defn.triggerPaths:
                defn.action(self)

    def _get_total_ir(self, batteries):
        sum_ir_recip = 0
        for batteryName in batteries:
            irdata = self._irs[batteryName]
            if irdata and irdata.ir:
                sum_ir_recip += 1/irdata.ir
            else:
                # missing IR - can't compute total IR
                return None

        return 1.0/sum_ir_recip if sum_ir_recip else None

    def _get_current_ratios(self, connectedBatteries, allowSupported):
        if self._currentRatioMethod == "ir":
            total_ir = self._get_total_ir(connectedBatteries)

        if self._currentRatioMethod != "count":
            aggr_cap = self.aggregators.get("/InstalledCapacity")
            # active total installed capacity
            # if allow is supported then assume batteries have /InstalledCapacity
            if allowSupported is not None:
                total_cap = 0
                for batteryName in connectedBatteries:
                    cap = aggr_cap.values.get(batteryName) if aggr_cap else None
                    if cap is None:
                        self.logger.warning(f"/InstalledCapacity is not available for {batteryName}")
                        total_cap = None
                        break
                    total_cap += cap
            else:
                # connectedBatteries are all batteries
                total_cap = self.service["/InstalledCapacity"]
                if total_cap is None:
                    self.logger.warning("Please set the \"capacity\" option in the config")

        batteryCount = len(connectedBatteries)

        ratios = []
        for batteryName in connectedBatteries:
            method = self._currentRatioMethod

            if method == "ir":
                irdata = self._irs.get(batteryName)
                if irdata and irdata.ir and total_ir:
                    ratio = irdata.ir/total_ir
                else:
                    method = "capacity"

            if method == "capacity":
                # assume internal resistance is inversely proportional to capacity
                cap = aggr_cap.values.get(batteryName) if aggr_cap else None
                if cap and total_cap:
                    ratio = total_cap/cap
                else:
                    method = "count"

            if method == "count":
                # assume internal resistance is the same for all batteries
                ratio = batteryCount

            ratios.append((ratio, method))

        return ratios

    def _is_dvcc(self):
        return (self.monitor.get_value("com.victronenergy.system", "/Control/Dvcc", 0) == 1)

    def _updateCCL(self):
        aggr_ccl = self.aggregators["/Info/MaxChargeCurrent"]
        self.logger.info(f"Individual CCLs: {aggr_ccl.values}")

        aggr_allow = self.aggregators["/Io/AllowToCharge"]
        connectedBatteries = [batteryName for batteryName, allow in aggr_allow.values.items() if allow != 0]
        self.logger.info(f"Connected batteries: {connectedBatteries}")

        currentRatios = self._get_current_ratios(connectedBatteries, aggr_allow.get_result())
        self.logger.info(f"Current ratios: {currentRatios}")

        cclPerBattery = []
        for i, batteryName in enumerate(connectedBatteries):
            ccl = aggr_ccl.values.get(batteryName)
            if ccl is not None:
                cclPerBattery.append(ccl*currentRatios[i][0])

        self.logger.info(f"CCL estimates: {cclPerBattery}")
        # return 0 if disabled or None if not available
        if cclPerBattery:
            ccl = min(cclPerBattery)
            if self._is_dvcc():
                user_limit = self.monitor.get_value("com.victronenergy.settings", "/Settings/SystemSetup/MaxChargeCurrent", -1)
                if user_limit > 0:
                    ccl = min(ccl, user_limit)
        elif aggr_ccl.get_result() > 0:
            # CCL is available but no connected batteries
            ccl = 0
        else:
            # CCL is not available
            ccl = None

        self.service["/Info/MaxChargeCurrent"] = ccl

    def _updateDCL(self):
        aggr_dcl = self.aggregators["/Info/MaxDischargeCurrent"]
        self.logger.info(f"Individual DCLs: {aggr_dcl.values}")

        aggr_allow = self.aggregators["/Io/AllowToDischarge"]
        connectedBatteries = [batteryName for batteryName, allow in aggr_allow.values.items() if allow != 0]
        self.logger.info(f"Connected batteries: {connectedBatteries}")

        currentRatios = self._get_current_ratios(connectedBatteries, aggr_allow.get_result())
        self.logger.info(f"Current ratios: {currentRatios}")

        dclPerBattery = []
        for i, batteryName in enumerate(connectedBatteries):
            dcl = aggr_dcl.values.get(batteryName)
            if dcl is not None:
                dclPerBattery.append(dcl*currentRatios[i][0])

        self.logger.info(f"DCL estimates: {dclPerBattery}")
        # return 0 if disabled or None if not available
        available = aggr_dcl.get_result() > 0
        self.service["/Info/MaxDischargeCurrent"] = min(dclPerBattery) if dclPerBattery else 0 if available else None

    def _updateCLs(self):
        self._updateCCL()
        self._updateDCL()

    def _updateCVL(self):
        aggr_cvl = self.aggregators["/Info/MaxChargeVoltage"]

        if self._cvlMode == "max_always":
            op = max
        elif self._cvlMode == "max_when_balancing":
            op = max if self.service["/Balancing"] == 1 else min
        elif self._cvlMode == "dvcc":
            op = None
            cvl = self.monitor.get_value("com.victronenergy.settings", "/Settings/SystemSetup/MaxChargeVoltage", 0)
        else:
            op = min

        if op is not None:
            cvlPerBattery = []
            for cvl in aggr_cvl.values.values():
                if cvl is not None:
                    cvlPerBattery.append(cvl)
    
            if cvlPerBattery:
                cvl = op(cvlPerBattery)
                if self._is_dvcc():
                    user_limit = self.monitor.get_value("com.victronenergy.settings", "/Settings/SystemSetup/MaxChargeVoltage", 0)
                    if user_limit > 0:
                        cvl = min(cvl, user_limit)
            else:
                cvl = None

        self.service["/Info/MaxChargeVoltage"] = cvl

    def _adjust_ir(self, batteryName, current):
        if current is None:
            return

        if current > 0:
            is_charging = True
        elif current < 0:
            is_charging = False
        else:
            return

        if is_charging:
            aggr_cl = self.aggregators["/Info/MaxChargeCurrent"]
        else:
            aggr_cl = self.aggregators["/Info/MaxDischargeCurrent"]

        current_abs = abs(current)
        cl = aggr_cl.values[batteryName]
        if cl and current_abs > cl:
            irdata = self._irs[batteryName]
            if irdata and irdata.ir:
                old_ir = irdata.ir
                irdata.correction *= cl/current_abs
                self.logger.info(f"Correcting over-current ({current_abs} > {cl}) for battery {batteryName}: adjusting IR {old_ir} -> {irdata.ir}")
                if is_charging:
                    self._updateCCL()
                else:
                    self._updateDCL()

    def __str__(self):
        return self._serviceName


class VirtualBatteryService(SettableService):
    def __init__(self, conn, serviceName, config, hook_config):
        super().__init__()
        if not is_battery_service_name(serviceName):
            raise ValueError(f"Invalid service name: {serviceName}")

        self.logger = logging.getLogger(serviceName)
        self.service = None
        self._registered = False
        self._conn = conn
        self._serviceName = serviceName

        for name in list(config):
            if not is_battery_service_name(name) and not Hook.is_hook(name) and not ServiceNameResolver.is_name(name):
                raise ValueError(f"Invalid service name: {name}")

        default_config_paths = list(BATTERY_PATHS)
        self._mergedServices = DataMerger(config, default_config_paths, ServiceNameResolver(conn))

        self.hooks = []
        for name in list(config):
            if Hook.is_hook(name):
                class_instance = name[len("class:"):]
                pos = class_instance.rfind('#')
                class_name = class_instance[:pos] if pos >= 0 else class_instance
                self.hooks.append(locate(class_name)(name, self._mergedServices, **hook_config.get(class_instance, {})))

        options = None  # currently not used afaik
        self.monitor = DbusMonitor(
            {
                'com.victronenergy.battery': {path: options for path in BATTERY_PATHS}
            },
            valueChangedCallback=self._battery_value_changed,
            deviceAddedCallback=self._battery_added,
            deviceRemovedCallback=self._battery_removed,
            includedServiceNames=[service_name for service_name in self._mergedServices.service_names if is_battery_service_name(service_name)],
            ignoreServices=[serviceName]
        )
        self.battery_service_names = [service_name for service_name in self.monitor.servicesByName]

    def register(self, timeout=0):
        self.service = VeDbusService(self._serviceName, self._conn, register=False)
        self.add_settable_path("/CustomName", "")
        self._init_settings(self._conn, timeout=timeout)
        id_offset = hashlib.sha1(self._serviceName.split('.')[-1].encode('utf-8')).digest()[0]
        di = self.register_device_instance("battery", f"virtbatt_{id_offset}", BASE_DEVICE_INSTANCE_ID + id_offset)
        self.service.add_mandatory_paths(__file__, VERSION, 'dbus', di,
                                     0, "Virtual Battery", FIRMWARE_VERSION, HARDWARE_VERSION, CONNECTED)
        for path, defn in BATTERY_PATHS.items():
            self.service.add_path(path, None, gettextcallback=defn.unit.gettextcallback)
        self.service.add_path("/System/Batteries", json.dumps(list(self.battery_service_names)))

        paths_changed = set()
        for batteryName in self.battery_service_names:
            changed = self._mergedServices.init_values(batteryName, self.monitor)
            paths_changed.update(changed)

        for hook in reversed(self.hooks):
            changed = hook.init_values()
            paths_changed.update(changed)

        self._batteries_changed()
        self._refresh_values(paths_changed)

        self.service.register()
        self._registered = True

    def _refresh_values(self, paths_changed):
        for path in paths_changed:
            self.service[path] = self._mergedServices.get_value(path)

    def _battery_value_changed(self, dbusServiceName, dbusPath, options, changes, deviceInstance):
        self.logger.debug(f"Battery value changed: {dbusServiceName} {dbusPath}")
        if self._registered:
            value = changes['Value']
            self._mergedServices.update_service_value(dbusServiceName, dbusPath, value)
            if self.hooks:
                paths_changed = set()
                paths_changed.add(dbusPath)
                for hook in reversed(self.hooks):
                    changed = hook.update_service_value(dbusServiceName, dbusPath, value)
                    paths_changed.update(changed)
                self._refresh_values(paths_changed)
            else:
                self.service[dbusPath] = self._mergedServices.get_value(dbusPath)

    def _battery_added(self, dbusServiceName, deviceInstance):
        self.logger.debug(f"Battery added: {dbusServiceName}")
        self.battery_service_names.append(dbusServiceName)
        if self._registered:
            paths_changed = self._mergedServices.init_values(dbusServiceName, self.monitor)
            self._batteries_changed()
            self._refresh_values(paths_changed)

    def _battery_removed(self, dbusServiceName, deviceInstance):
        self.logger.debug(f"Battery removed: {dbusServiceName}")
        self.battery_service_names.remove(dbusServiceName)
        if self._registered:
            paths_changed = self._mergedServices.clear_values(dbusServiceName)
            self._batteries_changed()
            self._refresh_values(paths_changed)

    def _batteries_changed(self):
        self.service["/System/Batteries"] = json.dumps(self.battery_service_names)

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

    logLevel = config.get("logLevel", "INFO")
    logger.setLevel(logLevel)
    logging.getLogger("com.victronenergy.battery").setLevel(logLevel)

    virtualBatteryConfigs = config.get("virtualBatteries", {})
    if virtualBatteryName:
        virtualBatteryConfig = virtualBatteryConfigs[virtualBatteryName]
        virtualBattery = VirtualBatteryService(dbusConnection(), virtualBatteryName, virtualBatteryConfig, config.get("classes"))
        virtualBattery.register(timeout=15)
        logger.info(f"Registered Virtual Battery {virtualBattery.service.name}")
    else:
        virtualBatteryConfigs = config.get("virtualBatteries", {})
        processes = []
        for virtualBatteryName in virtualBatteryConfigs:
            p = multiprocessing.Process(target=main, name=virtualBatteryName, args=(virtualBatteryName,), daemon=True)
            processes.append(p)
            p.start()

        def kill_handler(signum, frame):
            for p in processes:
                if p.is_alive():
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
            if batteryAggr.has_valid_batteries():
                batteryAggr.register(timeout=15)
                logger.info(f"Registered Battery Aggregator {batteryAggr.service.name}")
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
