-- Ozon получает цифровые заказы polling-ом. Все изменяющие Ozon переключатели
-- остаются выключенными по умолчанию; polling читает данные и не выдаёт ключи.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS orders_polling_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS orders_poll_interval_seconds integer NOT NULL DEFAULT 60
    CHECK (orders_poll_interval_seconds BETWEEN 10 AND 3600),
  ADD COLUMN IF NOT EXISTS next_orders_poll_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS last_orders_poll_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_orders_poll_error text NOT NULL DEFAULT '';

ALTER TABLE seller.order_items
  ADD COLUMN IF NOT EXISTS fulfillment_deadline_at timestamptz;

CREATE INDEX IF NOT EXISTS idx_marketplace_connections_orders_poll
  ON seller.marketplace_connections(next_orders_poll_at, id)
  WHERE status='active' AND orders_polling_enabled=true;

-- Публикация Ozon-остатка является отдельной идемпотентной очередью. Она
-- запускается только после подтверждённой отправки или явного действия оператора.
CREATE TABLE IF NOT EXISTS seller.ozon_stock_outbound_jobs (
  id bigserial PRIMARY KEY,
  fulfillment_id bigint REFERENCES seller.order_fulfillments(id) ON DELETE RESTRICT,
  job_kind text NOT NULL DEFAULT 'fulfillment'
    CHECK (job_kind IN ('fulfillment','manual')),
  connection_id bigint REFERENCES seller.marketplace_connections(id) ON DELETE RESTRICT,
  external_product_id text,
  requested_stock integer CHECK (requested_stock IS NULL OR requested_stock BETWEEN 0 AND 1000000),
  requested_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued','preparing','sending','succeeded','failed')),
  target_stock integer CHECK (target_stock IS NULL OR target_stock BETWEEN 0 AND 1000000),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 50),
  last_error text NOT NULL DEFAULT '',
  lock_token uuid,
  locked_until timestamptz,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  sending_at timestamptz,
  succeeded_at timestamptz,
  failed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ozon_stock_outbound_job_target_check CHECK (
    (job_kind='fulfillment' AND fulfillment_id IS NOT NULL
      AND connection_id IS NULL AND external_product_id IS NULL AND requested_stock IS NULL)
    OR
    (job_kind='manual' AND fulfillment_id IS NULL
      AND connection_id IS NOT NULL AND external_product_id IS NOT NULL AND requested_stock IS NOT NULL)
  )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_stock_fulfillment_job
  ON seller.ozon_stock_outbound_jobs(fulfillment_id)
  WHERE job_kind='fulfillment';

CREATE UNIQUE INDEX IF NOT EXISTS uq_ozon_stock_manual_active_job
  ON seller.ozon_stock_outbound_jobs(connection_id, external_product_id)
  WHERE job_kind='manual' AND state IN ('queued','preparing','sending');

CREATE INDEX IF NOT EXISTS idx_ozon_stock_outbound_queue
  ON seller.ozon_stock_outbound_jobs(state, next_attempt_at, id);

