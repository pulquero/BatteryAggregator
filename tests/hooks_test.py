from data_merger import DataMerger
import hooks
import unittest


class SimpleServiceNameResolver:
    def __init(self):
        pass

    def resolve_service_name(self, name):
        return name


class HooksTest(unittest.TestCase):
    def test_custom_charging_hook(self):
        battery_name = "com.victronenergy.battery.test"
        merger = DataMerger(["class:hooks.CustomChargingHook", battery_name], ["/Dc/0/Voltage", "/Info/MaxChargeCurrent"], SimpleServiceNameResolver())
        config = {
            "ccls": {
                "13": 15,
                "12": 10
            }
        }
        hook = hooks.CustomChargingHook("class:hooks.CustomChargingHook", merger, **config)
        merger.update_service_value(battery_name, "/Info/MaxChargeCurrent", 20)
        merger.update_service_value(battery_name, "/Dc/0/Voltage", 12.6)
        paths_changed = hook.update_service_value(battery_name, "/Dc/0/Voltage", 12.6)
        self.assertListEqual(paths_changed, ["/Info/MaxChargeCurrent"])
        self.assertEqual(merger.get_value("/Info/MaxChargeCurrent"), 10)

