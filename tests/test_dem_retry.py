import importlib.util
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("terrain_wind", ROOT / "scripts" / "terrain_wind.py")
terrain_wind = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(terrain_wind)


class DemRetryTests(unittest.TestCase):
    def test_http_bytes_retry_recovers_after_transient_failures(self):
        payload = b"II*\x00" + b"\x00" * 8
        outcomes = [
            urllib.error.URLError("transient"),
            urllib.error.URLError("transient"),
            (payload, {}),
        ]

        def fake_http(url, headers=None, timeout=120):
            item = outcomes.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        with patch.object(terrain_wind, "http_bytes", side_effect=fake_http), patch.object(
            terrain_wind.time, "sleep"
        ):
            data, _headers = terrain_wind.http_bytes_retry(
                "DEM download",
                "https://example.test/dem",
                validate=lambda body: body[:4] in terrain_wind.TIFF_MAGIC,
                fail_message="DEM download failed",
            )
        self.assertEqual(data, payload)


if __name__ == "__main__":
    unittest.main()
