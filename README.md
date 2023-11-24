
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

To set the installed capacity (if it is not available via aggregation), add:

		"capacity": 200

To create a virtual battery by merging two (or more) other batteries, add:

		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.shunt1", "com.victronenergy.battery.leastPrecedence"]
		}

## Examples

### Exclude a shunt from aggregation

	{
		"excludedServices": ["com.victronenergy.battery.shunt1"]
	}

### Create virtual batteries from two BMSes, each in series with a shunt

	{
		"virtualBatteries": {
			"com.victronenergy.battery.virtual1": ["com.victronenergy.battery.ttyS6", "com.victronenergy.battery.ttyUSB0"],
			"com.victronenergy.battery.virtual2": ["com.victronenergy.battery.ttyS7", "com.victronenergy.battery.ttyUSB1"]
		}
	}
