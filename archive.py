from __future__ import annotations

import gzip
import json
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHIVE_ROOT = PROJECT_ROOT / "output" / "raw_jd_archive"


def archive_root() -> Path:
    configured = os.environ.get("RAW_JD_ARCHIVE_ROOT", "").strip()
    return Path(configured).expanduser().resolve() if configured else DEFAULT_ARCHIVE_ROOT.resolve()


def safe_component(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z._-]+", "_", value).strip("._")
    return text[:80] or "source"


class RawArchiveWriter:
    """Append-only gzip JSONL archive for one source in one ingestion run."""

    def __init__(self, root: Path, run_id: str, source_file_id: str):
        self.root = root.expanduser().resolve()
        self.relative_path = Path(safe_component(run_id)) / f"{safe_component(source_file_id)}.jsonl.gz"
        self.path = (self.root / self.relative_path).resolve()
        if self.root not in self.path.parents:
            raise ValueError("原文归档路径超出配置根目录")
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def uri(self) -> str:
        return self.relative_path.as_posix()

    def append(self, payload: dict[str, Any]) -> None:
        self.append_many([payload])

    def append_many(self, payloads: Iterable[dict[str, Any]]) -> None:
        with gzip.open(self.path, "at", encoding="utf-8", newline="\n") as stream:
            for payload in payloads:
                stream.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")


def resolve_archive_uri(uri: str, root: Path | None = None) -> Path:
    base = (root or archive_root()).expanduser().resolve()
    path = (base / uri).resolve()
    if base != path and base not in path.parents:
        raise ValueError("原文归档 URI 超出配置根目录")
    return path


def load_archived_versions(uri: str, version_ids: set[str], root: Path | None = None) -> dict[str, dict[str, Any]]:
    if not uri or not version_ids:
        return {}
    path = resolve_archive_uri(uri, root)
    if not path.is_file():
        raise FileNotFoundError(f"原文归档不存在：{path}")
    found: dict[str, dict[str, Any]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if len(found) >= len(version_ids):
                break
            payload = json.loads(line)
            version_id = str(payload.get("version_id") or "")
            if version_id in version_ids:
                found[version_id] = dict(payload.get("raw") or {})
    return found


def hydrate_raw_rows(rows: Iterable[dict[str, Any]], root: Path | None = None) -> list[dict[str, Any]]:
    """Restore archived large fields while keeping Neo4j properties authoritative."""

    materialized = [dict(row) for row in rows]
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in materialized:
        if row.get("description"):
            continue
        uri = str(row.get("raw_archive_uri") or "")
        version_id = str(row.get("version_id") or "")
        if uri and version_id:
            grouped[uri].add(version_id)
    loaded: dict[tuple[str, str], dict[str, Any]] = {}
    for uri, version_ids in grouped.items():
        for version_id, raw in load_archived_versions(uri, version_ids, root).items():
            loaded[(uri, version_id)] = raw
    for index, row in enumerate(materialized):
        key = (str(row.get("raw_archive_uri") or ""), str(row.get("version_id") or ""))
        archived = loaded.get(key)
        if archived:
            merged = dict(row)
            for field, value in archived.items():
                # Large fields are removed from Neo4j after archival. Cypher
                # readers commonly return them as an empty string via
                # coalesce(), which must not overwrite the archived value.
                if not merged.get(field):
                    merged[field] = value
            materialized[index] = merged
    return materialized
