from fastapi.testclient import TestClient

from codex_monitor import server
from codex_monitor.demo import DEMO_AGENT_TOKEN, DEMO_PASSWORD, DEMO_USERNAME


def test_demo_is_synthetic_and_auto_authenticated(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_MONITOR_DEMO", "1")
    monkeypatch.setenv("CODEX_MONITOR_ENV", "development")
    monkeypatch.setenv("CODEX_MONITOR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CODEX_MONITOR_ADMIN_USERNAME", DEMO_USERNAME)
    monkeypatch.setenv("CODEX_MONITOR_ADMIN_DISPLAY_NAME", "Demo Owner")
    monkeypatch.setenv("CODEX_MONITOR_ADMIN_PASSWORD", DEMO_PASSWORD)
    monkeypatch.setenv("CODEX_MONITOR_AGENT_TOKEN", DEMO_AGENT_TOKEN)

    with TestClient(server.app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["demo"] is True
        snapshot = client.get("/api/snapshot")
        assert snapshot.status_code == 200
        payload = snapshot.json()
        assert payload["user"]["username"] == DEMO_USERNAME
        assert payload["stats"]["online_nodes"] == 1
        assert payload["tasks"]
        assert payload["approvals"]
        assert payload["codex_threads"]

