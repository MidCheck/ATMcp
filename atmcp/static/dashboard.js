"use strict";
// ATMcp dashboard: load a JSON snapshot, then live-update over WebSocket.
// The WS gives low-latency activity; a debounced snapshot refresh keeps the
// aggregates (roster presence, task board, rollup, knowledge) correct.

const qs = new URLSearchParams(location.search);
const TEAM = qs.get("team");
const TOKEN = qs.get("token");

let head = 0;
let ws = null;
let refreshTimer = null;
let pollTimer = null;

const el = (id) => document.getElementById(id);
const esc = (s) =>
  String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function goTeam() {
  const t = el("teamInput").value.trim();
  if (t) location.search = "?team=" + encodeURIComponent(t);
}
window.goTeam = goTeam;

function apiBase() {
  let u = `/api/teams/${encodeURIComponent(TEAM)}`;
  return TOKEN ? { snap: `${u}/snapshot?token=${encodeURIComponent(TOKEN)}` } : { snap: `${u}/snapshot` };
}

async function loadSnapshot() {
  try {
    const r = await fetch(apiBase().snap);
    if (!r.ok) {
      if (r.status === 404) setConn("down", "unknown team");
      return;
    }
    const s = await r.json();
    render(s);
    head = Math.max(head, s.head_event_id || 0);
    el("headCursor").textContent = "#" + head;
  } catch (e) {
    setConn("down", "offline");
  }
}

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    loadSnapshot();
  }, 350);
}

function setConn(cls, text) {
  const c = el("conn");
  c.className = "pill " + cls;
  c.textContent = text;
}

function fmtAgo(ts) {
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000));
  if (s < 60) return s + "s ago";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  return Math.round(s / 3600) + "h ago";
}

function render(s) {
  el("teamName").textContent = s.team;
  el("mcpUrl").textContent = (s.status && s.mcp_url) || el("mcpUrl").textContent;

  // Goal rollup
  const pct = s.rollup ? s.rollup.progress_pct : 0;
  el("goalFill").style.width = pct + "%";
  el("goalPct").textContent = pct + "%";
  el("goalDetail").textContent =
    `${s.rollup.done_weight}/${s.rollup.total_weight} weight · ${s.rollup.task_count} tasks`;
  el("agentsOnline").textContent = `${s.status.agents_online}/${s.status.agents_total} online`;

  // Stat tiles
  const by = s.rollup.by_status || {};
  const tiles = [
    ["open", by.open || 0], ["in progress", (by.in_progress || 0) + (by.claimed || 0)],
    ["done", by.done || 0], ["failed", by.failed || 0], ["knowledge", s.status.knowledge_count],
  ];
  el("statRow").innerHTML = tiles
    .map(([k, v]) => `<div class="stat"><b>${v}</b><span>${k}</span></div>`)
    .join("");

  // Agents
  el("agents").innerHTML =
    s.agents.length === 0
      ? '<div class="empty">no agents yet</div>'
      : s.agents
          .map((a) => {
            const prog = a.progress_pct || 0;
            return `<div class="agent">
              <div class="dot ${a.presence}" title="${a.presence}"></div>
              <div class="meta">
                <div class="name">${esc(a.display_name)}</div>
                <div class="sub">${esc(a.status_summary || "—")}${
              a.current_task_id ? " · task " + esc(a.current_task_id.slice(-6)) : ""
            } · ${fmtAgo(a.last_seen)}</div>
                <div class="mini"><div style="width:${prog}%"></div></div>
              </div>
            </div>`;
          })
          .join("");

  // Task board
  const order = ["open", "claimed", "in_progress", "blocked", "done", "failed"];
  const groups = { open: [], claimed: [], in_progress: [], blocked: [], done: [], failed: [], cancelled: [] };
  s.tasks.forEach((t) => (groups[t.status] || groups.open).push(t));
  const columns = [
    ["Open", ["open"]], ["Active", ["claimed", "in_progress", "blocked"]],
    ["Done", ["done"]], ["Failed", ["failed"]],
  ];
  el("board").innerHTML = columns
    .map(([label, sts]) => {
      const items = sts.flatMap((st) => groups[st]);
      const cards = items.length
        ? items
            .map(
              (t) => `<div class="task ${t.status}">
                <div class="t">${esc(t.title)}</div>
                <div class="a">${t.status}${t.assignee ? " · " + esc(t.assignee.slice(-6)) : ""}${
                t.progress_pct ? " · " + t.progress_pct + "%" : ""
              }</div>
              </div>`
            )
            .join("")
        : '<div class="empty">—</div>';
      return `<div class="col"><h3>${label} (${items.length})</h3>${cards}</div>`;
    })
    .join("");

  // Knowledge
  el("knowledge").innerHTML =
    s.knowledge.length === 0
      ? '<div class="empty">no shared knowledge yet</div>'
      : s.knowledge
          .map((k) => {
            const tags = JSON.parse(k.tags_json || "[]");
            return `<div class="know">
              <div>${esc(k.title)}</div>
              <div class="tags">${tags.map((t) => "#" + esc(t)).join(" ")}</div>
              <div class="by">by ${esc(k.first_author.slice(-6))}${
              k.contributor_count > 1 ? " +" + (k.contributor_count - 1) : ""
            }</div>
            </div>`;
          })
          .join("");

  if (s.events) renderFeed(s.events);
}

function renderFeed(events) {
  el("feed").innerHTML = events
    .map(
      (e) => `<div class="ev"><span class="k">${esc(e.kind)}</span>
        <span>${esc(e.entity_id ? e.entity_id.slice(-6) : e.entity_type)}</span>
        <span class="who">${esc(e.actor_agent ? e.actor_agent.slice(-6) : "")}</span></div>`
    )
    .join("");
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

function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let url = `${proto}//${location.host}/ws/${encodeURIComponent(TEAM)}?since_event_id=${head}`;
  if (TOKEN) url += `&token=${encodeURIComponent(TOKEN)}`;
  ws = new WebSocket(url);

  ws.onopen = () => setConn("live", "live");
  ws.onclose = () => {
    setConn("down", "reconnecting…");
    setTimeout(connectWS, 1500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (msg) => {
    const f = JSON.parse(msg.data);
    if (f.type === "hello") {
      head = Math.max(head, f.head_event_id || 0);
      return;
    }
    if (f.type === "event") {
      head = Math.max(head, f.event_id || 0);
      el("headCursor").textContent = "#" + head;
      prependEvent(f);
      scheduleRefresh();
    } else if (f.type === "presence") {
      scheduleRefresh();
    }
  };
}

function start() {
  if (!TEAM) {
    el("setup").style.display = "block";
    return;
  }
  el("app").style.display = "block";
  el("teamName").textContent = TEAM;
  loadSnapshot().then(connectWS);
  pollTimer = setInterval(loadSnapshot, 5000); // refresh presence aging
}

start();
