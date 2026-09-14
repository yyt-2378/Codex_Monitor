"use strict";

const BASE_PATH = document.documentElement.dataset.basePath || "";

const state = {
  csrf: "",
  user: null,
  snapshot: null,
  view: "overview",
  taskFilter: "all",
  pollTimer: null,
  toastTimer: null,
  selectedThread: null,
  sessionRefreshTimer: null,
  sessionRequestToken: 0,
  selectedTaskId: null,
};

const $ = selector => document.querySelector(selector);
const $$ = selector => Array.from(document.querySelectorAll(selector));

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("data-")) node.setAttribute(key, String(value));
    else if (key === "disabled") node.disabled = Boolean(value);
    else node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function safeLinkTarget(value) {
  try {
    const url = new URL(String(value || ""), location.origin);
    return ["http:", "https:", "mailto:"].includes(url.protocol) ? url.href : null;
  } catch (_) {
    return null;
  }
}

function appendInlineMarkdown(parent, value) {
  const text = String(value || "");
  const pattern = /(`[^`\n]+`|\[[^\]\n]+\]\((?:https?:\/\/|mailto:)[^\s)]+\)|\*\*[^*\n]+\*\*|__[^_\n]+__|~~[^~\n]+~~|\*[^*\n]+\*|_[^_\n]+_|https?:\/\/[^\s<]+)/g;
  let cursor = 0;
  for (const match of text.matchAll(pattern)) {
    if (match.index > cursor) parent.append(document.createTextNode(text.slice(cursor, match.index)));
    const token = match[0];
    if (token.startsWith("`")) {
      parent.append(element("code", {text: token.slice(1, -1)}));
    } else if (token.startsWith("[")) {
      const parts = token.match(/^\[([^\]]+)\]\((.+)\)$/);
      const href = parts ? safeLinkTarget(parts[2]) : null;
      if (parts && href) {
        parent.append(element("a", {href, target: "_blank", rel: "noopener noreferrer", text: parts[1]}));
      } else {
        parent.append(document.createTextNode(token));
      }
    } else if (token.startsWith("**") || token.startsWith("__")) {
      const strong = element("strong");
      appendInlineMarkdown(strong, token.slice(2, -2));
      parent.append(strong);
    } else if (token.startsWith("~~")) {
      const deleted = element("del");
      appendInlineMarkdown(deleted, token.slice(2, -2));
      parent.append(deleted);
    } else if (token.startsWith("*") || token.startsWith("_")) {
      const emphasis = element("em");
      appendInlineMarkdown(emphasis, token.slice(1, -1));
      parent.append(emphasis);
    } else {
      let rawUrl = token;
      let punctuation = "";
      while (/[.,;:!?，。；：！？]$/.test(rawUrl)) {
        punctuation = rawUrl.slice(-1) + punctuation;
        rawUrl = rawUrl.slice(0, -1);
      }
      const href = safeLinkTarget(rawUrl);
      parent.append(href
        ? element("a", {href, target: "_blank", rel: "noopener noreferrer", text: rawUrl})
        : document.createTextNode(rawUrl));
      if (punctuation) parent.append(document.createTextNode(punctuation));
    }
    cursor = match.index + token.length;
  }
  if (cursor < text.length) parent.append(document.createTextNode(text.slice(cursor)));
}

function inlineMarkdown(tag, text, attrs = {}) {
  const node = element(tag, attrs);
  appendInlineMarkdown(node, text);
  return node;
}

function splitMarkdownTableRow(line) {
  let value = String(line || "").trim();
  if (value.startsWith("|")) value = value.slice(1);
  if (value.endsWith("|")) value = value.slice(0, -1);
  const escapedPipe = "\u0000CODEX_MONITOR_PIPE\u0000";
  return value.replace(/\\\|/g, escapedPipe).split("|").map(cell => cell.trim().replaceAll(escapedPipe, "|"));
}

function isMarkdownTableSeparator(line) {
  const cells = splitMarkdownTableRow(line);
  return cells.length > 1 && cells.every(cell => /^:?-{3,}:?$/.test(cell.replace(/\s+/g, "")));
}

function codeBlock(source, language = "") {
  const copy = element("button", {class: "markdown-copy", type: "button", text: "复制"});
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(source);
      copy.textContent = "已复制";
      setTimeout(() => copy.textContent = "复制", 1400);
    } catch (_) {
      toast("复制失败，请长按选择内容", "error");
    }
  });
  return element("div", {class: "markdown-code"},
    element("div", {class: "markdown-code-head"},
      element("span", {text: language || "代码"}),
      copy,
    ),
    element("pre", {}, element("code", {text: source})),
  );
}

