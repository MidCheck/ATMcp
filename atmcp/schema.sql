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
  idem_key    TEXT NOT NULL,
  result_json TEXT NOT NULL,
  created_at  INTEGER NOT NULL,
  PRIMARY KEY (team_id, idem_key)
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
