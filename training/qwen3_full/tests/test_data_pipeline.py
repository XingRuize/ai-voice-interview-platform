from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DataPipelineTest(unittest.TestCase):
    def test_example_data_prepares_and_validates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "prepared"
            report = Path(temp_dir) / "validation_report.json"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "prepare_dataset.py"),
                    "--input",
                    str(ROOT / "data" / "annotated_turns.example.jsonl"),
                    "--outline",
                    str(ROOT / "data" / "interview_outline.json"),
                    "--output-dir",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "validate_dataset.py"),
                    "--data-dir",
                    str(output),
                    "--report",
                    str(report),
                ],
                check=True,
                capture_output=True,
                text=True,
            )

            validation = json.loads(report.read_text(encoding="utf-8"))
            self.assertTrue(validation["ok"])
            self.assertFalse(validation["participant_split_overlap"])
            self.assertEqual(
                sum(row["records"] for row in validation["counts"].values()),
                9,
            )

            participant_sets: dict[str, set[str]] = {}
            for split in ("train", "validation", "test"):
                participant_sets[split] = {
                    json.loads(line)["metadata"]["participant_id"]
                    for line in (output / f"{split}.jsonl").read_text(encoding="utf-8").splitlines()
                    if line.strip()
                }
            self.assertFalse(participant_sets["train"] & participant_sets["validation"])
            self.assertFalse(participant_sets["train"] & participant_sets["test"])
            self.assertFalse(participant_sets["validation"] & participant_sets["test"])


if __name__ == "__main__":
    unittest.main()
