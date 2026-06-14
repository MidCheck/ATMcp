"use strict";
// ATMcp dashboard: live snapshot + WebSocket, agent drill-down, and a /team console.

const qs = new URLSearchParams(location.search);
const TEAM = qs.get("team");
const TOKEN = qs.get("token"); // optional dashboard read token (when ATMCP_DASHBOARD_AUTH=1)

let head = 0;
let ws = null;
let refreshTimer = null;
let detailRefreshTimer = null;
let selAgent = null; // {id, name}
let detailHead = 0;
let CMDTOKEN = ""; // join token for console writes (stored in localStorage)

const el = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function goTeam() {
  const t = el("teamInput").value.trim();
  if (t) location.search = "?team=" + encodeURIComponent(t);
}
window.goTeam = goTeam;

function apiSnap() {
  const u = `/api/teams/${encodeURIComponent(TEAM)}/snapshot`;
  return TOKEN ? `${u}?token=${encodeURIComponent(TOKEN)}` : u;
}
function withToken(u) {
  return TOKEN ? u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : u;
}

async function loadSnapshot() {
  try {
    const r = await fetch(apiSnap());
    if (!r.ok) { if (r.status === 404) setConn("down", "unknown team"); return; }
    const s = await r.json();
    render(s);
    head = Math.max(head, s.head_event_id || 0);
    el("headCursor").textContent = "#" + head;
  } catch (e) { setConn("down", "offline"); }
}

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => { refreshTimer = null; loadSnapshot(); }, 350);
}

function setConn(cls, text) { const c = el("conn"); c.className = "pill " + cls; c.textContent = text; }

