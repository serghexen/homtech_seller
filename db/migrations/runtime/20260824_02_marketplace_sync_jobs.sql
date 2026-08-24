-- Долговечная очередь синхронизации без зависимости от памяти HTTP-процесса.
CREATE TABLE IF NOT EXISTS seller.marketplace_sync_jobs (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  sync_kind text NOT NULL CHECK (sync_kind IN ('catalog', 'orders')),
  status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 4 CHECK (max_attempts BETWEEN 1 AND 10),
  available_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  heartbeat_at timestamptz,
  finished_at timestamptz,
  error text NOT NULL DEFAULT '',
  synced_items integer NOT NULL DEFAULT 0 CHECK (synced_items >= 0),
  requested_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_seller_sync_jobs_one_active_kind
  ON seller.marketplace_sync_jobs(connection_id, sync_kind)
  WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS idx_seller_sync_jobs_claim
  ON seller.marketplace_sync_jobs(status, available_at, id)
  WHERE status='queued';

CREATE INDEX IF NOT EXISTS idx_seller_sync_jobs_workspace_created
  ON seller.marketplace_sync_jobs(workspace_id, created_at DESC, id DESC);
