"""Safely backfill stack metadata on the canonical Neo4j skill nodes.

The migration is additive and idempotent: existing non-empty values are
preserved, while blank NormalizedSkill.tech_stack values are filled from
mapped AbilityCandidates, legacy Skill nodes, or the checked-in registry.
Use --dry-run (the default) to inspect the plan; --apply performs only the
listed property updates and never deletes or rewires graph data.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.text_utils import normalize_text  # noqa: E402


IGNORED_STACKS = {"", "待审核", "未知", "未注明"}


def registry_stacks() -> dict[str, str]:
    path = PROJECT_ROOT / "trusted_graph_agent" / "skills_registry.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for item in payload.get("skills", []):
        stack = str(item.get("tech_stack") or "").strip()
        if stack and stack not in IGNORED_STACKS:
            for name in (item.get("canonical_name"), *(item.get("aliases") or [])):
                if name:
                    result[normalize_text(str(name))] = stack
    return result


def choose_stack(row: dict, legacy: dict[str, list[str]], registry: dict[str, str]) -> tuple[str, str]:
    current = str(row.get("tech_stack") or "").strip()
    if current and current not in IGNORED_STACKS:
        return current, "existing"
    candidates = [str(value).strip() for value in (row.get("ability_stacks") or [])]
    candidates.extend(legacy.get(normalize_text(str(row.get("canonical_name") or "")), []))
    candidates = [value for value in candidates if value not in IGNORED_STACKS]
    if candidates:
        # Counter + lexical tie-break makes repeated runs reproducible.
        counts = Counter(candidates)
        value = sorted(counts, key=lambda item: (-counts[item], item))[0]
        return value, "ability_or_legacy"
    value = registry.get(normalize_text(str(row.get("canonical_name") or "")), "")
    return value, "registry" if value else "unresolved"


def run(repository: Neo4jGraphRepository, *, apply: bool, report_path: Path | None = None) -> dict:
    rows = repository.client.query(
        """
        MATCH (skill:NormalizedSkill)
        OPTIONAL MATCH (ability:AbilityCandidate)-[:NORMALIZES_TO]->(skill)
        RETURN skill.concept_id AS concept_id, skill.canonical_name AS canonical_name,
               skill.tech_stack AS tech_stack,
               collect(DISTINCT ability.tech_stack) AS ability_stacks
        ORDER BY concept_id
        """
    )
    legacy_rows = repository.client.query(
        "MATCH (skill:Skill) WHERE coalesce(skill.tech_stack, '') <> '' "
        "RETURN coalesce(skill.canonical_name, skill.name) AS canonical_name, "
        "skill.tech_stack AS tech_stack"
    )
    legacy: dict[str, list[str]] = {}
    for item in legacy_rows:
        name = normalize_text(str(item.get("canonical_name") or ""))
        value = str(item.get("tech_stack") or "").strip()
        if name and value not in IGNORED_STACKS:
            legacy.setdefault(name, []).append(value)

    registry = registry_stacks()
    updates: list[dict[str, str]] = []
    source_counts: Counter[str] = Counter()
    stack_counts: Counter[str] = Counter()
    unresolved = 0
    for row in rows:
        value, source = choose_stack(row, legacy, registry)
        source_counts[source] += 1
        if source == "unresolved":
            unresolved += 1
        if source != "existing" and value:
            updates.append({"concept_id": str(row["concept_id"]), "tech_stack": value, "source": source})
        if value:
            stack_counts[value] += 1

    if apply and updates:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        repository.client.query(
            """
            UNWIND $rows AS row
            MATCH (skill:NormalizedSkill {concept_id:row.concept_id})
            WHERE coalesce(skill.tech_stack, '') = '' OR skill.tech_stack IN $ignored
            SET skill.tech_stack=row.tech_stack,
                skill.tech_stack_backfill_source=row.source,
                skill.tech_stack_backfilled_at=$now
            """,
            {"rows": updates, "ignored": list(IGNORED_STACKS), "now": now},
            access_mode="Write",
        )

    result = {
        "status": "APPLIED" if apply else "DRY_RUN",
        "normalized_skills": len(rows),
        "planned_updates": len(updates),
        "source_counts": dict(source_counts),
        "stack_counts": dict(sorted(stack_counts.items())),
        "unresolved": unresolved,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--neo4j-config", type=Path, default=PROJECT_ROOT / "config" / "neo4j_connection.json")
    parser.add_argument("--apply", action="store_true", help="perform additive property updates")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run(Neo4jGraphRepository(args.neo4j_config.resolve()), apply=args.apply, report_path=args.report)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
