import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("terrain_wind", ROOT / "scripts" / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(terrain_wind)

CONTRACT = json.loads((ROOT / "fixtures" / "scent-weather-contract.json").read_text(encoding="utf-8"))


class HrrrIdxSelectionTests(unittest.TestCase):
    def test_subset_includes_scent_variables(self):
        sample = "\n".join(
            [
                "1:0:d=2026102712:REFC:entire atmosphere:1 hour fcst:",
                "9:3292755:d=2026102712:GUST:surface:1 hour fcst:",
                "71:44965613:d=2026102712:TMP:2 m above ground:1 hour fcst:",
                "74:48855288:d=2026102712:DPT:2 m above ground:1 hour fcst:",
                "75:50483009:d=2026102712:RH:2 m above ground:1 hour fcst:",
                "77:51284843:d=2026102712:UGRD:10 m above ground:1 hour fcst:",
                "78:53666458:d=2026102712:VGRD:10 m above ground:1 hour fcst:",
                "79:56048073:d=2026102712:WIND:10 m above ground:0-1 hour max fcst:",
                "83:60150527:d=2026102712:PRATE:surface:1 hour fcst:",
                "116:81325983:d=2026102712:TCDC:entire atmosphere:1 hour fcst:",
                "123:90837547:d=2026102712:DSWRF:surface:1 hour fcst:",
            ]
        )
        rows = terrain_wind.parse_hrrr_idx_rows(sample)
        ranges = terrain_wind.hrrr_subset_ranges(rows, 99_999_999)
        names = {item[0] for item in ranges}
        self.assertEqual(names, set(terrain_wind.HRRR_SUBSET_VARIABLES))

    def test_surface_gust_is_not_selected(self):
        row = {
            "start": 0,
            "variable": "GUST",
            "level": "surface",
            "desc": "1 hour fcst",
        }
        self.assertFalse(terrain_wind.hrrr_message_selected(row))

    def test_boundary_layer_cloud_is_not_selected(self):
        row = {
            "start": 0,
            "variable": "TCDC",
            "level": "boundary layer cloud layer",
            "desc": "1 hour fcst",
        }
        self.assertFalse(terrain_wind.hrrr_message_selected(row))


class ScentWeatherPayloadTests(unittest.TestCase):
    def test_contract_keys_and_units(self):
        payload = terrain_wind.scent_weather_payload(0.42, 68.2, 285.0, 292.0, 4.2, 0.0001, 512.0)
        self.assertEqual(list(payload.keys()), CONTRACT["keys"])
        self.assertEqual(payload["cloud_cover"], 42.0)
        self.assertEqual(payload["temperature_2m"], 18.9)
        self.assertEqual(payload["dew_point_2m"], 11.9)
        self.assertEqual(payload["precipitation"], 0.36)
        self.assertEqual(payload["relative_humidity_2m"], 68.2)
        self.assertEqual(payload["wind_gusts_10m"], 4.2)
        self.assertEqual(payload["shortwave_radiation"], 512.0)
        self.assertIsInstance(payload["weather_code"], int)

    def test_derive_weather_code(self):
        self.assertEqual(terrain_wind.derive_weather_code(0.0, 10.0, 15.0), 0)
        self.assertEqual(terrain_wind.derive_weather_code(0.0, 60.0, 15.0), 2)
        self.assertEqual(terrain_wind.derive_weather_code(1.5, 40.0, 15.0), 61)
        self.assertEqual(terrain_wind.derive_weather_code(1.5, 40.0, -2.0), 71)

    def test_hour_record_includes_weather(self):
        weather = terrain_wind.scent_weather_payload(50.0, 70.0, 280.0, 290.0, 3.0, 0.0, 400.0)
        payload = terrain_wind.hour_record_payload(
            "t322_-816",
            "2026-10-25T12:00:00Z",
            [-79.29, 38.43, -79.19, 38.49],
            2,
            2,
            100.0,
            [1.0],
            [2.0],
            weather=weather,
        )
        self.assertEqual(payload["weather"], weather)
        self.assertEqual(set(payload["weather"].keys()), set(CONTRACT["keys"]))


class WeatherFromGribTests(unittest.TestCase):
    def test_weather_from_grib_samples_bands(self):
        grib = Path("/tmp/hrrr-subset.grib2")
        if not grib.is_file():
            self.skipTest("local HRRR subset fixture not present")
        bbox = [-98.0, 30.0, -97.0, 31.0]
        with tempfile.TemporaryDirectory() as tmp:
            weather = terrain_wind.weather_from_grib(grib, bbox, Path(tmp))
        self.assertEqual(set(weather.keys()), set(CONTRACT["keys"]))


if __name__ == "__main__":
    unittest.main()
