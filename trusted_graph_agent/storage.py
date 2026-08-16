from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .models import GraphBundle


SCHEMA = """
PRAGMA foreign_keys = OFF;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS role_families;
DROP TABLE IF EXISTS role_aliases;
DROP TABLE IF EXISTS role_relations;
DROP TABLE IF EXISTS roles;
DROP TABLE IF EXISTS role_profiles;
DROP TABLE IF EXISTS skills;
DROP TABLE IF EXISTS industries;
DROP TABLE IF EXISTS levels;
DROP TABLE IF EXISTS time_windows;
DROP TABLE IF EXISTS companies;
DROP TABLE IF EXISTS jds;
DROP TABLE IF EXISTS role_skill_edges;
DROP TABLE IF EXISTS role_skill_snapshots;
DROP TABLE IF EXISTS jd_skill_edges;
DROP TABLE IF EXISTS related_skill_edges;
DROP TABLE IF EXISTS evolution_edges;
DROP TABLE IF EXISTS review_tasks;

CREATE TABLE runs (run_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload_json TEXT NOT NULL);
CREATE TABLE role_families (
    family_id TEXT PRIMARY KEY, family_name TEXT NOT NULL, domain_id TEXT NOT NULL, domain_name TEXT NOT NULL
);
CREATE TABLE role_aliases (
    alias_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, role_name TEXT NOT NULL, alias TEXT NOT NULL
);
CREATE TABLE role_relations (
    relation_id TEXT PRIMARY KEY, parent_role_id TEXT NOT NULL, child_role_id TEXT NOT NULL, relation TEXT NOT NULL
);
CREATE TABLE roles (
    role_id TEXT PRIMARY KEY, role_name TEXT NOT NULL, parent_role_id TEXT, parent_role_name TEXT,
    family_id TEXT, family_name TEXT, domain_id TEXT, domain_name TEXT,
    document_count INTEGER, company_count INTEGER, industries TEXT
);
CREATE TABLE role_profiles (
    profile_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, role_name TEXT NOT NULL,
    industry_id TEXT, industry_name TEXT, level_id TEXT, level_name TEXT,
    window_id TEXT, time_window TEXT, window_start TEXT, jd_count INTEGER, company_count INTEGER,
    previous_profile_id TEXT
);
CREATE TABLE skills (
    skill_id TEXT PRIMARY KEY, canonical_name TEXT NOT NULL, aliases TEXT,
    competency_category TEXT, tech_stack TEXT, registry_version TEXT
);
CREATE TABLE industries (industry_id TEXT PRIMARY KEY, industry_name TEXT NOT NULL);
CREATE TABLE levels (level_id TEXT PRIMARY KEY, level_name TEXT NOT NULL);
CREATE TABLE time_windows (window_id TEXT PRIMARY KEY, time_window TEXT NOT NULL, window_start TEXT);
CREATE TABLE companies (
    company_id TEXT PRIMARY KEY, source_company_id TEXT, company_name TEXT NOT NULL
);
CREATE TABLE jds (
    jd_id TEXT PRIMARY KEY, raw_job_id TEXT, title TEXT, canonical_role TEXT, role_id TEXT,
    profile_id TEXT, company_id TEXT, company_name TEXT, industry_id TEXT, industry_name TEXT,
    industry_detail TEXT, level_id TEXT, level_name TEXT, education TEXT, experience TEXT,
    salary TEXT, location TEXT, posted_at TEXT, source_file TEXT, description TEXT, tags TEXT,
    ability_analysis TEXT, duplicate_of TEXT, duplicate_reason TEXT, template_cluster_id TEXT,
    template_weight REAL, time_weight REAL, base_weight REAL
);
CREATE TABLE role_skill_edges (
    edge_id TEXT PRIMARY KEY, profile_id TEXT, role_id TEXT, skill_id TEXT,
    relation TEXT, tier TEXT, jd_support REAL, company_support REAL, adjusted_support REAL,
    company_count INTEGER, effective_company_count REAL, evidence_count INTEGER,
    preferred_mentions INTEGER
);
CREATE TABLE role_skill_snapshots (
    snapshot_id TEXT PRIMARY KEY, role_id TEXT NOT NULL, skill_id TEXT NOT NULL,
    time_window TEXT NOT NULL, window_start TEXT NOT NULL, relation TEXT, tier TEXT,
    adjusted_support REAL, jd_support REAL, company_support REAL, evidence_count INTEGER,
    jd_count INTEGER, company_count INTEGER, previous_support REAL, delta REAL, trend TEXT
);
CREATE TABLE jd_skill_edges (
    edge_id TEXT PRIMARY KEY, jd_id TEXT, profile_id TEXT, skill_id TEXT, skill_name TEXT,
    raw_term TEXT, requirement_type TEXT, evidence_quote TEXT, evidence_status TEXT,
    confidence REAL, source TEXT, competency_category TEXT, tech_stack TEXT
);
CREATE TABLE related_skill_edges (
    edge_id TEXT PRIMARY KEY, source_skill_id TEXT, target_skill_id TEXT,
    relation TEXT, cooccurrence INTEGER, jaccard_score REAL
);
CREATE TABLE evolution_edges (
    evolution_id TEXT PRIMARY KEY, previous_profile_id TEXT, current_profile_id TEXT,
    skill_id TEXT, change_type TEXT, previous_support REAL, current_support REAL, delta REAL
);
CREATE TABLE review_tasks (
    task_id TEXT PRIMARY KEY, jd_id TEXT, skill_id TEXT, skill_name TEXT, reason TEXT,
    evidence_status TEXT, confidence REAL, evidence_quote TEXT, status TEXT, decision TEXT
);

CREATE INDEX idx_profiles_filters ON role_profiles(industry_id, level_id, window_start);
CREATE INDEX idx_roles_family ON roles(family_id, parent_role_id);
CREATE INDEX idx_role_aliases_alias ON role_aliases(alias);
CREATE INDEX idx_role_relations_parent ON role_relations(parent_role_id, child_role_id);
CREATE INDEX idx_role_edges_profile ON role_skill_edges(profile_id, adjusted_support);
CREATE INDEX idx_role_edges_skill ON role_skill_edges(skill_id);
CREATE INDEX idx_role_snapshots_filter ON role_skill_snapshots(role_id, time_window, adjusted_support);
CREATE INDEX idx_role_snapshots_skill ON role_skill_snapshots(skill_id, role_id, time_window);
CREATE INDEX idx_jd_edges_skill ON jd_skill_edges(skill_id, evidence_status);
CREATE INDEX idx_jds_role ON jds(role_id, profile_id);
CREATE INDEX idx_reviews_status ON review_tasks(status);
"""


