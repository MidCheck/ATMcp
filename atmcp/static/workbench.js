"use strict";
// ATMcp Workbench: team → agent → session tree (left) + streaming chat (right).

const qs = new URLSearchParams(location.search);
const TEAM = qs.get("team");
const TOKEN = qs.get("token"); // optional dashboard read token (ATMCP_DASHBOARD_AUTH=1)

let CMDTOKEN = "";            // join token for writes (localStorage)
let agents = [];             // [{agent_id, display_name, presence}]
let sessByAgent = {};        // agent_id -> [sessions]
let openAgents = {};         // agent_id -> bool (tree expansion)
let current = null;          // selected session {session_id, agent_id, title}
let detailHead = 0;          // last output seq rendered
let ws = null, refreshTimer = null;
let lastMsgEl = null, lastRole = null;

const el = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function goTeam() {
  const t = el("teamInput").value.trim();
  if (t) location.search = "?team=" + encodeURIComponent(t);
}
window.goTeam = goTeam;

function api(path) { return `/api/teams/${encodeURIComponent(TEAM)}${path}`; }
function withToken(u) {
  return TOKEN ? u + (u.includes("?") ? "&" : "?") + "token=" + encodeURIComponent(TOKEN) : u;
}
function setConn(cls, text) { const c = el("conn"); c.className = "pill " + cls; c.textContent = text; }

// ── token ────────────────────────────────────────────────────────────────────
function tokenKey() { return "atmcp_cmdtoken_" + TEAM; }
function loadToken() {
  CMDTOKEN = localStorage.getItem(tokenKey()) || "";
  if (CMDTOKEN) { el("tokenInput").value = CMDTOKEN; el("tokenState").textContent = "saved ✓"; }
}
function saveToken() {
  CMDTOKEN = el("tokenInput").value.trim();
  localStorage.setItem(tokenKey(), CMDTOKEN);
  el("tokenState").textContent = CMDTOKEN ? "saved ✓" : "cleared";
}
window.saveToken = saveToken;

function authHeaders() { return { "Content-Type": "application/json", "Authorization": "Bearer " + CMDTOKEN }; }

// ── data load ─────────────────────────────────────────────────────────────────
async function loadAgents() {
  try {
    const r = await fetch(withToken(api("/snapshot")));
    if (!r.ok) { setConn("down", r.status === 404 ? "unknown team" : "offline"); return; }
    const s = await r.json();
    agents = (s.agents || []).map((a) => ({ agent_id: a.agent_id, display_name: a.display_name, presence: a.presence }));
  } catch (e) { setConn("down", "offline"); }
}
async function loadSessions() {
  try {
    const r = await fetch(withToken(api("/sessions")));
    if (!r.ok) return;
    const list = (await r.json()).sessions || [];
    sessByAgent = {};
    list.forEach((s) => (sessByAgent[s.agent_id] = sessByAgent[s.agent_id] || []).push(s));
  } catch (e) {}
}
async function refreshTree() { await Promise.all([loadAgents(), loadSessions()]); renderTree(); }
function scheduleTree() { if (refreshTimer) return; refreshTimer = setTimeout(() => { refreshTimer = null; refreshTree(); }, 400); }

// ── tree render ───────────────────────────────────────────────────────────────
function renderTree() {
  const t = el("tree");
  if (!agents.length) { t.innerHTML = '<div class="empty">no agents online yet — start a worker</div>'; return; }
  t.innerHTML = agents.map((a) => {
    const sess = sessByAgent[a.agent_id] || [];
    const open = openAgents[a.agent_id] ? " open" : "";
    const rows = sess.length
      ? sess.map((s) => {
          const sel = current && current.session_id === s.session_id ? " sel" : "";
          const running = s.updated_at && (Date.now() - s.updated_at < 60000);
          return `<div class="sess${sel}" data-sid="${esc(s.session_id)}" data-aid="${esc(a.agent_id)}">
            <span class="stitle">${esc(s.title)}</span>
            <span class="sstat${running ? " running" : ""}">${esc(s.status)}</span></div>`;
        }).join("")
      : '<div class="empty">no sessions — + to start</div>';
    return `<div class="agentnode${open}" data-aid="${esc(a.agent_id)}">
      <div class="agentrow" data-toggle="${esc(a.agent_id)}">
        <span class="caret">▶</span>
        <span class="dot ${esc(a.presence)}"></span>
        <span class="nm">${esc(a.display_name)}</span>
        <span class="badge">${sess.length}</span>
        <span class="plus" data-new="${esc(a.agent_id)}" data-name="${esc(a.display_name)}" title="new session">＋</span>
      </div>
      <div class="sessions">${rows}</div>
    </div>`;
  }).join("");
}

