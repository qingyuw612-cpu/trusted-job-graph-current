"""简历结构化画像提取测试：规范化 / 校验 / 单条 / 批量（LLM 用注入假函数，不联网）。"""

import json

from src.core.dimensions import DIMENSION_KEYS
from src.tools.resume_extract import (
    build_resume_profile,
    extract_resume_batch,
    extract_resume_profile,
    load_resume_items,
    validate_resume_profile,
)

RESUME = (
    "王晖，后端开发工程师求职。本科学历，英语四级。"
    "熟悉 Java、Python、Go，掌握 Spring Boot、MySQL。"
    "自学能力强，抗压能力强，责任心强，热爱技术。"
)


def _simple_llm(prompt):
    return {
        "dimensions": {
            "knowledge": ["Java"],
            "skill": ["Spring Boot"],
            "qualifications": [],
            "preference": [],
            "motivation": [],
            "trait": [],
            "self_concept": [],
        }
    }


def test_build_resume_profile_maps_chinese_keys():
    llm_out = {
        "position": "后端开发工程师",
        "dimensions": {
            "知识": ["Java"],
            "技术": ["Spring Boot"],
            "任职条件": ["本科学历"],
            "招聘偏好": [],
            "动机": ["热爱技术"],
            "特质": ["自学能力强"],
            "自我概念": ["责任心强"],
            "unknown": ["忽略"],
        },
    }
    profile = build_resume_profile(RESUME, "", llm_out)
    assert set(profile["dimensions"]) == set(DIMENSION_KEYS)
    assert profile["dimensions"]["knowledge"] == ["Java"]
    assert profile["dimensions"]["skill"] == ["Spring Boot"]
    assert "unknown" not in profile["dimensions"]
    assert profile["position"] == "后端开发工程师"


def test_build_resume_profile_flat_dims():
    llm_out = {"knowledge": ["Java"], "skill": ["Spring Boot"]}
    profile = build_resume_profile(RESUME, "", llm_out)
    assert profile["dimensions"]["knowledge"] == ["Java"]
    assert profile["dimensions"]["qualifications"] == []


def test_validate_resume_profile_ok():
    profile = {
        "dimensions": {
            "knowledge": ["Java"],
            "skill": ["Spring Boot", "MySQL"],
            "qualifications": ["本科学历"],
            "preference": [],
            "motivation": ["热爱技术"],
            "trait": ["自学能力强"],
            "self_concept": ["责任心强"],
        }
    }
    res = validate_resume_profile(profile, RESUME)
    assert res["ok"] is True
    assert res["violations"] == []
    assert res["stats"]["total_items"] == 7


def test_validate_resume_profile_flags_invented_dup_long():
    profile = {
        "dimensions": {
            "knowledge": ["Java", "Java", "量子计算"],
            "skill": ["这一条明显超过三十个字而且简历里根本没有这一句话的原文内容哦哦"],
            "qualifications": [],
            "preference": [],
            "motivation": [],
            "trait": [],
            "self_concept": [],
        }
    }
    res = validate_resume_profile(profile, RESUME)
    assert res["ok"] is False
    msgs = "\n".join(res["violations"])
    assert "重复条目" in msgs
    assert "缺乏原文依据" in msgs
    assert "条目过长" in msgs


def test_validate_tolerates_category_word_suffix():
    resume = "掌握数据库设计与优化、Java 8、项目开发，熟悉 Spring Boot"
    profile = {
        "dimensions": {
            "knowledge": ["数据库设计与优化知识"],
            "skill": ["Java 8项目开发", "Spring Boot 后端开发"],
            "qualifications": [],
            "preference": [],
            "motivation": [],
            "trait": [],
            "self_concept": [],
        }
    }
    res = validate_resume_profile(profile, resume)
    assert res["ok"] is True, res["violations"]


def test_build_resume_profile_truncates_long_items_and_annotates():
    long_skill = "Spring Boot（了解，参与过基于Spring Boot的校园社交平台开发项目）"
    resume = RESUME + " Spring Boot（了解，参与过基于Spring Boot的校园社交平台开发项目）"

    def fake_llm(prompt):
        return {"dimensions": {"skill": [long_skill]}}

    profile = extract_resume_profile(resume, llm_func=fake_llm)
    assert profile["stats"]["truncated"] == 1
    assert len(profile["dimensions"]["skill"]) == 1
    assert len(profile["dimensions"]["skill"][0]) <= 30
    assert profile["truncations"][0]["original"] == long_skill
    assert profile["validation"]["ok"] is True, profile["validation"]["violations"]


def test_extract_resume_profile_with_fake_llm():
    def fake_llm(prompt):
        assert "knowledge" in prompt
        return {
            "dimensions": {
                "knowledge": ["Java"],
                "skill": ["Spring Boot"],
                "qualifications": ["本科学历"],
                "preference": [],
                "motivation": ["热爱技术"],
                "trait": ["自学能力强"],
                "self_concept": ["责任心强"],
            }
        }

    profile = extract_resume_profile(RESUME, position="后端开发工程师", llm_func=fake_llm)
    assert profile["validation"]["ok"] is True
    assert profile["stats"]["total_items"] == 6
    assert profile["position"] == "后端开发工程师"


def test_extract_resume_batch_failure_isolation():
    def flaky_llm(prompt):
        if "FAIL" in prompt:
            raise RuntimeError("boom")
        return {
            "dimensions": {
                "knowledge": ["Java"],
                "skill": ["Spring Boot"],
                "qualifications": [],
                "preference": [],
                "motivation": [],
                "trait": [],
                "self_concept": [],
            }
        }

    items = [
        {"file_name": "a.md", "text": RESUME, "position": None},
        {"file_name": "b.md", "text": "FAIL 王晖", "position": None},
    ]
    res = extract_resume_batch(items, llm_func=flaky_llm)
    assert res["total"] == 2
    assert res["ok"] == 1
    assert res["failed"] == 1
    assert res["errors"][0]["file_name"] == "b.md"
    assert res["coverage"]["knowledge"] == 1


def test_extract_resume_batch_threaded_preserves_order():
    items = [
        {"file_name": f"{i}.md", "text": RESUME, "position": None}
        for i in range(6)
    ]
    res = extract_resume_batch(items, llm_func=_simple_llm, max_workers=3)
    assert res["ok"] == 6
    assert [p["file_name"] for p in res["profiles"]] == [
        f"{i}.md" for i in range(6)
    ]


def test_extract_resume_batch_progress_callback():
    items = [
        {"file_name": "a.md", "text": RESUME, "position": None},
        {"file_name": "b.md", "text": RESUME, "position": None},
    ]
    calls = []
    res = extract_resume_batch(
        items,
        llm_func=_simple_llm,
        max_workers=2,
        progress_cb=lambda done, total: calls.append((done, total)),
    )
    assert res["ok"] == 2
    assert calls and calls[-1] == (2, 2)


def test_load_resume_items_from_folder(tmp_path):
    (tmp_path / "a.md").write_text("# 王晖\n本科学历", encoding="utf-8")
    (tmp_path / "b.txt").write_text("Python", encoding="utf-8")
    items = load_resume_items(str(tmp_path))
    assert [i["file_name"] for i in items] == ["a.md", "b.txt"]
    assert items[0]["text"].strip()


def test_load_resume_items_from_faircv_json(tmp_path):
    data = [
        {"content": RESUME, "metadata": {"position": "后端开发工程师"}},
        {"content": "", "metadata": {}},
    ]
    f = tmp_path / "in.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    items = load_resume_items(str(f))
    assert len(items) == 1
    assert items[0]["position"] == "后端开发工程师"

