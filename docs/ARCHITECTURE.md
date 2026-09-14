# Architecture

Codex Monitor is intentionally split into a control plane and an execution plane.

```text
Browser / PWA
    │ HTTP(S)
    ▼
Monitor server (FastAPI + SQLite)
    ▲ authenticated WebSocket
    │
Local agent
    │ local stdio JSONL
    ▼
Codex app-server running as the installing OS user
```

## Trust boundaries

- The local agent is the only component that launches `codex app-server`.
- Codex credentials and local session files remain on the execution machine.
- The server stores UI accounts, task metadata, approval records, and a limited event history.
- Allowed workspaces constrain the directories selectable from the web UI.
- The agent initiates the outbound connection; an execution machine does not need a public inbound port.

## Conversation modes

- **Observed session:** discovered from local Codex history. It is read-only unless the user explicitly continues it.
- **Managed session:** started by Codex Monitor. Streaming events, cancellation, and approvals are tracked end to end.
- **Safe continuation:** if another Codex client owns the writer lock, the agent forks or creates a context handoff instead of corrupting the original session.

## Current scope

Version 0.1 is a single-owner, self-hosted technical preview. The package is reusable by different people because every installation uses that person's local Codex process and local data. A shared hosted multi-tenant service requires per-user device enrollment, tenant-scoped storage, token rotation, and stronger privacy controls; it is deliberately not claimed as complete here.

## Protocol source

The adapter follows the documented Codex App Server lifecycle: `initialize`, thread start/resume/fork/read/list, turn start/steer/interrupt, streamed item notifications, and server-initiated approval requests.

