"""七维定义测试 — 零外部依赖。"""
from src.core.dimensions import (
    CATEGORY_TO_DIM,
    DEFAULT_WEIGHTS,
    DIMENSION_KEYS,
    DIM_LABELS,
    DIM_TO_CATEGORY,
    DIM_TO_OUTLINE,
    OUTLINE_FIVE_KEYS,
    project_to_five_dim,
)


class TestDimensionKeys:
    def test_seven_dimensions_in_order(self):
        assert len(DIMENSION_KEYS) == 7
        assert DIMENSION_KEYS == (
            "knowledge",
            "skill",
            "qualifications",
            "preference",
            "motivation",
            "trait",
            "self_concept",
        )

    def test_category_map_covers_all_categories(self):
        assert set(CATEGORY_TO_DIM) == {
            "知识", "技术", "任职条件", "招聘偏好", "动机", "特质", "自我概念",
        }
        assert set(CATEGORY_TO_DIM.values()) == set(DIMENSION_KEYS)

    def test_weights_sum_to_one(self):
        assert abs(sum(DEFAULT_WEIGHTS.values()) - 1.0) < 1e-9
        assert set(DEFAULT_WEIGHTS) == set(DIMENSION_KEYS)

    def test_labels_cover_all_dimensions(self):
        assert set(DIM_LABELS) == set(DIMENSION_KEYS)
        assert DIM_LABELS["knowledge"] == "知识"
        assert DIM_LABELS["skill"] == "技术"
        assert DIM_LABELS["qualifications"] == "任职条件"
        assert DIM_LABELS["preference"] == "招聘偏好"


class TestDimToCategory:
    def test_reverse_map_matches_category_map(self):
        assert set(DIM_TO_CATEGORY) == set(DIMENSION_KEYS)
        assert len(DIM_TO_CATEGORY) == len(CATEGORY_TO_DIM) == 7
        for category, dim in CATEGORY_TO_DIM.items():
            assert DIM_TO_CATEGORY[dim] == category

    def test_labels_align_with_category_reverse(self):
        for dim in DIMENSION_KEYS:
            assert DIM_LABELS[dim] == DIM_TO_CATEGORY[dim]


class TestProjectToFiveDim:
    def test_outline_five_keys(self):
        assert OUTLINE_FIVE_KEYS == (
            "knowledge",
            "skill",
            "motivation",
            "trait",
            "self_concept",
        )
        assert set(DIM_TO_OUTLINE) == set(DIMENSION_KEYS)
        assert set(DIM_TO_OUTLINE.values()) == set(OUTLINE_FIVE_KEYS)

    def test_merge_extra_dims(self):
        data = {
            "knowledge": ["A", "B"],
            "skill": ["C"],
            "qualifications": ["D"],      # → knowledge
            "preference": ["E"],          # → motivation
            "motivation": ["F"],
            "trait": ["G"],
            "self_concept": ["H"],
        }
        out = project_to_five_dim(data)
        assert list(out.keys()) == list(OUTLINE_FIVE_KEYS)
        assert out["knowledge"] == ["A", "B", "D"]
        assert out["skill"] == ["C"]
        assert out["motivation"] == ["E", "F"]
        assert out["trait"] == ["G"]
        assert out["self_concept"] == ["H"]

    def test_missing_dims_tolerated(self):
        out = project_to_five_dim({"skill": ["Java"]})
        assert out == {
            "knowledge": [],
            "skill": ["Java"],
            "motivation": [],
            "trait": [],
            "self_concept": [],
        }

    def test_empty_input(self):
        out = project_to_five_dim({})
        assert out == {k: [] for k in OUTLINE_FIVE_KEYS}

    def test_custom_mapping_override(self):
        data = {"preference": ["X"], "knowledge": ["Y"]}
        out = project_to_five_dim(
            data,
            mapping={"preference": "trait", "knowledge": "skill"},
        )
        assert out["trait"] == ["X"]
        assert out["skill"] == ["Y"]
        assert out["knowledge"] == []

    def test_dict_items_preserved(self):
        item = {"name": "Python", "weight": 2.0}
        out = project_to_five_dim({"skill": [item]})
        assert out["skill"] == [item]

