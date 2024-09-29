
class Hook:
    @staticmethod
    def is_hook(service_name):
        return service_name.startswith("class:")

    def init_values(self):
        ...

    def update_service_value(self, dbusServiceName, dbusPath, value):
        ...


class CustomNameHook(Hook):
    def __init__(self, service_name, merger, customName):
        self.service_name = service_name
        self.merger = merger
        self.customName = customName
        self.merger.data_by_path["/CustomName"] = {service_name: None}

    def init_values(self):
        self.merger.update_service_value(self.service_name, "/CustomName", self.customName)
        return ["/CustomName"]

    def update_service_value(self, dbusServiceName, dbusPath, value):
        return []


class CustomChargingHook(Hook):
    def __init__(self, service_name, merger, ccls=None, dcls=None):
        self.service_name = service_name
        self.merger = merger
        self.ccls = ccls
        self.dcls = dcls

    def init_values(self):
        initial_voltage = self.merger.get_value("/Dc/0/Voltage")
        return self.update_cls(initial_voltage) if initial_voltage is not None else []

    def update_service_value(self, dbusServiceName, dbusPath, value):
        if dbusPath == "/Dc/0/Voltage":
            voltage = self.merger.get_value(dbusPath)
            return self.update_cls(voltage)
        else:
            return []

    def update_cls(self, voltage):
        paths_changed = []

        ccl = None
        if self.ccls:
            for v, cl in self.ccls.items():
                if voltage >= float(v):
                    ccl = float(cl)
        if ccl is not None:
            self.merger.update_service_value(self.service_name, "/Info/MaxChargeCurrent", ccl)
            paths_changed.append("/Info/MaxChargeCurrent")

        dcl = None
        if self.dcls:
            for v, cl in self.dcls.items():
                if voltage >= float(v):
                    dcl = float(cl)
        if dcl is not None:
            self.merger.update_service_value(self.service_name, "/Info/MaxDischargeCurrent", dcl)
            paths_changed.append("/Info/MaxDischargeCurrent")

        return paths_changed
