from __future__ import annotations

from pathlib import Path

from new_role_discovery.neo4j_source import Neo4jEvolutionSource
from raw_jd_layer.archive import RawArchiveWriter


class _DescriptionClient:
    def __init__(self, archive_uri: str) -> None:
        self.archive_uri = archive_uri

    def query(self, _query: str, parameters: dict, **_kwargs):
        return [
            {
                "version_id": jd_id,
                "description": "" if jd_id == "jd-archived" else "负责在线职责",
                "raw_archive_uri": self.archive_uri if jd_id == "jd-archived" else "",
            }
            for jd_id in parameters["jd_ids"]
        ]


def test_load_descriptions_restores_archived_jd_body(
    tmp_path: Path,
    monkeypatch,
) -> None:
    archive_root = tmp_path / "raw-jd"
    writer = RawArchiveWriter(archive_root, "run-1", "source-1")
    writer.append(
        {
            "version_id": "jd-archived",
            "raw": {"description": "负责智能体应用设计与交付。"},
        }
    )
    monkeypatch.setenv("RAW_JD_ARCHIVE_ROOT", str(archive_root))
    source = Neo4jEvolutionSource(
        tmp_path / "neo4j.json",
        client=_DescriptionClient(writer.uri),
    )

    result = source.load_descriptions(["jd-archived", "jd-inline"])

    assert result == {
        "jd-archived": "负责智能体应用设计与交付。",
        "jd-inline": "负责在线职责",
    }
