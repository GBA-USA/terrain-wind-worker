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
            (payload, {}, 200, None),
        ]
        seen_urls = []

        def fake_response(url, headers=None, timeout=120):
            seen_urls.append(url)
            item = outcomes.pop(0)
            if isinstance(item, BaseException):
                return b"", None, None, item
            return item

        def url_for_attempt(attempt):
            return f"https://example.test/dem?attempt={attempt}"

        with patch.object(terrain_wind, "http_bytes_response", side_effect=fake_response), patch.object(
            terrain_wind.time, "sleep"
        ):
            data, _headers = terrain_wind.http_bytes_retry(
                "DEM download",
                url_for_attempt(0),
                url_for_attempt=url_for_attempt,
                validate=lambda body: body[:4] in terrain_wind.TIFF_MAGIC,
                fail_message="DEM download failed",
            )
        self.assertEqual(data, payload)
        self.assertEqual(len(seen_urls), 3)
        self.assertEqual(len(set(seen_urls)), 3)
        self.assertNotEqual(seen_urls[0], seen_urls[1])
        self.assertNotEqual(seen_urls[1], seen_urls[2])


if __name__ == "__main__":
    unittest.main()
