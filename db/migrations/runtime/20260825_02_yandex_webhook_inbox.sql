-- migrate:no-transaction
-- Создаёт долговечный inbox уведомлений Яндекс Маркета без включения обработки и выдачи.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS webhook_processing_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS seller.yandex_webhook_events (
  id bigserial PRIMARY KEY,
  workspace_id bigint REFERENCES seller.workspaces(id) ON DELETE SET NULL,
  connection_id bigint REFERENCES seller.marketplace_connections(id) ON DELETE SET NULL,
  event_fingerprint text NOT NULL,
  notification_type text NOT NULL,
  campaign_id text NOT NULL DEFAULT '',
  order_id text NOT NULL DEFAULT '',
  provider_status text NOT NULL DEFAULT '',
  provider_substatus text NOT NULL DEFAULT '',
  event_time timestamptz,
  source_ip text NOT NULL,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  processing_enabled_at_receive boolean NOT NULL DEFAULT false,
  processing_state text NOT NULL DEFAULT 'paused'
    CHECK (processing_state IN ('paused', 'received', 'processing', 'processed', 'ignored', 'failed', 'dead')),
  processing_attempts integer NOT NULL DEFAULT 0 CHECK (processing_attempts >= 0),
  processing_lock_token uuid,
  processing_locked_until timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_attempt_at timestamptz,
  last_error text NOT NULL DEFAULT '',
  processed_at timestamptz,
  duplicate_count integer NOT NULL DEFAULT 0 CHECK (duplicate_count >= 0),
  received_at timestamptz NOT NULL DEFAULT now(),
  last_received_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_yandex_webhook_events_fingerprint
  ON seller.yandex_webhook_events(event_fingerprint);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_yandex_webhook_events_order
  ON seller.yandex_webhook_events(connection_id, order_id, received_at DESC)
  WHERE order_id <> '';

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_yandex_webhook_events_pending
  ON seller.yandex_webhook_events(next_attempt_at, id)
  WHERE processing_state IN ('received', 'processing', 'failed');
