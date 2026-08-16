"""Codex对第一批176个高证据候选的概念判断。

只写入 ai_* 建议字段，不设置人工 APPROVED，不创建正式Role。
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from concept_standardization.ai_contract import AIDecision
from concept_standardization.engine import ConceptStandardizationEngine

PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
RESULTS = WORKSPACE / "2026数据51job" / "岗位概念标准化结果"
BATCH = RESULTS / "ai_batch_1_high_evidence.jsonl"
QUEUE = RESULTS / "concept_review_queue.csv"
OUTPUT = RESULTS / "ai_decisions_codex_first_batch.jsonl"
SUMMARY = RESULTS / "first_batch_decision_summary.csv"

ROLE = {
    "算法工程师": "role:002512ea11463fec", "需求工程师": "role:0615b6f70f47c4ee",
    "硬件工程师": "role:09171f7cf31e257a", "机器学习工程师": "role:178c0648fe4b0ba0",
    "硬件测试工程师": "role:18765a53127e4c64", "汽车电子工程师": "role:197c1f52d070546f",
    "运维工程师": "role:2b706db2b9d7d150", "C++开发工程师": "role:45bcbd9741c47b68",
    "智能驾驶工程师": "role:479aca63e210bd12", "自动控制工程师": "role:546b9153c9c6d024",
    "数据建模工程师": "role:61430b676efb31a3", "前端开发工程师": "role:5e6000924673bd4b",
    "计算机视觉工程师": "role:73682d473be54c64", "网络工程师": "role:7420ef27026bb1c9",
    "产品经理": "role:775f14fc808c83da", "游戏开发工程师": "role:7cfe3c45053c265b",
    "数据产品经理": "role:941176d98516b8a2", "平台产品经理": "role:932a40cd995999e1",
    "自动化测试工程师": "role:9641ca3b5b92d0b0", "Java开发工程师": "role:96d3148d3da1d7df",
    "ETL开发工程师": "role:9d75db2bd1707d21", "数据分析师": "role:a13c6b88bde1def9",
    "IC设计工程师": "role:ab2c72a36ed822cf", "软件测试工程师": "role:ac73d0a4492e5de9",
    "光电子工程师": "role:ae609f74f8ac9a8a", ".NET开发工程师": "role:b257290b279deb69",
    "Android开发工程师": "role:b503ef5c1d24bbd2", "嵌入式软件开发工程师": "role:d158cb6a847a4409",
    "电源开发工程师": "role:dcdc0188d0778629", "Python开发工程师": "role:f20db33d160764e8",
    "自然语言处理工程师": "role:fa9581040c1fc10d", "BI工程师": "role:fe9eb6dcd0bfadb5",
}

ALIASES = {
    ". JAVA Engineer": "Java开发工程师", "java": "Java开发工程师", "Java后端开发工程师": "Java开发工程师",
    "中 Java开发工程师": "Java开发工程师", "c#软件开发工程师": ".NET开发工程师",
    "Python开发": "Python开发工程师", "Product Manager": "产品经理", "BI开发工程师": "BI工程师",
    "nlp算法工程师": "自然语言处理工程师", "Software Test Engineer软件测试工程师": "软件测试工程师",
    "数据分析": "数据分析师", "算法": "算法工程师", "机器视觉算法工程师": "计算机视觉工程师",
    "IT运维工程师": "运维工程师", "系统运维岗": "运维工程师", "测试工程师": "软件测试工程师",
}

SUBROLES = {
    "AI 技术负责人": "算法工程师", "AI产品经理": "产品经理", "AI工程师": "算法工程师",
    "Android系统工程师": "Android开发工程师", "B端项目产品经理": "产品经理",
    "DevOps工程师": "运维工程师", "EMC测试工程师": "硬件测试工程师",
    "ERP运维工程师": "运维工程师", "Linux驱动开发工程师": "嵌入式软件开发工程师",
    "MES运维工程师": "运维工程师", "Python 数据开发 BI 工程师": "BI工程师",
    "SLAM算法工程师": "算法工程师", "产品经理 主管": "产品经理",
    "车载软件测试工程师": "软件测试工程师", "储能产品经理": "产品经理",
    "大数据开发": "ETL开发工程师", "单片机开发工程师": "嵌入式软件开发工程师",
    "电商产品经理": "产品经理", "电源工程师": "电源开发工程师", "电源硬件工程师": "电源开发工程师",
    "电子工程师": "硬件工程师", "电子硬件工程师": "硬件工程师", "调度算法工程师": "算法工程师",
    "光学工程师": "光电子工程师", "机械臂规划控制工程师": "自动控制工程师",
    "基础感知算法工程师": "计算机视觉工程师", "开关电源研发工程师": "电源开发工程师",
    "控制工程师": "自动控制工程师", "嵌入式软件测试工程师": "软件测试工程师",
    "商业分析师": "数据分析师", "上位机工程师": ".NET开发工程师",
    "摄像头驱动开发工程师": "嵌入式软件开发工程师", "数据分析经理": "数据分析师",
    "数据架构师": "数据建模工程师", "数据运营": "数据分析师", "数字后端工程师": "IC设计工程师",
    "图像调试工程师": "计算机视觉工程师", "物联网产品经理": "产品经理",
    "系统分析师": "需求工程师", "系统管理工程师": "运维工程师", "需求分析师": "需求工程师",
    "营销中台产品经理": "平台产品经理", "云计算工程师": "运维工程师",
    "智驾系统总监 工程师": "智能驾驶工程师", "自动化工程师": "自动控制工程师",
    "产品测试工程师": "硬件测试工程师", "AI设计师": "产品经理",
}

NEW_ROLES = {
    "AI Agent开发工程师": "AI应用工程师", "AI项目经理": "AI项目经理", "AI应用工程师": "AI应用工程师",
    "AE应用工程师": "现场应用工程师（FAE）", "FAE": "现场应用工程师（FAE）",
    "FAE工程师": "现场应用工程师（FAE）", "FAE技术支持工程师": "现场应用工程师（FAE）",
    "fae现场应用工程师": "现场应用工程师（FAE）", "FAE应用工程师": "现场应用工程师（FAE）",
    "现场应用工程师": "现场应用工程师（FAE）", "FDE前沿部署工程师": "AI部署工程师",
    "Helpdesk Engineer": "IT技术支持工程师", "技术支持工程师": "IT技术支持工程师",
    "售后工程师": "IT技术支持工程师", "IT经理": "IT经理", "IT主管": "IT经理",
    "MES工程师": "MES工程师", "ERP工程师": "ERP实施工程师", "ERP专员": "ERP实施工程师",
    "PLM软件系统实施工程师": "PLM实施工程师", "OA PLM开发工程师": "企业应用开发工程师",
    "PHP开发工程师": "PHP开发工程师", "Principal Software Engineer": "软件工程师",
    "RPA工程师": "RPA工程师", "UI设计师": "UI设计师", "产品设计师": "产品设计师",
    "全栈开发工程师": "全栈开发工程师", "软件工程师": "软件工程师", "软件开发": "软件工程师",
    "系统开发工程师": "软件工程师", "自动化软件工程师": "软件工程师",
    "编译器开发工程师": "编译器开发工程师", "实施工程师": "软件实施工程师",
    "软件实施经理": "软件实施工程师", "系统应用工程师": "企业应用开发工程师",
    "数据治理工程师": "数据治理工程师", "数据治理岗": "数据治理工程师",
    "数据工程师": "数据工程师", "Data Scientist": "数据科学家",
    "网络安全工程师": "网络安全工程师", "渗透测试工程师": "网络安全工程师",
    "系统集成工程师": "系统集成工程师", "信息系统工程师": "系统集成工程师",
    "信息系统监理工程师": "信息系统监理工程师", "解决方案": "解决方案工程师",
    "解决方案工程师": "解决方案工程师", "解决方案经理": "解决方案工程师",
    "政企解决方案经理": "解决方案工程师", "售前工程师": "售前解决方案工程师",
    "游戏策划": "游戏策划", "游戏数值策划": "游戏策划", "数据标注项目经理": "数据标注项目经理",
}

NON_IT = {
    "3D设计师", "AI视频制作", "BOM专员", "PCGS Business Development Manager", "SMT工艺工程师",
    "TikTok 短视频运营", "财务分析经理", "仓库管理员", "电子销售工程师", "电子元器件销售工程师",
    "短视频编导", "短视频剪辑", "工艺工程师", "机械工程师", "结构工程师", "维修工程师", "维修技术员",
    "设备技术员", "生产技术员", "文员", "珠宝设计师", "消防设计工程师", "资料员", "市场推广",
    "视频拍摄剪辑", "生物统计 经理", "早期临床项目经理", "消费者研究经理 市场调研项目经理",
    "医药产品经理", "运营经理", "量化研究员", "制冷系统工程师", "低温系统工程师", "热管理系统工程师",
    "声学工程师", "弱电工程师", "研发文员", "研发助理", "研究助理", "技术助理", "生信研发工程师",
    "销售工程师",
}

INSUFFICIENT = {
    "IT", "IT工程师", "信息技术岗", "项目经理", "项目管理", "项目工程师", "项目管理专员（Project Specialist",
    "项目协调员 Project Coordinator", "项目助理", "项目专员", "PMO专员", "交付经理", "应用工程师",
    "系统工程师", "测试", "助理工程师", "技术总监", "仿真工程师", "数据经理",
}

PARENT_FOR_NEW = {
    "AI应用工程师": ROLE["算法工程师"], "AI部署工程师": ROLE["算法工程师"],
    "网络安全工程师": ROLE["网络工程师"], "数据治理工程师": ROLE["数据建模工程师"],
    "数据科学家": ROLE["机器学习工程师"], "游戏策划": ROLE["游戏开发工程师"],
    "PHP开发工程师": ROLE["Python开发工程师"], "编译器开发工程师": ROLE["C++开发工程师"],
}


def build(name: str, candidate_id: str) -> dict[str, object]:
    if name in ALIASES:
        target = ALIASES[name]
        return {"candidate_id": candidate_id, "decision": "ALIAS", "target_role_id": ROLE[target],
                "canonical_name": target, "parent_role_id": "", "tags": [], "confidence": 0.97,
                "reason": f"名称是“{target}”的中英文、简称或常见写法，核心职责未形成独立岗位边界。"}
    if name in SUBROLES:
        target = SUBROLES[name]
        return {"candidate_id": candidate_id, "decision": "SUBROLE_OF", "target_role_id": ROLE[target],
                "canonical_name": name, "parent_role_id": ROLE[target], "tags": [name.replace(target, "").strip() or name],
                "confidence": 0.88, "reason": f"核心职责可归入“{target}”，差异主要来自技术、行业、对象或等级，应作为方向标签或子岗位。"}
    if name in NEW_ROLES:
        canonical = NEW_ROLES[name]
        return {"candidate_id": candidate_id, "decision": "NEW_ROLE_CANDIDATE", "target_role_id": "",
                "canonical_name": canonical, "parent_role_id": PARENT_FOR_NEW.get(canonical, ""), "tags": [],
                "confidence": 0.84, "reason": f"多企业和多JD证据支持“{canonical}”具有相对稳定的职责与能力组合，现有55个岗位无法完整覆盖，建议作为新岗位候选人工审核。"}
    if name in NON_IT:
        return {"candidate_id": candidate_id, "decision": "NON_IT", "target_role_id": "", "canonical_name": name,
                "parent_role_id": "", "tags": [], "confidence": 0.95,
                "reason": "岗位职责主要属于销售、制造、行政、内容、设计或行业业务，不属于当前受控IT岗位图谱范围。"}
    if name in INSUFFICIENT:
        return {"candidate_id": candidate_id, "decision": "INSUFFICIENT_INFO", "target_role_id": "", "canonical_name": name,
                "parent_role_id": "", "tags": [], "confidence": 0.60,
                "reason": "岗位名称过于宽泛或聚合了多个职责方向，必须回到单条JD重新分类，当前不宜合并或创建正式岗位。"}
    raise KeyError(name)


def main() -> int:
    tasks = [json.loads(line) for line in BATCH.read_text(encoding="utf-8").splitlines() if line.strip()]
    decisions, missing = [], []
    for task in tasks:
        try:
            item = build(str(task["source_name"]), str(task["candidate_id"]))
            item["model_version"] = "codex-concept-review-v1"
            decisions.append(AIDecision.from_dict(item))
        except KeyError:
            missing.append(str(task.get("source_name")))
    known = set(ALIASES) | set(SUBROLES) | set(NEW_ROLES) | NON_IT | INSUFFICIENT
    extras = sorted(known - {str(x["source_name"]) for x in tasks})
    if missing or extras:
        raise ValueError(json.dumps({"missing": missing, "extras": extras}, ensure_ascii=False))
    with OUTPUT.open("w", encoding="utf-8") as stream:
        for d in decisions:
            stream.write(json.dumps(d.__dict__, ensure_ascii=False) + "\n")
    with QUEUE.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    by_id = {d.candidate_id: d for d in decisions}
    for row in rows:
        d = by_id.get(row.get("candidate_id", ""))
        if not d:
            continue
        row.update({"ai_decision": d.decision, "ai_target_role_id": d.target_role_id,
                    "ai_canonical_name": d.canonical_name, "ai_parent_role_id": d.parent_role_id,
                    "ai_tags": "；".join(d.tags), "ai_confidence": f"{d.confidence:.4f}",
                    "ai_reason": d.reason, "ai_model_version": d.model_version})
    with QUEUE.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=ConceptStandardizationEngine.REVIEW_FIELDS, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)
    task_by_id = {str(x["candidate_id"]): x for x in tasks}
    with SUMMARY.open("w", encoding="utf-8-sig", newline="") as stream:
        fields = ["candidate_id", "source_name", "jd_count", "company_count", "decision", "canonical_name",
                  "target_role_id", "parent_role_id", "confidence", "reason"]
        writer = csv.DictWriter(stream, fieldnames=fields); writer.writeheader()
        for d in decisions:
            task = task_by_id[d.candidate_id]
            writer.writerow({"candidate_id": d.candidate_id, "source_name": task.get("source_name", ""),
                             "jd_count": task.get("jd_count", ""), "company_count": task.get("company_count", ""),
                             "decision": d.decision, "canonical_name": d.canonical_name,
                             "target_role_id": d.target_role_id, "parent_role_id": d.parent_role_id,
                             "confidence": f"{d.confidence:.4f}", "reason": d.reason})
    counts: dict[str, int] = {}
    for d in decisions: counts[d.decision] = counts.get(d.decision, 0) + 1
    print(json.dumps({"processed": len(decisions), "counts": counts, "queue": str(QUEUE),
                      "summary": str(SUMMARY), "formal_approvals": 0}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
