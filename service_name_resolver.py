import os
import sys
from script_utils import SCRIPT_HOME
sys.path.insert(1, os.path.join(os.path.dirname(__file__), f"{SCRIPT_HOME}/ext"))

from dbusmonitor import VE_INTERFACE


class ServiceNameResolver:
    @staticmethod
    def is_name(service_name):
        return service_name.startswith("name:")

    @staticmethod
    def get_name(service_name):
        return service_name[len("name:"):]

    def __init__(self, conn):
        self.conn = conn
        self._custom_names = None

    def _get_service_names(self):
        if self._custom_names is None:
            self._custom_names = {}
            service_names = self.conn.list_names()
            for service_name in service_names:
                if service_name.startswith("com.victronenergy."):
                    try:
                        custom_name = self.conn.call_blocking(service_name, "/CustomName", VE_INTERFACE, "GetValue", "", [])
                        self._custom_names[custom_name] = service_name
                    except:
                        pass
        return self._custom_names

    def resolve_service_name(self, name):
        if ServiceNameResolver.is_name(name):
            custom_name = ServiceNameResolver.get_name(name)
            service_name = self._get_service_names().get(custom_name)
            if not service_name:
                raise ValueError(f"Unknown name: {custom_name}")
            return service_name
        else:
            return name
