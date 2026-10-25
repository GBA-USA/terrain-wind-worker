import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_windninja.sh"


class BuildWindNinjaScriptTests(unittest.TestCase):
    def test_runtime_install_runs_before_cache_hit_exit(self):
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        install_pos = text.find("install_runtime_deps\n\nif [[ -x")
        cache_hit_pos = text.find('if [[ -x "$PREFIX/bin/WindNinja_cli" ]]; then')
        self.assertGreater(install_pos, -1)
        self.assertGreater(cache_hit_pos, -1)
        self.assertLess(install_pos, cache_hit_pos)
        self.assertIn("verify_windninja_cli", text)

    def test_build_packages_stay_on_cache_miss_path(self):
        text = BUILD_SCRIPT.read_text(encoding="utf-8")
        cache_hit_exit = text.find("exit 0", text.find('if [[ -x "$PREFIX/bin/WindNinja_cli" ]]; then'))
        build_essential_pos = text.find("build-essential", cache_hit_exit)
        self.assertGreater(build_essential_pos, cache_hit_exit)

    def test_cache_hit_installs_gdal_and_passes_ldd_check(self):
        if shutil.which("sudo") is None:
            self.skipTest("sudo is not available")
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "windninja"
            (prefix / "bin").mkdir(parents=True)
            shutil.copy("/bin/ls", prefix / "bin" / "WindNinja_cli")
            env = {**os.environ, "WINDNINJA_PREFIX": str(prefix)}
            subprocess.run(["bash", str(BUILD_SCRIPT)], check=True, env=env, timeout=600)
            self.assertIsNotNone(shutil.which("gdalinfo"))
            completed = subprocess.run(
                ["ldd", str(prefix / "bin" / "WindNinja_cli")],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertNotIn("not found", completed.stdout)


@unittest.skipUnless(os.environ.get("RUN_WINDNINJA_INTEGRATION") == "1", "set RUN_WINDNINJA_INTEGRATION=1")
class WindNinjaIntegrationTests(unittest.TestCase):
    def test_build_script_ldd_clean(self):
        if shutil.which("sudo") is None:
            self.skipTest("sudo is not available")
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "windninja"
            prefix.mkdir()
            env = {**os.environ, "WINDNINJA_PREFIX": str(prefix)}
            subprocess.run(["bash", str(BUILD_SCRIPT)], check=True, env=env, timeout=3600)
            completed = subprocess.run(
                ["ldd", str(prefix / "bin" / "WindNinja_cli")],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertNotIn("not found", completed.stdout)


if __name__ == "__main__":
    unittest.main()
