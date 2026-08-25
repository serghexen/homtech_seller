-- Долговечная очередь внешней выдачи. По умолчанию отправка выключена для каждого магазина.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS fulfillment_outbound_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS seller.fulfillment_outbound_jobs (
  id bigserial PRIMARY KEY,
  public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  fulfillment_id bigint NOT NULL UNIQUE
    REFERENCES seller.order_fulfillments(id) ON DELETE RESTRICT,
  requested_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued', 'preparing', 'sending', 'submitted', 'unknown', 'failed', 'cancelled')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  request_fingerprint text NOT NULL DEFAULT '',
  last_error text NOT NULL DEFAULT '',
  lock_token uuid,
  locked_until timestamptz,
  queued_at timestamptz NOT NULL DEFAULT now(),
  sending_at timestamptz,
  submitted_at timestamptz,
  unknown_at timestamptz,
  failed_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_outbound_jobs_queue
  ON seller.fulfillment_outbound_jobs(state, queued_at, id);

CREATE INDEX IF NOT EXISTS idx_fulfillment_outbound_jobs_lease
  ON seller.fulfillment_outbound_jobs(state, locked_until)
  WHERE state IN ('preparing', 'sending');
