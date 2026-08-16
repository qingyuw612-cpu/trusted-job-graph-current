"""Single-process, dependency-free review server for the three product views.

The default dataset is synthetic and contains no original job descriptions.  The
module intentionally uses only the Python standard library so the review image
stays small and can also be tested without network access.
"""

from __future__ import annotations

import argparse
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "demo_data" / "review_demo.json"
PANORAMA_PAGE = ROOT / "trusted_graph_agent" / "static" / "panorama.html"
NEW_ROLES_PAGE = ROOT / "new_role_discovery" / "static" / "index.html"
ABILITY_CHANGES_PAGE = ROOT / "new_role_discovery" / "static" / "ability_changes.html"


class DemoRepository:
    """Read-only adapter over the sanitized, reproducible review fixture."""

    def __init__(self, data_path: Path = DATA_PATH):
        self.data_path = data_path
        self.data = json.loads(data_path.read_text(encoding="utf-8"))

    def health(self) -> dict:
        return {
            "status": "ok",
            "backend": "sanitized_demo",
            "version": self.data["version"],
            "counts": self.data["counts"],
        }

    def facets(self) -> dict:
        return self.data["facets"]

    def roles(self) -> list[dict]:
        return self.data["roles"]

    def panorama(self, query: dict[str, list[str]]) -> dict:
        role_id = _first(query, "role_id") or self.data["roles"][0]["role_id"]
        role = next((item for item in self.data["roles"] if item["role_id"] == role_id), None)
        if role is None:
            raise KeyError("岗位不存在")
        skills = list(self.data["skills_by_role"].get(role_id, []))
        stack = _first(query, "stack")
        window = _first(query, "time_window")
        if stack:
            skills = [item for item in skills if item["stack"] == stack]
        limit = max(1, min(_integer(_first(query, "skill_limit"), 30), 50))
        skills = skills[:limit]
        categories = list(dict.fromkeys(item["category"] for item in skills))
        nodes = [{"id": f"role:{role_id}", "entity_id": role_id, "type": "role", "label": role["role_name"]}]
        nodes.extend(
            {"id": f"category:{name}", "entity_id": name, "type": "category", "label": name}
            for name in categories
        )
        nodes.extend(
            {
                "id": f"skill:{item['skill_id']}", "entity_id": item["skill_id"],
                "type": "skill", "label": item["name"], "category": item["category"],
                "stack": item["stack"], "role_id": role_id, "time_window": window,
            }
            for item in skills
        )
        edges = [
            {"source": f"role:{role_id}", "target": f"category:{name}", "relation": "HAS_ABILITY_GROUP"}
            for name in categories
        ]
        edges.extend(
            {"source": f"category:{item['category']}", "target": f"skill:{item['skill_id']}", "relation": "REQUIRES"}
            for item in skills
        )
        return {
            "nodes": nodes,
            "edges": edges,
            "available_windows": self.data["facets"]["time_windows"],
            "filters": {"stack": stack, "level": _first(query, "level"), "effective_time_window": window},
            "stats": {"filtered_jds": role["document_count"], "skills": len(skills), "evidence": sum(item["evidence_count"] for item in skills)},
            "quality": {"backend": "sanitized_demo", "synthetic": True},
        }

    def evidence(self, skill_id: str, query: dict[str, list[str]] | None = None) -> dict:
        del query
        rows = self.data["evidence"].get(skill_id, [])
        skill = next(
            (item for values in self.data["skills_by_role"].values() for item in values if item["skill_id"] == skill_id),
            None,
        )
        if skill is None:
            raise KeyError("能力不存在")
        return {"skill": skill, "evidence": rows}

    def runs(self) -> dict:
        return {"items": [self.data["evolution_run"]]}

    def evolution_result(self, task_id: str) -> dict:
        if task_id != self.data["evolution_run"]["task_id"]:
            raise KeyError("分析任务不存在")
        return self.data["evolution_result"]


