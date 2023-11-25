
# About

A battery monitor DBus service that aggregates the data for multiple batteries (currently, only parallel supported).


# Install

Install [SetupHelper](https://github.com/kwindrem/SetupHelper), use [this version](https://github.com/pulquero/SetupHelper) if you want this package
to be available by default, else manually add it.

Select the Battery Aggregator as the `Battery monitor` under `System setup`.


# Configuration

Optionally create the file `/data/setupOptions/BatteryAggregator/config.json`.

To exclude the battery service `com.victronenergy.battery.shunt1`, add:

		"excludedServices": ["com.victronenergy.battery.shunt1"]

To post-merge information from other services (e.g. a shunt), add:

		"primaryServices": {"com.victronenergy.battery.shunt1": ["/Soc"], "com.victronenergy.battery.leastPrecedence": ["/Dc/0/Current"]}

To pre-merge information from other services, add:

		"auxiliaryServices": ["com.victronenergy.battery.defaultMetadata", "com.victronenergy.battery.leastPrecedence"]

(or use the more advanced syntax if interested in specific DBus paths:

		"auxiliaryServices": {"com.victronenergy.battery.dev1": ["/Info/Location"]}

)

To set the installed capacity (if it is not available via aggregation), add:

		"capacity": 200

To create a virtual battery by merging two (or more) other batteries, add:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.shunt1", "com.victronenergy.battery.leastPrecedence"]
		}
	{

(or use the more advanced syntax if interested in specific DBus paths:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": {"com.victronenergy.battery.ttyS6": ["/Soc"], "com.victronenergy.battery.ttyUSB0": []},
			"com.victronenergy.battery.virtual2": {"com.victronenergy.battery.ttyS7": ["/Soc"], "com.victronenergy.battery.ttyUSB1": []]
		}
	}

)

## Examples

### Exclude a shunt from aggregation

	{
		"excludedServices": ["com.victronenergy.battery.shunt1"]
	}

### Aggregate all available batteries with SoC info provided by a shunt taking precedence

	{
		"primaryServices": {"com.victronenergy.battery.shunt1": ["/SoC"]}
	}

### Create two virtual batteries from two BMSes, each in series with a shunt, and aggregate them

	{
		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.ttyS6", "com.victronenergy.battery.ttyUSB0"],
			"com.victronenergy.battery.virtual2": ["com.victronenergy.battery.ttyS7", "com.victronenergy.battery.ttyUSB1"]
		}
	}