function renderMarkdown(value) {
  const root = element("div", {class: "markdown-body"});
  const lines = String(value || "").replace(/\r\n?/g, "\n").split("\n");
  let index = 0;

  const beginsBlock = at => {
    const line = lines[at] || "";
    return !line.trim()
      || /^\s*(```|~~~)/.test(line)
      || /^\s{0,3}#{1,4}\s+/.test(line)
      || /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)
      || /^\s*>/.test(line)
      || /^\s*(?:[-+*]|\d+[.)])\s+/.test(line)
      || (line.includes("|") && isMarkdownTableSeparator(lines[at + 1] || ""));
  };

  while (index < lines.length) {
    const line = lines[index];
    if (!line.trim()) {
      index += 1;
      continue;
    }

    const fence = line.match(/^\s*(```|~~~)\s*([^\s`]*)\s*$/);
    if (fence) {
      const closing = fence[1];
      const body = [];
      index += 1;
      while (index < lines.length && !new RegExp(`^\\s*${closing}\\s*$`).test(lines[index])) {
        body.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      root.append(codeBlock(body.join("\n"), fence[2]));
      continue;
    }

    const heading = line.match(/^\s{0,3}(#{1,4})\s+(.+?)\s*#*\s*$/);
    if (heading) {
      root.append(inlineMarkdown(`h${heading[1].length}`, heading[2]));
      index += 1;
      continue;
    }

    if (/^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
      root.append(element("hr"));
      index += 1;
      continue;
    }

    if (line.includes("|") && isMarkdownTableSeparator(lines[index + 1] || "")) {
      const header = splitMarkdownTableRow(line);
      const rows = [];
      index += 2;
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
        rows.push(splitMarkdownTableRow(lines[index]));
        index += 1;
      }
      const table = element("table", {class: "markdown-table"},
        element("thead", {}, element("tr", {}, ...header.map(cell => inlineMarkdown("th", cell)))),
        element("tbody", {}, ...rows.map(row => element("tr", {}, ...header.map((_, cellIndex) => inlineMarkdown("td", row[cellIndex] || ""))))),
      );
      root.append(element("div", {class: "markdown-table-wrap", tabindex: "0", role: "region", "aria-label": "可横向滚动的表格"}, table));
      continue;
    }

    const listMatch = line.match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
    if (listMatch) {
      const ordered = Boolean(listMatch[2]);
      const list = element(ordered ? "ol" : "ul");
      while (index < lines.length) {
        const item = lines[index].match(/^\s*(?:([-+*])|(\d+)[.)])\s+(.+)$/);
        if (!item || Boolean(item[2]) !== ordered) break;
        list.append(inlineMarkdown("li", item[3]));
        index += 1;
      }
      root.append(list);
      continue;
    }

    if (/^\s*>/.test(line)) {
      const quote = [];
      while (index < lines.length && /^\s*>/.test(lines[index])) {
        quote.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      const blockquote = element("blockquote");
      appendInlineMarkdown(blockquote, quote.join("\n"));
      root.append(blockquote);
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length && !beginsBlock(index)) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    root.append(inlineMarkdown("p", paragraph.join("\n")));
  }

  if (!root.childNodes.length) root.append(element("p", {class: "markdown-empty", text: "（空）"}));
  return root;
}

function toast(message, type = "ok") {
  const target = $("#toast");
  target.textContent = message;
  target.className = `toast show${type === "error" ? " error" : ""}`;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => target.className = "toast", 2800);
}

async function api(path, options = {}) {
  const method = options.method || "GET";
  const headers = {"Accept": "application/json", ...(options.headers || {})};
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (!/^(GET|HEAD)$/i.test(method) && state.csrf) headers["X-CSRF-Token"] = state.csrf;
  const response = await fetch(`${BASE_PATH}${path}`, {
    method,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    credentials: "same-origin",
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { payload = {}; }
  if (response.status === 401) {
    showLogin();
    throw new Error(payload.detail || "登录已过期");
  }
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

function showLogin() {
  clearTimeout(state.pollTimer);
  state.user = null;
  state.snapshot = null;
  state.csrf = "";
  $("#appShell").hidden = true;
  $("#loginScreen").hidden = false;
  setTimeout(() => $("#loginUsername").focus(), 30);
}

function roleLabel(role) {
  return {admin: "管理员", operator: "操作员", viewer: "观察员"}[role] || role;
}

function statusLabel(status) {
  return {
    queued: "等待中", running: "运行中", waiting_approval: "待审批",
    completed: "已完成", failed: "失败", cancelled: "已取消",
  }[status] || status;
}

function formatTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit"}).format(date);
}

function formatEpoch(value) {
  const seconds = Number(value || 0);
  return seconds > 0 ? formatTime(seconds * 1000) : "—";
}

function showApp(auth) {
  state.user = auth.user;
  state.csrf = auth.csrf_token;
  $("#loginScreen").hidden = true;
  $("#appShell").hidden = false;
  $("#userName").textContent = state.user.display_name;
  $("#userRole").textContent = roleLabel(state.user.role).toUpperCase();
  $("#userAvatar").textContent = state.user.display_name.slice(0, 1).toUpperCase();
  $$('[data-admin-only]').forEach(node => node.hidden = state.user.role !== "admin");
  $("#newTaskButton").hidden = state.user.role === "viewer";
  const initial = location.hash.replace("#", "") || "overview";
  showView(initial === "users" && state.user.role !== "admin" ? "overview" : initial);
  refreshSnapshot(true);
}

const titles = {overview: "任务总览", tasks: "受控任务", sessions: "Codex 会话", approvals: "人工审批", nodes: "执行节点", users: "成员权限", audit: "审计记录"};

function showView(view) {
  if (!titles[view]) view = "overview";
  if ((view === "users" || view === "audit") && state.user?.role !== "admin") view = "overview";
  state.view = view;
  location.hash = view;
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `view-${view}`));
  $$('[data-view]').forEach(node => node.classList.toggle("active", node.dataset.view === view));
  $("#viewTitle").textContent = titles[view];
  if (view === "users") refreshUsers();
  if (view === "audit") refreshAudit();
  window.scrollTo({top: 0, behavior: "smooth"});
}

function emptyState(title, detail, symbol = "·") {
  return element("div", {class: "empty-state"},
    element("div", {class: "empty-symbol", text: symbol}),
    element("strong", {text: title}),
    element("span", {text: detail}),
  );
}

function progressNode(value) {
  const bar = element("i");
  bar.style.width = `${Math.max(0, Math.min(100, Number(value) || 0))}%`;
  return element("div", {class: "progress-track"}, bar);
}

function miniTask(task) {
  const card = element("button", {class: "mini-task", type: "button"},
    element("div", {class: "mini-task-head"},
      element("div", {}, element("h3", {text: task.title}), element("div", {class: "metadata", text: `${task.node_id} · ${formatTime(task.created_at)}`})),
      element("span", {class: `status ${task.status}`, text: statusLabel(task.status)}),
    ),
    progressNode(task.progress),
    element("div", {class: "progress-label"}, element("span", {text: task.stage}), element("span", {text: `${task.progress}%`})),
  );
  card.addEventListener("click", () => openTaskDetail(task.id));
  return card;
}

function codexActivityLabel(value) {
  return {active: "受控执行中", recent: "最近活动", idle: "历史会话"}[value] || "历史会话";
}

function threadSourceKind(thread) {
  const source = thread?.source || "";
  if (typeof source === "object") return String(source.kind || source.type || "codex");
  try {
    const parsed = JSON.parse(source);
    return String(parsed.kind || parsed.type || source);
  } catch (_) {
    return String(source || "codex");
  }
}

function isExternalThread(thread) {
  if (thread?.forked_from_id) return false;
  const source = threadSourceKind(thread).toLowerCase();
  return ["vscode", "cli"].includes(source) || thread?.history_mode === "paginated";
}

function miniSession(thread) {
  const title = thread.name || thread.preview || "未命名 Codex 会话";
  const card = element("button", {class: "mini-task", type: "button"},
    element("div", {class: "mini-task-head"},
      element("div", {}, element("h3", {text: title}), element("div", {class: "metadata", text: `${thread.node_name} · ${formatEpoch(thread.updated_at_epoch)}`})),
      element("span", {class: `session-state ${thread.activity_state}`, text: codexActivityLabel(thread.activity_state)}),
    ),
    element("div", {class: "progress-label"}, element("span", {text: thread.cwd || "未报告工作目录"}), element("span", {text: thread.source || "Codex"})),
  );
  card.addEventListener("click", () => openSession(thread));
  return card;
}

function renderOverview() {
  const snap = state.snapshot;
  $("#statActive").textContent = snap.stats.active || 0;
  $("#statPending").textContent = snap.stats.pending_approvals || 0;
  $("#statNodes").textContent = snap.stats.online_nodes || 0;
  $("#statSessions").textContent = snap.stats.recent_codex_threads || 0;

  const activeTasks = snap.tasks.filter(task => ["queued", "running", "waiting_approval"].includes(task.status)).slice(0, 5);
  const taskBox = $("#overviewTasks");
  taskBox.replaceChildren(...(activeTasks.length ? activeTasks.map(miniTask) : [emptyState("暂时没有运行中的任务", "连接执行节点后即可从这里下发任务", "⌁")]));

  const pending = snap.approvals.filter(item => item.status === "pending").slice(0, 4);
  const approvalBox = $("#overviewApprovals");
  approvalBox.replaceChildren(...(pending.length ? pending.map(item => {
    const card = element("button", {class: "mini-task", type: "button"},
      element("div", {class: "mini-task-head"},
        element("div", {}, element("h3", {text: item.title}), element("div", {class: "metadata", text: item.task_title})),
        element("span", {class: `risk ${item.risk}`, text: item.risk === "high" ? "高风险" : "需确认"}),
      ),
      element("div", {class: "progress-label"}, element("span", {text: item.node_id}), element("span", {text: formatTime(item.created_at)})),
    );
    card.addEventListener("click", () => showView("approvals"));
    return card;
  }) : [emptyState("没有待审批请求", "Codex 需要授权时会立即出现在这里", "◇")]));

  const recentSessions = (snap.codex_threads || []).filter(item => ["active", "recent"].includes(item.activity_state)).slice(0, 5);
  $("#overviewSessions").replaceChildren(...(recentSessions.length
    ? recentSessions.map(miniSession)
    : [emptyState("没有同步到最近的 Codex 活动", "启动电脑上的 Codex Monitor Agent 后，当前与最近会话会显示在这里", "◉")]));
}

function taskCard(task) {
  const latest = task.events?.[0];
  const card = element("article", {class: "task-card interactive-card", tabindex: "0", role: "button", "aria-label": `查看任务进度：${task.title}`});
  card.addEventListener("click", () => openTaskDetail(task.id));
  card.addEventListener("keydown", event => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openTaskDetail(task.id);
    }
  });
  card.append(
    element("div", {class: "task-head"},
      element("div", {class: "task-title-block"},
        element("span", {class: "task-number", text: String(task.id).padStart(2, "0")}),
        element("div", {}, element("h3", {text: task.title}), element("div", {class: "metadata", text: `由 ${task.creator_name} 创建 · ${formatTime(task.created_at)}`})),
      ),
      element("span", {class: `status ${task.status}`, text: statusLabel(task.status)}),
    ),
    progressNode(task.progress),
    element("div", {class: "progress-label"}, element("span", {text: task.stage}), element("span", {text: `${task.progress}%`})),
    element("div", {class: "task-details"},
      element("div", {class: "detail"}, element("span", {text: "执行节点"}), element("strong", {text: task.node_id})),
      element("div", {class: "detail"}, element("span", {text: "启动时间"}), element("strong", {text: formatTime(task.started_at)})),
      element("div", {class: "detail"}, element("span", {text: "待审批"}), element("strong", {text: String(task.pending_approvals || 0)})),
    ),
  );
  if (latest) card.append(element("div", {class: "event-line", text: `${latest.message} · ${formatTime(latest.created_at)}`}));
  if (["queued", "running", "waiting_approval"].includes(task.status) && state.user.role !== "viewer") {
    const cancel = element("button", {class: "button ghost", type: "button", text: "取消任务"});
    cancel.addEventListener("click", event => {
      event.stopPropagation();
      cancelTask(task.id, task.title);
    });
    card.append(element("div", {class: "task-actions"}, cancel));
  }
  return card;
}

function renderTasks() {
  let tasks = state.snapshot.tasks;
  if (state.taskFilter === "active") tasks = tasks.filter(task => ["queued", "running", "waiting_approval"].includes(task.status));
  else if (state.taskFilter !== "all") tasks = tasks.filter(task => task.status === state.taskFilter);
  $("#taskList").replaceChildren(...(tasks.length ? tasks.map(taskCard) : [emptyState("没有符合条件的任务", "可以切换筛选条件或创建新任务", "▤")]));
}

function codexSessionCard(thread) {
  const title = thread.name || thread.preview || "未命名 Codex 会话";
  const preview = thread.preview && thread.preview !== thread.name ? thread.preview : "该会话暂时没有可用摘要。";
  const external = isExternalThread(thread);
  const card = element("button", {class: "session-card", type: "button", "aria-label": `打开 Codex 会话：${title}`},
    element("div", {class: "session-head"},
      element("div", {}, element("p", {class: "eyebrow", text: threadSourceKind(thread).toUpperCase()}), element("h3", {text: title})),
      element("span", {class: `session-state ${thread.activity_state}`, text: codexActivityLabel(thread.activity_state)}),
    ),
    element("p", {class: "session-preview", text: preview}),
    element("div", {class: "session-details"},
      element("div", {}, element("span", {text: "执行电脑"}), element("strong", {text: thread.node_name || thread.node_id})),
      element("div", {}, element("span", {text: "最近更新"}), element("strong", {text: formatEpoch(thread.updated_at_epoch)})),
      element("div", {}, element("span", {text: "控制方式"}), element("strong", {text: thread.remote_task_id ? `受控任务 #${thread.remote_task_id}` : external ? "外部历史 · 自动安全续接" : "网页可控会话"})),
    ),
    element("pre", {class: "workspace-list", text: thread.cwd || "未报告工作目录"}),
  );
  card.addEventListener("click", () => openSession(thread));
  return card;
}

function renderSessions() {
  const threads = state.snapshot.codex_threads || [];
  $("#codexSessionList").replaceChildren(...(threads.length
    ? threads.map(codexSessionCard)
    : [emptyState("尚未同步本机 Codex 会话", "需要在电脑上运行 Codex Monitor Agent；网页服务器无法直接读取你电脑上的 Codex 登录和本地会话", "◉")]));
}

function renderSessionHeader(thread) {
  const title = thread.name || thread.preview || "未命名 Codex 会话";
  $("#sessionDialogTitle").textContent = title;
  $("#sessionDialogMeta").replaceChildren(
    element("span", {text: thread.node_name || thread.node_id}),
    element("span", {text: thread.cwd || "未报告工作目录"}),
    element("span", {text: `最近更新 ${formatEpoch(thread.updated_at_epoch)}`}),
  );
  const composer = $("#sessionPromptForm");
  const canSend = state.user?.role !== "viewer" && thread.node_status === "online";
  const external = isExternalThread(thread);
  $("#sessionControlHint").textContent = external
    ? "该会话由 VS Code/CLI 持有。发送时会优先尝试原会话；若存在写入锁，将自动建立安全续接副本，原会话不会被修改。"
    : "该会话由控制节点持有，可直接继续并在手机上跟踪。";
  $("#sessionPromptSubmit").textContent = external ? "安全续接并跟踪" : "发送并跟踪进度";
  composer.hidden = !canSend;
  if (!canSend) $("#sessionPromptError").textContent = thread.node_status === "online" ? "当前账号只有查看权限" : "执行节点离线，暂时不能发送指令";
}

function messageBubble(message) {
  const labels = {user: "你", assistant: "Codex", activity: "执行记录"};
  const content = message.role === "activity"
    ? element("pre", {class: "plain-output", text: message.text || ""})
    : renderMarkdown(message.text || "");
  return element("article", {class: `thread-message ${message.role || "activity"}`},
    element("div", {class: "thread-message-head"},
      element("strong", {text: labels[message.role] || "执行记录"}),
      message.status ? element("span", {text: message.status}) : null,
    ),
    content,
  );
}

function renderSessionDetail(detail) {
  const turns = Array.isArray(detail?.turns) ? detail.turns : [];
  const transcript = $("#sessionTranscript");
  const wasNearBottom = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 90;
  const nodes = [];
  for (const turn of turns) {
    const messages = Array.isArray(turn.messages) ? turn.messages : [];
    if (!messages.length) continue;
    nodes.push(element("section", {class: "thread-turn"},
      element("div", {class: "thread-turn-head"},
        element("span", {text: `TURN ${String(turn.id || "").slice(0, 8)}`}),
        element("span", {class: `turn-status ${turn.status || "unknown"}`, text: turn.status || "unknown"}),
      ),
      ...messages.map(messageBubble),
    ));
  }
  if (detail?.has_more) nodes.unshift(element("p", {class: "history-note", text: "已显示最近 6 轮对话"}));
  transcript.replaceChildren(...(nodes.length ? nodes : [emptyState("暂无可显示的消息", "该会话可能还没有完成第一轮对话", "◌")]));
  const latestTurn = turns[turns.length - 1];
  const live = $("#sessionLiveState");
  const running = latestTurn?.status === "inProgress";
  live.className = `session-state ${running ? "active" : "recent"}`;
  live.textContent = running ? "Codex 正在执行" : "消息已同步";
  if (wasNearBottom || !transcript.dataset.loaded) transcript.scrollTop = transcript.scrollHeight;
  transcript.dataset.loaded = "1";
}

async function loadSessionDetail(showLoading = false) {
  const thread = state.selectedThread;
  const dialog = $("#sessionDialog");
  if (!thread || !dialog.open) return;
  const token = ++state.sessionRequestToken;
  clearTimeout(state.sessionRefreshTimer);
  if (showLoading) {
    $("#sessionTranscript").replaceChildren(emptyState("正在读取会话", "正在从执行节点加载最近消息", "◌"));
    $("#sessionLiveState").textContent = "正在读取";
  }
  $("#sessionDetailError").textContent = "";
  try {
    const payload = await api(`/api/codex-threads/${encodeURIComponent(thread.node_id)}/${encodeURIComponent(thread.thread_id)}`);
    if (token !== state.sessionRequestToken || !dialog.open) return;
    renderSessionDetail(payload.detail || {});
  } catch (error) {
    if (token === state.sessionRequestToken) $("#sessionDetailError").textContent = error.message;
  } finally {
    if (token === state.sessionRequestToken && dialog.open) state.sessionRefreshTimer = setTimeout(() => loadSessionDetail(false), 6000);
  }
}

function openSession(thread) {
  state.selectedThread = thread;
  $("#sessionTranscript").removeAttribute("data-loaded");
  $("#sessionPromptForm").reset();
  $("#sessionPromptError").textContent = "";
  renderSessionHeader(thread);
  const dialog = $("#sessionDialog");
  if (!dialog.open) dialog.showModal();
  loadSessionDetail(true);
}

function renderTaskDetail() {
  if (!state.snapshot || !state.selectedTaskId) return;
  const task = state.snapshot.tasks.find(item => item.id === state.selectedTaskId);
  if (!task) return;
  $("#taskDetailTitle").textContent = task.title;
  const summary = $("#taskDetailSummary");
  const continuationNote = {
    fork: "为避免与 VS Code/CLI 的写入锁冲突，本任务已在完整历史分支中继续；原会话未被修改。",
    handoff: "原会话无法由控制节点直接写入，本任务已携带最近对话上下文进入可控续接会话；原会话未被修改。",
    resume: task.source_thread_id ? "本任务已直接连接并续写原 Codex 会话。" : "",
  }[task.continuation_mode] || "";
  const summaryNodes = [
    element("div", {class: "task-detail-status"},
      element("span", {class: `status ${task.status}`, text: statusLabel(task.status)}),
      element("strong", {text: `${task.progress}%`}),
    ),
    progressNode(task.progress),
    element("p", {class: "task-detail-stage", text: task.stage || "等待状态更新"}),
    element("div", {class: "detail-meta"},
      element("span", {text: task.node_id}),
      element("span", {text: task.cwd || "默认工作目录"}),
      element("span", {text: formatTime(task.created_at)}),
    ),
    continuationNote ? element("div", {class: `continuation-note ${task.continuation_mode}`, text: continuationNote}) : null,
    element("div", {class: "task-prompt-block"}, element("strong", {text: "任务指令"}), element("pre", {text: task.prompt || ""})),
    task.result ? element("div", {class: "task-result-block"}, element("strong", {class: "result-label", text: "Codex 结果"}), renderMarkdown(task.result)) : null,
    task.error ? element("div", {class: "task-error-block"}, element("strong", {text: "错误"}), element("pre", {text: task.error})) : null,
  ].filter(Boolean);
  summary.replaceChildren(...summaryNodes);
  const events = Array.isArray(task.events) ? [...task.events].reverse() : [];
  $("#taskDetailEvents").replaceChildren(
    element("h3", {text: "执行时间线"}),
    ...(events.length ? events.map(item => element("div", {class: "detail-event"},
      element("i"),
      element("div", {}, element("strong", {text: item.message}), element("time", {text: formatTime(item.created_at)})),
    )) : [element("p", {class: "history-note", text: "暂无执行记录"})]),
  );
  $("#taskDetailApprovals").hidden = !(task.pending_approvals > 0);
}

function openTaskDetail(taskId) {
  state.selectedTaskId = taskId;
  renderTaskDetail();
  const dialog = $("#taskDetailDialog");
  if (!dialog.open) dialog.showModal();
}

function approvalCard(item) {
  const pending = item.status === "pending";
  const card = element("article", {class: `approval-card ${item.risk}`});
  card.append(
    element("div", {class: "approval-head"},
      element("div", {}, element("p", {class: "eyebrow", text: item.category.toUpperCase()}), element("h3", {text: item.title})),
      element("span", {class: `risk ${item.risk}`, text: item.risk === "high" ? "高风险" : "需确认"}),
    ),
    element("div", {class: "approval-context"},
      element("div", {}, element("span", {text: "所属任务"}), element("strong", {text: item.task_title})),
      element("div", {}, element("span", {text: "执行节点"}), element("strong", {text: item.node_id})),
      element("div", {}, element("span", {text: "请求时间"}), element("strong", {text: formatTime(item.created_at)})),
    ),
  );
  if (item.command_text) card.append(element("pre", {class: "command", text: item.command_text}));
  if (item.reason) card.append(element("p", {class: "approval-reason", text: `原因：${item.reason}`}));
  if (pending && state.user.role !== "viewer") {
    const accept = element("button", {class: "button primary", type: "button", text: "批准本次"});
    const decline = element("button", {class: "button ghost", type: "button", text: "拒绝"});
    const cancel = element("button", {class: "button danger", type: "button", text: "拒绝并终止任务"});
    accept.addEventListener("click", () => decide(item, "accept"));
    decline.addEventListener("click", () => decide(item, "decline"));
    cancel.addEventListener("click", () => decide(item, "cancel"));
    card.append(element("div", {class: "approval-actions"}, decline, accept, cancel));
  } else {
    const label = item.status === "pending" ? "等待有权限的成员处理" : `${item.decided_by_name || "管理员"} 已${{accept:"批准",decline:"拒绝",cancel:"拒绝并终止"}[item.decision] || "处理"}`;
    card.append(element("p", {class: "decision-note", text: label}));
  }
  return card;
}

function renderApprovals() {
  const approvals = state.snapshot.approvals;
  $("#approvalList").replaceChildren(...(approvals.length ? approvals.map(approvalCard) : [emptyState("审批队列为空", "需要人工确认的命令或文件修改会显示在这里", "◇")]));
}

function renderNodes() {
  const nodes = state.snapshot.nodes;
  $("#nodeList").replaceChildren(...(nodes.length ? nodes.map(node => {
    const workspaces = Array.isArray(node.workspaces) && node.workspaces.length ? node.workspaces.join("\n") : "未报告工作目录";
    return element("article", {class: "node-card"},
      element("div", {class: "node-top"},
        element("div", {}, element("p", {class: "eyebrow", text: "CODEX NODE"}), element("h3", {text: node.name})),
        element("span", {class: `node-status ${node.status}`}, element("i"), node.status === "online" ? "在线" : "离线"),
      ),
      element("div", {class: "node-meta"},
        element("div", {}, element("span", {text: "节点标识"}), element("strong", {text: node.node_id})),
        element("div", {}, element("span", {text: "系统"}), element("strong", {text: node.platform || "—"})),
        element("div", {}, element("span", {text: "Codex"}), element("strong", {text: node.codex_version || "—"})),
        element("div", {}, element("span", {text: "最后心跳"}), element("strong", {text: formatTime(node.last_seen_at)})),
      ),
      element("pre", {class: "workspace-list", text: workspaces}),
    );
  }) : [emptyState("尚未连接执行节点", "在电脑上启动 Codex Monitor Agent 后会自动出现在这里", "⌘")]));
}

function renderBadges() {
  const count = Number(state.snapshot.stats.pending_approvals || 0);
  for (const badge of [$("#approvalBadge"), $("#mobileApprovalBadge")]) {
    badge.textContent = String(count);
    badge.hidden = count === 0;
  }
}

function renderAll() {
  renderOverview();
  renderTasks();
  renderSessions();
  renderApprovals();
  renderNodes();
  renderBadges();
  populateNodeSelect();
  if ($("#taskDetailDialog").open) renderTaskDetail();
  if ($("#sessionDialog").open && state.selectedThread) {
    const updated = state.snapshot.codex_threads.find(item => item.node_id === state.selectedThread.node_id && item.thread_id === state.selectedThread.thread_id);
    if (updated) {
      state.selectedThread = updated;
      renderSessionHeader(updated);
    }
  }
}

async function refreshSnapshot(initial = false) {
  try {
    const payload = await api("/api/snapshot");
    const oldPending = Number(state.snapshot?.stats?.pending_approvals || 0);
    state.snapshot = payload;
    renderAll();
    const indicator = $("#connectionState");
    indicator.className = "connection-pill online";
    indicator.lastChild.textContent = "实时同步";
    if (!initial && payload.stats.pending_approvals > oldPending) toast("收到新的人工审批请求");
  } catch (error) {
    if (!state.user) return;
    const indicator = $("#connectionState");
    indicator.className = "connection-pill error";
    indicator.lastChild.textContent = "同步失败";
  } finally {
    clearTimeout(state.pollTimer);
    if (state.user) state.pollTimer = setTimeout(() => refreshSnapshot(false), document.hidden ? 9000 : 3000);
  }
}

function populateNodeSelect() {
  const select = $("#taskNode");
  const current = select.value;
  const online = state.snapshot?.nodes?.filter(node => node.status === "online") || [];
  const options = online.map(node => element("option", {value: node.node_id, text: `${node.name} (${node.node_id})`}));
  if (!options.length) options.push(element("option", {value: "", text: "暂无在线节点", disabled: true}));
  select.replaceChildren(...options);
  if (online.some(node => node.node_id === current)) select.value = current;
}

async function decide(item, decision) {
  const verb = {accept: "批准本次操作", decline: "拒绝本次操作", cancel: "拒绝并终止整个任务"}[decision];
  const context = item.command_text ? `\n\n${item.command_text.slice(0, 500)}` : "";
  if (!window.confirm(`确定要${verb}吗？${context}`)) return;
  try {
    await api(`/api/approvals/${item.id}/decision`, {method: "POST", body: {decision}});
    toast(`已${verb}`);
    await refreshSnapshot();
  } catch (error) { toast(error.message, "error"); }
}

async function cancelTask(taskId, title) {
  if (!window.confirm(`确定取消任务“${title}”吗？`)) return;
  try {
    await api(`/api/tasks/${taskId}/cancel`, {method: "POST", body: {}});
    toast("任务已取消");
    await refreshSnapshot();
  } catch (error) { toast(error.message, "error"); }
}

async function refreshUsers() {
  try {
    const {users} = await api("/api/users");
    const table = element("table", {},
      element("thead", {}, element("tr", {}, ...["成员", "登录账号", "角色", "状态", "创建时间"].map(text => element("th", {text})))),
      element("tbody", {}, ...users.map(user => {
        const role = element("select", {class: "mini-select"},
          element("option", {value: "viewer", text: "观察员"}),
          element("option", {value: "operator", text: "操作员"}),
          element("option", {value: "admin", text: "管理员"}),
        );
        role.value = user.role;
        role.disabled = user.id === state.user.id;
        role.addEventListener("change", () => updateUser(user.id, {role: role.value}));
        const active = element("button", {class: `switch-button ${user.active ? "on" : ""}`, type: "button", text: user.active ? "已启用" : "已停用", disabled: user.id === state.user.id});
        active.addEventListener("click", () => updateUser(user.id, {active: !Boolean(user.active)}, true));
        return element("tr", {},
          element("td", {}, element("div", {class: "user-cell"}, element("span", {class: "avatar", text: user.display_name.slice(0,1)}), element("strong", {text: user.display_name}))),
          element("td", {text: user.username}), element("td", {}, role), element("td", {}, active), element("td", {text: formatTime(user.created_at)}),
        );
      })),
    );
    $("#userList").replaceChildren(table);
  } catch (error) { toast(error.message, "error"); }
}

async function updateUser(userId, body, confirmStatus = false) {
  if (confirmStatus && !window.confirm("确定修改该成员的启用状态吗？")) { refreshUsers(); return; }
  try {
    await api(`/api/users/${userId}`, {method: "PATCH", body});
    toast("成员权限已更新");
    await refreshUsers();
  } catch (error) { toast(error.message, "error"); await refreshUsers(); }
}

async function refreshAudit() {
  try {
    const {audit} = await api("/api/audit");
    $("#auditList").replaceChildren(...(audit.length ? audit.map(item => element("article", {class: "audit-row"},
      element("time", {text: formatTime(item.created_at)}),
      element("span", {class: "audit-action", text: item.action}),
      element("span", {class: "audit-target", text: `${item.display_name || "系统"} · ${item.target_type}${item.target_id ? ` #${item.target_id}` : ""}`}),
      element("span", {class: "audit-ip", text: item.ip_address || "—"}),
    )) : [emptyState("暂无审计记录", "关键操作发生后会记录在这里", "≡")]));
  } catch (error) { toast(error.message, "error"); }
}

$("#loginForm").addEventListener("submit", async event => {
  event.preventDefault();
  $("#loginError").textContent = "";
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const auth = await api("/api/auth/login", {method: "POST", body: {username: $("#loginUsername").value, password: $("#loginPassword").value}});
    $("#loginPassword").value = "";
    showApp(auth);
  } catch (error) { $("#loginError").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#logoutButton").addEventListener("click", async () => {
  try { await api("/api/auth/logout", {method: "POST", body: {}}); } catch (_) {}
  showLogin();
});

$$('[data-view]').forEach(button => button.addEventListener("click", () => showView(button.dataset.view)));
$$('[data-go]').forEach(button => button.addEventListener("click", () => showView(button.dataset.go)));

$("#taskFilters").addEventListener("click", event => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.taskFilter = button.dataset.filter;
  $$("#taskFilters .filter").forEach(item => item.classList.toggle("active", item === button));
  renderTasks();
});

$("#newTaskButton").addEventListener("click", () => {
  populateNodeSelect();
  $("#taskError").textContent = "";
  $("#taskDialog").showModal();
});

$("#newUserButton").addEventListener("click", () => {
  $("#userError").textContent = "";
  $("#userDialog").showModal();
});

$("#passwordButton").addEventListener("click", () => {
  $("#passwordError").textContent = "";
  $("#passwordDialog").showModal();
});

$$('[data-close-dialog]').forEach(button => button.addEventListener("click", () => button.closest("dialog").close()));

$("#sessionDialog").addEventListener("close", () => {
  clearTimeout(state.sessionRefreshTimer);
  state.sessionRequestToken += 1;
  state.selectedThread = null;
});

$("#refreshSessionButton").addEventListener("click", () => loadSessionDetail(true));

$("#sessionPromptForm").addEventListener("submit", async event => {
  event.preventDefault();
  const thread = state.selectedThread;
  if (!thread) return;
  const form = event.currentTarget;
  const data = new FormData(form);
  const prompt = String(data.get("prompt") || "").trim();
  if (!prompt) return;
  const errorBox = $("#sessionPromptError");
  const button = $("#sessionPromptSubmit");
  errorBox.textContent = "";
  button.disabled = true;
  try {
    const titlePrefix = isExternalThread(thread) ? "安全续接" : "继续";
    const title = `${titlePrefix}：${thread.name || thread.preview || "Codex 会话"}`.slice(0, 160);
    const created = await api("/api/tasks", {method: "POST", body: {
      title,
      prompt,
      node_id: thread.node_id,
      cwd: thread.cwd || null,
      thread_id: thread.thread_id,
    }});
    form.reset();
    $("#sessionDialog").close();
    toast(`指令已发送，任务 #${created.task_id} 正在执行`);
    showView("tasks");
    await refreshSnapshot();
    openTaskDetail(created.task_id);
  } catch (error) {
    errorBox.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("#taskDetailDialog").addEventListener("close", () => {
  state.selectedTaskId = null;
});

$("#taskDetailApprovals").addEventListener("click", () => {
  $("#taskDetailDialog").close();
  showView("approvals");
});

$("#taskForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  $("#taskError").textContent = "";
  $("#taskSubmit").disabled = true;
  try {
    await api("/api/tasks", {method: "POST", body: {title: data.get("title"), node_id: data.get("node_id"), cwd: data.get("cwd") || null, prompt: data.get("prompt")}});
    form.reset();
    $("#taskDialog").close();
    toast("任务已发送到执行节点");
    showView("tasks");
    await refreshSnapshot();
  } catch (error) { $("#taskError").textContent = error.message; }
  finally { $("#taskSubmit").disabled = false; }
});

$("#userForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  $("#userError").textContent = "";
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("/api/users", {method: "POST", body: Object.fromEntries(data.entries())});
    form.reset();
    $("#userDialog").close();
    toast("成员已创建");
    await refreshUsers();
  } catch (error) { $("#userError").textContent = error.message; }
  finally { button.disabled = false; }
});

$("#passwordForm").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const data = new FormData(form);
  const currentPassword = String(data.get("current_password") || "");
  const newPassword = String(data.get("new_password") || "");
  const confirmation = String(data.get("confirm_password") || "");
  const errorBox = $("#passwordError");
  errorBox.textContent = "";
  if (newPassword !== confirmation) {
    errorBox.textContent = "两次输入的新密码不一致";
    return;
  }
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("/api/auth/password", {method: "POST", body: {current_password: currentPassword, new_password: newPassword}});
    form.reset();
    $("#passwordDialog").close();
    toast("密码已更新，其他设备上的旧会话已退出");
  } catch (error) { errorBox.textContent = error.message; }
  finally { button.disabled = false; }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.user) refreshSnapshot();
});

window.addEventListener("hashchange", () => {
  const next = location.hash.replace("#", "");
  if (state.user && next && next !== state.view) showView(next);
});

if ("serviceWorker" in navigator) navigator.serviceWorker.register(`${BASE_PATH}/sw.js`, {scope: `${BASE_PATH}/`}).catch(() => {});

(async function start() {
  try {
    const auth = await api("/api/auth/me");
    showApp(auth);
  } catch (_) {
    showLogin();
  }
})();