// ── session select + transcript ───────────────────────────────────────────────
function resetTranscript() { el("transcript").innerHTML = ""; lastMsgEl = null; lastRole = null; }
function hidePlaceholder() { const p = el("placeholder"); if (p) p.remove(); }

function appendMsg(role, text) {
  hidePlaceholder();
  const t = el("transcript");
  if (role === "agent" && lastRole === "agent" && lastMsgEl) {
    lastMsgEl.querySelector(".b").appendChild(document.createTextNode(text));
  } else {
    const d = document.createElement("div");
    d.className = "msg " + role;
    const label = role === "you" ? "you" : role === "agent" ? "agent" : "";
    d.innerHTML = (label ? `<div class="role">${label}</div>` : "") + `<div class="b"></div>`;
    d.querySelector(".b").appendChild(document.createTextNode(text));
    t.appendChild(d); lastMsgEl = d; lastRole = role;
  }
  t.scrollTop = t.scrollHeight;
}
function addEvt(text) {
  hidePlaceholder();
  const t = el("transcript"), d = document.createElement("div");
  d.className = "msg evt"; d.textContent = text; t.appendChild(d);
  lastRole = null; lastMsgEl = null; t.scrollTop = t.scrollHeight;
}

async function selectSession(sid, aid) {
  const agent = agents.find((a) => a.agent_id === aid);
  current = { session_id: sid, agent_id: aid, title: "" };
  document.querySelectorAll(".sess").forEach((c) => c.classList.toggle("sel", c.dataset.sid === sid));
  resetTranscript();
  el("input").disabled = false; el("sendBtn").disabled = false;
  el("renameBtn").style.display = ""; el("archiveBtn").style.display = "";
  try {
    const r = await fetch(withToken(api("/sessions/" + encodeURIComponent(sid))));
    if (!r.ok) { addEvt("could not load session"); return; }
    const d = await r.json();
    current.title = d.session.title;
    el("crumb").innerHTML = `${esc(agent ? agent.display_name : "")} <span class="mut">/</span> ${esc(d.session.title)}`;
    detailHead = d.head_seq || 0;
    // merge user messages (directives) + output by timestamp into a flowing transcript
    const items = [];
    (d.directives || []).forEach((x) => items.push({ ts: x.created_at, role: "you", text: x.instruction }));
    (d.output || []).forEach((c) => items.push({ ts: c.ts, seq: c.seq, role: "agent", text: c.text }));
    items.sort((a, b) => (a.ts - b.ts) || ((a.seq || 0) - (b.seq || 0)));
    items.forEach((it) => { if (!(it.role === "agent" && it.text.startsWith("▶ "))) appendMsg(it.role, it.text); });
    if (!items.length) addEvt("new session — say something to " + (agent ? agent.display_name : "the agent"));
  } catch (e) { addEvt("load failed"); }
}

// ── actions (writes need the join token) ───────────────────────────────────────
function needToken() { if (!CMDTOKEN) { addEvt("set a join token below to send / create."); return true; } return false; }

async function newSession(aid, name) {
  if (needToken()) return;
  const title = (prompt("Session title for " + name + ":", "New session") || "").trim();
  if (title === null) return;
  try {
    const r = await fetch(api("/sessions"), { method: "POST", headers: authHeaders(),
      body: JSON.stringify({ agent: aid, title: title || "New session" }) });
    if (r.status === 401) { addEvt("invalid join token"); return; }
    const s = await r.json();
    openAgents[aid] = true;
    await refreshTree();
    selectSession(s.session_id, aid);
  } catch (e) { addEvt("create failed"); }
}

