import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from codex_monitor.agent import (
    AgentRuntime,
    AppServer,
    Config,
    RelayProxy,
    codex_app_server_command,
    codex_environment,
    relay_target_allowed,
    relay_websocket_url,
    websocket_url,
)


class FakeProcess:
    returncode = None


class FakeWebSocket:
    def __init__(self):
        self.messages = []

    async def send(self, raw):
        self.messages.append(json.loads(raw))


class FakeTaskServer:
    def __init__(self, *, resume_error=None, fork_error=None, read_turns=None):
        self.process = FakeProcess()
        self.notifications = asyncio.Queue()
        self.server_requests = asyncio.Queue()
        self.calls = []
        self.resume_error = resume_error
        self.fork_error = fork_error
        self.read_turns = read_turns or []

    def drain_notifications(self):
        return None

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "thread-new"}}
        if method == "thread/resume":
            if self.resume_error:
                raise RuntimeError(self.resume_error)
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/fork":
            if self.fork_error:
                raise RuntimeError(self.fork_error)
            return {"thread": {"id": "thread-fork"}}
        if method == "thread/read":
            return {"thread": {"id": params["threadId"], "turns": self.read_turns}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1"}}
        if method == "turn/interrupt":
            return {}
        raise AssertionError(method)

    @staticmethod
    def key(value):
        return str(value)

    async def respond(self, request_id, result=None, error=None):
        return None


class AgentConfigurationTests(unittest.TestCase):
    def test_root_server_url_uses_standalone_agent_endpoint(self):
        self.assertEqual(websocket_url("https://monitor.example"), "wss://monitor.example/ws/agent")

    def test_codex_environment_maps_explicit_node_settings(self):
        with patch.dict(
            "os.environ",
            {
                "CODEX_MONITOR_CODEX_HOME": "/srv/codex",
                "CODEX_MONITOR_CODEX_BASE_URL": "https://codex.example/v1",
                "CODEX_MONITOR_CODEX_PROXY": "http://127.0.0.1:7890",
            },
            clear=True,
        ):
            environment = codex_environment()
        self.assertEqual(environment["CODEX_HOME"], "/srv/codex")
        self.assertEqual(environment["OPENAI_BASE_URL"], "https://codex.example/v1")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(environment["https_proxy"], "http://127.0.0.1:7890")

    def test_codex_command_can_match_code_mode_host(self):
        with patch("codex_monitor.agent.codex_executable", return_value="/bin/codex"):
            with patch.dict("os.environ", {"CODEX_MONITOR_CODEX_CODE_MODE_HOST": "1"}, clear=True):
                command = codex_app_server_command()
        self.assertEqual(
            command,
            ["/bin/codex", "-c", "features.code_mode_host=true", "app-server", "--listen", "stdio://"],
        )

    def test_relay_url_and_allowlist_are_restricted(self):
        self.assertEqual(
            relay_websocket_url("wss://control.example/control/ws/agent", "chatgpt.com", 443),
            "wss://control.example/control/ws/relay?host=chatgpt.com&port=443",
        )
        self.assertTrue(relay_target_allowed("api.openai.com", 443))
        self.assertTrue(relay_target_allowed("draw.openai-next.com", 443))
        self.assertFalse(relay_target_allowed("openai.com.attacker.example", 443))
        self.assertFalse(relay_target_allowed("127.0.0.1", 443))
        self.assertFalse(relay_target_allowed("api.openai.com", 80))

    def test_relay_proxy_overrides_an_ambient_proxy_for_codex_only(self):
        with patch.dict("os.environ", {"CODEX_MONITOR_CODEX_PROXY": "http://old-proxy:7890"}, clear=True):
            environment = codex_environment("http://127.0.0.1:17892")
        self.assertEqual(environment["HTTPS_PROXY"], "http://127.0.0.1:17892")


class PersistentRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_loopback_proxy_rejects_non_openai_targets(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Config("ws://example/control/ws/agent", "token", "node", "Node", (Path(temp_dir),), True, 0)
            proxy = RelayProxy(config)
            await proxy.start()
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
                writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com\r\n\r\n")
                await writer.drain()
                response = await reader.read(256)
                self.assertIn(b"403 Forbidden", response)
                writer.close()
                await writer.wait_closed()
            finally:
                await proxy.close()

    async def test_runtime_reuses_one_app_server(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = AgentRuntime(Config("ws://example", "token", "node", "Node", (Path(temp_dir),)))

            async def fake_start(server):
                server.process = FakeProcess()

            async def fake_close(server):
                server.process = None

            with patch.object(AppServer, "start", fake_start), patch.object(AppServer, "close", fake_close):
                first = await runtime.ensure_codex_server()
                second = await runtime.ensure_codex_server()
                self.assertIs(first, second)
                await runtime.close()
                self.assertIsNone(runtime.codex_server)

    async def test_null_thread_id_starts_a_new_thread_instead_of_resuming_none(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = AgentRuntime(Config("ws://example", "token", "node", "Node", (Path(temp_dir),)))
            runtime.websocket = FakeWebSocket()
            server = FakeTaskServer()
            await server.notifications.put({
                "method": "turn/completed",
                "params": {"turn": {"status": "completed"}},
            })
            runtime.ensure_codex_server = AsyncMock(return_value=server)

            await runtime.run_task({
                "id": 8,
                "title": "New thread",
                "prompt": "Start fresh",
                "thread_id": None,
                "cwd": temp_dir,
            })

            self.assertEqual(server.calls[0][0], "thread/start")
            self.assertEqual(server.calls[1][0], "turn/start")
            kinds = [message["type"] for message in runtime.websocket.messages]
            self.assertIn("task.completed", kinds)

    async def test_terminal_network_wait_fails_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = AgentRuntime(Config("ws://example", "token", "node", "Node", (Path(temp_dir),)))
            runtime.websocket = FakeWebSocket()
            server = FakeTaskServer()
            await server.notifications.put({
                "method": "codex/event/error",
                "params": {"message": "Reconnecting... waiting for network"},
            })
            runtime.ensure_codex_server = AsyncMock(return_value=server)

            await runtime.run_task({
                "id": 7,
                "title": "Continue thread",
                "prompt": "Continue",
                "thread_id": "thread-1",
                "cwd": temp_dir,
            })

            kinds = [message["type"] for message in runtime.websocket.messages]
            self.assertIn("task.started", kinds)
            self.assertIn("task.failed", kinds)
            failure = next(message for message in runtime.websocket.messages if message["type"] == "task.failed")
            self.assertIn("CODEX_MONITOR_CODEX_BASE_URL", failure["error"])
            self.assertIsNone(runtime.active_server)

    async def test_active_writer_creates_a_safe_fork(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = AgentRuntime(Config("ws://example", "token", "node", "Node", (Path(temp_dir),)))
            runtime.websocket = FakeWebSocket()
            server = FakeTaskServer(resume_error="thread already has an active writer")
            await server.notifications.put({
                "method": "turn/completed",
                "params": {"turn": {"status": "completed"}},
            })
            runtime.ensure_codex_server = AsyncMock(return_value=server)

            await runtime.run_task({
                "id": 9,
                "title": "Safe continuation",
                "prompt": "Continue safely",
                "thread_id": "thread-vscode",
                "cwd": temp_dir,
            })

            self.assertEqual([call[0] for call in server.calls[:3]], ["thread/resume", "thread/fork", "turn/start"])
            self.assertEqual(server.calls[2][1]["threadId"], "thread-fork")
            started = next(message for message in runtime.websocket.messages if message["type"] == "task.started")
            self.assertEqual(started["thread_id"], "thread-fork")
            self.assertEqual(started["source_thread_id"], "thread-vscode")
            self.assertEqual(started["continuation_mode"], "fork")

    async def test_active_writer_falls_back_to_a_context_handoff(self):
        history = [{
            "id": "turn-old",
            "items": [
                {"type": "userMessage", "content": [{"type": "text", "text": "Old question"}]},
                {"type": "agentMessage", "text": "Old answer"},
            ],
        }]
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = AgentRuntime(Config("ws://example", "token", "node", "Node", (Path(temp_dir),)))
            runtime.websocket = FakeWebSocket()
            server = FakeTaskServer(
                resume_error="thread already has an active writer",
                fork_error="paginated history cannot be forked",
                read_turns=history,
            )
            await server.notifications.put({
                "method": "turn/completed",
                "params": {"turn": {"status": "completed"}},
            })
            runtime.ensure_codex_server = AsyncMock(return_value=server)

            await runtime.run_task({
                "id": 10,
                "title": "Handoff continuation",
                "prompt": "New instruction",
                "thread_id": "thread-vscode",
                "cwd": temp_dir,
            })

            methods = [call[0] for call in server.calls]
            self.assertEqual(methods[:5], ["thread/resume", "thread/fork", "thread/read", "thread/start", "turn/start"])
            submitted_prompt = server.calls[4][1]["input"][0]["text"]
            self.assertIn("Old question", submitted_prompt)
            self.assertIn("Old answer", submitted_prompt)
            self.assertIn("New instruction", submitted_prompt)
            started = next(message for message in runtime.websocket.messages if message["type"] == "task.started")
            self.assertEqual(started["thread_id"], "thread-new")
            self.assertEqual(started["continuation_mode"], "handoff")


if __name__ == "__main__":
    unittest.main()
