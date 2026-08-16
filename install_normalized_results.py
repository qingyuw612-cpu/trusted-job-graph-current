from __future__ import annotations

import argparse
import csv
import sqlite3
from collections import defaultdict
from pathlib import Path

from trusted_graph_agent.normalization_experiment import NormalizationConfig, corrected_category, normalize_surface
from trusted_graph_agent.text_utils import normalize_text, stable_id


def read_csv(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def main() -> None:
    parser = argparse.ArgumentParser(description="将最终归一化岗位技能写入图谱SQLite数据库")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--normalization-dir", type=Path, required=True)
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(__file__).resolve().parent / "trusted_graph_agent" / "normalization_config.json",
    )
    args = parser.parse_args()
    database = args.database.resolve()
    normalization_dir = args.normalization_dir.resolve()
    category_quotas = NormalizationConfig.load(args.config.resolve()).top_k

    top_rows = read_csv(normalization_dir / "role_top_skills.csv")
    mapping_rows = read_csv(normalization_dir / "skill_normalization_mapping.csv")
    concepts = {
        row["concept_id"]: row
        for row in read_csv(normalization_dir / "normalized_concepts.csv")
    }
    selected_concepts = {row["concept_id"] for row in top_rows}
    role_rows: dict[str, list[dict]] = defaultdict(list)
    for row in top_rows:
        role_rows[row["role"]].append(row)

    normalized_edges = []
    for role, rows in role_rows.items():
        for rank, row in enumerate(sorted(rows, key=lambda item: float(item["final_score"]), reverse=True), 1):
            normalized_edges.append(
                (
                    stable_id("role", role),
                    row["concept_id"],
                    float(row["final_score"]),
                    int(row["company_count"]),
                    int(row["jd_count"]),
                    int(row["verified_jd_count"]),
                    rank,
                )
            )

    mapping = {
        (row["source_category"], normalize_text(row["source_name"])): row["concept_id"]
        for row in mapping_rows
        if row["concept_id"] in selected_concepts
    }

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS normalized_role_skill_snapshots;
            DROP TABLE IF EXISTS normalized_evidence_map;
            DROP TABLE IF EXISTS normalized_role_skills;
            DROP TABLE IF EXISTS normalized_skills;
            CREATE TABLE normalized_skills (
                concept_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                category TEXT NOT NULL,
                concept_status TEXT NOT NULL
            );
            CREATE TABLE normalized_role_skills (
                role_id TEXT NOT NULL,
                concept_id TEXT NOT NULL,
                final_score REAL NOT NULL,
                company_count INTEGER NOT NULL,
                jd_count INTEGER NOT NULL,
                verified_jd_count INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                PRIMARY KEY (role_id, concept_id)
            );
            CREATE TABLE normalized_evidence_map (
                jd_id TEXT NOT NULL,
                original_skill_id TEXT NOT NULL,
                concept_id TEXT NOT NULL,
                PRIMARY KEY (jd_id, original_skill_id, concept_id)
            );
            CREATE TABLE normalized_role_skill_snapshots (
                role_id TEXT NOT NULL,
                concept_id TEXT NOT NULL,
                time_window TEXT NOT NULL,
                window_start TEXT NOT NULL,
                final_score REAL NOT NULL,
                company_count INTEGER NOT NULL,
                jd_count INTEGER NOT NULL,
                verified_jd_count INTEGER NOT NULL,
                rank INTEGER NOT NULL,
                trend TEXT NOT NULL,
                delta REAL NOT NULL,
                PRIMARY KEY (role_id, concept_id, time_window)
            );
            CREATE INDEX normalized_role_skill_rank ON normalized_role_skills(role_id, rank);
            CREATE INDEX normalized_evidence_concept ON normalized_evidence_map(concept_id, jd_id);
            CREATE INDEX normalized_snapshot_window ON normalized_role_skill_snapshots(role_id, time_window, rank);
            """
        )
        connection.executemany(
            "INSERT INTO normalized_skills VALUES (?, ?, ?, ?)",
            [
                (
                    concept_id,
                    concepts[concept_id]["canonical_name"],
                    concepts[concept_id]["category"],
                    concepts[concept_id]["status"],
                )
                for concept_id in sorted(selected_concepts)
            ],
        )
        connection.executemany(
            "INSERT INTO normalized_role_skills VALUES (?, ?, ?, ?, ?, ?, ?)",
            normalized_edges,
        )
        evidence_rows = []
        for row in connection.execute(
            """
            SELECT e.jd_id, e.skill_id, e.skill_name, e.raw_term, e.competency_category
            FROM jd_skill_edges e JOIN jds j ON j.jd_id = e.jd_id
            WHERE j.duplicate_of = ''
              AND e.evidence_status IN ('VERIFIED', 'LOW_CONFIDENCE', 'ANALYSIS_ONLY')
            """
        ):
            name = normalize_surface(row[2] or row[3])
            category = corrected_category(name, row[4] or "其他能力")
            concept_id = mapping.get((category, normalize_text(name)))
            if concept_id:
                evidence_rows.append((row[0], row[1], concept_id))
        connection.executemany(
            "INSERT OR IGNORE INTO normalized_evidence_map VALUES (?, ?, ?)",
            evidence_rows,
        )
        totals = {
            (row[0], row[1]): (row[2], row[3])
            for row in connection.execute(
                """
                SELECT j.role_id, p.time_window, COUNT(DISTINCT j.jd_id), COUNT(DISTINCT j.company_id)
                FROM jds j JOIN role_profiles p ON p.profile_id = j.profile_id
                WHERE j.duplicate_of = ''
                GROUP BY j.role_id, p.time_window
                """
            )
        }
        snapshot_candidates: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        score_lookup: dict[tuple[str, str, str], float] = {}
        for row in connection.execute(
            """
            SELECT j.role_id, m.concept_id, p.time_window, MIN(p.window_start),
                   COUNT(DISTINCT j.jd_id), COUNT(DISTINCT j.company_id),
                   COUNT(DISTINCT CASE WHEN e.evidence_status = 'VERIFIED' THEN j.jd_id END),
                   AVG(e.confidence)
            FROM normalized_evidence_map m
            JOIN jd_skill_edges e ON e.jd_id = m.jd_id AND e.skill_id = m.original_skill_id
            JOIN jds j ON j.jd_id = e.jd_id
            JOIN role_profiles p ON p.profile_id = j.profile_id
            WHERE j.duplicate_of = ''
            GROUP BY j.role_id, m.concept_id, p.time_window
            """
        ):
            role_id, concept_id, window, window_start, jd_count, company_count, verified_count, confidence = row
            total_jds, total_companies = totals[(role_id, window)]
            score = (
                0.55 * company_count / max(1, total_companies)
                + 0.30 * jd_count / max(1, total_jds)
                + 0.15 * float(confidence or 0)
            )
            item = {
                "role_id": role_id,
                "concept_id": concept_id,
                "time_window": window,
                "window_start": window_start,
                "final_score": round(score, 6),
                "company_count": company_count,
                "jd_count": jd_count,
                "verified_jd_count": verified_count,
            }
            category = concepts[concept_id]["category"]
            snapshot_candidates[(role_id, window, category)].append(item)
            score_lookup[(role_id, window, concept_id)] = score

        selected_by_window: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for (role_id, window, category), candidates in snapshot_candidates.items():
            quota = category_quotas.get(category, 0)
            selected_by_window[(role_id, window)].extend(
                sorted(candidates, key=lambda item: item["final_score"], reverse=True)[:quota]
            )
        role_windows: dict[str, list[str]] = defaultdict(list)
        for role_id, window in selected_by_window:
            role_windows[role_id].append(window)
        snapshot_rows = []
        for role_id, windows in role_windows.items():
            ordered_windows = sorted(set(windows))
            for window_index, window in enumerate(ordered_windows):
                previous_window = ordered_windows[window_index - 1] if window_index else ""
                selected = sorted(
                    selected_by_window[(role_id, window)],
                    key=lambda item: item["final_score"],
                    reverse=True,
                )
                for rank, item in enumerate(selected, 1):
                    previous = score_lookup.get((role_id, previous_window, item["concept_id"])) if previous_window else None
                    delta = 0.0 if previous is None else item["final_score"] - previous
                    if previous is None and previous_window:
                        trend = "emerging"
                    elif delta >= 0.05:
                        trend = "rising"
                    elif delta <= -0.05:
                        trend = "falling"
                    else:
                        trend = "stable"
                    snapshot_rows.append(
                        (
                            item["role_id"], item["concept_id"], item["time_window"], item["window_start"],
                            item["final_score"], item["company_count"], item["jd_count"],
                            item["verified_jd_count"], rank, trend, round(delta, 6),
                        )
                    )
        connection.executemany(
            "INSERT INTO normalized_role_skill_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            snapshot_rows,
        )
        connection.commit()

    print(f"标准技能：{len(selected_concepts)}")
    print(f"岗位技能关系：{len(normalized_edges)}")
    print(f"证据映射：{len(evidence_rows)}")
    print(f"季度技能快照：{len(snapshot_rows)}")


if __name__ == "__main__":
    main()
