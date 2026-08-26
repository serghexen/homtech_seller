-- Публикация заданного остатка запускается отдельно от выдачи и по умолчанию полностью выключена.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS stock_outbound_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE seller.product_card_settings
  ADD COLUMN IF NOT EXISTS published_stock integer CHECK (published_stock IS NULL OR published_stock >= 0),
  ADD COLUMN IF NOT EXISTS last_stock_sync_at timestamptz,
  ADD COLUMN IF NOT EXISTS last_stock_sync_error text NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS seller.yandex_stock_outbound_jobs (
  id bigserial PRIMARY KEY,
  fulfillment_id bigint NOT NULL UNIQUE
    REFERENCES seller.order_fulfillments(id) ON DELETE RESTRICT,
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued','preparing','sending','succeeded','failed')),
  target_stock integer CHECK (target_stock IS NULL OR target_stock >= 0),
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
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Подготавливает один актуальный пересчёт для уже завершённых Seller-выдач.
-- Одна карточка даёт не более одного задания, а оба kill switch всё ещё выключены.
INSERT INTO seller.yandex_stock_outbound_jobs(fulfillment_id)
SELECT latest.id
FROM (
  SELECT DISTINCT ON (connection_id, offer_id) id
  FROM seller.order_fulfillments
  WHERE status IN ('submitted','delivered')
  ORDER BY connection_id, offer_id, COALESCE(delivered_at, submitted_at, updated_at) DESC, id DESC
) AS latest
ON CONFLICT (fulfillment_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_yandex_stock_outbound_queue
  ON seller.yandex_stock_outbound_jobs(state, next_attempt_at, id);

CREATE INDEX IF NOT EXISTS idx_yandex_stock_outbound_lease
  ON seller.yandex_stock_outbound_jobs(state, locked_until)
  WHERE state IN ('preparing','sending');
