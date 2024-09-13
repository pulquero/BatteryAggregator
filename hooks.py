class CustomNameHook:
    def __init__(self, service_name, merger, customName):
        self.service_name = service_name
        self.merger = merger
        self.customName = customName
        self.merger.data_by_path["/CustomName"] = {service_name: None}

    def init_values(self, api):
        self.merger.update_service_value(self.service_name, "/CustomName", self.customName)
        return ["/CustomName"]

    def update_service_value(self, dbusServiceName, dbusPath, value):
        return []
