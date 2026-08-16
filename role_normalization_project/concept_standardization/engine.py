"""用名称、能力画像和招聘证据归纳标准岗位概念。

AI结构化结论默认进入岗位草案，人工结论可覆盖；模块不会写入 Neo4j。
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

from role_normalizer.registry import normalize_lookup_key
from role_normalizer.taxonomy_adapter import load_role_registry
from .ai_judge import AIRoleJudge


DECISIONS = {
    "EXISTING_ROLE", "ALIAS", "SUBROLE_OF", "NEW_ROLE_CANDIDATE",
    "NON_IT", "NOISE", "INSUFFICIENT_INFO",
}
FINAL_DECISIONS = {"EXISTING_ROLE", "ALIAS", "SUBROLE_OF", "NEW_ROLE_CANDIDATE", "NON_IT", "NOISE"}
TECH_CATEGORIES = ("技术", "知识")
QUALIFICATION_NOISE = re.compile(r"(?:学历|专业$|相关专业|工作经验|项目经验|管理经验)$")
GENERIC_NAMES = {"工程师", "开发工程师", "技术员", "IT", "信息技术岗", "助理工程师", "测试"}
NON_IT_HINTS = {
    "文员", "仓库管理员", "珠宝设计师", "短视频编导", "短视频剪辑", "视频拍摄剪辑",
    "市场推广", "资料员", "采购文员", "财务分析经理", "生物统计", "医药产品经理",
}


@dataclass(frozen=True)
class EngineConfig:
    """保守阈值：自动归并只允许确定性命中，模糊结果均进入审核。"""

    title_weight: float = 0.58
    skill_weight: float = 0.42
    review_merge_score: float = 0.72
    review_subrole_score: float = 0.58
    min_new_role_jds: int = 3
    min_new_role_companies: int = 3
    min_new_role_skills: int = 3
    top_k: int = 5
    sample_jds: int = 3
    version: str = "1.0.0"


def stable_role_id(name: str) -> str:
    return "role:" + hashlib.sha1(normalize_lookup_key(name).encode("utf-8")).hexdigest()[:16]


def parse_skills(value: str) -> list[str]:
    """兼容能力提取 JSON 和分隔文本，只读取技术、知识。"""

    text = str(value or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = None
    parts: list[str] = []
    if isinstance(payload, dict):
        for category in TECH_CATEGORIES:
            raw = payload.get(category, "")
            if isinstance(raw, list):
                parts.extend(str(x).strip() for x in raw)
            else:
                parts.extend(re.split(r"[；;,，|、\n]+", str(raw)))
    else:
        parts.extend(re.split(r"[；;,，|、\n]+", text))
    return list(dict.fromkeys(x.strip() for x in parts if x.strip() and not QUALIFICATION_NOISE.search(x.strip())))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def _write_csv(path: Path, rows: Iterable[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _short_jd(value: str, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


class ConceptStandardizationEngine:
    """生成名称级审核队列，并将已批准决策回填到招聘记录。"""

    REVIEW_FIELDS = [
        "candidate_id", "source_name", "jd_count", "company_count", "original_variant_count",
        "top_original_names", "top_skills", "evidence_level", "suggested_decision",
        "suggested_role_id", "suggested_role_name", "suggested_confidence", "suggested_reason",
        "candidate_1", "candidate_1_score", "candidate_2", "candidate_2_score",
        "ai_decision", "ai_target_role_id", "ai_canonical_name", "ai_parent_role_id",
        "ai_tags", "ai_confidence", "ai_reason", "ai_model_version",
        "reviewer_decision", "reviewer_role_id", "reviewer_canonical_name",
        "reviewer_parent_role_id", "reviewer_tags", "reviewer_note", "review_status",
        "definition_version", "ai_review",
    ]

    def __init__(self, registry_path: Path, role_skills_path: Path | None = None,
                 config: EngineConfig | None = None) -> None:
        self.config = config or EngineConfig()
        self.registry = load_role_registry(registry_path)
        self.roles = list(self.registry)
        self.role_by_id = {role.role_id: role for role in self.roles}
        self.role_skills = self._load_role_skills(role_skills_path)

    def _load_role_skills(self, path: Path | None) -> dict[str, dict[str, float]]:
        profiles: dict[str, dict[str, float]] = defaultdict(dict)
        if path is None or not path.is_file():
            return profiles
        for row in _read_csv(path):
            if row.get("category") not in TECH_CATEGORIES:
                continue
            role_name = str(row.get("role") or "").strip()
            skill = str(row.get("canonical_name") or "").strip()
            if not role_name or not skill:
                continue
            try:
                score = float(row.get("final_score") or 0)
            except ValueError:
                score = 0.0
            profiles[role_name][normalize_lookup_key(skill)] = max(score, profiles[role_name].get(normalize_lookup_key(skill), 0))
        return profiles

    def _aggregate(self, rows: list[dict[str, str]]) -> dict[str, dict[str, Any]]:
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            name = str(row.get("岗位名称") or "").strip()
            if not name:
                continue
            item = grouped.setdefault(name, {
                "rows": [], "companies": set(), "originals": Counter(), "skills": Counter(), "samples": []
            })
            item["rows"].append(row)
            company = str(row.get("公司全称") or "").strip()
            if company:
                item["companies"].add(company)
            original = str(row.get("原始职位名称") or name).strip()
            item["originals"][original] += 1
            for skill in set(parse_skills(row.get("能力提取结果", ""))):
                item["skills"][skill] += 1
            if len(item["samples"]) < self.config.sample_jds:
                sample = _short_jd(row.get("JD全文", ""))
                if sample:
                    item["samples"].append(sample)
        return grouped

    @staticmethod
    def _title_score(source: str, target: str, aliases: list[str]) -> float:
        source_key = normalize_lookup_key(source)
        values = [target, *aliases]
        best = 0.0
        for value in values:
            target_key = normalize_lookup_key(value)
            ratio = SequenceMatcher(None, source_key, target_key).ratio()
            if target_key and (target_key in source_key or source_key in target_key):
                ratio = max(ratio, min(len(target_key), len(source_key)) / max(len(target_key), len(source_key)))
            best = max(best, ratio)
        return best

    def _skill_score(self, skills: Counter[str], role_name: str) -> float:
        profile = self.role_skills.get(role_name, {})
        if not profile or not skills:
            return 0.0
        source = {normalize_lookup_key(x) for x in skills}
        total = sum(profile.values()) or 1.0
        return min(1.0, sum(weight for skill, weight in profile.items() if skill in source) / total * 2.5)

    def _rank_roles(self, name: str, skills: Counter[str]) -> list[dict[str, Any]]:
        ranked = []
        for role in self.roles:
            title = self._title_score(name, role.canonical_name, role.aliases)
            skill = self._skill_score(skills, role.canonical_name)
            combined = self.config.title_weight * title + self.config.skill_weight * skill
            ranked.append({"role_id": role.role_id, "name": role.canonical_name,
                           "title_score": round(title, 4), "skill_score": round(skill, 4),
                           "score": round(combined, 4)})
        return sorted(ranked, key=lambda x: (-x["score"], x["role_id"]))[:self.config.top_k]

    def _suggest(self, name: str, evidence: dict[str, Any], ranked: list[dict[str, Any]]) -> tuple[str, str, str, float, str]:
        exact = self.registry.match_exact(name)
        if exact:
            role, kind = exact
            decision = "EXISTING_ROLE" if kind.value == "EXACT" else "ALIAS"
            return decision, role.role_id, role.canonical_name, 1.0, "确定性命中受控标准名或别名"
        if name in GENERIC_NAMES:
            return "INSUFFICIENT_INFO", "", "", 0.25, "名称过于宽泛，必须结合单条JD判断"
        if any(hint.lower() in name.lower() for hint in NON_IT_HINTS):
            return "NON_IT", "", "", 0.80, "名称明显偏离当前IT岗位图谱范围，需人工确认"
        best = ranked[0] if ranked else {"score": 0, "role_id": "", "name": "", "title_score": 0}
        if best["score"] >= self.config.review_merge_score and best["title_score"] >= 0.72:
            return "ALIAS", best["role_id"], best["name"], best["score"], "名称与能力画像共同支持归并；等待人工批准"
        if best["score"] >= self.config.review_subrole_score:
            return "SUBROLE_OF", best["role_id"], best["name"], best["score"], "接近现有岗位但包含技术/行业/等级差异；建议作为方向或子岗位"
        enough = (len(evidence["rows"]) >= self.config.min_new_role_jds and
                  len(evidence["companies"]) >= self.config.min_new_role_companies and
                  len(evidence["skills"]) >= self.config.min_new_role_skills)
        if enough:
            return "NEW_ROLE_CANDIDATE", "", name, max(0.55, best["score"]), "跨JD、企业和能力证据充足，但与现有岗位差异较大；仅作为新岗位候选"
        return "INSUFFICIENT_INFO", best["role_id"], best["name"], best["score"], "证据不足，保留观察，不创建正式岗位"

    def run(self, input_csv: Path, output_dir: Path) -> dict[str, Any]:
        rows = _read_csv(input_csv)
        grouped = self._aggregate(rows)
        output_dir.mkdir(parents=True, exist_ok=True)
        review_path = output_dir / "concept_review_queue.csv"
        previous = {r.get("candidate_id", ""): r for r in _read_csv(review_path)} if review_path.exists() else {}
        review_rows: list[dict[str, Any]] = []
        evidence_rows: list[dict[str, Any]] = []
        ai_judge = AIRoleJudge()
        ai_rows: list[dict[str, Any]] = []
        deterministic: dict[str, dict[str, str]] = {}
        for name in sorted(grouped, key=normalize_lookup_key):
            evidence = grouped[name]
            ranked = self._rank_roles(name, evidence["skills"])
            decision, role_id, role_name, confidence, reason = self._suggest(name, evidence, ranked)
            candidate_id = "candidate:" + hashlib.sha1(normalize_lookup_key(name).encode("utf-8")).hexdigest()[:16]
            level = "HIGH" if len(evidence["rows"]) >= 3 and len(evidence["companies"]) >= 3 else "LOW"
            item: dict[str, Any] = {
                "candidate_id": candidate_id, "source_name": name, "jd_count": len(evidence["rows"]),
                "company_count": len(evidence["companies"]), "original_variant_count": len(evidence["originals"]),
                "top_original_names": "；".join(x for x, _ in evidence["originals"].most_common(5)),
                "top_skills": "；".join(x for x, _ in evidence["skills"].most_common(12)),
                "evidence_level": level, "suggested_decision": decision, "suggested_role_id": role_id,
                "suggested_role_name": role_name, "suggested_confidence": f"{confidence:.4f}",
                "suggested_reason": reason,
                "candidate_1": ranked[0]["name"] if ranked else "", "candidate_1_score": ranked[0]["score"] if ranked else "",
                "candidate_2": ranked[1]["name"] if len(ranked) > 1 else "", "candidate_2_score": ranked[1]["score"] if len(ranked) > 1 else "",
                "ai_decision": "", "ai_target_role_id": "", "ai_canonical_name": "",
                "ai_parent_role_id": "", "ai_tags": "", "ai_confidence": "",
                "ai_reason": "", "ai_model_version": "",
                "reviewer_decision": "", "reviewer_role_id": "", "reviewer_canonical_name": "",
                "reviewer_parent_role_id": "", "reviewer_tags": "", "reviewer_note": "",
                "review_status": "PENDING", "definition_version": "1",
            }
            if ai_judge.enabled and len(evidence["rows"]) >= self.config.min_new_role_jds and len(evidence["companies"]) >= self.config.min_new_role_companies:
                ai_result = ai_judge.judge(
                    {"candidate_id": candidate_id, "name": name, "jd_count": len(evidence["rows"]), "company_count": len(evidence["companies"]), "skills": evidence["skills"].most_common(12), "sample_jds": evidence["samples"]},
                    ranked,
                )
                item["ai_review"] = json.dumps(ai_result, ensure_ascii=False)
                ai_rows.append({"candidate_id": candidate_id, "source_name": name, **ai_result})
            old = previous.get(candidate_id, {})
            for key in ("ai_decision", "ai_target_role_id", "ai_canonical_name", "ai_parent_role_id",
                        "ai_tags", "ai_confidence", "ai_reason", "ai_model_version",
                        "reviewer_decision", "reviewer_role_id", "reviewer_canonical_name",
                        "reviewer_parent_role_id", "reviewer_tags", "reviewer_note", "review_status", "definition_version"):
                if old.get(key):
                    item[key] = old[key]
            review_rows.append(item)
            evidence_rows.append({"candidate_id": candidate_id, "source_name": name, "top_candidates": ranked,
                                  "sample_jds": evidence["samples"], "top_skills": evidence["skills"].most_common(20),
                                  "jd_count": len(evidence["rows"]), "company_count": len(evidence["companies"])})
            if decision in {"EXISTING_ROLE", "ALIAS"} and confidence == 1.0:
                deterministic[name] = {"decision": decision, "role_id": role_id, "canonical_name": role_name,
                                       "confidence": "1.0000", "status": "SYSTEM_APPROVED"}

        _write_csv(review_path, review_rows, self.REVIEW_FIELDS)
        with (output_dir / "ai_decisions.jsonl").open("w", encoding="utf-8") as stream:
            for row in ai_rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        with (output_dir / "ai_review_payload.jsonl").open("w", encoding="utf-8") as stream:
            for item in evidence_rows:
                stream.write(json.dumps(item, ensure_ascii=False) + "\n")

        approved = self._approved_map(review_rows, deterministic)
        self._write_master(output_dir, review_rows)
        self._write_aliases(output_dir, approved)
        self._write_relations(output_dir, approved)
        mapped_count = self._write_job_mapping(output_dir, rows, approved)
        unresolved = [r for r in review_rows if r["source_name"] not in approved]
        _write_csv(output_dir / "unresolved_names.csv", unresolved, self.REVIEW_FIELDS)
        manifest = {
            "format_version": self.config.version, "input_rows": len(rows), "unique_names": len(grouped),
            "controlled_roles": len(self.roles), "deterministic_names": len(deterministic),
            "approved_name_mappings": len(approved), "mapped_job_rows": mapped_count,
            "unresolved_names": len(unresolved),
            "ai_default_mappings": sum(bool(r.get("ai_decision")) and r.get("ai_decision") in FINAL_DECISIONS for r in review_rows),
            "new_roles_approved": len({
                str(r.get("reviewer_canonical_name") or r.get("ai_canonical_name") or "").strip()
                for r in review_rows
                if ((r.get("review_status") == "APPROVED" and r.get("reviewer_decision") == "NEW_ROLE_CANDIDATE")
                    or (r.get("review_status") not in {"OBSERVE", "REJECTED"}
                        and not r.get("reviewer_decision") and r.get("ai_decision") == "NEW_ROLE_CANDIDATE"))
                and str(r.get("reviewer_canonical_name") or r.get("ai_canonical_name") or "").strip()
            }),
            "neo4j_written": False,
        }
        (output_dir / "concept_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self._write_history(output_dir, review_rows)
        self._write_graph_draft(output_dir)
        return manifest

    def _approved_map(self, review_rows: list[dict[str, Any]], deterministic: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
        result = dict(deterministic)
        for row in review_rows:
            source_name = row["source_name"]
            if row.get("review_status") in {"OBSERVE", "REJECTED"}:
                result.pop(source_name, None)
                continue
            human = row.get("review_status") == "APPROVED" and row.get("reviewer_decision") in FINAL_DECISIONS
            ai_default = not row.get("reviewer_decision") and row.get("ai_decision") in FINAL_DECISIONS
            if not human and not ai_default:
                continue
            decision = row["reviewer_decision"] if human else row["ai_decision"]
            if decision == "NEW_ROLE_CANDIDATE":
                canonical = str((row.get("reviewer_canonical_name") if human else row.get("ai_canonical_name")) or "").strip()
                if not canonical:
                    continue
                role_id = str((row.get("reviewer_role_id") if human else "") or stable_role_id(canonical)).strip()
            elif decision in {"NON_IT", "NOISE"}:
                role_id, canonical = "", ""
            else:
                role_id = str((row.get("reviewer_role_id") if human else row.get("ai_target_role_id"))
                              or row.get("suggested_role_id") or "").strip()
                role = self.role_by_id.get(role_id)
                fallback_name = row.get("reviewer_canonical_name") if human else row.get("ai_canonical_name")
                canonical = role.canonical_name if role else str(fallback_name or "").strip()
                if not role_id or not canonical:
                    continue
            parent = row.get("reviewer_parent_role_id") if human else row.get("ai_parent_role_id")
            tags = row.get("reviewer_tags") if human else row.get("ai_tags")
            result[source_name] = {"decision": decision, "role_id": role_id,
                                          "canonical_name": canonical, "confidence": "1.0000",
                                          "parent_role_id": str(parent or ""), "tags": str(tags or ""),
                                          "definition_version": str(row.get("definition_version") or "1"),
                                          "status": "HUMAN_APPROVED" if human else "AI_APPROVED"}
        return result

    def _write_master(self, output_dir: Path, review_rows: list[dict[str, Any]]) -> None:
        rows = [{"role_id": r.role_id, "canonical_name": r.canonical_name, "family": r.family,
                 "parent_role_id": "", "definition_version": "1", "status": "CONTROLLED"} for r in self.roles]
        seen = {normalize_lookup_key(r["canonical_name"]) for r in rows}
        for item in review_rows:
            if item.get("review_status") in {"OBSERVE", "REJECTED"}:
                continue
            human = item.get("review_status") == "APPROVED" and item.get("reviewer_decision") == "NEW_ROLE_CANDIDATE"
            ai_default = not item.get("reviewer_decision") and item.get("ai_decision") == "NEW_ROLE_CANDIDATE"
            if not human and not ai_default:
                continue
            canonical = str((item.get("reviewer_canonical_name") if human else item.get("ai_canonical_name")) or "").strip()
            if not canonical or normalize_lookup_key(canonical) in seen:
                continue
            seen.add(normalize_lookup_key(canonical))
            parent = item.get("reviewer_parent_role_id") if human else item.get("ai_parent_role_id")
            rows.append({"role_id": (item.get("reviewer_role_id") if human else "") or stable_role_id(canonical),
                         "canonical_name": canonical, "family": "", "parent_role_id": parent or "",
                         "definition_version": item.get("definition_version", "1"),
                         "status": "HUMAN_APPROVED_NEW" if human else "AI_APPROVED_NEW"})
        _write_csv(output_dir / "role_master_draft.csv", rows,
                   ["role_id", "canonical_name", "family", "parent_role_id", "definition_version", "status"])

    @staticmethod
    def _write_aliases(output_dir: Path, approved: dict[str, dict[str, str]]) -> None:
        rows = [{"source_name": name, **value} for name, value in approved.items()]
        _write_csv(output_dir / "role_alias_draft.csv", rows,
                   ["source_name", "role_id", "canonical_name", "decision", "confidence", "tags", "status"])

    @staticmethod
    def _write_relations(output_dir: Path, approved: dict[str, dict[str, str]]) -> None:
        rows = []
        for name, value in approved.items():
            if value.get("decision") != "SUBROLE_OF":
                continue
            rows.append({"subrole_name": name, "relation_type": "SUBROLE_OF",
                         "parent_role_id": value.get("role_id", ""),
                         "parent_role_name": value.get("canonical_name", ""),
                         "tags": value.get("tags", ""), "status": value.get("status", "")})
        _write_csv(output_dir / "role_relation_draft.csv", rows,
                   ["subrole_name", "relation_type", "parent_role_id", "parent_role_name", "tags", "status"])

    @staticmethod
    def _write_job_mapping(output_dir: Path, rows: list[dict[str, str]], approved: dict[str, dict[str, str]]) -> int:
        output = []
        mapped = 0
        for row in rows:
            source_name = str(row.get("岗位名称") or "").strip()
            decision = approved.get(source_name, {})
            if decision:
                mapped += 1
            output.append({"职位ID": row.get("职位ID", ""), "原始职位名称": row.get("原始职位名称", ""),
                           "归一化候选名称": source_name, "role_id": decision.get("role_id", ""),
                           "标准岗位名称": decision.get("canonical_name", ""), "匹配类型": decision.get("decision", "PENDING"),
                           "置信度": decision.get("confidence", ""), "上级岗位ID": decision.get("parent_role_id", ""),
                           "方向标签": decision.get("tags", ""), "定义版本": decision.get("definition_version", ""),
                           "审核状态": decision.get("status", "PENDING")})
        _write_csv(output_dir / "job_role_mapping_draft.csv", output,
                   ["职位ID", "原始职位名称", "归一化候选名称", "role_id", "标准岗位名称", "匹配类型", "置信度",
                    "上级岗位ID", "方向标签", "定义版本", "审核状态"])
        return mapped

    @staticmethod
    def _write_history(output_dir: Path, review_rows: list[dict[str, Any]]) -> None:
        path = output_dir / "decision_history.jsonl"
        previous: dict[str, str] = {}
        if path.exists():
            with path.open("r", encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        item = json.loads(line)
                        previous[str(item.get("candidate_id"))] = str(item.get("signature"))
        additions = []
        for row in review_rows:
            if row.get("review_status") != "APPROVED":
                continue
            signature = hashlib.sha1(json.dumps({
                "decision": row.get("reviewer_decision"), "role_id": row.get("reviewer_role_id"),
                "canonical": row.get("reviewer_canonical_name"), "parent": row.get("reviewer_parent_role_id"),
                "tags": row.get("reviewer_tags"), "version": row.get("definition_version"),
            }, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            if previous.get(row["candidate_id"]) != signature:
                additions.append({"candidate_id": row["candidate_id"], "signature": signature,
                                  "decision": row.get("reviewer_decision"), "role_id": row.get("reviewer_role_id"),
                                  "canonical_name": row.get("reviewer_canonical_name"),
                                  "definition_version": row.get("definition_version"), "reviewer_note": row.get("reviewer_note")})
        if additions:
            with path.open("a", encoding="utf-8") as stream:
                for item in additions:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def _write_graph_draft(output_dir: Path) -> None:
        def rows(name: str) -> list[dict[str, str]]:
            path = output_dir / name
            return _read_csv(path) if path.exists() else []
        payload = {"mode": "DRAFT_NO_GRAPH_WRITE", "roles": rows("role_master_draft.csv"),
                   "aliases": rows("role_alias_draft.csv"), "relations": rows("role_relation_draft.csv")}
        (output_dir / "graph_import_draft.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