class Neo4jReviewRepository:
    """Full read-only graph plus previously reviewed evolution artifacts."""

    result_artifacts = {
        "manifest": "run_manifest.json",
        "new_role_candidates": "new_role_candidates.json",
        "role_skill_changes": "role_skill_changes.json",
        "data_quality": "data_quality_report.json",
        "llm_usage": "llm_usage.json",
    }

    def __init__(self, config_path: Path, evolution_root: Path, http_uri: str = ""):
        from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository

        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.graph = Neo4jGraphRepository.from_connection(
            http_uri or config["http_uri"],
            config.get("database", "neo4j"),
            config.get("username", "neo4j"),
            config["password"],
            int(config.get("timeout_seconds", 120)),
        )
        self.evolution_root = evolution_root
        self.jobs_path = evolution_root / "role_evolution_jobs" / "jobs.json"
        self.runs_root = evolution_root / "role_evolution_runs"

    def health(self) -> dict:
        health = self.graph.health()
        health["evolution_runs"] = len(self._jobs())
        return health

    def facets(self) -> dict:
        return self.graph.facets()

    def roles(self) -> list[dict]:
        return self.graph.roles()

    def panorama(self, query: dict[str, list[str]]) -> dict:
        return self.graph.panorama(
            level=_first(query, "level"),
            stack=_first(query, "stack"),
            category=_first(query, "category"),
            time_window=_first(query, "time_window"),
            role_id=_first(query, "role_id"),
            min_support=_number(_first(query, "min_support"), 0.10),
            skill_limit=_integer(_first(query, "skill_limit"), 30),
        )

    def evidence(self, skill_id: str, query: dict[str, list[str]] | None = None) -> dict:
        query = query or {}
        return self.graph.skill_evidence(
            skill_id,
            role_id=_first(query, "role_id"),
            time_window=_first(query, "time_window"),
            level=_first(query, "level"),
            stack=_first(query, "stack"),
            limit=_integer(_first(query, "limit"), 30),
        )

    def runs(self) -> dict:
        jobs = sorted(self._jobs(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {"items": [self._public_job(item) for item in jobs]}

    def evolution_result(self, task_id: str) -> dict:
        job = next((item for item in self._jobs() if item.get("task_id") == task_id), None)
        if job is None or job.get("status") != "REVIEW_READY":
            raise KeyError("分析任务或结果不存在")
        run_id = str(job.get("run_id") or "")
        run_dir = self.runs_root / run_id.replace(":", "_")
        if run_dir.parent != self.runs_root or not run_dir.is_dir():
            raise KeyError("分析结果目录不存在")
        result: dict[str, object] = {"task": self._public_job(job)}
        for key, filename in self.result_artifacts.items():
            path = run_dir / filename
            result[key] = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else ([] if key in {"new_role_candidates", "role_skill_changes"} else {})
        candidates = result.get("new_role_candidates")
        if isinstance(candidates, list):
            result["candidate_result_window"] = {"returned": min(len(candidates), 50), "total": len(candidates)}
            result["new_role_candidates"] = candidates[:50]
        return result

    def _jobs(self) -> list[dict]:
        if not self.jobs_path.is_file():
            return []
        value = json.loads(self.jobs_path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []

    @staticmethod
    def _public_job(job: dict) -> dict:
        return {key: value for key, value in job.items() if key != "output_dir"}


class ReviewRequestHandler(BaseHTTPRequestHandler):
    repository: DemoRepository
    pages = {
        "/": PANORAMA_PAGE,
        "/index.html": PANORAMA_PAGE,
        "/panorama": PANORAMA_PAGE,
        "/panorama.html": PANORAMA_PAGE,
        "/new-roles": NEW_ROLES_PAGE,
        "/new-roles.html": NEW_ROLES_PAGE,
        "/ability-changes": ABILITY_CHANGES_PAGE,
        "/ability-changes.html": ABILITY_CHANGES_PAGE,
    }

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path).rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if path in self.pages:
                self._page(self.pages[path])
            elif path in {"/healthz", "/api/health", "/api/v1/evolution/health"}:
                self._json(self.repository.health())
            elif path == "/api/v1/facets":
                self._json(self.repository.facets())
            elif path == "/api/v1/roles":
                self._json(self.repository.roles())
            elif path == "/api/v1/graph/panorama":
                self._json(self.repository.panorama(query))
            elif path.startswith("/api/v1/skills/") and path.endswith("/evidence"):
                skill_id = path.removeprefix("/api/v1/skills/").removesuffix("/evidence").strip("/")
                self._json(self.repository.evidence(skill_id, query))
            elif path == "/api/v1/evolution/runs":
                self._json(self.repository.runs())
            elif path.startswith("/api/v1/evolution/runs/") and path.endswith("/result"):
                task_id = path.removeprefix("/api/v1/evolution/runs/").removesuffix("/result").strip("/")
                self._json(self.repository.evolution_result(task_id))
            else:
                self._json({"error": "not_found", "path": path}, HTTPStatus.NOT_FOUND)
        except KeyError as error:
            self._json({"error": "not_found", "message": str(error)}, HTTPStatus.NOT_FOUND)
        except (OSError, ValueError) as error:
            self._json({"error": "bad_request", "message": str(error)}, HTTPStatus.BAD_REQUEST)

    def _page(self, page: Path) -> None:
        payload = page.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._headers()
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self._headers()
        self.end_headers()
        self.wfile.write(payload)

    def _headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")

    def log_message(self, format: str, *args: object) -> None:
        # Avoid leaking request paths or client addresses in shared review logs.
        del format, args


def _first(query: dict[str, list[str]], key: str, default: str = "") -> str:
    return str(query.get(key, [default])[0])


def _integer(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _number(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def create_repository(data_path: Path = DATA_PATH):
    backend = os.getenv("DATA_BACKEND", "demo").strip().lower()
    if backend == "demo":
        return DemoRepository(data_path)
    if backend != "neo4j":
        raise ValueError(f"不支持的数据后端：{backend}")
    config_path = Path(os.getenv("NEO4J_CONFIG_PATH", "/run/secrets/neo4j_connection.json"))
    evolution_root = Path(os.getenv("EVOLUTION_DATA_ROOT", "/data/evolution"))
    return Neo4jReviewRepository(
        config_path,
        evolution_root,
        os.getenv("NEO4J_HTTP_URI", ""),
    )


def create_server(host: str = "0.0.0.0", port: int = 8080, data_path: Path = DATA_PATH) -> ThreadingHTTPServer:
    repository = create_repository(data_path)
    handler = type("ConfiguredReviewRequestHandler", (ReviewRequestHandler,), {"repository": repository})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="岗位能力图谱统一评审入口")
    parser.add_argument("--host", default=os.getenv("APP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "8080")))
    parser.add_argument("--data", type=Path, default=Path(os.getenv("DEMO_DATA_PATH", str(DATA_PATH))))
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.data)
    print(f"岗位能力图谱已启动：http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
