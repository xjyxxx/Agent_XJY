"""盲测工具：一致性系数 + 题单/评分流程（不含真实人工数据）。"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from evaluator.blind_test import RATER_COLUMNS, cohen_kappa, fleiss_kappa, score


class KappaTests(unittest.TestCase):
    def test_perfect_agreement(self) -> None:
        self.assertEqual(cohen_kappa(["A", "B", "A"], ["A", "B", "A"]), 1.0)

    def test_total_disagreement(self) -> None:
        # 3 条全反向：Po=0，Pe=2/3·1/3+1/3·2/3=4/9 → kappa=(0-4/9)/(1-4/9)=-0.8
        self.assertEqual(cohen_kappa(["A", "A", "B"], ["B", "B", "A"]), -0.8)

    def test_chance_level_agreement(self) -> None:
        # 一人全 A、一人全 B：观测一致率 0，期望也 0 → kappa 0
        self.assertEqual(cohen_kappa(["A", "A", "A"], ["B", "B", "B"]), 0.0)

    def test_worked_example_matches_hand_computation(self) -> None:
        # left = A×7 B×3；right = A×6 B A B B
        # 一致 8/10=0.8；Pe=0.7²+0.3²=0.58 → (0.8-0.58)/(1-0.58)=0.5238
        left = ["A"] * 7 + ["B"] * 3
        right = ["A"] * 6 + ["B", "A", "B", "B"]
        self.assertAlmostEqual(cohen_kappa(left, right), 0.5238, places=3)

    def test_constant_rater_gives_zero(self) -> None:
        # 一人全 A，另一人 7A/3B：Po=0.7，Pe=1.0×0.7=0.7 → kappa=0
        self.assertAlmostEqual(cohen_kappa(["A"] * 10, ["A"] * 7 + ["B"] * 3), 0.0, places=3)

    def test_fleiss_perfect(self) -> None:
        table = [[3, 0], [3, 0], [0, 3]]
        self.assertEqual(fleiss_kappa(table), 1.0)

    def test_fleiss_needs_at_least_two_raters(self) -> None:
        self.assertEqual(fleiss_kappa([[1, 0], [1, 0]]), 0.0)


class ScoreTests(unittest.TestCase):
    def _write(self, directory: Path, answers: dict) -> Path:
        key = {
            "r01": {"user_input": "在吗", "with_persona": "x", "without_persona": "y", "real_option": "A"},
            "r02": {"user_input": "谢谢", "with_persona": "x", "without_persona": "y", "real_option": "B"},
        }
        (directory / "blind_key.json").write_text(
            json.dumps(key, ensure_ascii=False), encoding="utf-8"
        )
        path = directory / "blind_sheet.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=["round", "用户说", "选项A", "选项B", *RATER_COLUMNS],
            )
            writer.writeheader()
            writer.writerow({"round": "r01", "用户说": "在吗", "选项A": "x", "选项B": "y", **answers["r01"]})
            writer.writerow({"round": "r02", "用户说": "谢谢", "选项A": "x", "选项B": "y", **answers["r02"]})
        return path

    def test_perfect_raters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                Path(directory),
                {
                    "r01": {"rater_1": "A", "rater_2": "A", "rater_3": "A"},
                    "r02": {"rater_1": "B", "rater_2": "B", "rater_3": "B"},
                },
            )
            result = score(path)
        self.assertEqual(result["accuracy_mean"], 1.0)
        self.assertEqual(result["fleiss_kappa"], 1.0)

    def test_missing_answers_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                Path(directory),
                {
                    "r01": {"rater_1": "A", "rater_2": "", "rater_3": ""},
                    "r02": {"rater_1": "A", "rater_2": "", "rater_3": ""},
                },
            )
            result = score(path)
        self.assertEqual(result["raters"], ["rater_1"])
        self.assertEqual(result["accuracy_per_rater"]["rater_1"], 0.5)

    def test_empty_sheet_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(
                Path(directory),
                {"r01": {"rater_1": "", "rater_2": "", "rater_3": ""},
                 "r02": {"rater_1": "", "rater_2": "", "rater_3": ""}},
            )
            with self.assertRaisesRegex(ValueError, "没有任何标注"):
                score(path)


if __name__ == "__main__":
    unittest.main()
