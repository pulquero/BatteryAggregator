from typing import Dict, List

class DataMerger:
    def __init__(self, config, default_config_paths: List[str], service_name_resolver):
        if isinstance(config, list):
            # convert short-hand format
            expanded_config = {service_name_resolver.resolve_service_name(serviceName): default_config_paths for serviceName in config}
        elif isinstance(config, dict):
            expanded_config = {}
            for k, v in config.items():
                if not v:
                    v = default_config_paths
                expanded_config[service_name_resolver.resolve_service_name(k)] = v
        elif config is None:
            expanded_config = {}
        else:
            raise ValueError(f"Unsupported config object: {type(config)}")

        self.service_names: List[str] = list(expanded_config)

        self.data_by_path: Dict[str, Dict[str, str]] = {}
        for service_name, path_list in expanded_config.items():
            for p in path_list:
                path_values = self.data_by_path.get(p)
                if path_values is None:
                    path_values = {}
                    self.data_by_path[p] = path_values
                path_values[service_name] = None

    def init_values(self, service_name: str, api):
        paths_changed = []
        for p, path_values in self.data_by_path.items():
            if service_name in path_values:
                path_values[service_name] = api.get_value(service_name, p)
                paths_changed.append(p)
        return paths_changed

    def clear_values(self, service_name: str):
        paths_changed = []
        for p, path_values in self.data_by_path.items():
            if service_name in path_values:
                path_values[service_name] = None
                paths_changed.append(p)
        return paths_changed

    def update_service_value(self, service_name: str, path: str, value):
        path_values = self.data_by_path.get(path)
        if path_values:
            if service_name in path_values:
                path_values[service_name] = value

    def get_value(self, path: str):
        path_values = self.data_by_path.get(path)
        if path_values:
            for service_name in self.service_names:
                v = path_values.get(service_name)
                if v is not None:
                    return v
        return None
