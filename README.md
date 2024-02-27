
# About

A battery monitor DBus service that aggregates the data for multiple batteries (currently, only parallel supported).


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

(or use the more advanced syntax if interested in specific DBus paths:

		"auxiliaryServices": {"com.victronenergy.battery.dev1": ["/Info/Location"]}

)

To set the installed capacity (if it is not available via aggregation), add:

		"capacity": 200

To create a virtual battery by merging two (or more) other batteries, add:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.ttyS2", "com.victronenergy.battery.leastPrecedence"]
		}
	{

(or use the more advanced syntax if interested in specific DBus paths:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": {"com.victronenergy.battery.ttyS6": ["/Soc"], "com.victronenergy.battery.ttyUSB0": []},
			"com.victronenergy.battery.virtual2": {"com.victronenergy.battery.ttyS7": ["/Soc"], "com.victronenergy.battery.ttyUSB1": []]
		}
	}

note, empty array `[]` means include all paths)

## Examples

### Exclude a shunt from aggregation

	{
		"excludedServices": ["com.victronenergy.battery.ttyS1"]
	}

### Aggregate all available batteries with SoC info provided by a shunt taking precedence

	{
		"primaryServices": {"com.victronenergy.battery.ttyS5": ["/SoC"]}
	}

### Create two virtual batteries from two BMSes, each in series with a shunt, and aggregate them

	{
		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.ttyS6", "com.victronenergy.battery.ttyUSB0"],
			"com.victronenergy.battery.virtual2": ["com.victronenergy.battery.ttyS7", "com.victronenergy.battery.ttyUSB1"]
		}
	}
	
## Troubleshooting

Check the log:

	cat /var/log/BatteryAggregator/current

Restart the service:

	svc -d /service/BatteryAggregator
	svc -u /service/BatteryAggregator
