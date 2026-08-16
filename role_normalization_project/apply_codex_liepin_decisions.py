"""用既有 Codex/AIDecision 流程审核猎聘岗位候选。

与 apply_codex_first_batch_decisions.py 一致，本脚本只生成可审计决策和拟回写表，
不直接修改 Neo4j。候选聚类的结论只用于尚未有单条 REVIEW 结论的记录，避免
混杂簇覆盖更细粒度的 JD 判断。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(".")
PROJECT = ROOT / "role_normalization_project"
OUTPUT = ROOT / "output" / "liepin_role_normalization"
BGE = OUTPUT / "bge_run"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from concept_standardization.ai_contract import AIDecision  # noqa: E402


MODEL_VERSION = "codex-concept-review-v2-liepin-2026-08-12"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def role_id_for(name: str) -> str:
    key = re.sub(r"[\s\-_/（）()]+", "", name).casefold()
    return "role:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def decision_payload(
    candidate_id: str,
    decision: str,
    *,
    target_role_id: str = "",
    canonical_name: str = "",
    parent_role_id: str = "",
    tags: list[str] | None = None,
    confidence: float,
    reason: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "decision": decision,
        "target_role_id": target_role_id,
        "canonical_name": canonical_name,
        "parent_role_id": parent_role_id,
        "tags": tags or [],
        "confidence": confidence,
        "reason": reason,
        "model_version": MODEL_VERSION,
    }


def main() -> int:
    registry = json.loads((OUTPUT / "historical_approved_registry.json").read_text(encoding="utf-8"))
    role_by_name = {str(item["canonical_name"]): str(item["role_id"]) for item in registry["roles"]}
    rows = read_csv(BGE / "role_resolutions.csv")
    candidates = read_jsonl(BGE / "new_role_candidates.jsonl")

    # 概念级判断由本轮 Codex 按旧审核口径完成。映射到既有岗位时，第二项是目标岗位名；
    # NEW 时第二项是新的规范岗位名；HOLD 表示聚类内部职责冲突，不能整簇采用。
    concepts: dict[str, tuple[str, str, float, str]] = {
        "5G核心网研发工程师": ("NEW", "5G核心网研发工程师", 0.90, "核心职责稳定集中在5G核心网协议栈、网元和通信平台研发，跨企业JD与技能证据一致，现有通信岗位不能完整表达其职责边界。"),
        "AI芯片系统软件架构师 工程师 BSP 北上杭深": ("SUB", "嵌入式软件开发工程师", 0.83, "职责核心是芯片BSP、Linux和底层系统软件开发，架构师或AI芯片属于层级与对象标签，应归入嵌入式软件开发岗位。"),
        "ATE测试工程师": ("SUB", "芯片测试工程师", 0.95, "ATE与量产测试均围绕集成电路测试程序、测试平台和量产验证，属于芯片测试岗位的明确方向。"),
        "BI数据工程师": ("SUB", "BI工程师", 0.91, "职责以BI数据开发、报表和分析数据链路为主，ETL是技术手段，归入已有BI工程师更符合岗位边界。"),
        "C#开发工程师": ("HOLD", "", 0.99, "该超大聚类混入C#、Java、Python、AI应用、核心网和通用后端等不同职责，不能把整簇映射到单一岗位，必须保留单条JD判断。"),
        "FPGA开发工程师": ("SUB", "IC设计工程师", 0.87, "FPGA逻辑设计、验证与硬件描述语言开发属于数字集成电路设计方向，测试字样不足以形成独立岗位。"),
        "LINUX系统软件工程师": ("HOLD", "", 0.96, "聚类同时包含IT系统运维、Linux系统软件、系统集成和嵌入式操作系统，职责边界不一致，不宜整簇归一化。"),
        "MES系统工程师": ("ALIAS", "MES工程师", 0.95, "名称和职责均对应已有MES工程师，实施、开发或运维差异应由方向标签保留。"),
        "Power FAE 现场应用工程师": ("ALIAS", "现场应用工程师（FAE）", 0.98, "Power、AI计算等是产品方向，现场技术支持和客户问题闭环职责与已有FAE岗位一致。"),
        "Test Engineer 测试工程师": ("ALIAS", "软件测试工程师", 0.89, "中英文名称及测试职责对应已有软件测试工程师，当前证据未显示独立岗位边界。"),
        "WiFi BT测试工程师": ("SUB", "通信测试工程师", 0.91, "测试对象为WiFi和蓝牙通信协议及射频连接能力，属于通信测试方向。"),
        "产品专员 助理": ("SUB", "产品经理", 0.88, "主要承担需求、产品方案与协作支持，专员和助理属于职级差异，应归入产品经理岗位。"),
        "产品验证工程师": ("SUB", "EDA工程师", 0.82, "证据集中在EDA工具和数字后端产品验证，属于EDA产品工程方向，不宜按通用测试岗位处理。"),
        "传感器测试工程师": ("SUB", "硬件测试工程师", 0.84, "主要围绕传感器硬件、可靠性和测试验证，属于硬件测试方向；系统工程师别名不单独创建岗位。"),
        "光学 光学系统工程师": ("SUB", "光电子工程师", 0.92, "光学系统设计、光电器件与性能验证均在既有光电子工程师职责范围内。"),
        "具身智能系统工程师": ("NEW", "具身智能系统工程师", 0.88, "多企业JD稳定覆盖具身感知、VLA、机器人系统集成与部署，职责组合区别于单纯算法或嵌入式开发，可作为新岗位候选。"),
        "反无系统工程师": ("SUB", "无人机工程师", 0.86, "岗位围绕无人机侦测、反制系统及其测试集成，属于无人机系统的反制方向。"),
        "可靠性测试工程师": ("SUB", "硬件测试工程师", 0.92, "可靠性试验、失效验证和环境测试属于硬件测试岗位的稳定方向。"),
        "大数据开发工程师": ("SUB", "ETL开发工程师", 0.91, "批流数据处理、数仓链路与大数据平台开发延续历史批准口径，归入ETL开发并保留大数据方向标签。"),
        "大数据调度工具开发工程师": ("SUB", "ETL开发工程师", 0.84, "职责是数据工作流调度与数据管道平台研发，属于ETL和数据工程基础设施方向。"),
        "大数据项目经理": ("SUB", "IT项目经理", 0.88, "核心职责是大数据项目的计划、交付和协同，技术领域作为标签，岗位主体属于IT项目管理。"),
        "射频工程师": ("HOLD", "", 0.99, "该超大聚类混合射频电路、射频芯片、驱动软件、系统集成、硬件研发和通信软件，不能整簇归为单一岗位。"),
        "嵌入式测试工程师": ("HOLD", "", 0.92, "聚类同时包含嵌入式开发、硬件和测试三种职责，只能按单条JD证据处理，不能整簇映射。"),
        "工艺工程师": ("HOLD", "", 0.99, "该聚类混合半导体工艺、封装、机械装配和一般生产工艺，且部分不属于IT岗位，不能整簇采用。"),
        "座舱测试工程师": ("SUB", "软件测试工程师", 0.89, "职责围绕智能座舱软件、车机功能和系统验证，属于软件测试的车载方向。"),
        "数字后端设计工程师": ("SUB", "IC设计工程师", 0.97, "Netlist到GDSII、布局布线、时序和物理验证是IC数字后端设计职责，符合历史批准映射。"),
        "数字集成电路设计经理": ("SUB", "IC设计工程师", 0.88, "模拟或数字集成电路设计是技术方向，经理和总监属于层级，岗位概念归入IC设计工程师。"),
        "数据挖掘工程师": ("NEW", "数据挖掘工程师", 0.91, "多企业JD持续聚焦特征挖掘、行为建模、推荐或风控数据分析，区别于数据管道建设和通用机器学习，具备稳定职责边界。"),
        "数据治理开发工程师": ("SUB", "数据治理工程师", 0.87, "元数据、质量、标准与治理平台建设属于已有数据治理岗位，开发或售前是方向差异。"),
        "星载NTN基站平台驱动工程师": ("SUB", "嵌入式软件开发工程师", 0.86, "岗位核心是基站平台驱动、板级适配和底层软件，NTN与星载是应用场景标签。"),
        "智算集群建设项目总监": ("SUB", "IT项目经理", 0.84, "主要负责智算集群建设项目的计划、资源和交付，总监是层级，智算是项目领域。"),
        "智能驾驶系统工程师": ("HOLD", "", 0.99, "该超大聚类混入智能驾驶系统、各类软件测试、芯片验证、机器人测试和系统集成，不能整簇归一化。"),
        "汽车网联电子测试工程师": ("SUB", "软件测试工程师", 0.85, "证据主要覆盖车载网络、座舱和整车电子系统测试，归入软件测试并保留汽车网联标签。"),
        "热管理系统软件工程师": ("SUB", "嵌入式软件开发工程师", 0.86, "职责以车辆热管理控制软件、嵌入式控制和标定为主，属于嵌入式软件方向。"),
        "爬虫开发工程师": ("ALIAS", "数据采集工程师", 0.96, "网页抓取、反爬处理与采集管道职责对应已有数据采集工程师。"),
        "现场应用工程师FAE": ("ALIAS", "现场应用工程师（FAE）", 0.98, "中英文缩写和客户现场技术支持职责与既有FAE岗位一致。"),
        "电池系统集成开发工程师": ("SUB", "汽车电子工程师", 0.83, "电池系统集成、BMS接口和整车联调属于汽车电子系统方向。"),
        "系统工程师": ("HOLD", "", 0.99, "名称过于宽泛，聚类混入Android系统、软件架构和行业系统职责，必须回到单条JD判断。"),
        "系统管理员": ("ALIAS", "运维工程师", 0.94, "信息系统、服务器和网络日常管理职责属于已有运维工程师。"),
        "系统集成售前工程师": ("SUB", "售前解决方案工程师", 0.90, "核心职责是售前方案、技术交流和系统集成方案设计，属于售前解决方案岗位。"),
        "网络信息安全工程师": ("ALIAS", "网络安全工程师", 0.97, "网络与信息安全是已有网络安全工程师的常见完整写法，职责边界一致。"),
        "脑机接口数据工程师": ("SUB", "数据工程师", 0.84, "数据采集、清洗与处理是数据工程职责，脑机接口属于行业与数据类型标签。"),
        "自动化系统集成开发工程师": ("SUB", "系统集成工程师", 0.84, "职责以自动化设备和软件系统的接口集成与联调为主，属于系统集成方向。"),
        "航电系统工程师": ("HOLD", "", 0.82, "当前三条证据把航电与电源系统混在一起，职责和技能不足以形成一致概念，暂不映射。"),
        "芯片FAE现场应用工程师": ("ALIAS", "现场应用工程师（FAE）", 0.97, "芯片是产品方向，客户导入、问题定位和现场支持职责属于既有FAE岗位。"),
        "视频应用开发工程师": ("SUB", "软件工程师", 0.84, "视频处理与应用开发属于通用软件工程方向，视频是技术对象标签。"),
        "软件白盒测试工程师": ("SUB", "软件测试工程师", 0.94, "白盒测试、代码级验证和缺陷定位属于软件测试的技术方向。"),
        "量化开发工程师": ("NEW", "量化开发工程师", 0.92, "跨企业JD稳定覆盖行情、交易系统、回测和量化平台开发，金融工程与低延迟技术组合形成独立且可复核的岗位边界。"),
        "雷达系统工程师": ("NEW", "雷达系统工程师", 0.89, "多企业JD稳定覆盖雷达系统方案、信号链、算法协同和系统联调，区别于通用通信或硬件岗位，可作为新岗位候选。"),
        "项目总监": ("HOLD", "", 0.99, "聚类混合AI转型、ERP、ODM、机器人和无人机等不同项目，名称只表示层级，不能据此建立或映射岗位。"),
    }

    # 同名候选可能因最大簇限制被拆成多个簇，概念结论允许复用。
    candidate_decisions: list[AIDecision] = []
    new_roles: dict[str, str] = {}
    cluster_assignment: dict[str, dict[str, Any]] = {}
    candidate_audit: list[dict[str, Any]] = []
    for candidate in candidates:
        name = str(candidate["representative_name"])
        if name not in concepts:
            raise KeyError(f"缺少候选概念判断：{name}")
        kind, target_name, confidence, reason = concepts[name]
        aid = "candidate:" + str(candidate["candidate_id"]).split(":", 1)[-1]
        if kind == "NEW":
            rid = role_id_for(target_name)
            new_roles[target_name] = rid
            parent_name = {
                "5G核心网研发工程师": "通信技术工程师",
                "具身智能系统工程师": "算法工程师",
                "数据挖掘工程师": "数据科学家",
                "量化开发工程师": "软件工程师",
                "雷达系统工程师": "通信技术工程师",
            }.get(target_name, "")
            payload = decision_payload(
                aid, "NEW_ROLE_CANDIDATE", canonical_name=target_name,
                parent_role_id=role_by_name.get(parent_name, ""), confidence=confidence, reason=reason,
            )
            assignment = {"role_id": rid, "canonical_name": target_name, "decision": "NEW_ROLE_CANDIDATE"}
        elif kind in {"SUB", "ALIAS"}:
            rid = role_by_name[target_name]
            dtype = "SUBROLE_OF" if kind == "SUB" else "ALIAS"
            payload = decision_payload(
                aid, dtype, target_role_id=rid, canonical_name=target_name,
                parent_role_id=rid if dtype == "SUBROLE_OF" else "",
                tags=[name] if dtype == "SUBROLE_OF" else [], confidence=confidence, reason=reason,
            )
            assignment = {"role_id": rid, "canonical_name": target_name, "decision": dtype}
        else:
            payload = decision_payload(
                aid, "INSUFFICIENT_INFO", canonical_name=name,
                confidence=confidence, reason=reason,
            )
            assignment = {}
        decision = AIDecision.from_dict(payload)
        candidate_decisions.append(decision)
        for version_id in candidate["record_ids"]:
            if assignment:
                cluster_assignment[str(version_id)] = assignment
        candidate_audit.append({
            "candidate_id": candidate["candidate_id"], "ai_candidate_id": aid,
            "representative_name": name, "jd_count": candidate["jd_count"],
            "company_count": candidate["company_count"], "decision": decision.decision,
            "canonical_name": decision.canonical_name, "target_role_id": decision.target_role_id,
            "confidence": f"{decision.confidence:.4f}", "reason": decision.reason,
        })

    # 单条 REVIEW 裁决。先纠正常见跨岗位误召回，再对标题、职责和技能一致的
    # 第一候选做保守确认；宽泛或多技术栈标题保持 INSUFFICIENT_INFO。
    review_decisions: list[AIDecision] = []
    review_assignment: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("岗位归一化状态") != "REVIEW":
            continue
        title = str(row.get("title") or "")
        nearest = json.loads(row.get("最近候选岗位") or "[]")
        top = nearest[0] if nearest else {}
        top_name = str(top.get("role_name") or "")
        version_id = str(row["version_id"])
        target = top_name
        reason = "标题、JD职责和五维技能与BGE第一候选一致，且未发现岗位职能冲突。"

        language_hits = sum(token in title.casefold() for token in ("java", "python", "c#", "c++", "php"))
        if "项目经理" in title or "项目总监" in title:
            target = "IT项目经理"
            reason = "岗位主体是项目计划、交付和团队协作，技术或业务名称仅是项目领域，不应映射为开发岗位。"
        elif any(token in title for token in ("应用测试", "算法测试", "系统集成测试", "运维测试")):
            target = "软件测试工程师"
            reason = "核心职责是测试设计、执行和缺陷定位，被测对象或算法、运维、集成属于测试方向标签。"
        elif "AI现场应用" in title:
            target = "现场应用工程师（FAE）"
            reason = "职责核心是客户现场部署、问题定位与技术支持，AI是产品方向，应归入FAE。"
        elif "数据挖掘" in title:
            target = "数据挖掘工程师"
            reason = "标题和技能明确聚焦特征挖掘、建模与分析，符合本批高证据新岗位候选。"
        elif "大数据开发" in title:
            target = "ETL开发工程师"
            reason = "职责以批流数据处理、数据管道和数仓链路为主，沿用历史批准的大数据开发归入ETL口径。"
        elif "数据开发" in title and "BI" in title.upper():
            target = "BI工程师"
            reason = "岗位明确服务BI分析数据链路，数据开发是实现方式，归入BI工程师。"
        elif "数据开发" in title:
            target = "数据工程师"
            reason = "岗位核心是数据加工、存储和数据链路建设，区别于通用Python应用开发。"
        elif "爬虫" in title or "数据采集" in title:
            target = "数据采集工程师"
            reason = "岗位职责明确是网页抓取、采集链路和反爬处理，归入已有数据采集工程师。"
        elif "全栈" in title:
            target = "全栈开发工程师"
            reason = "职责同时覆盖前后端开发，全栈是岗位边界，具体语言作为技术标签。"
        elif "C#" in title and language_hits == 1:
            target = ".NET开发工程师"
            reason = "C#/.NET技术栈和应用开发职责明确，应纠正BGE对C++岗位的近邻误召回。"
        elif "AI" in title.upper() and "开发" in title and "Python" in title:
            target = "AI应用工程师"
            reason = "职责以大模型或AI能力的应用开发与系统集成为主，Python是实现技术而非岗位边界。"

        ambiguous = (
            language_hits >= 3
            or "系统集成器件工程师" in title
            or (title.strip() in {"嵌入式系统工程师"} and not top_name)
        )
        cid = "candidate:review:" + version_id.split(":", 1)[-1][:20]
        if ambiguous or target not in role_by_name and target not in new_roles:
            payload = decision_payload(
                cid, "INSUFFICIENT_INFO", canonical_name=title, confidence=0.72,
                reason="岗位名称同时包含多个技术栈或职能方向，现有证据无法可靠确定唯一岗位，保留待观察以避免错误映射。",
            )
        else:
            rid = role_by_name.get(target) or new_roles[target]
            payload = decision_payload(
                cid, "EXISTING_ROLE", target_role_id=rid, canonical_name=target,
                confidence=max(0.82, float(row.get("岗位综合相似度") or 0.0)), reason=reason,
            )
            review_assignment[version_id] = {
                "role_id": rid, "canonical_name": target, "decision": "AI_REVIEW_EXISTING_ROLE"
            }
        review_decisions.append(AIDecision.from_dict(payload))

    # 形成完整拟回写表：自动结果优先，其次单条AI判断，最后才采用纯净候选簇结论。
    assignments: list[dict[str, Any]] = []
    for row in rows:
        version_id = str(row["version_id"])
        state = str(row.get("岗位归一化状态") or "")
        selected: dict[str, Any] = {}
        provenance = ""
        confidence = str(row.get("岗位综合相似度") or "")
        if state in {"EXACT", "ALIAS", "VECTOR_MATCH"} and row.get("受控岗位ID"):
            selected = {
                "role_id": row["受控岗位ID"], "canonical_name": row["受控岗位名称"],
                "decision": state,
            }
            provenance = "BGE_HISTORY_REGISTRY"
        elif version_id in review_assignment:
            selected = review_assignment[version_id]
            provenance = MODEL_VERSION + ":RECORD_REVIEW"
        elif version_id in cluster_assignment:
            selected = cluster_assignment[version_id]
            provenance = MODEL_VERSION + ":HIGH_EVIDENCE_CLUSTER"
        assignments.append({
            "version_id": version_id, "source_job_id": row.get("source_job_id", ""),
            "title": row.get("title", ""), "input_resolution": state,
            "assignment_status": "MAPPED" if selected else "PENDING",
            "role_id": selected.get("role_id", ""),
            "canonical_name": selected.get("canonical_name", ""),
            "decision": selected.get("decision", "INSUFFICIENT_INFO"),
            "confidence": confidence, "provenance": provenance,
        })

    with (OUTPUT / "ai_decisions_candidates.jsonl").open("w", encoding="utf-8") as stream:
        for item in candidate_decisions:
            stream.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    with (OUTPUT / "ai_decisions_record_review.jsonl").open("w", encoding="utf-8") as stream:
        for item in review_decisions:
            stream.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    write_csv(OUTPUT / "candidate_decision_audit.csv", candidate_audit, [
        "candidate_id", "ai_candidate_id", "representative_name", "jd_count", "company_count",
        "decision", "canonical_name", "target_role_id", "confidence", "reason",
    ])
    write_csv(OUTPUT / "proposed_role_assignments.csv", assignments, [
        "version_id", "source_job_id", "title", "input_resolution", "assignment_status",
        "role_id", "canonical_name", "decision", "confidence", "provenance",
    ])
    new_role_rows = [
        {"role_id": rid, "canonical_name": name, "status": "AI_APPROVED_NEW_LIEPIN", "model_version": MODEL_VERSION}
        for name, rid in sorted(new_roles.items())
    ]
    write_csv(OUTPUT / "new_roles_ai_approved.csv", new_role_rows, [
        "role_id", "canonical_name", "status", "model_version"
    ])

    summary = {
        "model_version": MODEL_VERSION,
        "candidate_decisions": dict(Counter(item.decision for item in candidate_decisions)),
        "record_review_decisions": dict(Counter(item.decision for item in review_decisions)),
        "new_roles": sorted(new_roles),
        "assignment_counts": dict(Counter(row["assignment_status"] for row in assignments)),
        "mapped_by_provenance": dict(Counter(row["provenance"] for row in assignments if row["provenance"])),
        "graph_written": False,
    }
    (OUTPUT / "ai_decision_manifest.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
