from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__


def _data_dir(value: str | None = None) -> Path:
    return Path(value or os.environ.get("CODEX_MONITOR_DATA_DIR") or Path.home() / ".codex-monitor").expanduser().resolve()


def _set_if(name: str, value: str | None) -> None:
    if value:
        os.environ[name] = value


def _open_later(url: str) -> None:
    timer = threading.Timer(1.2, lambda: webbrowser.open(url))
    timer.daemon = True
    timer.start()


def _ensure_owner(data_dir: Path, username: str | None) -> None:
    os.environ["CODEX_MONITOR_DATA_DIR"] = str(data_dir)
    from . import server

    server.init_db()
    if server.fetch_one("SELECT id FROM users LIMIT 1"):
        return
    owner = (username or input("Owner username [admin]: ").strip() or "admin").strip()
    password = os.environ.get("CODEX_MONITOR_ADMIN_PASSWORD") or getpass.getpass("Create owner password (12+ chars): ")
    if len(password) < 12:
        raise SystemExit("Owner password must contain at least 12 characters.")
    if "CODEX_MONITOR_ADMIN_PASSWORD" not in os.environ:
        repeated = getpass.getpass("Repeat owner password: ")
        if password != repeated:
            raise SystemExit("Passwords do not match.")
    os.environ["CODEX_MONITOR_ADMIN_USERNAME"] = owner
    os.environ["CODEX_MONITOR_ADMIN_DISPLAY_NAME"] = owner
    os.environ["CODEX_MONITOR_ADMIN_PASSWORD"] = password


def _workspace_values(values: list[str] | None) -> list[str]:
    raw = values or [os.getcwd()]
    result: list[str] = []
    for value in raw:
        path = Path(value).expanduser().resolve()
        if not path.is_dir():
            raise SystemExit(f"Workspace does not exist: {path}")
        result.append(str(path))
    return result


def command_demo(args: argparse.Namespace) -> None:
    from .demo import DEMO_AGENT_TOKEN, DEMO_PASSWORD, DEMO_USERNAME

    demo_root = Path(tempfile.mkdtemp(prefix="codex-monitor-demo-"))
    os.environ.update(
        {
            "CODEX_MONITOR_DEMO": "1",
            "CODEX_MONITOR_ENV": "development",
            "CODEX_MONITOR_DATA_DIR": str(demo_root),
            "CODEX_MONITOR_ADMIN_USERNAME": DEMO_USERNAME,
            "CODEX_MONITOR_ADMIN_DISPLAY_NAME": "Demo Owner",
            "CODEX_MONITOR_ADMIN_PASSWORD": DEMO_PASSWORD,
            "CODEX_MONITOR_AGENT_TOKEN": DEMO_AGENT_TOKEN,
        }
    )
    url = f"http://127.0.0.1:{args.port}"
    print(f"Codex Monitor demo: {url}")
    print("Synthetic data only; no local Codex account is accessed.")
    if not args.no_browser:
        _open_later(url)
    try:
        uvicorn.run("codex_monitor.server:app", host="127.0.0.1", port=args.port, log_level=args.log_level)
    finally:
        shutil.rmtree(demo_root, ignore_errors=True)


