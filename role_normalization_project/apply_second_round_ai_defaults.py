"""对第二轮语义簇应用保守的AI默认岗位概念，并回填招聘映射草案。"""
from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from role_normalizer.registry import normalize_lookup_key

PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
RESULTS = WORKSPACE / "2026数据51job" / "岗位概念标准化结果"
CLUSTER_DIR = RESULTS / "second_round_clustering"
EVIDENCE = CLUSTER_DIR / "second_round_cluster_evidence.jsonl"
MASTER = RESULTS / "role_master_draft.csv"
ALIASES = RESULTS / "role_alias_draft.csv"
MAPPING = RESULTS / "job_role_mapping_draft.csv"
OUTPUT = CLUSTER_DIR / "second_round_ai_decisions.csv"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore"); writer.writeheader(); writer.writerows(rows)


def stable_id(name: str) -> str:
    return "role:" + hashlib.sha1(normalize_lookup_key(name).encode("utf-8")).hexdigest()[:16]


def clean_name(value: str) -> str:
    text = re.sub(r"(?:急招|高薪|双休|五险一金|包吃住|十三薪|周末双休)", "", str(value or ""), flags=re.I)
    text = re.sub(r"^[：:。.\-\s]+|[（(].{0,20}[）)]$", "", text).strip()
    return text[:40]


NON_IT = re.compile(r"文员|行政|档案|采购|销售|医药代表|教师|老师|舞台|灯光|演员|编剧|短视频|剪辑|编导|原画|动画师|操作工|装配|生产|工艺|维修|质检|检验员|财务|保险|物流|临床|科研助理|研究助理|珠宝|造价|装修|客服|市场推广|数据录入|资料录入|化学仪器", re.I)
IT_HINT = re.compile(r"(?<![A-Za-z])IT(?![A-Za-z])|软件|开发|算法|数据|系统|网络|运维|安全|云|Java|Python|C\+\+|\.NET|AI|人工智能|芯片|嵌入式|测试|数据库|BI|ETL|ERP|MES|前端|后端|产品经理|FAE|DevOps", re.I)
STRONG_NON_IT = re.compile(r"文员|行政|档案|采购|销售|教师|老师|短视频|剪辑|编导|原画|动画师|数据录入|资料录入|化学仪器|财务|保险|物流|客服", re.I)
GENERIC = {"技术员", "工程师", "开发工程师", "系统工程师", "应用工程师", "仿真工程师", "IT", "IT工程师", "信息技术岗", "助理工程师", "项目经理", "项目专员", "项目助理", "项目工程师", "项目管理", "项目协调员", "Project Coordinator"}


PATTERNS = [
    (r"java", "Java开发工程师"), (r"python", "Python开发工程师"), (r"c\+\+|cpp", "C++开发工程师"),
    (r"\.net|c#|asp\.net", ".NET开发工程师"), (r"php", "PHP开发工程师"),
    (r"android|安卓", "Android开发工程师"), (r"前端|web前端", "前端开发工程师"),
    (r"全栈", "全栈开发工程师"), (r"测试开发", "测试开发工程师"),
    (r"自动化测试", "自动化测试工程师"), (r"软件测试", "软件测试工程师"),
    (r"硬件测试|emc测试", "硬件测试工程师"), (r"网络安全|渗透|安全工程师", "网络安全工程师"),
    (r"devops|云原生|云计算|系统运维|运维", "运维工程师"), (r"网络工程师|网络管理员", "网络工程师"),
    (r"数据治理", "数据治理工程师"), (r"数据科学|data scientist", "数据科学家"),
    (r"数据架构|数据建模", "数据建模工程师"), (r"数据工程师|数据开发|大数据开发|数仓|etl", "数据工程师"),
    (r"bi engineer|bi开发|powerbi", "BI工程师"), (r"数据分析|商业分析", "数据分析师"),
    (r"机器视觉|计算机视觉|图像算法|视觉算法", "计算机视觉工程师"),
    (r"nlp|自然语言", "自然语言处理工程师"), (r"机器学习", "机器学习工程师"),
    (r"深度学习", "深度学习工程师"), (r"算法|ai算法", "算法工程师"),
    (r"ai应用|大模型应用|agent开发", "AI应用工程师"), (r"智能驾驶|智驾|adas", "智能驾驶工程师"),
    (r"fae|现场应用|功率器件.*应用|芯片应用工程师", "现场应用工程师（FAE）"),
    (r"产品经理|product manager", "产品经理"), (r"需求分析", "需求工程师"),
    (r"系统集成", "系统集成工程师"), (r"解决方案", "解决方案工程师"),
    (r"软件实施|实施工程师|实施顾问", "软件实施工程师"), (r"erp", "ERP实施工程师"), (r"mes", "MES工程师"),
    (r"嵌入式.*软件|驱动开发|单片机", "嵌入式软件开发工程师"),
    (r"嵌入式.*硬件", "嵌入式硬件开发工程师"), (r"电源", "电源开发工程师"),
    (r"硬件|电子工程师", "硬件工程师"), (r"自动控制|控制工程师|自动化工程师", "自动控制工程师"),
    (r"ui设计", "UI设计师"), (r"游戏策划", "游戏策划"), (r"游戏开发|unity|ue4|cocos", "游戏开发工程师"),
]


