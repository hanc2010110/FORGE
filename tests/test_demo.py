from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class ReleaseDemoTests(unittest.TestCase):
    def test_root_dev_command_runs_the_release_readiness_vertical_slice(self) -> None:
        completed = subprocess.run(
            [str(ROOT / "forge"), "dev"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        payload = json.loads(completed.stdout)
        serialized = json.dumps(payload, sort_keys=True)

        self.assertEqual(
            payload["product"], "FORGE engineering change release verification"
        )
        self.assertEqual(
            payload["trace"]["hardware_revision"],
            {"from": "HW-11", "to": "HW-12"},
        )
        self.assertIn("hardware", payload["trace"]["changed_domains"])
        self.assertTrue(payload["trace"]["required_retests"])
        self.assertTrue(payload["trace"]["ingested_evidence_ids"])
        self.assertEqual(
            payload["release_decision"]["decision"]["report"]["status"], "ready"
        )
        self.assertTrue(payload["idempotency_replayed"])
        self.assertNotIn("fake_score", serialized)
        self.assertNotIn("fake.double", serialized)


if __name__ == "__main__":
    unittest.main()
