from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from normalize_split_job_titles import normalize_csv_file
from trusted_graph_agent.job_title_normalizer import JobTitleNormalizer


class NormalizeSplitJobTitlesTests(unittest.TestCase):
    def test_adds_one_normalized_column_without_changing_original_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.csv"
            destination = root / "output.csv"
            with source.open("w", encoding="utf-8-sig", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["职位ID", "原始职位名称", "JD全文"])
                writer.writeheader()
                writer.writerow(
                    {
                        "职位ID": "1",
                        "原始职位名称": "高级Java开发工程师（北京）",
                        "JD全文": "第一行\n第二行",
                    }
                )
                writer.writerow(
                    {
                        "职位ID": "2",
                        "原始职位名称": "算法工程师（大模型方向）",
                        "JD全文": "岗位描述",
                    }
                )

            result = normalize_csv_file(
                source=source,
                destination=destination,
                normalizer=JobTitleNormalizer(),
            )

            self.assertEqual(2, result.rows)
            with destination.open("r", encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(
                ["职位ID", "原始职位名称", "JD全文", "归一化岗位名称"],
                list(rows[0]),
            )
            self.assertEqual("高级Java开发工程师（北京）", rows[0]["原始职位名称"])
            self.assertEqual("第一行\n第二行", rows[0]["JD全文"])
            self.assertEqual("Java开发工程师", rows[0]["归一化岗位名称"])
            self.assertEqual("算法工程师", rows[1]["归一化岗位名称"])

    def test_existing_destination_is_skipped_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "input.csv"
            destination = root / "output.csv"
            source.write_text("原始职位名称\n产品经理\n", encoding="utf-8-sig")
            destination.write_text("不要覆盖", encoding="utf-8")

            result = normalize_csv_file(
                source=source,
                destination=destination,
                normalizer=JobTitleNormalizer(),
            )

            self.assertEqual("skipped", result.status)
            self.assertEqual("不要覆盖", destination.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