def decide(item: dict[str, object], role_by_name: dict[str, dict[str, str]], alias_index: dict[str, dict[str, str]]) -> dict[str, object]:
    representative = str(item.get("representative_name") or "").strip()
    names = str(item.get("top_names") or "")
    skills = str(item.get("top_skills") or "")
    searches = str(item.get("search_keywords") or "")
    # 证据强度按“职位名称 > 技能/JD摘要 > 搜索入口词”排列。搜索词来自采集入口，
    # 不能因为记录由“数据分析师”等关键词搜到，就把文员或项目助理归成该岗位。
    combined = " ".join((representative, names, skills))
    count, companies = int(item.get("record_count") or 0), int(item.get("company_count") or 0)
    exact = alias_index.get(normalize_lookup_key(representative))
    if exact and exact.get("role_id"):
        return {"decision": "ALIAS", "role_id": exact["role_id"], "canonical_name": exact["canonical_name"],
                "confidence": 0.98, "reason": "代表名称已命中第一轮AI默认岗位或受控别名。"}
    if STRONG_NON_IT.search(representative) or (NON_IT.search(representative) and not IT_HINT.search(representative)):
        return {"decision": "NON_IT", "role_id": "", "canonical_name": "非IT岗位", "confidence": 0.97,
                "reason": "职位名称明确属于行政、销售、内容、制造或其他非IT岗位。"}
    if representative in GENERIC:
        return {"decision": "INSUFFICIENT_INFO", "role_id": "", "canonical_name": representative, "confidence": 0.62,
                "reason": "代表名称仍然过于宽泛，搜索入口词不能替代岗位概念证据。"}
    for pattern, canonical in PATTERNS:
        # 自动映射要求岗位名称本身命中；技能和搜索词仅用于解释，不能单独触发。
        if re.search(pattern, representative, re.I) and canonical in role_by_name:
            role = role_by_name[canonical]
            return {"decision": "SUBROLE_OF" if representative != canonical else "EXISTING_ROLE",
                    "role_id": role["role_id"], "canonical_name": canonical, "confidence": 0.88,
                    "reason": f"簇内名称、技能和搜索证据共同指向“{canonical}”。"}
    if NON_IT.search(names) and not IT_HINT.search(representative + " " + skills):
        return {"decision": "NON_IT", "role_id": "", "canonical_name": "非IT岗位", "confidence": 0.94,
                "reason": "簇内职责主要属于行政、内容、销售、制造或其他非IT岗位。"}
    if re.search(r"IT项目经理|软件项目经理|信息系统项目经理|数据项目经理", representative, re.I):
        canonical = "IT项目经理"
        return {"decision": "NEW_ROLE_CANDIDATE", "role_id": stable_id(canonical), "canonical_name": canonical,
                "confidence": 0.86, "reason": "项目管理职责稳定，且技能和搜索入口明确属于软件、数据或信息系统项目。"}
    if re.search(r"产品管理", representative) and "产品经理" in role_by_name:
        role = role_by_name["产品经理"]
        return {"decision": "SUBROLE_OF", "role_id": role["role_id"], "canonical_name": "产品经理",
                "confidence": 0.86, "reason": "岗位名称表明核心职责为产品管理，芯片等对象作为方向标签。"}
    if count >= 3 and companies >= 3 and IT_HINT.search(representative):
        canonical = clean_name(representative)
        return {"decision": "NEW_ROLE_CANDIDATE", "role_id": stable_id(canonical), "canonical_name": canonical,
                "confidence": 0.78, "reason": "簇内至少有3条跨企业记录，职责方向一致且现有岗位库没有明确覆盖。"}
    if NON_IT.search(combined):
        return {"decision": "NON_IT", "role_id": "", "canonical_name": "非IT岗位", "confidence": 0.90,
                "reason": "岗位概念不属于当前IT岗位图谱范围。"}
    return {"decision": "INSUFFICIENT_INFO", "role_id": "", "canonical_name": clean_name(representative),
            "confidence": 0.58, "reason": "仅有少量记录或岗位边界不稳定，暂不创建新岗位。"}


