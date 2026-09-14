from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import sqlite3
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash("codex-monitor-dummy-password-value")
COOKIE_NAME = "codex_monitor_session"
SESSION_DAYS = 7
MAX_EVENT_TEXT = 12_000
ALLOWED_ROLES = {"admin", "operator", "viewer"}
WRITE_ROLES = {"admin", "operator"}
RELAY_ALLOWED_SUFFIXES = ("chatgpt.com", "openai.com", "openai-next.com", "oaistatic.com", "oaiusercontent.com")
RELAY_BUFFER_SIZE = 64 * 1024
relay_slots = asyncio.Semaphore(16)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def db_path() -> Path:
    data_dir = Path(os.environ.get("CODEX_MONITOR_DATA_DIR", str(Path.home() / ".codex-monitor")))
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "codex_monitor.db"


def demo_mode() -> bool:
    return os.environ.get("CODEX_MONITOR_DEMO", "").strip().lower() in {"1", "true", "yes", "on"}


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(db_path(), timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    return connection


def execute(sql: str, values: tuple[Any, ...] = ()) -> int:
    with db() as connection:
        cursor = connection.execute(sql, values)
        return int(cursor.lastrowid or 0)


def fetch_one(sql: str, values: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    with db() as connection:
        return connection.execute(sql, values).fetchone()


def fetch_all(sql: str, values: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    with db() as connection:
        return connection.execute(sql, values).fetchall()


def init_db() -> None:
    with db() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'operator', 'viewer')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_hash TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                csrf_token TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                ip_address TEXT,
                user_agent TEXT
            );

            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                platform TEXT,
                codex_version TEXT,
                status TEXT NOT NULL DEFAULT 'offline',
                current_task_id INTEGER,
                workspaces_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                connected_at TEXT,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS codex_threads (
                node_id TEXT NOT NULL REFERENCES nodes(node_id) ON DELETE CASCADE,
                thread_id TEXT NOT NULL,
                session_id TEXT,
                name TEXT,
                preview TEXT NOT NULL DEFAULT '',
                cwd TEXT,
                source TEXT,
                runtime_status TEXT NOT NULL DEFAULT 'notLoaded',
                history_mode TEXT,
                forked_from_id TEXT,
                created_at_epoch INTEGER NOT NULL DEFAULT 0,
                updated_at_epoch INTEGER NOT NULL DEFAULT 0,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY(node_id, thread_id)
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prompt TEXT NOT NULL,
                node_id TEXT NOT NULL,
                cwd TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                stage TEXT NOT NULL DEFAULT '等待节点接收',
                thread_id TEXT,
                source_thread_id TEXT,
                continuation_mode TEXT NOT NULL DEFAULT 'new',
                turn_id TEXT,
                result TEXT,
                error TEXT,
                created_by INTEGER NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                node_id TEXT NOT NULL,
                rpc_id TEXT NOT NULL,
                method TEXT NOT NULL,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                command_text TEXT,
                risk TEXT NOT NULL,
                reason TEXT,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                decided_by INTEGER REFERENCES users(id),
                created_at TEXT NOT NULL,
                decided_at TEXT,
                UNIQUE(node_id, rpc_id)
            );

            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT,
                details_json TEXT NOT NULL DEFAULT '{}',
                ip_address TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token_hash);
            CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);
            CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_events_task ON task_events(task_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_approvals_status ON approvals(status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_logs(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_codex_threads_updated ON codex_threads(updated_at_epoch DESC);
            """
        )
        existing_columns = {
            table: {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            for table in ("codex_threads", "tasks")
        }
        migrations = {
            "codex_threads": {
                "history_mode": "TEXT",
                "forked_from_id": "TEXT",
            },
            "tasks": {
                "source_thread_id": "TEXT",
                "continuation_mode": "TEXT NOT NULL DEFAULT 'new'",
            },
        }
        for table, columns in migrations.items():
            for column, declaration in columns.items():
                if column not in existing_columns[table]:
                    connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        connection.execute("PRAGMA optimize")


def bootstrap_admin() -> None:
    username = os.environ.get("CODEX_MONITOR_ADMIN_USERNAME", "").strip()
    password = os.environ.get("CODEX_MONITOR_ADMIN_PASSWORD", "")
    display_name = os.environ.get("CODEX_MONITOR_ADMIN_DISPLAY_NAME", "管理员").strip() or "管理员"
    if not username or not password:
        if os.environ.get("CODEX_MONITOR_ENV", "development").lower() == "production":
            raise RuntimeError("CODEX_MONITOR_ADMIN_USERNAME and CODEX_MONITOR_ADMIN_PASSWORD are required")
        return
    if len(password) < 12:
        raise RuntimeError("CODEX_MONITOR_ADMIN_PASSWORD must contain at least 12 characters")
    existing = fetch_one("SELECT id FROM users WHERE username = ?", (username,))
    if existing:
        return
    now = utc_now()
    execute(
        "INSERT INTO users(username, display_name, password_hash, role, active, created_at, updated_at) VALUES(?,?,?,?,1,?,?)",
        (username, display_name, PASSWORD_HASHER.hash(password), "admin", now, now),
    )


def compact_json(value: Any, limit: int = 60_000) -> str:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return encoded[:limit]


def audit(user_id: int | None, action: str, target_type: str, target_id: Any = None, details: Any = None, ip: str | None = None) -> None:
    execute(
        "INSERT INTO audit_logs(user_id, action, target_type, target_id, details_json, ip_address, created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, action, target_type, str(target_id) if target_id is not None else None, compact_json(details or {}), ip, utc_now()),
    )


def event(task_id: int, kind: str, message: str, payload: Any = None) -> None:
    execute(
        "INSERT INTO task_events(task_id, kind, message, payload_json, created_at) VALUES(?,?,?,?,?)",
        (task_id, kind[:50], message[:MAX_EVENT_TEXT], compact_json(payload or {}), utc_now()),
    )


def session_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def secure_cookie() -> bool:
    return os.environ.get("CODEX_MONITOR_ENV", "development").lower() == "production"


@dataclass
class Identity:
    id: int
    username: str
    display_name: str
    role: str
    csrf_token: str
    session_id: int


def current_identity(request: Request) -> Identity:
    if demo_mode():
        row = fetch_one("SELECT id,username,display_name,role FROM users ORDER BY id LIMIT 1")
        if not row:
            raise HTTPException(status_code=503, detail="演示数据尚未初始化")
        return Identity(
            id=row["id"], username=row["username"], display_name=row["display_name"],
            role=row["role"], csrf_token="demo", session_id=0,
        )
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="请先登录")
    row = fetch_one(
        """
        SELECT s.id AS session_id, s.csrf_token, s.expires_at,
               u.id, u.username, u.display_name, u.role, u.active
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
        """,
        (session_hash(token),),
    )
    if not row or not row["active"] or row["expires_at"] <= utc_now():
        if row:
            execute("DELETE FROM sessions WHERE id = ?", (row["session_id"],))
        raise HTTPException(status_code=401, detail="登录已过期")
    execute("UPDATE sessions SET last_seen_at = ? WHERE id = ?", (utc_now(), row["session_id"]))
    return Identity(
        id=row["id"], username=row["username"], display_name=row["display_name"],
        role=row["role"], csrf_token=row["csrf_token"], session_id=row["session_id"],
    )


def require_role(*roles: str) -> Callable[[Identity], Identity]:
    def dependency(identity: Identity = Depends(current_identity)) -> Identity:
        if identity.role not in roles:
            raise HTTPException(status_code=403, detail="权限不足")
        return identity
    return dependency


def verify_csrf(request: Request, identity: Identity) -> None:
    if demo_mode():
        return
    supplied = request.headers.get("x-csrf-token", "")
    if not supplied or not secrets.compare_digest(supplied, identity.csrf_token):
        raise HTTPException(status_code=403, detail="安全校验失败，请刷新页面后重试")


@dataclass
class ConnectedAgent:
    node_id: str
    websocket: WebSocket
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending_requests: dict[str, asyncio.Future[Any]] = field(default_factory=dict)

    async def send(self, payload: dict[str, Any]) -> None:
        async with self.send_lock:
            await self.websocket.send_json(payload)


agents: dict[str, ConnectedAgent] = {}
agents_lock = asyncio.Lock()
login_attempts: dict[str, list[float]] = {}


def agent_token_valid(token: str) -> bool:
    expected = os.environ.get("CODEX_MONITOR_AGENT_TOKEN", "")
    return bool(expected and token and secrets.compare_digest(expected, token))


def relay_target_allowed(host: str, port: int) -> bool:
    normalized = host.strip().rstrip(".").lower()
    if port != 443 or not normalized or len(normalized) > 253:
        return False
    if not re.fullmatch(r"[a-z0-9.-]+", normalized) or ".." in normalized:
        return False
    return any(normalized == suffix or normalized.endswith(f".{suffix}") for suffix in RELAY_ALLOWED_SUFFIXES)


async def open_public_relay_target(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    loop = asyncio.get_running_loop()
    addresses = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=10,
    )
    failures: list[Exception] = []
    seen: set[tuple[int, str]] = set()
    for family, _, _, _, sockaddr in addresses:
        address = str(sockaddr[0])
        key = (family, address)
        if key in seen:
            continue
        seen.add(key)
        try:
            if not ipaddress.ip_address(address).is_global:
                continue
        except ValueError:
            continue
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(address, port, family=family),
                timeout=10,
            )
        except (OSError, asyncio.TimeoutError) as exc:
            failures.append(exc)
    if failures:
        raise ConnectionError("OpenAI relay target is unreachable") from failures[-1]
    raise ConnectionError("OpenAI relay target did not resolve to a public address")


def bearer_value(value: str | None) -> str:
    if not value or not value.lower().startswith("bearer "):
        return ""
    return value[7:].strip()


def row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def safe_json(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def approval_summary(method: str, params: dict[str, Any]) -> tuple[str, str, str, str | None, str | None]:
    command_value = params.get("command") or params.get("cmd")
    if isinstance(command_value, list):
        command_text = " ".join(str(part) for part in command_value)
    elif command_value is None:
        command_text = None
    else:
        command_text = str(command_value)
    reason = params.get("reason") or params.get("justification")
    if method == "item/fileChange/requestApproval":
        changes = params.get("changes") or params.get("fileChanges") or []
        count = len(changes) if isinstance(changes, (list, dict)) else 1
        return "文件修改", f"请求修改 {count} 项文件", "medium", command_text, str(reason) if reason else None
    if method == "item/commandExecution/requestApproval":
        dangerous = bool(command_text and re.search(r"(?i)(rm\s+-rf|remove-item|del\s+/[sq]|format\s+|shutdown|reboot|reg\s+delete|diskpart)", command_text))
        return "命令执行", "请求运行系统命令", "high" if dangerous else "medium", command_text, str(reason) if reason else None
    return "人工确认", "Codex 正在等待你的决定", "medium", command_text, str(reason) if reason else None


class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=512)


class PasswordChangeBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=12, max_length=512)


class TaskCreateBody(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    prompt: str = Field(min_length=1, max_length=30_000)
    node_id: str = Field(min_length=1, max_length=120)
    cwd: str | None = Field(default=None, max_length=1_000)
    thread_id: str | None = Field(default=None, max_length=160)


class DecisionBody(BaseModel):
    decision: str

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, value: str) -> str:
        if value not in {"accept", "decline", "cancel"}:
            raise ValueError("unsupported decision")
        return value


class UserCreateBody(BaseModel):
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9_.-]+$")
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=12, max_length=512)
    role: str

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        if value not in ALLOWED_ROLES:
            raise ValueError("unsupported role")
        return value


class UserUpdateBody(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    password: str | None = Field(default=None, min_length=12, max_length=512)
    role: str | None = None
    active: bool | None = None

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_ROLES:
            raise ValueError("unsupported role")
        return value


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    bootstrap_admin()
    if os.environ.get("CODEX_MONITOR_ENV", "development").lower() == "production":
        agent_token = os.environ.get("CODEX_MONITOR_AGENT_TOKEN", "")
        if len(agent_token) < 32:
            raise RuntimeError("CODEX_MONITOR_AGENT_TOKEN must contain at least 32 characters")
    execute("UPDATE nodes SET status = 'offline', current_task_id = NULL")
    execute("DELETE FROM sessions WHERE expires_at <= ?", (utc_now(),))
    if demo_mode():
        from .demo import seed_demo_data

        seed_demo_data(server=sys.modules[__name__])
    yield


app = FastAPI(title="Codex Monitor", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_ROOT), name="static")


@app.middleware("http")
async def security_headers(request: Request, call_next: Callable):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self' wss:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith("/api/") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    if secure_cookie():
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_ROOT / "index.html")


@app.get("/manifest.webmanifest", include_in_schema=False)
async def manifest() -> FileResponse:
    return FileResponse(STATIC_ROOT / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def service_worker() -> FileResponse:
    return FileResponse(STATIC_ROOT / "sw.js", media_type="application/javascript")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "service": "codex-monitor", "demo": demo_mode(), "time": utc_now()}


@app.post("/api/auth/login")
async def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    ip = client_ip(request)
    now_monotonic = asyncio.get_running_loop().time()
    attempt_keys = (f"ip:{ip}", f"user:{body.username.strip().casefold()}")
    current_attempts = {
        key: [value for value in login_attempts.get(key, []) if now_monotonic - value < 900]
        for key in attempt_keys
    }
    if any(len(attempts) >= 8 for attempts in current_attempts.values()):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    row = fetch_one("SELECT * FROM users WHERE username = ?", (body.username.strip(),))
    candidate_hash = row["password_hash"] if row and row["active"] else DUMMY_PASSWORD_HASH
    try:
        password_valid = PASSWORD_HASHER.verify(candidate_hash, body.password)
    except (VerifyMismatchError, InvalidHashError):
        password_valid = False
    valid = bool(row and row["active"] and password_valid)
    if not valid:
        for key, attempts in current_attempts.items():
            attempts.append(now_monotonic)
            login_attempts[key] = attempts
        audit(row["id"] if row else None, "auth.login_failed", "session", details={"username": body.username}, ip=ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    for key in attempt_keys:
        login_attempts.pop(key, None)
    if PASSWORD_HASHER.check_needs_rehash(row["password_hash"]):
        execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (PASSWORD_HASHER.hash(body.password), utc_now(), row["id"]))
    token = secrets.token_urlsafe(48)
    csrf = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds")
    session_id = execute(
        "INSERT INTO sessions(token_hash, user_id, csrf_token, expires_at, last_seen_at, ip_address, user_agent) VALUES(?,?,?,?,?,?,?)",
        (session_hash(token), row["id"], csrf, expires, now.isoformat(timespec="seconds"), ip, request.headers.get("user-agent", "")[:500]),
    )
    response.set_cookie(
        COOKIE_NAME, token, max_age=SESSION_DAYS * 86400, httponly=True,
        secure=secure_cookie(), samesite="strict", path="/",
    )
    audit(row["id"], "auth.login", "session", session_id, ip=ip)
    return {"user": {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "role": row["role"]}, "csrf_token": csrf}


@app.get("/api/auth/me")
async def me(identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    return {"user": {"id": identity.id, "username": identity.username, "display_name": identity.display_name, "role": identity.role}, "csrf_token": identity.csrf_token}


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response, identity: Identity = Depends(current_identity)) -> dict[str, bool]:
    verify_csrf(request, identity)
    execute("DELETE FROM sessions WHERE id = ?", (identity.session_id,))
    response.delete_cookie(COOKIE_NAME, path="/", secure=secure_cookie(), samesite="strict")
    audit(identity.id, "auth.logout", "session", identity.session_id, ip=client_ip(request))
    return {"ok": True}


@app.post("/api/auth/password")
async def change_password(body: PasswordChangeBody, request: Request, identity: Identity = Depends(current_identity)) -> dict[str, bool]:
    verify_csrf(request, identity)
    user = fetch_one("SELECT password_hash FROM users WHERE id=?", (identity.id,))
    try:
        valid = bool(user and PASSWORD_HASHER.verify(user["password_hash"], body.current_password))
    except (VerifyMismatchError, InvalidHashError):
        valid = False
    if not valid:
        raise HTTPException(status_code=401, detail="当前密码不正确")
    now = utc_now()
    execute("UPDATE users SET password_hash=?, updated_at=? WHERE id=?", (PASSWORD_HASHER.hash(body.new_password), now, identity.id))
    execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (identity.id, identity.session_id))
    audit(identity.id, "auth.password_changed", "user", identity.id, ip=client_ip(request))
    return {"ok": True}


@app.get("/api/snapshot")
async def snapshot(identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    task_rows = fetch_all(
        """
        SELECT t.*, u.display_name AS creator_name,
               (SELECT COUNT(*) FROM approvals a WHERE a.task_id=t.id AND a.status='pending') AS pending_approvals
        FROM tasks t JOIN users u ON u.id=t.created_by ORDER BY t.id DESC LIMIT 80
        """
    )
    tasks = []
    for row in task_rows:
        item = row_dict(row)
        item["events"] = [row_dict(e) for e in fetch_all("SELECT id,kind,message,created_at FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 8", (row["id"],))]
        tasks.append(item)
    approvals = [row_dict(row) for row in fetch_all(
        """
        SELECT a.*, t.title AS task_title, u.display_name AS decided_by_name
        FROM approvals a JOIN tasks t ON t.id=a.task_id
        LEFT JOIN users u ON u.id=a.decided_by
        ORDER BY CASE a.status WHEN 'pending' THEN 0 ELSE 1 END, a.id DESC LIMIT 80
        """
    )]
    nodes = []
    for row in fetch_all("SELECT * FROM nodes ORDER BY status DESC, name"):
        item = row_dict(row)
        item["workspaces"] = safe_json(item.pop("workspaces_json"), [])
        item["metadata"] = safe_json(item.pop("metadata_json"), {})
        nodes.append(item)
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    codex_threads = []
    for row in fetch_all(
        """
        SELECT c.*, n.name AS node_name, n.status AS node_status,
               (SELECT t.id FROM tasks t WHERE t.node_id=c.node_id AND t.thread_id=c.thread_id ORDER BY t.id DESC LIMIT 1) AS remote_task_id,
               (SELECT t.status FROM tasks t WHERE t.node_id=c.node_id AND t.thread_id=c.thread_id ORDER BY t.id DESC LIMIT 1) AS remote_task_status
        FROM codex_threads c JOIN nodes n ON n.node_id=c.node_id
        ORDER BY c.updated_at_epoch DESC LIMIT 100
        """
    ):
        item = row_dict(row)
        if item["runtime_status"] == "active":
            item["activity_state"] = "active"
        elif item["node_status"] == "online" and item["updated_at_epoch"] >= now_epoch - 180:
            item["activity_state"] = "recent"
        else:
            item["activity_state"] = "idle"
        codex_threads.append(item)
    stats_row = fetch_one(
        """SELECT COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN status IN ('running','waiting_approval','queued') THEN 1 ELSE 0 END),0) AS active,
        COALESCE(SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END),0) AS completed,
        COALESCE(SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END),0) AS failed FROM tasks"""
    )
    pending = fetch_one("SELECT COUNT(*) AS count FROM approvals WHERE status='pending'")
    recent_codex_threads = sum(1 for item in codex_threads if item["activity_state"] in {"active", "recent"})
    return {
        "user": {"id": identity.id, "username": identity.username, "display_name": identity.display_name, "role": identity.role},
        "stats": {**row_dict(stats_row), "pending_approvals": pending["count"], "online_nodes": sum(1 for node in nodes if node["status"] == "online"), "recent_codex_threads": recent_codex_threads},
        "tasks": tasks, "approvals": approvals, "nodes": nodes, "codex_threads": codex_threads, "server_time": utc_now(),
    }


@app.post("/api/tasks")
async def create_task(body: TaskCreateBody, request: Request, identity: Identity = Depends(require_role(*WRITE_ROLES))) -> dict[str, Any]:
    verify_csrf(request, identity)
    thread_id = body.thread_id.strip() if body.thread_id else None
    thread = None
    if thread_id:
        thread = fetch_one("SELECT * FROM codex_threads WHERE node_id=? AND thread_id=?", (body.node_id, thread_id))
        if not thread:
            raise HTTPException(status_code=404, detail="Codex 会话不存在或已经移动")
    cwd = (thread["cwd"] if thread and thread["cwd"] else body.cwd) or None
    if isinstance(cwd, str):
        cwd = cwd.strip() or None
    now = utc_now()
    if demo_mode():
        task_id = execute(
            """INSERT INTO tasks(title,prompt,node_id,cwd,status,progress,stage,thread_id,source_thread_id,continuation_mode,created_by,created_at,started_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                body.title.strip(), body.prompt.strip(), body.node_id, cwd, "running", 12,
                "演示节点已接收任务", thread_id, thread_id, "resume" if thread_id else "new",
                identity.id, now, now,
            ),
        )
        event(task_id, "created", "演示任务已创建")
        event(task_id, "started", "演示节点已开始执行")
        return {"ok": True, "task_id": task_id, "demo": True}
    async with agents_lock:
        connected = agents.get(body.node_id)
    if not connected:
        raise HTTPException(status_code=409, detail="所选执行节点当前不在线")
    task_id = execute(
        """INSERT INTO tasks(title,prompt,node_id,cwd,status,progress,stage,thread_id,source_thread_id,continuation_mode,created_by,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            body.title.strip(), body.prompt.strip(), body.node_id, cwd, "queued", 0, "等待节点接收",
            thread_id, thread_id, "pending" if thread_id else "new", identity.id, now,
        ),
    )
    event(task_id, "created", "已发送会话后续指令" if thread_id else "任务已创建")
    audit(identity.id, "task.continue" if thread_id else "task.create", "task", task_id, {"node_id": body.node_id, "title": body.title, "thread_id": thread_id}, client_ip(request))
    try:
        await connected.send({"type": "task.start", "task": {"id": task_id, "title": body.title.strip(), "prompt": body.prompt.strip(), "cwd": cwd, "thread_id": thread_id}})
    except Exception as exc:
        execute("UPDATE tasks SET status='failed', stage='发送失败', error=?, completed_at=? WHERE id=?", (str(exc)[:1000], utc_now(), task_id))
        raise HTTPException(status_code=502, detail="任务未能发送到执行节点") from exc
    return {"ok": True, "task_id": task_id}


@app.get("/api/codex-threads/{node_id}/{thread_id}")
async def codex_thread_detail(node_id: str, thread_id: str, _: Identity = Depends(current_identity)) -> dict[str, Any]:
    thread = fetch_one("SELECT * FROM codex_threads WHERE node_id=? AND thread_id=?", (node_id, thread_id))
    if not thread:
        raise HTTPException(status_code=404, detail="Codex 会话不存在")
    if demo_mode():
        from .demo import demo_thread_detail

        return {"thread": row_dict(thread), "detail": demo_thread_detail(thread_id)}
    async with agents_lock:
        connected = agents.get(node_id)
    if not connected:
        raise HTTPException(status_code=409, detail="执行节点已离线，暂时无法读取会话详情")

    request_id = secrets.token_urlsafe(18)
    future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
    connected.pending_requests[request_id] = future
    try:
        await connected.send({"type": "thread.detail.request", "request_id": request_id, "thread_id": thread_id})
        response = await asyncio.wait_for(future, timeout=45)
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="读取 Codex 会话超时，请重试") from exc
    except (ConnectionError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail="执行节点连接中断") from exc
    finally:
        connected.pending_requests.pop(request_id, None)

    if response.get("type") == "thread.detail.error":
        raise HTTPException(status_code=502, detail=str(response.get("error") or "无法读取 Codex 会话")[:500])
    detail = response.get("detail") if isinstance(response.get("detail"), dict) else {}
    return {"thread": row_dict(thread), "detail": detail}


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: int, request: Request, identity: Identity = Depends(require_role(*WRITE_ROLES))) -> dict[str, bool]:
    verify_csrf(request, identity)
    task = fetch_one("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task["status"] not in {"queued", "running", "waiting_approval"}:
        raise HTTPException(status_code=409, detail="任务已经结束")
    async with agents_lock:
        connected = agents.get(task["node_id"])
    if connected:
        await connected.send({"type": "task.cancel", "task_id": task_id})
    now = utc_now()
    execute("UPDATE tasks SET status='cancelled', stage='已取消', completed_at=? WHERE id=?", (now, task_id))
    execute("UPDATE approvals SET status='cancelled', decision='cancel', decided_by=?, decided_at=? WHERE task_id=? AND status='pending'", (identity.id, now, task_id))
    execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=? AND current_task_id=?", (task["node_id"], task_id))
    event(task_id, "cancelled", f"{identity.display_name} 取消了任务")
    audit(identity.id, "task.cancel", "task", task_id, ip=client_ip(request))
    return {"ok": True}


@app.post("/api/approvals/{approval_id}/decision")
async def decide_approval(approval_id: int, body: DecisionBody, request: Request, identity: Identity = Depends(require_role(*WRITE_ROLES))) -> dict[str, bool]:
    verify_csrf(request, identity)
    approval = fetch_one("SELECT * FROM approvals WHERE id=?", (approval_id,))
    if not approval:
        raise HTTPException(status_code=404, detail="审批不存在")
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail="该请求已经处理")
    async with agents_lock:
        connected = agents.get(approval["node_id"])
    if not connected and not demo_mode():
        raise HTTPException(status_code=409, detail="执行节点已离线，暂时无法送达决定")
    if connected:
        await connected.send({"type": "approval.decision", "task_id": approval["task_id"], "rpc_id": approval["rpc_id"], "method": approval["method"], "decision": body.decision})
        if body.decision == "cancel":
            await connected.send({"type": "task.cancel", "task_id": approval["task_id"]})
    now = utc_now()
    execute("UPDATE approvals SET status='decided', decision=?, decided_by=?, decided_at=? WHERE id=?", (body.decision, identity.id, now, approval_id))
    if body.decision == "cancel":
        execute("UPDATE tasks SET status='cancelled', stage='已取消', completed_at=? WHERE id=?", (now, approval["task_id"]))
        execute("UPDATE approvals SET status='cancelled', decision='cancel', decided_by=?, decided_at=? WHERE task_id=? AND status='pending' AND id<>?", (identity.id, now, approval["task_id"], approval_id))
        execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=? AND current_task_id=?", (approval["node_id"], approval["task_id"]))
    else:
        execute("UPDATE tasks SET status='running', stage='继续执行' WHERE id=? AND status='waiting_approval'", (approval["task_id"],))
    readable = {"accept": "批准", "decline": "拒绝", "cancel": "拒绝并终止"}[body.decision]
    event(approval["task_id"], "approval", f"{identity.display_name} 已{readable}：{approval['title']}")
    audit(identity.id, f"approval.{body.decision}", "approval", approval_id, {"task_id": approval["task_id"]}, client_ip(request))
    return {"ok": True}


@app.get("/api/users")
async def list_users(_: Identity = Depends(require_role("admin"))) -> dict[str, Any]:
    rows = fetch_all("SELECT id,username,display_name,role,active,created_at,updated_at FROM users ORDER BY id")
    return {"users": [row_dict(row) for row in rows]}


@app.post("/api/users")
async def create_user(body: UserCreateBody, request: Request, identity: Identity = Depends(require_role("admin"))) -> dict[str, Any]:
    verify_csrf(request, identity)
    now = utc_now()
    try:
        user_id = execute(
            "INSERT INTO users(username,display_name,password_hash,role,active,created_at,updated_at) VALUES(?,?,?,?,1,?,?)",
            (body.username.strip(), body.display_name.strip(), PASSWORD_HASHER.hash(body.password), body.role, now, now),
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="用户名已经存在") from exc
    audit(identity.id, "user.create", "user", user_id, {"username": body.username, "role": body.role}, client_ip(request))
    return {"ok": True, "user_id": user_id}


@app.patch("/api/users/{user_id}")
async def update_user(user_id: int, body: UserUpdateBody, request: Request, identity: Identity = Depends(require_role("admin"))) -> dict[str, bool]:
    verify_csrf(request, identity)
    target = fetch_one("SELECT * FROM users WHERE id=?", (user_id,))
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user_id == identity.id and body.active is False:
        raise HTTPException(status_code=409, detail="不能停用当前登录账号")
    removes_active_admin = bool(
        target["active"]
        and target["role"] == "admin"
        and (body.active is False or (body.role is not None and body.role != "admin"))
    )
    if removes_active_admin:
        active_admins = fetch_one("SELECT COUNT(*) AS count FROM users WHERE active=1 AND role='admin'")
        if active_admins and active_admins["count"] <= 1:
            raise HTTPException(status_code=409, detail="必须至少保留一名启用的管理员")
    updates: list[str] = []
    values: list[Any] = []
    for name, value in (("display_name", body.display_name), ("role", body.role)):
        if value is not None:
            updates.append(f"{name}=?")
            values.append(value.strip() if isinstance(value, str) else value)
    if body.active is not None:
        updates.append("active=?")
        values.append(1 if body.active else 0)
    if body.password:
        updates.append("password_hash=?")
        values.append(PASSWORD_HASHER.hash(body.password))
    if updates:
        updates.append("updated_at=?")
        values.append(utc_now())
        values.append(user_id)
        execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", tuple(values))
    if body.active is False or body.password:
        execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (user_id, identity.session_id))
    audit(identity.id, "user.update", "user", user_id, {"fields": [name.split("=")[0] for name in updates]}, client_ip(request))
    return {"ok": True}


@app.get("/api/audit")
async def audit_list(_: Identity = Depends(require_role("admin"))) -> dict[str, Any]:
    rows = fetch_all(
        """SELECT a.id,a.action,a.target_type,a.target_id,a.details_json,a.ip_address,a.created_at,u.display_name
        FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id ORDER BY a.id DESC LIMIT 200"""
    )
    items = []
    for row in rows:
        item = row_dict(row)
        item["details"] = safe_json(item.pop("details_json"), {})
        items.append(item)
    return {"audit": items}


async def register_agent(websocket: WebSocket, hello: dict[str, Any]) -> ConnectedAgent:
    node = hello.get("node") if isinstance(hello.get("node"), dict) else {}
    node_id = str(node.get("id", "")).strip()[:120]
    name = str(node.get("name", node_id)).strip()[:160]
    if not node_id or not name:
        raise ValueError("invalid node identity")
    connected = ConnectedAgent(node_id=node_id, websocket=websocket)
    async with agents_lock:
        previous = agents.get(node_id)
        if previous and previous.websocket is not websocket:
            try:
                await previous.websocket.close(code=4001, reason="replaced by a new connection")
            except Exception:
                pass
        agents[node_id] = connected
    now = utc_now()
    execute(
        """
        INSERT INTO nodes(node_id,name,platform,codex_version,status,current_task_id,workspaces_json,metadata_json,connected_at,last_seen_at)
        VALUES(?,?,?,?,?,NULL,?,?,?,?)
        ON CONFLICT(node_id) DO UPDATE SET name=excluded.name,platform=excluded.platform,codex_version=excluded.codex_version,
        status='online',workspaces_json=excluded.workspaces_json,metadata_json=excluded.metadata_json,connected_at=excluded.connected_at,last_seen_at=excluded.last_seen_at
        """,
        (node_id, name, str(node.get("platform", ""))[:120], str(node.get("codex_version", ""))[:120], "online", compact_json(node.get("workspaces", [])), compact_json(node.get("metadata", {})), now, now),
    )
    audit(None, "node.connected", "node", node_id, {"name": name})
    return connected


async def handle_agent_message(connected: ConnectedAgent, message: dict[str, Any]) -> None:
    kind = str(message.get("type", ""))
    now = utc_now()
    execute("UPDATE nodes SET last_seen_at=?, status='online' WHERE node_id=?", (now, connected.node_id))
    if kind == "heartbeat":
        return
    if kind in {"thread.detail", "thread.detail.error"}:
        request_id = str(message.get("request_id", ""))
        future = connected.pending_requests.pop(request_id, None)
        if future and not future.done():
            future.set_result(message)
        return
    if kind == "codex.threads":
        raw_threads = message.get("threads")
        if not isinstance(raw_threads, list):
            return
        rows: list[tuple[Any, ...]] = []
        for thread in raw_threads[:100]:
            if not isinstance(thread, dict):
                continue
            thread_id = str(thread.get("id", "")).strip()[:160]
            if not thread_id:
                continue
            status_value = thread.get("status")
            runtime_status = status_value.get("type", "notLoaded") if isinstance(status_value, dict) else status_value
            source_value = thread.get("source")
            source = compact_json(source_value, 300) if isinstance(source_value, (dict, list)) else str(source_value or "")[:300]
            try:
                created_at_epoch = max(0, int(thread.get("createdAt", 0) or 0))
                updated_at_epoch = max(0, int(thread.get("updatedAt", 0) or 0))
            except (TypeError, ValueError):
                continue
            rows.append((
                connected.node_id,
                thread_id,
                str(thread.get("sessionId", ""))[:160] or None,
                str(thread.get("name", ""))[:300] or None,
                str(thread.get("preview", ""))[:2000],
                str(thread.get("cwd", ""))[:1000] or None,
                source,
                str(runtime_status or "notLoaded")[:80],
                str(thread.get("historyMode", ""))[:80] or None,
                str(thread.get("forkedFromId", ""))[:160] or None,
                created_at_epoch,
                updated_at_epoch,
                now,
            ))
        if rows:
            with db() as connection:
                connection.executemany(
                    """
                    INSERT INTO codex_threads(node_id,thread_id,session_id,name,preview,cwd,source,runtime_status,history_mode,forked_from_id,created_at_epoch,updated_at_epoch,last_seen_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(node_id,thread_id) DO UPDATE SET session_id=excluded.session_id,name=excluded.name,
                    preview=excluded.preview,cwd=excluded.cwd,source=excluded.source,runtime_status=excluded.runtime_status,
                    history_mode=excluded.history_mode,forked_from_id=excluded.forked_from_id,
                    created_at_epoch=excluded.created_at_epoch,updated_at_epoch=excluded.updated_at_epoch,last_seen_at=excluded.last_seen_at
                    """,
                    rows,
                )
        await connected.send({"type": "codex.threads.ack", "count": len(rows), "server_time": now})
        return
    task_id = int(message.get("task_id", 0) or 0)
    task = fetch_one("SELECT id,status FROM tasks WHERE id=? AND node_id=?", (task_id, connected.node_id)) if task_id else None
    if not task:
        return
    if task["status"] in {"completed", "failed", "cancelled"} and kind != "task.cancelled":
        return
    if kind == "task.started":
        continuation_mode = str(message.get("continuation_mode", "resume"))
        if continuation_mode not in {"new", "resume", "fork", "handoff"}:
            continuation_mode = "resume"
        source_thread_id = str(message.get("source_thread_id") or "").strip()[:160] or None
        stage, started_message = {
            "new": ("Codex 已启动", "执行节点已接收任务"),
            "resume": ("已连接原会话", "已直接续写原 Codex 会话"),
            "fork": ("已建立安全续接分支", "原会话正被占用，已从完整历史建立安全续接分支"),
            "handoff": ("已建立上下文续接会话", "原会话无法直接写入，已用最近上下文建立可控续接会话"),
        }[continuation_mode]
        execute(
            """UPDATE tasks SET status='running', progress=5, stage=?, thread_id=?,
            source_thread_id=COALESCE(source_thread_id,?), continuation_mode=?, started_at=? WHERE id=?""",
            (stage, message.get("thread_id"), source_thread_id, continuation_mode, now, task_id),
        )
        execute("UPDATE nodes SET current_task_id=? WHERE node_id=?", (task_id, connected.node_id))
        event(task_id, "started", started_message, {"continuation_mode": continuation_mode, "source_thread_id": source_thread_id})
    elif kind == "task.progress":
        progress = max(0, min(99, int(message.get("progress", 0))))
        stage = str(message.get("stage", "执行中"))[:240]
        execute("UPDATE tasks SET status='running', progress=?, stage=?, turn_id=COALESCE(?,turn_id) WHERE id=?", (progress, stage, message.get("turn_id"), task_id))
    elif kind == "task.event":
        event(task_id, str(message.get("kind", "event")), str(message.get("message", "任务状态已更新")), message.get("payload"))
    elif kind == "approval.request":
        method = str(message.get("method", "unknown"))[:200]
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        rpc_id = str(message.get("rpc_id", ""))[:240]
        category, title, risk, command_text, reason = approval_summary(method, params)
        try:
            approval_id = execute(
                """INSERT INTO approvals(task_id,node_id,rpc_id,method,category,title,command_text,risk,reason,payload_json,status,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)""",
                (task_id, connected.node_id, rpc_id, method, category, title, command_text[:MAX_EVENT_TEXT] if command_text else None, risk, reason[:2000] if reason else None, compact_json(params), now),
            )
        except sqlite3.IntegrityError:
            return
        execute("UPDATE tasks SET status='waiting_approval', stage='等待管理员审批' WHERE id=?", (task_id,))
        event(task_id, "approval_required", f"需要审批：{title}", {"approval_id": approval_id, "risk": risk})
        audit(None, "approval.requested", "approval", approval_id, {"task_id": task_id, "method": method})
    elif kind == "task.completed":
        if task["status"] == "cancelled":
            execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=?", (connected.node_id,))
            return
        result = str(message.get("result", ""))[:MAX_EVENT_TEXT]
        execute("UPDATE tasks SET status='completed', progress=100, stage='已完成', result=?, completed_at=? WHERE id=?", (result, now, task_id))
        execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=?", (connected.node_id,))
        event(task_id, "completed", "任务执行完成")
    elif kind == "task.failed":
        if task["status"] == "cancelled":
            execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=?", (connected.node_id,))
            return
        error = str(message.get("error", "未知错误"))[:MAX_EVENT_TEXT]
        execute("UPDATE tasks SET status='failed', stage='执行失败', error=?, completed_at=? WHERE id=?", (error, now, task_id))
        execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=?", (connected.node_id,))
        event(task_id, "failed", error)
    elif kind == "task.cancelled":
        execute("UPDATE tasks SET status='cancelled', stage='已取消', completed_at=COALESCE(completed_at,?) WHERE id=?", (now, task_id))
        execute("UPDATE nodes SET current_task_id=NULL WHERE node_id=?", (connected.node_id,))


@app.websocket("/ws/agent")
async def agent_socket(websocket: WebSocket) -> None:
    token = bearer_value(websocket.headers.get("authorization"))
    if not agent_token_valid(token):
        await websocket.close(code=4401, reason="unauthorized")
        return
    await websocket.accept()
    connected: ConnectedAgent | None = None
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=15)
        if hello.get("type") != "hello":
            await websocket.close(code=4400, reason="hello required")
            return
        connected = await register_agent(websocket, hello)
        await connected.send({"type": "hello.ack", "server_time": utc_now()})
        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict):
                await handle_agent_message(connected, message)
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        if connected:
            for future in connected.pending_requests.values():
                if not future.done():
                    future.set_exception(ConnectionError("agent disconnected"))
            connected.pending_requests.clear()
            async with agents_lock:
                if agents.get(connected.node_id) is connected:
                    agents.pop(connected.node_id, None)
            active_node = fetch_one("SELECT current_task_id FROM nodes WHERE node_id=?", (connected.node_id,))
            active_task_id = active_node["current_task_id"] if active_node else None
            if active_task_id:
                now = utc_now()
                execute(
                    "UPDATE tasks SET status='failed', stage='执行节点连接中断', error='执行节点连接中断', completed_at=? WHERE id=? AND status IN ('queued','running','waiting_approval')",
                    (now, active_task_id),
                )
                execute("UPDATE approvals SET status='expired', decision='cancel', decided_at=? WHERE task_id=? AND status='pending'", (now, active_task_id))
                event(active_task_id, "failed", "执行节点连接中断")
            execute("UPDATE nodes SET status='offline', current_task_id=NULL, last_seen_at=? WHERE node_id=?", (utc_now(), connected.node_id))
            audit(None, "node.disconnected", "node", connected.node_id)


@app.websocket("/ws/relay")
async def codex_network_relay(websocket: WebSocket, host: str = "", port: int = 443) -> None:
    """Relay end-to-end encrypted Codex traffic for authenticated execution nodes only."""
    token = bearer_value(websocket.headers.get("authorization"))
    if not agent_token_valid(token):
        await websocket.close(code=4401, reason="unauthorized")
        return
    if not relay_target_allowed(host, port):
        await websocket.close(code=4403, reason="target denied")
        return

    await websocket.accept()
    acquired = False
    writer: asyncio.StreamWriter | None = None
    relay_tasks: set[asyncio.Task[Any]] = set()
    try:
        await asyncio.wait_for(relay_slots.acquire(), timeout=10)
        acquired = True
        reader, writer = await open_public_relay_target(host, port)
        await websocket.send_text("ready")

        async def websocket_to_target() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                data = message.get("bytes")
                if not isinstance(data, bytes):
                    raise ValueError("binary frames required")
                writer.write(data)
                await writer.drain()

        async def target_to_websocket() -> None:
            while True:
                data = await reader.read(RELAY_BUFFER_SIZE)
                if not data:
                    return
                await websocket.send_bytes(data)

        relay_tasks = {
            asyncio.create_task(websocket_to_target()),
            asyncio.create_task(target_to_websocket()),
        }
        _, pending = await asyncio.wait(relay_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*relay_tasks, return_exceptions=True)
    except (asyncio.TimeoutError, ConnectionError, OSError, ValueError):
        try:
            await websocket.close(code=1011, reason="relay unavailable")
        except RuntimeError:
            pass
    finally:
        for task in relay_tasks:
            if not task.done():
                task.cancel()
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
        if acquired:
            relay_slots.release()


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
