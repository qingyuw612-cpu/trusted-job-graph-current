"""对猎聘第二轮语义簇应用既有保守 AI 默认分类。

复用旧版 second_round 的证据结构和决策枚举；本轮不再创建新岗位，避免
岗位库膨胀。所有结果先写入拟回写表，不直接修改 Neo4j。
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(".")
PROJECT = ROOT / "role_normalization_project"
OUTPUT = ROOT / "output" / "liepin_role_normalization"
SECOND = OUTPUT / "second_round"
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from concept_standardization.ai_contract import AIDecision  # noqa: E402


MODEL_VERSION = "codex-second-round-v2-liepin-2026-08-12"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def decide(name: str, top_names: str, top_skills: str, role_by_name: dict[str, str]) -> tuple[str, str, float, str]:
    text = f"{name} {top_names}".casefold()
    skills = top_skills.casefold()
    # 非IT/制造工艺：即使域过滤曾判IT，也不能借搜索入口进入岗位图谱。
    if re.search(r"生产工艺|机械装配|装配工艺|电镀工艺", text):
        return "NON_IT", "", 0.96, "名称与职责属于生产制造或装配工艺，不属于当前IT岗位图谱。"
    if re.search(r"半导体|晶圆|光刻|刻蚀|离子注入|pvd|cvd|封装|量测|工艺开发|工艺工程师", text):
        return "SUBROLE_OF", "半导体工程师", 0.89, "职责集中在半导体制程、封装或晶圆工艺，归入已有半导体工程师。"
    if re.search(r"c#|\.net|asp\.net", text) and not re.search(r"c#.*c\+\+.*java|java.*c#.*c\+\+", text):
        return "ALIAS", ".NET开发工程师", 0.95, "C#/.NET技术栈与软件开发职责明确，归入已有.NET开发工程师。"
    if re.search(r"数字后端|physical backend|gdsii", text):
        return "SUBROLE_OF", "IC设计工程师", 0.96, "布局布线、时序与物理实现属于IC数字后端设计方向。"
    if re.search(r"射频.*(ic|芯片|集成电路|模拟|pa|lna)|氮化镓射频", text):
        return "SUBROLE_OF", "IC设计工程师", 0.88, "职责以射频芯片或模拟集成电路设计为主，归入IC设计工程师。"
    if re.search(r"射频.*(驱动|软件)|协议栈|bsp|嵌入式.*(软件|操作系统)|mcu系统软件|npu系统软件|bmc固件|bios固件", text):
        return "SUBROLE_OF", "嵌入式软件开发工程师", 0.89, "核心职责是驱动、固件、BSP或底层系统软件开发，射频和芯片是技术对象。"
    if re.search(r"射频测试|无线.*测试|通信.*测试", text):
        return "SUBROLE_OF", "通信测试工程师", 0.89, "测试对象是射频或通信协议，属于通信测试方向。"
    if re.search(r"射频|微波|天线|无线通信标准|无线通信.*协议|通信系统", text):
        return "SUBROLE_OF", "通信技术工程师", 0.84, "职责属于无线通信、射频系统或通信协议研发，归入通信技术工程师并保留方向标签。"
    if re.search(r"fae|现场应用|现场技术支持|应用工程师", text) and re.search(r"技术支持|客户|现场|芯片|gpu|mcu|处理器|应用", text + skills):
        return "ALIAS", "现场应用工程师（FAE）", 0.93, "客户导入、现场问题定位与技术支持职责符合既有FAE岗位。"
    if re.search(r"系统集成测试|系统测试|集成测试|app测试|功能测试|测试工程师|qa|sqa|验证工程师|validation|测试软件", text):
        return "SUBROLE_OF", "软件测试工程师", 0.89, "核心职责为测试设计、执行、验证和缺陷定位，被测系统作为方向标签。"
    if re.search(r"硬件.*测试|测试.*硬件|可靠性测试", text):
        return "SUBROLE_OF", "硬件测试工程师", 0.88, "测试对象和技能证据以硬件、电子或可靠性验证为主。"
    if re.search(r"机器人.*(系统集成|系统工程|软件系统|控制系统)|具身.*系统", text):
        return "SUBROLE_OF", "系统集成工程师", 0.85, "机器人软硬件、控制和模块联调职责属于系统集成方向。"
    if re.search(r"机器人.*测试|无人机.*测试", text):
        return "SUBROLE_OF", "软件测试工程师", 0.84, "核心职责为机器人或无人机系统验证与测试，对象作为方向标签。"
    if re.search(r"智能驾驶|自动驾驶|智驾|adas|行车系统|泊车系统", text):
        if re.search(r"测试", text):
            return "SUBROLE_OF", "软件测试工程师", 0.88, "职责以智能驾驶系统测试为主，归入软件测试的智驾方向。"
        return "SUBROLE_OF", "智能驾驶工程师", 0.90, "系统方案、功能开发与集成职责属于既有智能驾驶工程师。"
    if re.search(r"系统集成|集成交付", text):
        return "SUBROLE_OF", "系统集成工程师", 0.87, "职责以多系统接口、部署和联调集成为主。"
    if re.search(r"it项目经理|信息化项目|医疗信息化项目|erp.*项目总监|软件项目|项目总监|研发项目", text):
        return "SUBROLE_OF", "IT项目经理", 0.85, "岗位主体是IT或研发项目的计划、交付与协同，领域和总监属于标签与层级。"
    if re.search(r"系统管理员|it系统工程师|运营系统工程师|运维", text):
        return "SUBROLE_OF", "运维工程师", 0.88, "系统日常管理、运行保障和问题处理属于运维岗位。"
    if re.search(r"系统软件|linux系统|软件系统工程师|服务器系统优化", text):
        return "SUBROLE_OF", "软件工程师", 0.84, "职责以系统级软件开发、性能优化和平台软件为主，归入软件工程师。"
    if re.search(r"python", text) and re.search(r"开发|后端|爬虫|运维", text):
        if re.search(r"爬虫|采集", text):
            return "SUBROLE_OF", "数据采集工程师", 0.91, "职责以数据抓取和采集链路开发为主。"
        return "SUBROLE_OF", "Python开发工程师", 0.90, "Python技术栈和软件开发职责明确。"
    if re.search(r"java", text) and re.search(r"开发|后端|软件", text):
        return "SUBROLE_OF", "Java开发工程师", 0.91, "Java技术栈和软件开发职责明确。"
    if re.search(r"c\+\+|cpp", text) and re.search(r"开发|软件", text):
        return "SUBROLE_OF", "C++开发工程师", 0.89, "C++技术栈和软件开发职责明确。"
    if re.search(r"产品助理|产品专员|产品 系统", text):
        return "SUBROLE_OF", "产品经理", 0.85, "需求与产品协作是岗位主体，助理或专员属于层级。"
    if re.search(r"硬件安全", text):
        return "SUBROLE_OF", "网络安全工程师", 0.82, "职责以硬件安全分析和安全验证为主，归入安全岗位并保留硬件方向。"
    if re.search(r"硬件|电气系统|电控|电子测试", text):
        return "SUBROLE_OF", "硬件工程师", 0.82, "职责与技能以电子硬件、电气系统或电控研发为主。"
    return "INSUFFICIENT_INFO", "", 0.65, "岗位名称过于宽泛或簇内仍存在多种职责，现有证据不足以确定唯一岗位。"


def main() -> int:
    registry = json.loads((OUTPUT / "historical_approved_registry.json").read_text(encoding="utf-8"))
    role_by_name = {str(item["canonical_name"]): str(item["role_id"]) for item in registry["roles"]}
    for row in read_csv(OUTPUT / "new_roles_ai_approved.csv"):
        role_by_name[row["canonical_name"]] = row["role_id"]
    evidence = [json.loads(line) for line in (SECOND / "clustering" / "second_round_cluster_evidence.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    decisions: list[dict[str, Any]] = []
    record_assignment: dict[str, dict[str, str]] = {}
    contract_rows: list[AIDecision] = []
    for item in evidence:
        dtype, target, confidence, reason = decide(
            str(item["representative_name"]), str(item.get("top_names") or ""),
            str(item.get("top_skills") or ""), role_by_name,
        )
        candidate_id = "candidate:" + str(item["cluster_id"]).split(":", 1)[-1]
        target_id = role_by_name.get(target, "")
        payload = {
            "candidate_id": candidate_id, "decision": dtype,
            "target_role_id": target_id, "canonical_name": target or str(item["representative_name"]),
            "parent_role_id": target_id if dtype == "SUBROLE_OF" else "", "tags": [],
            "confidence": confidence, "reason": reason, "model_version": MODEL_VERSION,
        }
        contract_rows.append(AIDecision.from_dict(payload))
        decisions.append({
            "cluster_id": item["cluster_id"], "representative_name": item["representative_name"],
            "record_count": item["record_count"], "company_count": item["company_count"],
            "cohesion": item["cohesion"], "decision": dtype, "role_id": target_id,
            "canonical_name": target, "confidence": f"{confidence:.4f}", "reason": reason,
            "model_version": MODEL_VERSION,
        })
        if dtype not in {"INSUFFICIENT_INFO", "NON_IT"}:
            for version_id in item["record_ids"]:
                record_assignment[str(version_id)] = {
                    "role_id": target_id, "canonical_name": target, "decision": dtype,
                }

    proposed = read_csv(OUTPUT / "proposed_role_assignments.csv")
    applied = 0
    for row in proposed:
        if row["assignment_status"] != "PENDING":
            continue
        selected = record_assignment.get(row["version_id"])
        if not selected:
            continue
        applied += 1
        row.update({
            "assignment_status": "MAPPED", "role_id": selected["role_id"],
            "canonical_name": selected["canonical_name"], "decision": selected["decision"],
            "confidence": "", "provenance": MODEL_VERSION + ":SECOND_ROUND_CLUSTER",
        })
    write_csv(OUTPUT / "proposed_role_assignments.csv", proposed, list(proposed[0].keys()))
    write_csv(SECOND / "second_round_ai_decisions.csv", decisions, [
        "cluster_id", "representative_name", "record_count", "company_count", "cohesion",
        "decision", "role_id", "canonical_name", "confidence", "reason", "model_version",
    ])
    with (SECOND / "second_round_ai_contract.jsonl").open("w", encoding="utf-8") as stream:
        for item in contract_rows:
            stream.write(json.dumps(item.__dict__, ensure_ascii=False) + "\n")
    result = {
        "clusters": len(decisions), "decisions": dict(Counter(row["decision"] for row in decisions)),
        "resolved_records": applied,
        "remaining_pending": sum(row["assignment_status"] == "PENDING" for row in proposed),
        "new_roles_created": 0, "graph_written": False,
    }
    (SECOND / "second_round_ai_manifest.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
