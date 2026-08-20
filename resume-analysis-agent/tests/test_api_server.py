from fastapi.testclient import TestClient

import api_server


client = TestClient(api_server.app)


def test_rank_endpoint_works_without_llm_key():
    response = client.post(
        "/rank",
        json={"resume_text": "Python FastAPI MySQL Docker", "topk": 3},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 3
    assert len(payload["results"]) == 3


def test_gap_accepts_request_scoped_llm_config(monkeypatch):
    captured = {}

    def fake_analyze(role, resume_text, llm_func=None):
        captured["llm_result"] = llm_func("return json")
        return {"role_name": role["role_name"], "report": {}}

    def fake_call(prompt, temperature=0.0, cfg=None):
        captured["cfg"] = cfg
        return {"ok": True}

    monkeypatch.setattr(api_server, "analyze_gap", fake_analyze)
    monkeypatch.setattr(api_server, "call_llm_json", fake_call)

    response = client.post(
        "/gap",
        json={
            "role": {"role_name": "后端开发工程师"},
            "resume_text": "熟悉 Python 和 FastAPI",
            "llm": {
                "provider": "deepseek",
                "api_key": "request-only-secret",
                "model": "deepseek-chat",
                "base_url": "",
            },
        },
    )

    assert response.status_code == 200
    assert captured["llm_result"] == {"ok": True}
    assert captured["cfg"]["api_key"] == "request-only-secret"
    assert captured["cfg"]["base_url"] == "https://api.deepseek.com/v1"


def test_custom_provider_requires_base_url(monkeypatch):
    response = client.post(
        "/gap",
        json={
            "role": {"role_name": "后端开发工程师"},
            "resume_text": "熟悉 Python",
            "llm": {
                "provider": "custom",
                "api_key": "request-only-secret",
                "model": "custom-model",
                "base_url": "",
            },
        },
    )

    assert response.status_code == 400
    assert "base_url" in response.json()["error"]