TABLE_COLUMNS = {
    "role_families": ["family_id", "family_name", "domain_id", "domain_name"],
    "role_aliases": ["alias_id", "role_id", "role_name", "alias"],
    "role_relations": ["relation_id", "parent_role_id", "child_role_id", "relation"],
    "roles": [
        "role_id", "role_name", "parent_role_id", "parent_role_name",
        "family_id", "family_name", "domain_id", "domain_name",
        "document_count", "company_count", "industries",
    ],
    "role_profiles": [
        "profile_id", "role_id", "role_name", "industry_id", "industry_name", "level_id", "level_name",
        "window_id", "time_window", "window_start", "jd_count", "company_count", "previous_profile_id",
    ],
    "skills": ["skill_id", "canonical_name", "aliases", "competency_category", "tech_stack", "registry_version"],
    "industries": ["industry_id", "industry_name"],
    "levels": ["level_id", "level_name"],
    "time_windows": ["window_id", "time_window", "window_start"],
    "companies": ["company_id", "source_company_id", "company_name"],
    "jds": [
        "jd_id", "raw_job_id", "title", "canonical_role", "role_id", "profile_id", "company_id",
        "company_name", "industry_id", "industry_name", "industry_detail", "level_id", "level_name",
        "education", "experience", "salary", "location", "posted_at", "source_file", "description", "tags",
        "ability_analysis", "duplicate_of", "duplicate_reason", "template_cluster_id", "template_weight",
        "time_weight", "base_weight",
    ],
    "role_skill_edges": [
        "edge_id", "profile_id", "role_id", "skill_id", "relation", "tier", "jd_support",
        "company_support", "adjusted_support", "company_count", "effective_company_count", "evidence_count",
        "preferred_mentions",
    ],
    "role_skill_snapshots": [
        "snapshot_id", "role_id", "skill_id", "time_window", "window_start", "relation", "tier",
        "adjusted_support", "jd_support", "company_support", "evidence_count", "jd_count",
        "company_count", "previous_support", "delta", "trend",
    ],
    "jd_skill_edges": [
        "edge_id", "jd_id", "profile_id", "skill_id", "skill_name", "raw_term", "requirement_type",
        "evidence_quote", "evidence_status", "confidence", "source", "competency_category", "tech_stack",
    ],
    "related_skill_edges": [
        "edge_id", "source_skill_id", "target_skill_id", "relation", "cooccurrence", "jaccard_score",
    ],
    "evolution_edges": [
        "evolution_id", "previous_profile_id", "current_profile_id", "skill_id", "change_type",
        "previous_support", "current_support", "delta",
    ],
    "review_tasks": [
        "task_id", "jd_id", "skill_id", "skill_name", "reason", "evidence_status", "confidence",
        "evidence_quote", "status", "decision",
    ],
}


def _insert(connection: sqlite3.Connection, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    columns = TABLE_COLUMNS[table]
    placeholders = ",".join("?" for _ in columns)
    statement = f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    connection.executemany(statement, [[row.get(column, "") for column in columns] for row in rows])


def write_database(bundle: GraphBundle, database_path: Path) -> Path:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO runs(run_id, state, payload_json) VALUES (?, ?, ?)",
            (bundle.run["run_id"], bundle.run["state"], json.dumps(bundle.run, ensure_ascii=False)),
        )
        for table in TABLE_COLUMNS:
            _insert(connection, table, getattr(bundle, table))
        connection.commit()
    finally:
        connection.close()
    return database_path


def update_run_record(database_path: Path, run: dict) -> None:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "UPDATE runs SET state = ?, payload_json = ? WHERE run_id = ?",
            (run["state"], json.dumps(run, ensure_ascii=False), run["run_id"]),
        )
        connection.commit()
    finally:
        connection.close()
