-- Нормализует заказ целиком для безопасного подсчёта оборота и хранит
-- отдельный read-only снимок обращений покупателей по каждому подключению.
CREATE TABLE IF NOT EXISTS seller.marketplace_orders (
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  external_order_id text NOT NULL,
  provider_status text NOT NULL DEFAULT '',
  provider_substatus text NOT NULL DEFAULT '',
  normalized_status text NOT NULL DEFAULT 'problem'
    CHECK (normalized_status IN ('processing','in_delivery','delivered','cancelled','problem')),
  created_at timestamptz,
  updated_at timestamptz,
  sales_amount numeric(20, 4) CHECK (sales_amount IS NULL OR sales_amount >= 0),
  currency_code text NOT NULL DEFAULT '',
  is_fake boolean NOT NULL DEFAULT false,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  synced_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, external_order_id)
);

WITH latest_order AS (
  SELECT DISTINCT ON (item.connection_id, item.external_order_id)
         item.connection_id, item.external_order_id, connection.provider_code,
         item.provider_status, item.provider_substatus, item.normalized_status,
         item.created_at, item.updated_at, item.raw_payload, item.synced_at
  FROM seller.order_items AS item
  JOIN seller.marketplace_connections AS connection ON connection.id=item.connection_id
  ORDER BY item.connection_id, item.external_order_id, item.synced_at DESC, item.external_item_id
)
INSERT INTO seller.marketplace_orders(
  connection_id, external_order_id, provider_status, provider_substatus,
  normalized_status, created_at, updated_at, sales_amount, currency_code,
  is_fake, raw_payload, synced_at
)
SELECT connection_id, external_order_id, provider_status, provider_substatus,
       normalized_status, created_at, updated_at,
       CASE WHEN provider_code='yandex_market' THEN
         COALESCE(CASE WHEN raw_payload #>> '{prices,payment,value}' ~ '^[0-9]+([.][0-9]+)?$'
                       THEN (raw_payload #>> '{prices,payment,value}')::numeric END, 0)
         + COALESCE(CASE WHEN raw_payload #>> '{prices,cashback,value}' ~ '^[0-9]+([.][0-9]+)?$'
                         THEN (raw_payload #>> '{prices,cashback,value}')::numeric END, 0)
         + COALESCE(CASE WHEN raw_payload #>> '{prices,subsidy,value}' ~ '^[0-9]+([.][0-9]+)?$'
                         THEN (raw_payload #>> '{prices,subsidy,value}')::numeric END, 0)
         ELSE NULL END,
       COALESCE(
         raw_payload #>> '{prices,payment,currencyId}',
         raw_payload #>> '{prices,cashback,currencyId}',
         raw_payload #>> '{prices,subsidy,currencyId}',
         ''
       ),
       CASE WHEN lower(COALESCE(raw_payload->>'fake', ''))='true' THEN true ELSE false END,
       raw_payload, synced_at
FROM latest_order
ON CONFLICT (connection_id, external_order_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_marketplace_orders_connection_created
  ON seller.marketplace_orders(connection_id, created_at DESC, external_order_id);

CREATE TABLE IF NOT EXISTS seller.marketplace_dashboard_snapshots (
  connection_id bigint PRIMARY KEY REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  pending_reviews_count integer NOT NULL DEFAULT 0 CHECK (pending_reviews_count >= 0),
  pending_chats_count integer NOT NULL DEFAULT 0 CHECK (pending_chats_count >= 0),
  unassigned_reviews_count integer NOT NULL DEFAULT 0 CHECK (unassigned_reviews_count >= 0),
  last_successful_sync_at timestamptz,
  last_attempt_at timestamptz,
  next_refresh_at timestamptz NOT NULL DEFAULT now(),
  last_error text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, connection_id)
);

CREATE INDEX IF NOT EXISTS idx_marketplace_dashboard_snapshots_due
  ON seller.marketplace_dashboard_snapshots(next_refresh_at, connection_id);

ALTER TABLE seller.marketplace_sync_jobs
  DROP CONSTRAINT IF EXISTS marketplace_sync_jobs_sync_kind_check;

ALTER TABLE seller.marketplace_sync_jobs
  ADD CONSTRAINT marketplace_sync_jobs_sync_kind_check
  CHECK (sync_kind IN ('catalog', 'orders', 'dashboard'));
