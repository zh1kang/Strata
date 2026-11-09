"""SQLite schema. Bump SCHEMA_VERSION on any incompatible change."""

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS revisions (
  revision INTEGER PRIMARY KEY,
  committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL CHECK (json_valid(payload)),
  content_hash TEXT NOT NULL,
  revision INTEGER NOT NULL REFERENCES revisions(revision)
);
CREATE INDEX IF NOT EXISTS events_session ON events(session_id, seq);

CREATE TABLE IF NOT EXISTS slots (
  slot_id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL,
  attribute TEXT NOT NULL,
  UNIQUE (subject, attribute)
);

CREATE TABLE IF NOT EXISTS fact_versions (
  version_id INTEGER PRIMARY KEY,
  slot_id INTEGER NOT NULL REFERENCES slots(slot_id),
  op TEXT NOT NULL CHECK (op IN ('assert', 'retract')),
  value TEXT CHECK (value IS NULL OR json_valid(value)),
  source_seq INTEGER NOT NULL REFERENCES events(seq),
  revision INTEGER NOT NULL REFERENCES revisions(revision)
);
CREATE INDEX IF NOT EXISTS fact_versions_slot ON fact_versions(slot_id, version_id);

-- changed_revision only advances when the effective value changes, so
-- re-asserting an identical value never invalidates dependents.
CREATE TABLE IF NOT EXISTS slot_heads (
  slot_id INTEGER PRIMARY KEY REFERENCES slots(slot_id),
  version_id INTEGER NOT NULL REFERENCES fact_versions(version_id),
  changed_revision INTEGER NOT NULL
);

-- Revision at which any slot of the subject last changed (phantom dependencies).
CREATE TABLE IF NOT EXISTS subjects (
  subject TEXT PRIMARY KEY,
  changed_revision INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
  node_id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,
  node_key TEXT NOT NULL,
  level INTEGER NOT NULL CHECK (level >= 1),
  cost REAL NOT NULL DEFAULT 1.0 CHECK (cost >= 0),
  code_version TEXT NOT NULL DEFAULT '1',
  state TEXT NOT NULL CHECK (state IN ('pending','valid','stale','suspect','failed')),
  value TEXT CHECK (value IS NULL OR json_valid(value)),
  value_hash TEXT,
  verified_revision INTEGER,
  changed_revision INTEGER,
  dirty_since INTEGER,
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT,
  access_count INTEGER NOT NULL DEFAULT 0,
  UNIQUE (kind, node_key)
);
CREATE INDEX IF NOT EXISTS nodes_state ON nodes(state);

-- Observed inputs of each node.  Unused reference columns hold '' / 0.
CREATE TABLE IF NOT EXISTS node_deps (
  node_id INTEGER NOT NULL REFERENCES nodes(node_id),
  dep_kind TEXT NOT NULL CHECK (dep_kind IN ('slot','subject','node')),
  dep_subject TEXT NOT NULL DEFAULT '',
  dep_attribute TEXT NOT NULL DEFAULT '',
  dep_node INTEGER NOT NULL DEFAULT 0,
  observed_revision INTEGER NOT NULL,
  PRIMARY KEY (node_id, dep_kind, dep_subject, dep_attribute, dep_node)
);
CREATE INDEX IF NOT EXISTS node_deps_slot ON node_deps(dep_kind, dep_subject, dep_attribute);
CREATE INDEX IF NOT EXISTS node_deps_node ON node_deps(dep_kind, dep_node);

CREATE TABLE IF NOT EXISTS leases (
  node_id INTEGER PRIMARY KEY REFERENCES nodes(node_id),
  token TEXT NOT NULL,
  expires_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS search_docs (
  doc_id INTEGER PRIMARY KEY,
  doc_kind TEXT NOT NULL CHECK (doc_kind IN ('event','fact','node')),
  ref INTEGER NOT NULL,
  UNIQUE (doc_kind, ref)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(text, tokenize='unicode61');

CREATE TRIGGER IF NOT EXISTS immutable_events_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_events_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_facts_update BEFORE UPDATE ON fact_versions
BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS immutable_facts_delete BEFORE DELETE ON fact_versions
BEGIN SELECT RAISE(ABORT, 'facts are append-only'); END;
"""
