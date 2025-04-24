import json
import jsonschema
from jsonschema import validate
from pathlib import Path
import sys


with open("/data/BatteryAggregator/config-schema.json", "r") as fp:
    schema = json.load(fp)


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


def check_json_file(config_file):

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


if len(sys.argv) > 1:
    config_file = sys.argv[1]
else:
    setup_options = Path("/data/setupOptions/BatteryAggregator")
    config_file = setup_options/"config.json"

# Check the JSON Config file
is_valid, is_warning, message = check_json_file(config_file)
if is_warning:
    print("WARNING: Could not find config file: " + message)
elif is_valid:
    print("SUCCESS: The Config file is valid")
else:
    print("ERROR: The Config file is not valid")
    print(message)
