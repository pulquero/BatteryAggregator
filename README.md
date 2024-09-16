
# About

A battery monitor D-Bus service that aggregates the data for multiple batteries (currently, only parallel supported).


# Install

Install [SetupHelper](https://github.com/kwindrem/SetupHelper).

Select the Battery Aggregator as the `Battery monitor` under `System setup`.


# Configuration

Optionally create the file `/data/setupOptions/BatteryAggregator/config.json`.

To exclude, for instance, a shunt from being aggregated, first run `dbus-spy` from the command line to obtain its service name.
It will likely be of the form `com.victronenergy.battery.ttyS9`.
Then add:

		"excludedServices": ["com.victronenergy.battery.ttyS9"]

To post-merge information from other services (e.g. a shunt), add:

		"primaryServices": {"com.victronenergy.battery.ttyS5": ["/Soc"], "com.victronenergy.battery.leastPrecedence": ["/Dc/0/Current"]}

(use SoC from the shunt, and current from the BMS with service name `com.victronenergy.battery.leastPrecedence`).

To pre-merge information from other services, add:

		"auxiliaryServices": ["com.victronenergy.battery.defaultMetadata", "com.victronenergy.battery.leastPrecedence"]

(or use the more advanced syntax if interested in specific D-Bus paths:

		"auxiliaryServices": {"com.victronenergy.battery.dev1": ["/Info/Location"]}

)

To set the installed capacity (if it is not available via aggregation), add:

		"capacity": 200

To create a virtual battery by merging two (or more) other batteries, add:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.ttyS2", "com.victronenergy.battery.leastPrecedence"]
		}
	}

(or use the more advanced syntax if interested in specific DBus paths:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": {"com.victronenergy.battery.ttyS6": ["/Soc"], "com.victronenergy.battery.ttyUSB0": []},
			"com.victronenergy.battery.virtual2": {"com.victronenergy.battery.ttyS7": ["/Soc"], "com.victronenergy.battery.ttyUSB1": []}
		}
	}

note, empty array `[]` means include all paths)

## Service names

Instead of service names, you can use custom names by prepending the custom name with `name:`, e.g. `"name:My Battery"` instead of `"com.victronenergy.battery.ttyS7"`.

## Charging parameters

### CCL

There are three ways to calculate the CCL:

"ir" - sample V and I during discharge to determine the individual internal resistances. Best with a good shunt to give good measurements,
useful with batteries of different capacities and ages. Note: until enough samples have been built up, the second method below is used.

"capacity" - generally, battery capacity is inversely proportional to internal resistance. Useful with batteries of different capacities.

"count" - batteries are assumed to have the same capacity and performance.

For example, to use "capacity", add the following:

		"currentRatioMethod": "capacity"

### CVL

There are four ways to calculate the CVL:

"max_when_balancing" (default) - use the min value of all the batteries, but when balancing use the max.

"min_when_balancing" - always use the min value of all the batteries.

"max_always" - always use the max value of all the batteries.

"dvcc" - track the DVCC value (`com.victronenergy.settings/Settings/SystemSetup/MaxChargeVoltage`).

For example, to use "min_when_balancing", add the following:

		"cvlMode": "min_when_balancing"


## Logging

To change the log level to debug, add:

		"logLevel": "DEBUG"


## Examples

### Exclude a shunt from aggregation

	{
		"excludedServices": ["com.victronenergy.battery.ttyS1"]
	}

### Aggregate all available batteries with SoC info provided by a shunt taking precedence

	{
		"primaryServices": {"com.victronenergy.battery.ttyS5": ["/Soc"]}
	}

### Create two virtual batteries from two BMSes, each in series with a shunt, and aggregate them

	{
		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.ttyS6", "com.victronenergy.battery.ttyUSB0"],
			"com.victronenergy.battery.virtual2": ["com.victronenergy.battery.ttyS7", "com.victronenergy.battery.ttyUSB1"]
		}
	}


## Config Generator
In order to make it easier to generate the config file, a small simple html has been created.
This should help you to generate the JSON for your Configuration.

You can find the Generator here: [Config Generator](https://pulquero.github.io/BatteryAggregator/config_generator/index.html)

Alternatively, you can also run the generator locally:

Download the `index.html` file from `docs/config_generator` and open it in your browser.

After you have filled in the form, you can copy the generated JSON and paste it into your config file `/data/setupOptions/BatteryAggregator/config.json`


## Config Checker
This project contains a small script which allows you to validate the config. It will also try to show you the issue(s) ( if any ).
To use it, run the following command:
```
bash /data/BatteryAggregator/check_config.sh
```

In case of errors, adjust the config.json and run the command again until no errors are shown.

### Note
The Config Checker requires the following packages:
- python3-pip
- jsonschema

The command above will automatically check for the packages and will ask you if you want to install them if they are not present.

In case of permission errors, make sure that the `check_config.sh` is executable. If not, run the following command:
```
chmod +x /data/BatteryAggregator/check_config.sh
```

## Troubleshooting

Check the log:

	cat /var/log/BatteryAggregator/current

Debug logging can be enable by setting the D-Bus path `/LogLevel` to `INFO`.

Restart the service:

	svc -d /service/BatteryAggregator
	svc -u /service/BatteryAggregator
