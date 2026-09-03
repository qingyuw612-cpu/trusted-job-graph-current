from __future__ import annotations

import sqlite3
from pathlib import Path

from trusted_graph_agent.api_server import GraphRepository, _safe_51job_url
from trusted_graph_agent.neo4j_filtered_view import (
    _safe_51job_url as neo4j_safe_51job_url,
    load_role_openings,
)


class _Neo4jClient:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def query(self, statement: str, parameters: dict | None = None, **kwargs):
        del parameters, kwargs
        self.statements.append(statement)
        if "RETURN properties(role) AS role" in statement:
            return [{"role": {"role_id": "role:pm", "name": "产品经理"}}]
        return [
            {
                "source_job_id": "j-1",
                "version_id": "v-1",
                "company_id": "c-1",
                "source_platform": "51job",
                "title": "产品经理",
                "company_name": "示例企业",
                "location": "北京",
                "salary": "15-25K",
                "education": "本科",
                "experience": "3-5年",
                "posted_at": "2026-08-30",
                "collected_at": "2026-08-31",
                "description": "负责产品规划",
                "job_url": "https://jobs.51job.com/beijing/123.html#fragment",
            },
            # A second version of the same source vacancy must not occupy a
            # second card.
            {
                "source_job_id": "j-1",
                "version_id": "v-0",
                "company_name": "示例企业",
                "title": "产品经理（旧版本）",
                "job_url": "https://jobs.51job.com/beijing/123.html",
            },
            {
                "source_job_id": "j-2",
                "version_id": "v-2",
                "company_name": "不可信链接",
                "title": "产品经理",
                "job_url": "https://evil.example/jobs/2",
            },
        ]


def test_role_openings_query_is_current_completed_and_standard_role_scoped() -> None:
    client = _Neo4jClient()

    payload = load_role_openings(client, "role:pm")

    assert payload["returned"] == 1
    assert payload["openings"][0]["company_name"] == "示例企业"
    assert payload["openings"][0]["job_url"] == "https://jobs.51job.com/beijing/123.html"
    assert payload["openings"][0]["availability"] == "CHECK_ON_SOURCE"
    assert "crawl_keyword" not in payload["openings"][0]
    assert "raw_archive_uri" not in payload["openings"][0]
    query = client.statements[1]
    assert "[:CURRENT_VERSION]" in query
    assert "ProcessedJD {status:'COMPLETED'}" in query
    assert "raw.standard_role_id = $role_id" in query
    assert "raw.domain_role IN [role.name, role.role_name]" in query
    assert "raw.source_normalized_role IN [role.name, role.role_name]" in query
    assert "raw.declared_role IN [role.name, role.role_name]" in query
    assert "source_platform" in query
    assert "raw.job_link" in query


def test_51job_url_allowlist_rejects_credentials_ports_and_other_hosts() -> None:
    accepted = "https://jobs.51job.com/a/1.html?x=1#drop"
    assert _safe_51job_url(accepted) == "https://jobs.51job.com/a/1.html?x=1"
    assert neo4j_safe_51job_url(accepted) == "https://jobs.51job.com/a/1.html?x=1"
    for value in (
        "javascript:alert(1)",
        "https://user:pass@jobs.51job.com/a",
        "https://jobs.51job.com:444/a",
        "https://51job.com.evil.example/a",
    ):
        assert _safe_51job_url(value) == ""
        assert neo4j_safe_51job_url(value) == ""


def test_sqlite_fallback_returns_only_valid_linked_51job_rows(tmp_path: Path) -> None:
    database = tmp_path / "graph.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE roles (role_id TEXT PRIMARY KEY, role_name TEXT);
        CREATE TABLE jds (
            jd_id TEXT, raw_job_id TEXT, title TEXT, role_id TEXT,
            company_id TEXT, company_name TEXT, location TEXT, salary TEXT,
            education TEXT, experience TEXT, posted_at TEXT, description TEXT,
            source_file TEXT, duplicate_of TEXT, source_platform TEXT, job_link TEXT
        );
        INSERT INTO roles VALUES ('role:pm', '产品经理');
        INSERT INTO jds VALUES ('1','j-1','产品经理','role:pm','c-1','企业A','北京','15K','本科','3年','2026-08-31','详情','a.csv','','前程无忧','https://jobs.51job.com/a/1');
        INSERT INTO jds VALUES ('2','j-2','产品经理','role:pm','c-2','企业B','北京','15K','本科','3年','2026-08-30','详情','b.csv','','前程无忧','https://evil.example/b');
        """
    )
    connection.commit()
    connection.close()

    payload = GraphRepository(database).role_openings("role:pm")

    assert payload["returned"] == 1
    assert payload["openings"][0]["company_name"] == "企业A"
    assert "source_file" not in payload["items"][0]


def test_sqlite_openings_pagination_has_stable_metadata_and_no_overlap(tmp_path: Path) -> None:
    database = tmp_path / "paged.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE roles (role_id TEXT PRIMARY KEY, role_name TEXT);
        CREATE TABLE jds (
            jd_id TEXT, raw_job_id TEXT, title TEXT, role_id TEXT,
            company_id TEXT, company_name TEXT, location TEXT, salary TEXT,
            education TEXT, experience TEXT, posted_at TEXT, description TEXT,
            source_file TEXT, duplicate_of TEXT, source_platform TEXT, job_link TEXT
        );
        INSERT INTO roles VALUES ('role:pm', '产品经理');
        """
    )
    rows = [
        (
            str(index), f"j-{index}", f"产品经理-{index}", "role:pm", "c-1",
            f"企业{index}", "北京", "15K", "本科", "3年", f"2026-08-{index:02d}",
            "详情", "export.csv", "", "前程无忧", f"https://jobs.51job.com/a/{index}"
        )
        for index in range(1, 26)
    ]
    connection.executemany("INSERT INTO jds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()

    repository = GraphRepository(database)
    first = repository.role_openings("role:pm", page=1, page_size=12)
    second = repository.role_openings("role:pm", page=2, page_size=12)

    assert first["page"] == 1
    assert first["page_size"] == 12
    assert first["total"] == 25
    assert first["total_pages"] == 3
    assert first["has_previous"] is False
    assert first["has_next"] is True
    assert len(first["items"]) == 12
    assert second["page"] == 2
    assert second["has_previous"] is True
    assert second["has_next"] is True
    assert len(second["items"]) == 12
    assert {item["source_job_id"] for item in first["items"]}.isdisjoint(
        {item["source_job_id"] for item in second["items"]}
    )


def test_neo4j_openings_page_size_is_bounded_and_metadata_is_exposed() -> None:
    client = _Neo4jClient()
    payload = load_role_openings(client, "role:pm", page=2, page_size=999)

    assert payload["page"] == 2
    assert payload["page_size"] == 50
    assert payload["items"] == payload["openings"]
    assert payload["has_previous"] is True
    assert "SKIP $skip LIMIT $page_size" in client.statements[1]
