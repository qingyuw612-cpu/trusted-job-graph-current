from __future__ import annotations

import json
import threading
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from deployment_app import DATA_PATH, DemoRepository, create_server


@pytest.fixture()
def review_server():
    server = create_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def get_json(url: str):
    with urlopen(url, timeout=3) as response:
        return response.status, json.load(response)


@pytest.mark.parametrize("path", ["/", "/panorama", "/new-roles", "/ability-changes"])
def test_three_views_are_served(review_server, path):
    with urlopen(review_server + path, timeout=3) as response:
        body = response.read().decode("utf-8")
        assert response.status == 200
        assert "<!doctype html>" in body.lower()


def test_health_and_panorama_api(review_server):
    _, health = get_json(review_server + "/healthz")
    assert health["status"] == "ok"
    assert health["backend"] == "sanitized_demo"

    _, graph = get_json(review_server + "/api/v1/graph/panorama?role_id=agent_engineer&stack=AI%E5%B7%A5%E7%A8%8B&skill_limit=2")
    assert graph["quality"]["synthetic"] is True
    assert graph["stats"]["skills"] == 2
    assert any(node["label"] == "智能体应用工程师" for node in graph["nodes"])


def test_evolution_apis_power_both_views(review_server):
    _, runs = get_json(review_server + "/api/v1/evolution/runs")
    run = runs["items"][0]
    assert run["status"] == "REVIEW_READY"
    _, result = get_json(review_server + f"/api/v1/evolution/runs/{run['task_id']}/result")
    assert len(result["new_role_candidates"]) >= 2
    assert len(result["role_skill_changes"]) >= 2


def test_evidence_and_not_found(review_server):
    _, evidence = get_json(review_server + "/api/v1/skills/prompt_design/evidence")
    assert evidence["evidence"][0]["company_name"].startswith("示例")
    with pytest.raises(HTTPError) as error:
        urlopen(review_server + "/api/v1/skills/does-not-exist/evidence", timeout=3)
    assert error.value.code == 404


def test_repository_validates_role_and_run():
    repository = DemoRepository(DATA_PATH)
    with pytest.raises(KeyError):
        repository.panorama({"role_id": ["missing"]})
    with pytest.raises(KeyError):
        repository.evolution_result("missing")
    graph = repository.panorama({"skill_limit": ["invalid"]})
    assert graph["stats"]["skills"] > 0
