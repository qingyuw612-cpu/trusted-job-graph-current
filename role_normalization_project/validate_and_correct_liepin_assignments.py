"""发布前对猎聘拟岗位映射做冲突审计和证据级校正。"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any


ROOT = Path(".")
OUTPUT = ROOT / "output" / "liepin_role_normalization"
BGE = OUTPUT / "bge_run"
MODEL = "codex-prepublish-audit-v1-liepin-2026-08-12"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def stable_role_id(name: str) -> str:
    key = re.sub(r"[\s\-_/（）()]+", "", name).casefold()
    return "role:" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def main() -> int:
    proposed = read_csv(OUTPUT / "proposed_role_assignments.csv")
    source = {row["version_id"]: row for row in read_csv(BGE / "role_resolutions.csv")}
    registry = json.loads((OUTPUT / "historical_approved_registry.json").read_text(encoding="utf-8"))
    role_by_name = {str(item["canonical_name"]): str(item["role_id"]) for item in registry["roles"]}
    new_rows = read_csv(OUTPUT / "new_roles_ai_approved.csv")
    for row in new_rows: role_by_name[row["canonical_name"]] = row["role_id"]
    if "射频工程师" not in role_by_name:
        rid = stable_role_id("射频工程师")
        role_by_name["射频工程师"] = rid
        new_rows.append({"role_id": rid, "canonical_name": "射频工程师", "status": "AI_APPROVED_NEW_LIEPIN", "model_version": MODEL})

    audit: list[dict[str, str]] = []
    correction_counts: Counter[str] = Counter()
    for row in proposed:
        if row["assignment_status"] != "MAPPED": continue
        src = source[row["version_id"]]
        title = row["title"]
        skills = json.loads(src.get("normalized_skills") or "[]")
        skill_text = " ".join(str(value) for value in skills)
        description = str(src.get("description") or "")
        industry = str(src.get("industry") or "")
        old_name = row["canonical_name"]
        target = old_name
        reason = ""

        # 多语言通用岗位不按任一语言单栈归类。
        language_count = sum(bool(re.search(pattern, title, re.I)) for pattern in (r"java", r"python", r"c#|\.net", r"c\+\+|cpp", r"php", r"golang|\bgo\b"))
        if language_count >= 3 and old_name in {"Java开发工程师", "Python开发工程师", "C++开发工程师", ".NET开发工程师"}:
            target = "软件工程师"; reason = "标题同时声明三个及以上开发语言，证据体现通用软件开发而非单一语言岗位。"

        # 射频按设计对象拆分，防止被少数芯片样本或大簇中心带偏。
        if "射频" in title or re.search(r"\bRF\b|微波|天线", title, re.I):
            if re.search(r"驱动|协议栈|固件|BSP", title, re.I) or ("Linux/Unix" in skills and "C++" in skills and "射频电路设计" not in skills):
                target = "嵌入式软件开发工程师"; reason = "标题和技能表明岗位主体是射频驱动、协议栈或底层软件开发。"
            elif re.search(r"测试|验证", title):
                target = "通信测试工程师"; reason = "岗位主体是射频或通信测试验证。"
            elif re.search(r"芯片|集成电路|\bIC\b|模拟.*设计|版图|PA|LNA", title, re.I):
                target = "IC设计工程师"; reason = "岗位明确从事射频芯片、模拟集成电路或版图设计。"
            elif re.search(r"FAE|现场应用|技术支持", title, re.I):
                target = "现场应用工程师（FAE）"; reason = "岗位主体是射频产品的客户导入与现场技术支持。"
            else:
                target = "射频工程师"; reason = "职责集中在射频电路、天线、微波链路设计与调试，跨企业证据支持独立岗位概念。"

        # 普通工艺不能因为和半导体簇近邻就进入半导体岗位。
        if old_name == "半导体工程师" and "工艺" in title:
            has_semiconductor = bool(
                re.search(r"半导体|集成电路|芯片", industry + " " + skill_text + " " + description[:600], re.I)
                or re.search(r"光刻|刻蚀|晶圆|封装|离子注入|PVD|CVD|CMP|WET|RTP|OPC|3D IC", title, re.I)
            )
            if not has_semiconductor:
                target = ""; reason = "缺少半导体行业、制程或芯片能力证据，普通生产/NPI工艺不应进入IT岗位图谱。"

        # 智驾系统与智驾测试分开，不能由同簇测试样本覆盖。
        if old_name == "软件测试工程师" and re.search(r"智能驾驶|自动驾驶|智驾|ADAS|行车系统|泊车系统", title, re.I) and "测试" not in title:
            target = "智能驾驶工程师"; reason = "标题和职责为智能驾驶系统方案、开发或集成，不是测试岗位。"
        if old_name == "软件测试工程师" and re.search(r"芯片.*(测试|验证)|SoC.*验证", title, re.I):
            target = "芯片测试工程师"; reason = "测试对象是芯片或SoC验证，归入芯片测试岗位。"

        # C# 单栈被旧BGE误召回到C++时纠正；双栈保留原AI判断。
        if old_name == "C++开发工程师" and re.search(r"C#|\.NET", title, re.I) and not re.search(r"C\+\+", title):
            target = ".NET开发工程师"; reason = "标题明确为C#/.NET单栈开发，纠正C++近邻误召回。"

        if target != old_name:
            audit.append({"version_id": row["version_id"], "title": title, "old_role": old_name, "new_role": target or "PENDING", "reason": reason})
            correction_counts[f"{old_name}->{target or 'PENDING'}"] += 1
            if target:
                row.update({"role_id": role_by_name[target], "canonical_name": target, "decision": "AI_AUDIT_CORRECTION", "provenance": MODEL})
            else:
                row.update({"assignment_status": "PENDING", "role_id": "", "canonical_name": "", "decision": "INSUFFICIENT_INFO", "confidence": "", "provenance": MODEL})

    # 结构与引用完整性。
    duplicates = [key for key, count in Counter(row["version_id"] for row in proposed).items() if count != 1]
    mapped = [row for row in proposed if row["assignment_status"] == "MAPPED"]
    pending = [row for row in proposed if row["assignment_status"] == "PENDING"]
    known_ids = set(role_by_name.values())
    errors = []
    if duplicates: errors.append(f"重复version_id：{len(duplicates)}")
    if len(proposed) != 3443: errors.append(f"记录数异常：{len(proposed)}")
    if any(not row["role_id"] or not row["canonical_name"] for row in mapped): errors.append("存在缺少岗位ID或名称的已映射记录")
    if any(row["role_id"] not in known_ids for row in mapped): errors.append("存在未知岗位ID")
    if any(row["role_id"] or row["canonical_name"] for row in pending): errors.append("待观察记录仍携带岗位")
    csharp_cpp = [row for row in mapped if re.search(r"C#", row["title"]) and not re.search(r"C\+\+", row["title"]) and row["canonical_name"] == "C++开发工程师"]
    if csharp_cpp: errors.append(f"仍有C#单栈误归C++：{len(csharp_cpp)}")
    multi_single = [row for row in mapped if sum(bool(re.search(p, row["title"], re.I)) for p in (r"java",r"python",r"c#|\.net",r"c\+\+|cpp",r"php",r"golang|\bgo\b")) >= 3 and row["canonical_name"] in {"Java开发工程师","Python开发工程师","C++开发工程师",".NET开发工程师"}]
    if multi_single: errors.append(f"仍有多语言误归单栈：{len(multi_single)}")

    write_csv(OUTPUT / "proposed_role_assignments.csv", proposed, list(proposed[0].keys()))
    write_csv(OUTPUT / "prepublish_corrections.csv", audit, ["version_id", "title", "old_role", "new_role", "reason"])
    write_csv(OUTPUT / "new_roles_ai_approved.csv", new_rows, ["role_id", "canonical_name", "status", "model_version"])
    report = {
        "records": len(proposed), "mapped": len(mapped), "pending": len(pending),
        "corrections": len(audit), "correction_counts": dict(correction_counts),
        "known_role_ids": len(known_ids), "errors": errors, "valid": not errors,
        "graph_written": False,
    }
    (OUTPUT / "prepublish_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