function fmtAgo(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

function render(s) {
  el("teamName").textContent = s.team;
  const pct = s.rollup ? s.rollup.progress_pct : 0;
  el("goalFill").style.width = pct + "%";
  el("goalPct").textContent = pct + "%";
  el("goalDetail").textContent =
    `${s.rollup.done_weight}/${s.rollup.total_weight} weight · ${s.rollup.task_count} tasks`;
  el("agentsOnline").textContent = `${s.status.agents_online}/${s.status.agents_total} online`;

  const by = s.rollup.by_status || {};
  const tiles = [
    ["open", by.open || 0], ["in progress", (by.in_progress || 0) + (by.claimed || 0)],
    ["done", by.done || 0], ["failed", by.failed || 0], ["knowledge", s.status.knowledge_count],
  ];
  el("statRow").innerHTML = tiles.map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`).join("");

  el("agents").innerHTML = s.agents.length === 0
    ? '<div class="empty">no agents yet</div>'
    : s.agents.map((a) => {
        const prog = a.progress_pct || 0;
        const selCls = selAgent && selAgent.id === a.agent_id ? " sel" : "";
        return `<div class="agent${selCls}" data-agent-id="${esc(a.agent_id)}" data-agent-name="${esc(a.display_name)}">
          <div class="dot ${esc(a.presence)}" title="${esc(a.presence)}"></div>
          <div class="meta">
            <div class="name">${esc(a.display_name)}</div>
            <div class="sub">${esc(a.status_summary || "—")}${a.current_task_id ? " · task " + esc(a.current_task_id.slice(-6)) : ""} · ${fmtAgo(a.last_seen)}</div>
            <div class="mini"><div style="width:${prog}%"></div></div>
          </div>
        </div>`;
      }).join("");

  const groups = { open: [], claimed: [], in_progress: [], blocked: [], done: [], failed: [], cancelled: [] };
  s.tasks.forEach((t) => (groups[t.status] || groups.open).push(t));
  const columns = [["Open", ["open"]], ["Active", ["claimed", "in_progress", "blocked"]], ["Done", ["done"]], ["Failed", ["failed"]]];
  el("board").innerHTML = columns.map(([label, sts]) => {
    const items = sts.flatMap((st) => groups[st]);
    const cards = items.length ? items.map((t) => `<div class="task ${esc(t.status)}">
        <div class="t">${esc(t.title)}</div>
        <div class="a">${esc(t.status)}${t.assignee ? " · " + esc(t.assignee.slice(-6)) : ""}${t.progress_pct ? " · " + t.progress_pct + "%" : ""}</div>
      </div>`).join("") : '<div class="empty">—</div>';
    return `<div class="col"><h3>${label} (${items.length})</h3>${cards}</div>`;
  }).join("");

  el("knowledge").innerHTML = s.knowledge.length === 0
    ? '<div class="empty">no shared knowledge yet</div>'
    : s.knowledge.map((k) => {
        const tags = JSON.parse(k.tags_json || "[]");
        return `<div class="know"><div>${esc(k.title)}</div>
          <div class="tags">${tags.map((t) => "#" + esc(t)).join(" ")}</div>
          <div class="by">by ${esc(k.first_author.slice(-6))}${k.contributor_count > 1 ? " +" + (k.contributor_count - 1) : ""}</div></div>`;
      }).join("");

  if (s.events) renderFeed(s.events);
}

function renderFeed(events) {
  el("feed").innerHTML = events.map((e) => `<div class="ev"><span class="k">${esc(e.kind)}</span>
    <span>${esc(e.entity_id ? e.entity_id.slice(-6) : e.entity_type)}</span>
    <span class="who">${esc(e.actor_agent ? e.actor_agent.slice(-6) : "")}</span></div>`).join("");
}

function prependEvent(e) {
  const feed = el("feed");
  const div = document.createElement("div");
  div.className = "ev";
  div.innerHTML = `<span class="k">${esc(e.kind)}</span>
    <span>${esc(e.entity_id ? e.entity_id.slice(-6) : e.entity_type)}</span>
    <span class="who">${esc(e.actor_agent ? e.actor_agent.slice(-6) : "")}</span>`;
  feed.insertBefore(div, feed.firstChild);
  while (feed.childNodes.length > 100) feed.removeChild(feed.lastChild);
}

// ── Center tabs ──────────────────────────────────────────────────────────────
const TABS = ["board", "activity", "knowledge", "detail"];
function showTab(name) {
  TABS.forEach((t) => {
    const panel = el("tab-" + t);
    const btn = el("tabbtn-" + t);
    if (panel) panel.style.display = t === name ? "" : "none";
    if (btn) btn.classList.toggle("active", t === name);
  });
}
window.showTab = showTab;

// ── Agent detail (opens in the center "Agent" tab) ───────────────────────────
async function openDetail(agentId, name) {
  selAgent = { id: agentId, name };
  el("detailName").textContent = name;
  document.querySelectorAll(".agent").forEach((c) =>
    c.classList.toggle("sel", c.dataset.agentId === agentId));
  el("tabbtn-detail").style.display = "";
  showTab("detail");
  try {
    const r = await fetch(withToken(`/api/teams/${encodeURIComponent(TEAM)}/agents/${encodeURIComponent(agentId)}/detail`));
    if (r.ok) renderDetail(await r.json());
  } catch (e) {}
}
window.openDetail = openDetail;

function closeDetail() {
  selAgent = null;
  el("tabbtn-detail").style.display = "none";
  document.querySelectorAll(".agent").forEach((c) => c.classList.remove("sel"));
  showTab("board");
}
window.closeDetail = closeDetail;

function renderDetail(d) {
  const a = d.agent || {};
  el("detailPresence").textContent = a.presence || "?";
  el("detailDirectives").innerHTML = (d.directives || []).length
    ? d.directives.map((x) => `<div class="drow"><span class="badge ${esc(x.status)}">${esc(x.status)}</span> ${esc((x.instruction || "").slice(0, 90))}${x.result_summary ? ' <span class="muted">· ' + esc(x.result_summary) + "</span>" : ""}</div>`).join("")
    : '<div class="empty">no directives</div>';
  el("detailTasks").innerHTML = (d.tasks || []).length
    ? d.tasks.map((x) => `<div class="drow"><span class="badge ${esc(x.status)}">${esc(x.status)}</span> ${esc(x.title)}${x.progress_pct ? " · " + x.progress_pct + "%" : ""}</div>`).join("")
    : '<div class="empty">no tasks</div>';
  detailHead = d.head_seq || 0;
  const log = el("detailOutput");
  log.textContent = "";
  (d.output || []).forEach((c) => appendDetailLine(c));
  log.scrollTop = log.scrollHeight;
}

function appendDetailLine(c) {
  const log = el("detailOutput");
  log.appendChild(document.createTextNode((c.source === "hook" ? "[hook] " : "") + c.text + "\n"));
  log.scrollTop = log.scrollHeight;
}

function scheduleDetailRefresh() {
  if (detailRefreshTimer || !selAgent) return;
  detailRefreshTimer = setTimeout(async () => {
    detailRefreshTimer = null;
    if (!selAgent) return;
    try {
      const r = await fetch(withToken(`/api/teams/${encodeURIComponent(TEAM)}/agents/${encodeURIComponent(selAgent.id)}/detail`));
      if (r.ok) renderDetail(await r.json());
    } catch (e) {}
  }, 500);
}

// ── Team console (pseudo-model chat) ────────────────────────────────────────
function tokenKey() { return "atmcp_cmdtoken_" + TEAM; }
function loadToken() {
  CMDTOKEN = localStorage.getItem(tokenKey()) || "";
  if (CMDTOKEN) { el("tokenInput").value = CMDTOKEN; el("tokenState").textContent = "saved"; }
}
function saveToken() {
  CMDTOKEN = el("tokenInput").value.trim();
  localStorage.setItem(tokenKey(), CMDTOKEN);
  el("tokenState").textContent = CMDTOKEN ? "saved ✓" : "cleared";
}
window.saveToken = saveToken;

function convAdd(role, text) {
  const conv = el("conv");
  const div = document.createElement("div");
  div.className = "msg " + role;
  const label = role === "you" ? "you" : role === "evt" ? "team · event" : role === "err" ? "error" : "team";
  div.innerHTML = `<div class="role">${label}</div><div class="body">${esc(text)}</div>`;
  conv.appendChild(div);
  conv.scrollTop = conv.scrollHeight;
  while (conv.childNodes.length > 200) conv.removeChild(conv.firstChild);
}

function renderConsoleData(data) {
  if (!data || !data.data) return;
  const d = data.data;
  if (data.kind === "logs" && Array.isArray(d.chunks)) {
    if (!d.chunks.length) convAdd("team", "(no output yet)");
    d.chunks.forEach((c) => convAdd("team", (c.source === "hook" ? "[hook] " : "") + c.text));
  } else if (data.kind === "status" && Array.isArray(d.agents)) {
    convAdd("team", d.agents.map((a) => `${a.presence === "healthy" ? "●" : a.presence === "degraded" ? "◐" : "○"} ${a.display_name}${a.current_task_id ? " · task " + a.current_task_id.slice(-6) : ""}${a.progress_pct ? " · " + a.progress_pct + "%" : ""}`).join("\n") || "(no agents)");
  } else if (data.kind === "directives" && Array.isArray(d.directives)) {
    convAdd("team", d.directives.map((x) => `${x.status.padEnd(8)} ${x.directive_id.slice(-6)}  ${(x.instruction || "").slice(0, 50)}`).join("\n") || "(none)");
  } else if (data.kind === "todo" && Array.isArray(d.tasks)) {
    convAdd("team", d.tasks.map((t) => `[${t.status}] ${t.title}`).join("\n") || "(no tasks)");
  }
}

async function runConsole() {
  const inp = el("cmdInput");
  const text = inp.value.trim();
  if (!text) return;
  inp.value = "";
  convAdd("you", text);
  if (!CMDTOKEN) { convAdd("err", "set a join token below to send commands"); return; }
  try {
    const r = await fetch(`/api/teams/${encodeURIComponent(TEAM)}/console/command`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: text, token: CMDTOKEN, console: "dashboard" }),
    });
    if (r.status === 401) { convAdd("err", "invalid join token"); return; }
    const data = await r.json();
    convAdd(data.ok === false ? "err" : "team", data.message || JSON.stringify(data));
    renderConsoleData(data);
    scheduleRefresh();
  } catch (e) { convAdd("err", "request failed"); }
}
window.runConsole = runConsole;

// ── WebSocket ───────────────────────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let url = `${proto}//${location.host}/ws/${encodeURIComponent(TEAM)}?since_event_id=${head}`;
  if (TOKEN) url += `&token=${encodeURIComponent(TOKEN)}`;
  ws = new WebSocket(url);
  ws.onopen = () => setConn("live", "live");
  ws.onclose = () => { setConn("down", "reconnecting…"); setTimeout(connectWS, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    const f = JSON.parse(msg.data);
    if (f.type === "hello") { head = Math.max(head, f.head_event_id || 0); return; }
    if (f.type === "output") {
      if (selAgent && f.agent_id === selAgent.id && (f.seq || 0) > detailHead) {
        detailHead = f.seq;
        appendDetailLine({ text: f.text, source: f.source });
      }
      return;
    }
    if (f.type === "presence") { scheduleRefresh(); return; }
    if (f.type === "event") {
      head = Math.max(head, f.event_id || 0);
      el("headCursor").textContent = "#" + head;
      prependEvent(f);
      scheduleRefresh();
      if (f.kind && f.kind.indexOf("directive_") === 0) {
        const lbl = { directive_sent: "→ sent", directive_claimed: "claimed", directive_done: "✓ done", directive_failed: "✗ failed", directive_canceled: "canceled" }[f.kind] || f.kind;
        const summ = f.payload && f.payload.result_summary ? " — " + f.payload.result_summary : "";
        convAdd("evt", `${lbl} ${f.entity_id ? f.entity_id.slice(-6) : ""}${summ}`);
        if (selAgent) scheduleDetailRefresh();
      }
    }
  };
}

function start() {
  if (!TEAM) { el("setup").style.display = "block"; return; }
  el("app").style.display = "flex";
  el("teamName").textContent = TEAM;
  loadToken();
  el("agents").addEventListener("click", (e) => {
    const card = e.target.closest(".agent");
    if (card && card.dataset.agentId) openDetail(card.dataset.agentId, card.dataset.agentName);
  });
  el("cmdInput").addEventListener("keydown", (e) => { if (e.key === "Enter") runConsole(); });
  loadSnapshot().then(connectWS);
  setInterval(loadSnapshot, 5000);
}

start();
