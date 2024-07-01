import json
import jsonschema
from jsonschema import validate
from pathlib import Path

# Define the JSON schema for validation
schema = {
    "type": "object",
    "properties": {
        "excludedServices": {"type": "array", "items": {"type": "string"}},
        "primaryServices": {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}},
        "auxiliaryServices": {
            "anyOf": [
                {"type": "array", "items": {"type": "string"}},
                {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}
            ]
        },
        "capacity": {"type": "integer"},
        "virtualBatteries": {
            "type": "object",
            "additionalProperties": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "object", "additionalProperties": {"type": "array", "items": {"type": "string"}}}
                ]
            }
        },
        "currentRatioMethod": {"type": "string", "enum": ["ir", "capacity", "count"]},
        "cvlMode": {"type": "string", "enum": ["max_when_balancing", "min_when_balancing", "max_always", "dvcc"]},
        "logLevel": {"type": "string", "enum": ["NOTSET", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]}
    },
    "additionalProperties": False
}


def validate_json(json_data):
    try:
        validate(instance=json_data, schema=schema)
    except jsonschema.exceptions.ValidationError as err:
        error_message = "Issue: " + err.message + "\nProperty: "
        if len(err.absolute_path) > 2:
            error_message += str(err.absolute_path[0]) + "\nItem: " + str(err.absolute_path[1]) + "\nPosition: " + str(err.absolute_path[2])
        elif len(err.absolute_path) > 1:
            error_message += str(err.absolute_path[0]) + "\nPosition: " + str(err.absolute_path[1])
        else:
            error_message += err.path[0]
        return False, False, error_message
    return True, False, "JSON data is schema compliant"


def check_json_file():
    setup_options = Path("/data/setupOptions/BatteryAggregator")
    config_file = setup_options/"config.json"

    def dict_raise_on_duplicates(ordered_pairs):
        d = {}
        for k, v in ordered_pairs:
            if k in d:
                raise ValueError(f"Duplicate Property: {k}")
            else:
                d[k] = v
        return d

    try:
        with open(config_file, 'r') as file:
            data = json.load(file, object_pairs_hook=dict_raise_on_duplicates)
    except ValueError as e:
        return False, False, str(e)
    except FileNotFoundError:
        return False, True, str(config_file)
    return validate_json(data)


# Check the JSON Config file
is_valid, is_warning, message = check_json_file()
if is_warning:
    print("WARNING: Could not find config file: " + message)
elif is_valid:
    print("SUCCESS: The Config file is valid")
else:
    print("ERROR: The Config file is not valid")
    print(message)
