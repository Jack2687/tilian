-- Durable source of truth for distributed mode.
-- PostgreSQL syntax; MySQL 8 uses the same columns with BIGINT/BINARY(16)
-- adjustments documented in docs/high-availability.md.
CREATE TABLE IF NOT EXISTS tasks (
  id VARCHAR(64) PRIMARY KEY,
  uid VARCHAR(128) NOT NULL,
  mode VARCHAR(32) NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL,
  version BIGINT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS task_items (
  task_id VARCHAR(64) NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  item_index INTEGER NOT NULL,
  status VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (task_id, item_index)
);

CREATE TABLE IF NOT EXISTS quota_cards (
  code VARCHAR(128) PRIMARY KEY,
  quota BIGINT NOT NULL,
  used_quota BIGINT NOT NULL DEFAULT 0,
  reserved_quota BIGINT NOT NULL DEFAULT 0,
  status VARCHAR(32) NOT NULL,
  version BIGINT NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS quota_ledger (
  id BIGSERIAL PRIMARY KEY,
  request_id VARCHAR(128) NOT NULL,
  card_code VARCHAR(128) NOT NULL REFERENCES quota_cards(code),
  task_id VARCHAR(64),
  item_index INTEGER,
  delta BIGINT NOT NULL,
  kind VARCHAR(32) NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (request_id, kind)
);

CREATE TABLE IF NOT EXISTS execution_leases (
  lease_key VARCHAR(256) PRIMARY KEY,
  coordinator_id VARCHAR(256) NOT NULL,
  task_id VARCHAR(64) NOT NULL,
  item_index INTEGER NOT NULL,
  kind VARCHAR(32) NOT NULL,
  expires_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS coordinator_members (
  coordinator_id VARCHAR(256) PRIMARY KEY,
  last_seen_at TIMESTAMPTZ NOT NULL,
  is_leader BOOLEAN NOT NULL DEFAULT false,
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS task_items_status_idx ON task_items(status);
CREATE INDEX IF NOT EXISTS execution_leases_expiry_idx ON execution_leases(expires_at);
