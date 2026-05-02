
class Hook:
    @staticmethod
    def is_hook(service_name):
        return service_name.startswith("class:")

    def init_values(self):
        ...

    def update_service_value(self, dbusServiceName, dbusPath, value):
        ...


class FixedValueHook(Hook):
    def __init__(self, service_name, merger, path, value=None):
        self.service_name = service_name
        self.merger = merger
        self.path = path
        self.value = value
        self.merger.data_by_path[path] = {service_name: None}

    def init_values(self):
        self.merger.update_service_value(self.service_name, self.path, self.value)
        return [self.path]

    def update_service_value(self, dbusServiceName, dbusPath, value):
        return []


class AbstractChargingHook(Hook):
    def __init__(self, value_path, service_name, merger, ccls=None, dcls=None):
        self.value_path = value_path
        self.service_name = service_name
        self.merger = merger
        self.ccls = ccls
        self.dcls = dcls

    def init_values(self):
        initial_value = self.merger.get_value(self.value_path)
        return self.update_cls(initial_value) if initial_value is not None else []

    def update_service_value(self, dbusServiceName, dbusPath, value):
        if dbusPath == self.value_path:
            merged_value = self.merger.get_value(dbusPath)
            return self.update_cls(merged_value)
        else:
            return []

    def update_cls(self, value):
        '''Update charging limits'''
        paths_changed = []

        ccl = None
        if self.ccls:
            for v, cl in self.ccls.items():
                if value >= float(v):
                    ccl = float(cl)
        if ccl is not None:
            self.merger.update_service_value(self.service_name, "/Info/MaxChargeCurrent", ccl)
            paths_changed.append("/Info/MaxChargeCurrent")

        dcl = None
        if self.dcls:
            for v, cl in self.dcls.items():
                if value >= float(v):
                    dcl = float(cl)
        if dcl is not None:
            self.merger.update_service_value(self.service_name, "/Info/MaxDischargeCurrent", dcl)
            paths_changed.append("/Info/MaxDischargeCurrent")

        return paths_changed


class CustomChargingHook(AbstractChargingHook):
    def __init__(self, service_name, merger, ccls=None, dcls=None):
        super().__init__("/Dc/0/Voltage", service_name, merger, ccls=ccls, dcls=dcls)


class TemperatureChargingHook(AbstractChargingHook):
    def __init__(self, service_name, merger, ccls=None, dcls=None):
        super().__init__("/Dc/0/Temperature", service_name, merger, ccls=ccls, dcls=dcls)
