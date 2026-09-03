from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from raw_jd_layer.archive import RawArchiveWriter, archive_root  # noqa: E402
from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402


FETCH_QUERY = """
MATCH (raw:RawJDVersion)
USING INDEX raw:RawJDVersion(version_id)
WHERE raw.version_id > $cursor AND raw.description IS NOT NULL
RETURN raw.version_id AS version_id,raw.description AS description
ORDER BY raw.version_id
LIMIT $batch_size
"""

WRITE_QUERY = """
UNWIND $rows AS row
MATCH (raw:RawJDVersion {version_id:row.version_id})
SET raw.raw_archive_uri=row.raw_archive_uri,
    raw.raw_archive_format='jsonl+gzip;v=1',
    raw.description_sha256=row.description_sha256,
    raw.description_length=row.description_length,
    raw.raw_archived_at=$now
REMOVE raw.description
RETURN count(raw) AS migrated
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def migrate(
    repository: Neo4jGraphRepository,
    root: Path,
    report_path: Path,
    *,
    batch_size: int = 500,
    limit: int = 0,
) -> dict[str, Any]:
    previous = {}
    if report_path.exists():
        try:
            previous = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}
    cursor = str(previous.get("cursor") or "")
    migrated = int(previous.get("migrated") or 0)
    archived_bytes = int(previous.get("archived_description_bytes") or 0)
    migration_id = str(previous.get("migration_id") or "") or datetime.now().strftime("archive_%Y%m%d_%H%M%S")
    started_at = str(previous.get("started_at") or "") or utc_now()
    batch_number = int(previous.get("batches") or 0)
    while not limit or migrated < limit:
        page_size = min(batch_size, limit - migrated) if limit else batch_size
        rows = repository.client.query(FETCH_QUERY, {"cursor": cursor, "batch_size": page_size})
        if not rows:
            break
        batch_number += 1
        writer = RawArchiveWriter(root, migration_id, f"batch-{batch_number:08d}")
        payloads = []
        updates = []
        for row in rows:
            version_id = str(row.get("version_id") or "")
            description = str(row.get("description") or "")
            payloads.append({"version_id": version_id, "raw": {"description": description}})
            updates.append(
                {
                    "version_id": version_id,
                    "raw_archive_uri": writer.uri,
                    "description_sha256": hashlib.sha256(description.encode("utf-8")).hexdigest(),
                    "description_length": len(description),
                }
            )
            archived_bytes += len(description.encode("utf-8"))
            cursor = version_id
        # Archive durability precedes graph pointer updates. An interrupted
        # graph write can safely retry and only leaves an unreferenced record.
        writer.append_many(payloads)
        result = repository.client.query(
            WRITE_QUERY,
            {"rows": updates, "now": utc_now()},
            access_mode="Write",
        )
        migrated += int(result[0].get("migrated") or 0) if result else 0
        state = {
            "status": "RUNNING",
            "migration_id": migration_id,
            "started_at": started_at,
            "cursor": cursor,
            "migrated": migrated,
            "batches": batch_number,
            "archived_description_bytes": archived_bytes,
            "archive_root": str(root),
        }
        write_report(report_path, state)
        print(f"archived_descriptions={migrated} cursor={cursor}", flush=True)
    remaining_rows = repository.client.query(
        "MATCH (raw:RawJDVersion) WHERE raw.description IS NOT NULL RETURN count(raw) AS remaining"
    )
    result = {
        "status": "COMPLETED" if not int(remaining_rows[0].get("remaining") or 0) else "PARTIAL",
        "migration_id": migration_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "cursor": cursor,
        "migrated": migrated,
        "batches": batch_number,
        "archived_description_bytes": archived_bytes,
        "remaining": int(remaining_rows[0].get("remaining") or 0),
        "archive_root": str(root),
    }
    write_report(report_path, result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="把Neo4j中的存量JD原文分批迁移到压缩磁盘归档")
    parser.add_argument("--neo4j-config", type=Path, default=PROJECT_ROOT / "config" / "neo4j_connection.json")
    parser.add_argument("--archive-root", type=Path, default=archive_root())
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "output" / "raw_jd_archive" / "migration.json")
    args = parser.parse_args()
    result = migrate(
        Neo4jGraphRepository(args.neo4j_config.resolve()),
        args.archive_root.expanduser().resolve(),
        args.report.expanduser().resolve(),
        batch_size=max(1, min(args.batch_size, 2000)),
        limit=max(0, args.limit),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
