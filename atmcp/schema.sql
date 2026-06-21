-- ATMcp durable schema (SQLite, WAL). team_id leads every index = structural
-- multi-tenant isolation. The `events` table is the monotonic audit/replay/feed
-- backbone; Redis only ever holds rebuildable soft state.

PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ── Tenancy & identity ──────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS teams (
  team_id              TEXT PRIMARY KEY,
  name                 TEXT NOT NULL UNIQUE,
  join_token_hash      TEXT NOT NULL,
  dashboard_token_hash TEXT,
  created_at           INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  team_id           TEXT NOT NULL,
  agent_id          TEXT NOT NULL,
  display_name      TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  session_id        TEXT,                 -- current MCP session bound at join
  joined_at         INTEGER NOT NULL,
  last_seen         INTEGER NOT NULL,     -- durable presence fallback
  status_summary    TEXT,
  current_task_id   TEXT,
  progress_pct      INTEGER NOT NULL DEFAULT 0,
  retired           INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (team_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_agents_team ON agents(team_id, retired);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_name ON agents(team_id, display_name);

-- ── Knowledge: append-only + content-addressed (OR-Set with projection) ─────
CREATE TABLE IF NOT EXISTS knowledge_objects (
  content_id TEXT PRIMARY KEY,            -- sha256(canonical_json)
  title      TEXT NOT NULL,
  body       TEXT NOT NULL,
  tags_json  TEXT NOT NULL DEFAULT '[]',
  byte_size  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS knowledge_contributions (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id      TEXT NOT NULL,
  content_id   TEXT NOT NULL REFERENCES knowledge_objects(content_id),
  author_agent TEXT NOT NULL,
  task_id      TEXT,
  created_at   INTEGER NOT NULL,
  event_id     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kc_team_content ON knowledge_contributions(team_id, content_id);
CREATE INDEX IF NOT EXISTS idx_kc_team_time    ON knowledge_contributions(team_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_current (
  team_id           TEXT NOT NULL,
  content_id        TEXT NOT NULL,
  title             TEXT NOT NULL,
  body              TEXT NOT NULL,
  tags_json         TEXT NOT NULL,
  first_author      TEXT NOT NULL,
  contributor_count INTEGER NOT NULL DEFAULT 1,
  present           INTEGER NOT NULL DEFAULT 1,  -- OR-Set: 1=present, 0=tombstoned
  first_seen_at     INTEGER NOT NULL,
  last_seen_at      INTEGER NOT NULL,
  last_event_id     INTEGER NOT NULL,
  PRIMARY KEY (team_id, content_id)
);
CREATE INDEX IF NOT EXISTS idx_kcur_team_present ON knowledge_current(team_id, present, last_seen_at);

-- Full-text index over current knowledge (FTS5). team_id/content_id UNINDEXED
-- so we can filter by tenant while MATCHing title/body/tags.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
  team_id UNINDEXED,
  content_id UNINDEXED,
  title,
  body,
  tags,
  tokenize = 'unicode61'
);

-- ── Memory: LWW-register ordered by a per-team Lamport clock ─────────────────
CREATE TABLE IF NOT EXISTS memory_current (
  team_id       TEXT NOT NULL,
  mem_key       TEXT NOT NULL,
  value_json    TEXT NOT NULL,
  lclock        INTEGER NOT NULL,
  writer_agent  TEXT NOT NULL,
  updated_at    INTEGER NOT NULL,
  version       INTEGER NOT NULL DEFAULT 1,
  last_event_id INTEGER NOT NULL,
  PRIMARY KEY (team_id, mem_key)
);

CREATE TABLE IF NOT EXISTS memory_history (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id      TEXT NOT NULL,
  mem_key      TEXT NOT NULL,
  value_json   TEXT NOT NULL,
  lclock       INTEGER NOT NULL,
  writer_agent TEXT NOT NULL,
  version      INTEGER NOT NULL,
  ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memhist_team_key ON memory_history(team_id, mem_key, version);

CREATE TABLE IF NOT EXISTS team_clock (
  team_id TEXT PRIMARY KEY,
  lclock  INTEGER NOT NULL DEFAULT 0
);

-- ── Idempotency: durable, checked INSIDE the write txn (single-writer lock) so a
-- retried mutating tool returns the exact stored result and never double-applies.
-- Old rows are pruned by the reaper (retention = ATMCP_IDEM_TTL_S).
CREATE TABLE IF NOT EXISTS idempotency (
  team_id     TEXT NOT NULL,
  agent_id    TEXT NOT NULL,                 -- scope keys to the caller (no cross-agent collisions)
  idem_key    TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (team_id, agent_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_idem_created ON idempotency(created_at);

-- ── Goals & tasks: scheduler with leases + fencing tokens + DAG ──────────────
CREATE TABLE IF NOT EXISTS goals (
  team_id     TEXT NOT NULL,
  goal_id     TEXT NOT NULL,
  title       TEXT NOT NULL,
  description TEXT,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (team_id, goal_id)
);

CREATE TABLE IF NOT EXISTS tasks (
  team_id          TEXT NOT NULL,
  task_id          TEXT NOT NULL,
  parent_id        TEXT,
  goal_id          TEXT,
  title            TEXT NOT NULL,
  description      TEXT,
  status           TEXT NOT NULL DEFAULT 'open',  -- open|claimed|in_progress|blocked|done|failed|cancelled
  priority         INTEGER NOT NULL DEFAULT 0,
  weight           INTEGER NOT NULL DEFAULT 1,
  assignee         TEXT,
  fencing_token    INTEGER NOT NULL DEFAULT 0,
  lease_ttl_s      INTEGER,                       -- per-task override (NULL => default)
  lease_expires_at INTEGER,                       -- authoritative lease deadline (epoch ms)
  progress_pct     INTEGER NOT NULL DEFAULT 0,
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL DEFAULT 5,
  result_summary   TEXT,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL,
  last_event_id    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (team_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_team_status ON tasks(team_id, status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_lease       ON tasks(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS task_deps (
  team_id    TEXT NOT NULL,
  task_id    TEXT NOT NULL,
  depends_on TEXT NOT NULL,
  PRIMARY KEY (team_id, task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS task_claims (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id       TEXT NOT NULL,
  task_id       TEXT NOT NULL,
  agent_id      TEXT NOT NULL,
  fencing_token INTEGER NOT NULL,
  claimed_at    INTEGER NOT NULL,
  released_at   INTEGER,
  outcome       TEXT                              -- done|failed|expired|released
);
CREATE INDEX IF NOT EXISTS idx_claims_team_task ON task_claims(team_id, task_id);

-- ── Sessions (threads): a conversation line with one agent (workbench) ───────
-- One session = one chat thread = one independent memory. Maps to an executor
-- session (cli_session_id for CLI drivers) or a server-stored transcript (API
-- drivers). Directives + output rows reference a session_id (nullable: a NULL
-- session_id is the agent's legacy/default thread, keeping /team compatible).
CREATE TABLE IF NOT EXISTS sessions (
  session_id     TEXT PRIMARY KEY,
  team_id        TEXT NOT NULL,
  agent_id       TEXT NOT NULL,                 -- the agent this thread belongs to
  title          TEXT NOT NULL DEFAULT 'New session',
  driver         TEXT,                          -- executor kind (claude|codex|cursor|openai-compat)
  cli_session_id TEXT,                          -- executor's resumable session id (set by worker)
  worktree       TEXT,                          -- per-session working dir (set by worker)
  status         TEXT NOT NULL DEFAULT 'active',-- active|archived
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  last_event_id  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_team_agent ON sessions(team_id, agent_id, status, updated_at);

-- ── Directives: point-to-point commands (console -> a specific agent) ───────
CREATE TABLE IF NOT EXISTS directives (
  directive_id   TEXT PRIMARY KEY,
  team_id        TEXT NOT NULL,
  from_agent     TEXT NOT NULL,                 -- issuer (the console)
  to_agent       TEXT NOT NULL,                 -- target agent_id
  session_id     TEXT,                          -- which thread (NULL = default/legacy thread)
  instruction    TEXT NOT NULL,
  payload_json   TEXT NOT NULL DEFAULT '{}',
  status         TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|canceled
  priority       INTEGER NOT NULL DEFAULT 0,
  result_summary TEXT,
  result_output  TEXT,                          -- final result text from the worker
  created_at     INTEGER NOT NULL,
  updated_at     INTEGER NOT NULL,
  last_event_id  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_directives_inbox ON directives(team_id, to_agent, status, priority DESC);
CREATE INDEX IF NOT EXISTS idx_directives_from  ON directives(team_id, from_agent, created_at);

-- ── Agent output stream: what each agent is printing (for "view agent output") ─
CREATE TABLE IF NOT EXISTS agent_output (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,  -- monotonic tail cursor
  team_id      TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  session_id   TEXT,                               -- which thread (NULL = default/legacy)
  directive_id TEXT,
  source       TEXT NOT NULL DEFAULT 'agent',      -- agent | hook
  text         TEXT NOT NULL,
  ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_output_tail ON agent_output(team_id, agent_id, id);
-- idx_output_session is created in db._migrate (after session_id is guaranteed to exist,
-- so it also works when ALTERing a pre-existing agent_output table).

-- ── Usage: per-execution token/cost accounting (append-only meter) ──────────
-- One row per worker model run (parsed from `claude -p --output-format json`'s
-- usage + total_cost_usd). Powers the dashboard token meters, rolling-window
-- views, and budget brakes. Append-only; old rows pruned by the reaper.
CREATE TABLE IF NOT EXISTS usage_events (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id        TEXT NOT NULL,
  agent_id       TEXT NOT NULL,
  directive_id   TEXT,
  model          TEXT,
  input_tokens   INTEGER NOT NULL DEFAULT 0,
  output_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_read     INTEGER NOT NULL DEFAULT 0,
  cache_creation INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL    NOT NULL DEFAULT 0,
  num_turns      INTEGER NOT NULL DEFAULT 0,
  duration_ms    INTEGER NOT NULL DEFAULT 0,
  ts             INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_team_ts    ON usage_events(team_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_team_agent ON usage_events(team_id, agent_id, ts);

-- ── Events: monotonic activity log (audit + replay + dashboard feed) ─────────
CREATE TABLE IF NOT EXISTS events (
  event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
  team_id      TEXT NOT NULL,
  kind         TEXT NOT NULL,
  entity_type  TEXT NOT NULL,
  entity_id    TEXT,
  actor_agent  TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  ts           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_team_id ON events(team_id, event_id);
