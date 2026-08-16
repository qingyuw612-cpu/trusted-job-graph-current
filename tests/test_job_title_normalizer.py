from __future__ import annotations

import unittest

from trusted_graph_agent.job_title_normalizer import JobTitleNormalizer, normalize_job_title


class JobTitleNormalizerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.normalizer = JobTitleNormalizer()

    def assert_normalized(self, source: str, name: str, tags: list[str]) -> None:
        result = self.normalizer.normalize(source).to_dict()
        self.assertEqual(result["original_name"], source)
        self.assertEqual(result["normalized_name"], name)
        self.assertEqual(result["tags"], tags)

    def test_removes_level_and_location(self) -> None:
        self.assert_normalized("高级Java开发工程师（北京）", "Java开发工程师", [])

    def test_removes_recruitment_type_in_parentheses(self) -> None:
        self.assert_normalized("产品经理（实习生）", "产品经理", [])

    def test_keeps_large_model_direction_as_tag(self) -> None:
        self.assert_normalized(
            "AI算法工程师（大模型方向）",
            "算法工程师",
            ["大模型方向"],
        )

    def test_extracts_direction_after_hyphen(self) -> None:
        self.assert_normalized(
            "软件开发工程师-后端方向",
            "软件工程师",
            ["后端方向"],
        )

    def test_normalizes_english_letter_case(self) -> None:
        self.assert_normalized("JAVA开发工程师", "Java开发工程师", [])

    def test_supports_square_and_book_title_brackets(self) -> None:
        self.assert_normalized(
            "算法工程师【AI方向】[深圳]",
            "算法工程师",
            ["AI方向"],
        )

    def test_mixed_parenthetical_keeps_direction_and_removes_noise(self) -> None:
        self.assert_normalized(
            "产品经理（AI方向，北京，15薪）",
            "产品经理",
            ["AI方向"],
        )

    def test_removes_level_code_and_recruitment_description(self) -> None:
        self.assert_normalized("急招 P6 资深 Java工程师", "Java开发工程师", [])

    def test_removes_inline_salary_before_separator_normalization(self) -> None:
        self.assert_normalized("Java开发工程师 10-20K", "Java开发工程师", [])

    def test_splits_attached_direction_without_losing_core_title(self) -> None:
        self.assert_normalized("数据分析师商业方向", "数据分析师", ["商业方向"])

    def test_business_suffix_is_not_always_a_direction(self) -> None:
        self.assert_normalized("外贸业务", "外贸业务", [])

    def test_recovers_role_from_mixed_parenthetical_content(self) -> None:
        self.assert_normalized(
            "应届生（初级助理硬件工程师）",
            "助理硬件工程师",
            [],
        )

    def test_unknown_parenthetical_is_conservatively_preserved(self) -> None:
        self.assert_normalized("产品经理（供应链）", "产品经理", ["供应链"])

    def test_function_entry_returns_json_compatible_dict(self) -> None:
        self.assertEqual(
            normalize_job_title("产品专员（杭州）"),
            {
                "original_name": "产品专员（杭州）",
                "normalized_name": "产品经理",
                "tags": [],
            },
        )


if __name__ == "__main__":
    unittest.main()