async def _run_local_async(args: argparse.Namespace) -> None:
    from . import agent
    from .server import app

    config = uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level)
    web_server = uvicorn.Server(config)
    server_task = asyncio.create_task(web_server.serve(), name="codex-monitor-web")
    await asyncio.sleep(0.35)
    agent_task = asyncio.create_task(agent.run(), name="codex-monitor-agent")
    done, pending = await asyncio.wait({server_task, agent_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in done:
        error = task.exception()
        if error:
            raise error
    web_server.should_exit = True
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


def command_local(args: argparse.Namespace) -> None:
    data_dir = _data_dir(args.data_dir)
    _ensure_owner(data_dir, args.username)
    workspaces = _workspace_values(args.workspace)
    token = secrets.token_urlsafe(48)
    os.environ.update(
        {
            "CODEX_MONITOR_ENV": "development",
            "CODEX_MONITOR_DATA_DIR": str(data_dir),
            "CODEX_MONITOR_AGENT_TOKEN": token,
            "CODEX_MONITOR_SERVER_URL": f"http://127.0.0.1:{args.port}",
            "CODEX_MONITOR_WORKSPACES": ";".join(workspaces),
        }
    )
    _set_if("CODEX_MONITOR_NODE_NAME", args.node_name)
    url = f"http://127.0.0.1:{args.port}"
    print(f"Codex Monitor local mode: {url}")
    print(f"Allowed workspaces: {'; '.join(workspaces)}")
    if args.host != "127.0.0.1":
        print("Warning: non-loopback HTTP is not suitable for untrusted networks.", file=sys.stderr)
    if not args.no_browser:
        _open_later(url)
    asyncio.run(_run_local_async(args))


def command_serve(args: argparse.Namespace) -> None:
    _set_if("CODEX_MONITOR_DATA_DIR", args.data_dir)
    uvicorn.run("codex_monitor.server:app", host=args.host, port=args.port, log_level=args.log_level)


def command_agent(args: argparse.Namespace) -> None:
    workspaces = _workspace_values(args.workspace)
    token = os.environ.get("CODEX_MONITOR_AGENT_TOKEN", "").strip()
    if not token:
        token = getpass.getpass("Agent token: ").strip()
    if not token:
        raise SystemExit("Agent token is required.")
    os.environ.update(
        {
            "CODEX_MONITOR_SERVER_URL": args.server,
            "CODEX_MONITOR_AGENT_TOKEN": token,
            "CODEX_MONITOR_WORKSPACES": ";".join(workspaces),
        }
    )
    _set_if("CODEX_MONITOR_NODE_NAME", args.node_name)
    from .agent import run

    asyncio.run(run())


def command_doctor(_: argparse.Namespace) -> None:
    from .agent import codex_app_server_command, codex_executable

    executable = codex_executable()
    resolved = shutil.which(executable) if executable == "codex" else executable
    print(f"Codex Monitor {__version__}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"Data directory: {_data_dir()}")
    if not resolved or not Path(resolved).is_file():
        print("Codex CLI: not found")
        print("Install and sign in to Codex before using local or agent mode.")
        raise SystemExit(1)
    completed = subprocess.run([resolved, "--version"], capture_output=True, text=True, timeout=10, check=False)
    version = (completed.stdout or completed.stderr).strip() or "unknown"
    print(f"Codex CLI: {resolved}")
    print(f"Codex version: {version}")
    print(f"App Server command: {' '.join(codex_app_server_command())}")
    if completed.returncode:
        raise SystemExit(completed.returncode)
    print("Status: ready")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-monitor", description="Monitor your own local Codex tasks and approvals.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    demo = subparsers.add_parser("demo", help="Run the UI with synthetic data and without touching Codex.")
    demo.add_argument("--port", type=int, default=8765)
    demo.add_argument("--no-browser", action="store_true")
    demo.add_argument("--log-level", default="warning")
    demo.set_defaults(func=command_demo)

    local = subparsers.add_parser("local", help="Run the web UI and local Codex agent together.")
    local.add_argument("--host", default="127.0.0.1")
    local.add_argument("--port", type=int, default=8765)
    local.add_argument("--data-dir")
    local.add_argument("--username")
    local.add_argument("--workspace", action="append", help="Allowed workspace; repeat for more than one.")
    local.add_argument("--node-name")
    local.add_argument("--no-browser", action="store_true")
    local.add_argument("--log-level", default="warning")
    local.set_defaults(func=command_local)

    serve = subparsers.add_parser("serve", help="Run only the self-hosted control server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8080")))
    serve.add_argument("--data-dir")
    serve.add_argument("--log-level", default="info")
    serve.set_defaults(func=command_serve)

    agent = subparsers.add_parser("agent", help="Connect this user's local Codex to a control server.")
    agent.add_argument("--server", required=True)
    agent.add_argument("--workspace", action="append", help="Allowed workspace; repeat for more than one.")
    agent.add_argument("--node-name")
    agent.set_defaults(func=command_agent)

    doctor = subparsers.add_parser("doctor", help="Check Python and the local Codex installation.")
    doctor.set_defaults(func=command_doctor)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nCodex Monitor stopped.")