def main() -> int:
    master_rows = read_csv(MASTER); role_by_name = {row["canonical_name"]: row for row in master_rows}
    alias_index = {normalize_lookup_key(row["source_name"]): row for row in read_csv(ALIASES)}
    evidence = [json.loads(line) for line in EVIDENCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    decisions, record_decisions = [], {}
    for item in evidence:
        result = decide(item, role_by_name, alias_index)
        row = {"cluster_id": item["cluster_id"], "representative_name": item["representative_name"],
               "record_count": item["record_count"], "company_count": item["company_count"],
               "cohesion": item["cohesion"], **result, "status": "AI_APPROVED", "manual_override": ""}
        decisions.append(row)
        if result["decision"] != "INSUFFICIENT_INFO":
            for job_id in item["record_ids"]:
                record_decisions[str(job_id)] = row

    fields = ["cluster_id", "representative_name", "record_count", "company_count", "cohesion", "decision",
              "role_id", "canonical_name", "confidence", "reason", "status", "manual_override"]
    write_csv(OUTPUT, decisions, fields)

    # AI新岗位进入主表草案；同名只保留一个稳定role_id。
    seen = {normalize_lookup_key(row["canonical_name"]) for row in master_rows}
    for row in decisions:
        if row["decision"] != "NEW_ROLE_CANDIDATE" or not row["canonical_name"]: continue
        key = normalize_lookup_key(str(row["canonical_name"]))
        if key in seen: continue
        seen.add(key); master_rows.append({"role_id": row["role_id"], "canonical_name": row["canonical_name"],
                                           "family": "", "parent_role_id": "", "definition_version": "1",
                                           "status": "AI_APPROVED_NEW_ROUND2"})
    write_csv(MASTER, master_rows, ["role_id", "canonical_name", "family", "parent_role_id", "definition_version", "status"])

    mapping_rows = read_csv(MAPPING); resolved_records = 0
    for row in mapping_rows:
        decision = record_decisions.get(str(row.get("职位ID") or ""))
        if not decision: continue
        resolved_records += 1
        row.update({"role_id": decision["role_id"], "标准岗位名称": decision["canonical_name"],
                    "匹配类型": decision["decision"], "置信度": f"{float(decision['confidence']):.4f}",
                    "审核状态": "AI_APPROVED_ROUND2", "定义版本": "1"})
    mapping_fields = ["职位ID", "原始职位名称", "归一化候选名称", "role_id", "标准岗位名称", "匹配类型", "置信度",
                      "上级岗位ID", "方向标签", "定义版本", "审核状态"]
    write_csv(MAPPING, mapping_rows, mapping_fields)
    counts = Counter(str(x["decision"]) for x in decisions)
    result = {"clusters": len(decisions), "decisions": dict(counts), "resolved_records": resolved_records,
              "remaining_cluster_singletons": sum(1 for row in mapping_rows if row.get("匹配类型") == "PENDING"),
              "role_master_count": len(master_rows), "neo4j_written": False}
    (CLUSTER_DIR / "second_round_resolution_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
