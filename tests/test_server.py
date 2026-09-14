import os
import time
from concurrent.futures import ThreadPoolExecutor

os.environ["CODEX_MONITOR_ENV"] = "development"
os.environ["CODEX_MONITOR_ADMIN_USERNAME"] = "rootadmin"
os.environ["CODEX_MONITOR_ADMIN_DISPLAY_NAME"] = "Root Admin"
os.environ["CODEX_MONITOR_ADMIN_PASSWORD"] = "correct-horse-battery-staple"
os.environ["CODEX_MONITOR_AGENT_TOKEN"] = "agent-test-token-that-is-long-and-random"

from fastapi.testclient import TestClient

from codex_monitor.server import app, relay_target_allowed


def test_relay_allowlist_is_strict():
    assert relay_target_allowed("chatgpt.com", 443)
    assert relay_target_allowed("api.openai.com", 443)
    assert relay_target_allowed("draw.openai-next.com", 443)
    assert not relay_target_allowed("openai.com.example.org", 443)
    assert not relay_target_allowed("localhost", 443)
    assert not relay_target_allowed("api.openai.com", 22)


def login(client: TestClient, username: str, password: str) -> str:
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_auth_roles_task_and_remote_approval(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_MONITOR_DATA_DIR", str(tmp_path))
    with TestClient(app) as admin:
        assert admin.get("/api/snapshot").status_code == 401
        csrf = login(admin, "rootadmin", "correct-horse-battery-staple")
        me = admin.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["role"] == "admin"

        created = admin.post(
            "/api/users",
            headers={"X-CSRF-Token": csrf},
            json={"username": "viewer.one", "display_name": "Viewer", "password": "viewer-password-123", "role": "viewer"},
        )
        assert created.status_code == 200, created.text

        with TestClient(app) as viewer:
            viewer_csrf = login(viewer, "viewer.one", "viewer-password-123")
            denied = viewer.post(
                "/api/tasks",
                headers={"X-CSRF-Token": viewer_csrf},
                json={"title": "Denied", "prompt": "No", "node_id": "lab-pc"},
            )
            assert denied.status_code == 403

        with admin.websocket_connect(
            "/ws/agent", headers={"Authorization": "Bearer agent-test-token-that-is-long-and-random"}
        ) as websocket:
            websocket.send_json({"type": "hello", "node": {"id": "lab-pc", "name": "Lab PC", "platform": "Windows", "codex_version": "test", "workspaces": ["C:/work"]}})
            assert websocket.receive_json()["type"] == "hello.ack"
            websocket.send_json({
                "type": "codex.threads",
                "threads": [{
                    "id": "thread-1",
                    "sessionId": "session-1",
                    "name": "Current Codex conversation",
                    "preview": "Build the Codex Monitor control center",
                    "cwd": "C:/work",
                    "source": "vscode",
                    "status": {"type": "notLoaded"},
                    "historyMode": "paginated",
                    "forkedFromId": None,
                    "createdAt": int(time.time()) - 60,
                    "updatedAt": int(time.time()),
                }],
            })
            assert websocket.receive_json()["type"] == "codex.threads.ack"

            synced_snapshot = admin.get("/api/snapshot").json()
            assert synced_snapshot["stats"]["recent_codex_threads"] == 1
            assert synced_snapshot["codex_threads"][0]["thread_id"] == "thread-1"
            assert synced_snapshot["codex_threads"][0]["activity_state"] == "recent"
            assert synced_snapshot["codex_threads"][0]["history_mode"] == "paginated"

            with ThreadPoolExecutor(max_workers=1) as pool:
                detail_future = pool.submit(admin.get, "/api/codex-threads/lab-pc/thread-1")
                detail_request = websocket.receive_json()
                assert detail_request["type"] == "thread.detail.request"
                assert detail_request["thread_id"] == "thread-1"
                websocket.send_json({
                    "type": "thread.detail",
                    "request_id": detail_request["request_id"],
                    "thread_id": "thread-1",
                    "detail": {"turns": [{"id": "turn-0", "status": "completed", "messages": [{"role": "assistant", "text": "Ready"}]}], "has_more": False},
                })
                detail = detail_future.result(timeout=5)
            assert detail.status_code == 200, detail.text
            assert detail.json()["detail"]["turns"][0]["messages"][0]["text"] == "Ready"

            created_task = admin.post(
                "/api/tasks",
                headers={"X-CSRF-Token": csrf},
                json={"title": "Run checks", "prompt": "Run the safe checks", "node_id": "lab-pc", "cwd": "C:/work", "thread_id": "thread-1"},
            )
            assert created_task.status_code == 200, created_task.text
            task_id = created_task.json()["task_id"]
            dispatched = websocket.receive_json()
            assert dispatched["type"] == "task.start"
            assert dispatched["task"]["id"] == task_id
            assert dispatched["task"]["thread_id"] == "thread-1"

            websocket.send_json({
                "type": "task.started",
                "task_id": task_id,
                "thread_id": "thread-1",
                "source_thread_id": "thread-1",
                "continuation_mode": "resume",
            })
            websocket.send_json({
                "type": "approval.request",
                "task_id": task_id,
                "rpc_id": "41",
                "method": "item/commandExecution/requestApproval",
                "params": {"command": "python -m pytest", "reason": "Run project tests"},
            })

            snapshot = admin.get("/api/snapshot").json()
            approval = next(item for item in snapshot["approvals"] if item["status"] == "pending")
            assert approval["command_text"] == "python -m pytest"
            assert next(item for item in snapshot["tasks"] if item["id"] == task_id)["status"] == "waiting_approval"
            continued_task = next(item for item in snapshot["tasks"] if item["id"] == task_id)
            assert continued_task["source_thread_id"] == "thread-1"
            assert continued_task["continuation_mode"] == "resume"
            assert snapshot["codex_threads"][0]["remote_task_id"] == task_id

            decision = admin.post(
                f"/api/approvals/{approval['id']}/decision",
                headers={"X-CSRF-Token": csrf},
                json={"decision": "accept"},
            )
            assert decision.status_code == 200, decision.text
            delivered = websocket.receive_json()
            assert delivered["type"] == "approval.decision"
            assert delivered["decision"] == "accept"

            websocket.send_json({"type": "task.completed", "task_id": task_id, "result": "All checks passed"})
            final = admin.get("/api/snapshot").json()
            task = next(item for item in final["tasks"] if item["id"] == task_id)
            assert task["status"] == "completed"
            assert task["progress"] == 100

            cancellable = admin.post(
                "/api/tasks",
                headers={"X-CSRF-Token": csrf},
                json={"title": "Cancel me", "prompt": "Wait", "node_id": "lab-pc", "cwd": "C:/work"},
            )
            cancelled_task_id = cancellable.json()["task_id"]
            assert websocket.receive_json()["type"] == "task.start"
            cancelled = admin.post(
                f"/api/tasks/{cancelled_task_id}/cancel",
                headers={"X-CSRF-Token": csrf},
            )
            assert cancelled.status_code == 200
            assert websocket.receive_json()["type"] == "task.cancel"
            websocket.send_json({"type": "task.failed", "task_id": cancelled_task_id, "error": "late failure"})
            cancelled_snapshot = admin.get("/api/snapshot").json()
            cancelled_task = next(item for item in cancelled_snapshot["tasks"] if item["id"] == cancelled_task_id)
            assert cancelled_task["status"] == "cancelled"

        audit = admin.get("/api/audit")
        assert audit.status_code == 200
        assert any(item["action"] == "approval.accept" for item in audit.json()["audit"])

        keep_last_admin = admin.patch(
            "/api/users/1",
            headers={"X-CSRF-Token": csrf},
            json={"role": "viewer"},
        )
        assert keep_last_admin.status_code == 409

        wrong_password = admin.post(
            "/api/auth/password",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": "incorrect-password", "new_password": "new-password-value-123"},
        )
        assert wrong_password.status_code == 401

        changed_password = admin.post(
            "/api/auth/password",
            headers={"X-CSRF-Token": csrf},
            json={"current_password": "correct-horse-battery-staple", "new_password": "new-password-value-123"},
        )
        assert changed_password.status_code == 200


def test_csrf_is_required(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_MONITOR_DATA_DIR", str(tmp_path))
    with TestClient(app) as client:
        login(client, "rootadmin", "correct-horse-battery-staple")
        response = client.post(
            "/api/users",
            json={"username": "operator.one", "display_name": "Operator", "password": "operator-password-123", "role": "operator"},
        )
        assert response.status_code == 403
