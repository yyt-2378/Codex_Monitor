from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

DEMO_USERNAME = "demo"
DEMO_PASSWORD = "codex-monitor-demo"
DEMO_AGENT_TOKEN = "demo-agent-token-not-for-production-use-0001"


def demo_thread_detail(thread_id: str) -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "has_more": False,
        "turns": [
            {
                "id": "turn_demo_1",
                "status": "completed",
                "messages": [
                    {
                        "role": "user",
                        "text": "请检查这个项目的测试失败原因，并给出最小修复方案。",
                    },
                    {
                        "role": "assistant",
                        "text": (
                            "## 检查结果\n\n"
                            "问题来自配置加载顺序，而不是业务逻辑。\n\n"
                            "- 已定位到初始化阶段\n"
                            "- 已补充回归测试\n"
                            "- 正在等待执行测试命令的人工批准\n\n"
                            "```bash\npytest -q\n```"
                        ),
                    },
                ],
            }
        ],
    }


def seed_demo_data(server: Any) -> None:
    """Populate a disposable database with synthetic, non-user demo data."""

    now = datetime.now(timezone.utc)
    now_text = now.isoformat(timespec="seconds")
    owner = server.fetch_one("SELECT id FROM users WHERE username = ?", (DEMO_USERNAME,))
    if not owner:
        server.bootstrap_admin()
        owner = server.fetch_one("SELECT id FROM users WHERE username = ?", (DEMO_USERNAME,))
    if not owner:
        raise RuntimeError("demo owner was not created")
    owner_id = int(owner["id"])

    server.execute(
        """
        INSERT INTO nodes(node_id,name,platform,codex_version,status,current_task_id,workspaces_json,metadata_json,connected_at,last_seen_at)
        VALUES(?,?,?,?,?,NULL,?,?,?,?)
        ON CONFLICT(node_id) DO UPDATE SET status='online',last_seen_at=excluded.last_seen_at
        """,
        (
            "demo-workstation",
            "开发工作站",
            "Windows 11",
            "codex-cli (demo)",
            "online",
            '["C:/Projects/codex-monitor","C:/Projects/sample-app"]',
            '{"mode":"demo","transport":"stdio"}',
            now_text,
            now_text,
        ),
    )

    if not server.fetch_one("SELECT id FROM tasks LIMIT 1"):
        running_id = server.execute(
            """
            INSERT INTO tasks(title,prompt,node_id,cwd,status,progress,stage,thread_id,source_thread_id,continuation_mode,turn_id,result,error,created_by,created_at,started_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "检查测试失败并生成修复建议",
                "运行测试，定位失败原因；任何命令执行前都需要请求批准。",
                "demo-workstation",
                "C:/Projects/sample-app",
                "waiting_approval",
                62,
                "等待批准测试命令",
                "thread_demo_active",
                None,
                "new",
                "turn_demo_active",
                None,
                None,
                owner_id,
                (now - timedelta(minutes=7)).isoformat(timespec="seconds"),
                (now - timedelta(minutes=6)).isoformat(timespec="seconds"),
                None,
            ),
        )
        completed_id = server.execute(
            """
            INSERT INTO tasks(title,prompt,node_id,cwd,status,progress,stage,thread_id,source_thread_id,continuation_mode,turn_id,result,error,created_by,created_at,started_at,completed_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "整理项目架构说明",
                "阅读代码结构并生成一份简明的 Markdown 架构说明。",
                "demo-workstation",
                "C:/Projects/codex-monitor",
                "completed",
                100,
                "已完成",
                "thread_demo_complete",
                None,
                "new",
                "turn_demo_complete",
                "## 已完成\n\n已生成组件边界、数据流和安全说明。\n\n| 模块 | 职责 |\n| --- | --- |\n| Agent | 连接本机 Codex |\n| Hub | 状态与审批路由 |\n| Web | 移动端控制界面 |",
                None,
                owner_id,
                (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                (now - timedelta(hours=2, minutes=-1)).isoformat(timespec="seconds"),
                (now - timedelta(hours=1, minutes=48)).isoformat(timespec="seconds"),
            ),
        )
        server.execute(
            """
            INSERT INTO approvals(task_id,node_id,rpc_id,method,category,title,command_text,risk,reason,payload_json,status,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                running_id,
                "demo-workstation",
                "rpc_demo_approval",
                "item/commandExecution/requestApproval",
                "命令执行",
                "运行项目测试",
                "python -m pytest -q",
                "medium",
                "需要验证修复是否通过全部测试",
                '{"command":["python","-m","pytest","-q"],"cwd":"C:/Projects/sample-app"}',
                "pending",
                (now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            ),
        )
        event_rows = [
            (running_id, "created", "任务已创建", now - timedelta(minutes=7)),
            (running_id, "started", "Codex 已开始分析项目", now - timedelta(minutes=6)),
            (running_id, "progress", "已定位到配置加载顺序", now - timedelta(minutes=3)),
            (running_id, "approval_required", "需要批准：运行项目测试", now - timedelta(minutes=1)),
            (completed_id, "created", "任务已创建", now - timedelta(hours=2)),
            (completed_id, "completed", "Markdown 架构说明已生成", now - timedelta(hours=1, minutes=48)),
        ]
        for task_id, kind, message, created_at in event_rows:
            server.execute(
                "INSERT INTO task_events(task_id,kind,message,payload_json,created_at) VALUES(?,?,?,?,?)",
                (task_id, kind, message, "{}", created_at.isoformat(timespec="seconds")),
            )

    threads = [
        (
            "thread_demo_active",
            "测试失败诊断",
            "请检查这个项目的测试失败原因，并给出最小修复方案。",
            "C:/Projects/sample-app",
            "active",
            int((now - timedelta(minutes=1)).timestamp()),
        ),
        (
            "thread_demo_complete",
            "Codex Monitor 架构整理",
            "整理独立组件、数据流与安全边界。",
            "C:/Projects/codex-monitor",
            "notLoaded",
            int((now - timedelta(hours=1, minutes=48)).timestamp()),
        ),
        (
            "thread_demo_history",
            "移动端审批界面优化",
            "优化小屏幕下的 Markdown、表格和审批按钮。",
            "C:/Projects/codex-monitor",
            "notLoaded",
            int((now - timedelta(days=1)).timestamp()),
        ),
    ]
    for thread_id, name, preview, cwd, runtime_status, updated_at in threads:
        server.execute(
            """
            INSERT INTO codex_threads(node_id,thread_id,session_id,name,preview,cwd,source,runtime_status,history_mode,forked_from_id,created_at_epoch,updated_at_epoch,last_seen_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(node_id,thread_id) DO UPDATE SET name=excluded.name,preview=excluded.preview,runtime_status=excluded.runtime_status,updated_at_epoch=excluded.updated_at_epoch,last_seen_at=excluded.last_seen_at
            """,
            (
                "demo-workstation",
                thread_id,
                None,
                name,
                preview,
                cwd,
                "demo",
                runtime_status,
                "persisted",
                None,
                updated_at - 600,
                updated_at,
                now_text,
            ),
        )