async function send() {
  const inp = el("input");
  const text = inp.value.trim();
  if (!text || !current) return;
  if (needToken()) return;
  inp.value = "";
  appendMsg("you", text);
  try {
    const r = await fetch(api("/sessions/" + encodeURIComponent(current.session_id) + "/message"),
      { method: "POST", headers: authHeaders(), body: JSON.stringify({ text }) });
    if (r.status === 401) { addEvt("invalid join token"); }
    else if (!r.ok) { addEvt("send failed (" + r.status + ")"); }
  } catch (e) { addEvt("send failed"); }
}
window.send = send;

async function renameCurrent() {
  if (!current || needToken()) return;
  const title = (prompt("Rename session:", current.title) || "").trim();
  if (!title) return;
  await fetch(api("/sessions/" + encodeURIComponent(current.session_id) + "/rename"),
    { method: "POST", headers: authHeaders(), body: JSON.stringify({ title }) });
  current.title = title; refreshTree();
  el("crumb").innerHTML = el("crumb").innerHTML.replace(/\/ .*$/, "/ " + esc(title));
}
window.renameCurrent = renameCurrent;

async function archiveCurrent() {
  if (!current || needToken()) return;
  if (!confirm("Archive this session?")) return;
  await fetch(api("/sessions/" + encodeURIComponent(current.session_id) + "/archive"),
    { method: "POST", headers: authHeaders(), body: JSON.stringify({}) });
  current = null; el("crumb").innerHTML = '<span class="mut">select or start a session →</span>';
  el("input").disabled = true; el("sendBtn").disabled = true;
  el("renameBtn").style.display = "none"; el("archiveBtn").style.display = "none";
  resetTranscript(); el("transcript").innerHTML = '<div class="placeholder">Session archived.</div>';
  refreshTree();
}
window.archiveCurrent = archiveCurrent;

function toggleSidebar() { el("sidebar").classList.toggle("collapsed"); }
window.toggleSidebar = toggleSidebar;

// ── tree click delegation ──────────────────────────────────────────────────────
function wireTree() {
  el("tree").addEventListener("click", (e) => {
    const plus = e.target.closest("[data-new]");
    if (plus) { e.stopPropagation(); newSession(plus.dataset.new, plus.dataset.name); return; }
    const sess = e.target.closest(".sess");
    if (sess) { selectSession(sess.dataset.sid, sess.dataset.aid); return; }
    const tog = e.target.closest("[data-toggle]");
    if (tog) { const a = tog.dataset.toggle; openAgents[a] = !openAgents[a]; renderTree(); }
  });
}

// ── WebSocket (reuse the team feed; filter output by session) ───────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  let url = `${proto}//${location.host}/ws/${encodeURIComponent(TEAM)}`;
  if (TOKEN) url += `?token=${encodeURIComponent(TOKEN)}`;
  ws = new WebSocket(url);
  ws.onopen = () => setConn("live", "live");
  ws.onclose = () => { setConn("down", "reconnecting…"); setTimeout(connectWS, 1500); };
  ws.onerror = () => ws.close();
  ws.onmessage = (m) => {
    const f = JSON.parse(m.data);
    if (f.type === "output") {
      if (current && f.session_id === current.session_id && (f.seq || 0) > detailHead) {
        detailHead = f.seq;
        if (!String(f.text).startsWith("▶ ")) appendMsg("agent", f.text);
      }
      return;
    }
    if (f.type === "presence") { scheduleTree(); return; }
    if (f.type === "event") {
      const k = f.kind || "";
      if (k.startsWith("session_")) scheduleTree();
      if (current && k === "directive_done" && f.payload && f.payload.session_id === current.session_id) {/* turn ended */}
      if (current && k === "directive_failed" && f.payload && f.payload.session_id === current.session_id)
        addEvt("✗ failed" + (f.payload.result_summary ? " — " + f.payload.result_summary : ""));
    }
  };
}

function start() {
  if (!TEAM) { el("setup").style.display = "block"; return; }
  el("app").style.display = "flex";
  el("teamName").textContent = TEAM;
  el("dashLink").href = "/dashboard?team=" + encodeURIComponent(TEAM);
  loadToken();
  wireTree();
  el("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  refreshTree().then(connectWS);
  setInterval(refreshTree, 8000);
}
start();
