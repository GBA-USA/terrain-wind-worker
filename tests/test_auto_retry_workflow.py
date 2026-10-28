import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "auto-retry.yml"


class AutoRetryWorkflowTests(unittest.TestCase):
    def test_yaml_contract(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_run:", text)
        self.assertIn("terrain-wind", text)
        self.assertIn("offline-packs", text)
        self.assertIn("run_attempt == 1", text)
        self.assertIn("actions: write", text)
        self.assertIn("gh run rerun", text)
        self.assertIn("'failure'", text)
        self.assertIn("'timed_out'", text)
        self.assertNotIn("'cancelled'", text)


if __name__ == "__main__":
    unittest.main()
