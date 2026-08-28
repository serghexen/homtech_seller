-- Self-service запуск магазина. Новые подключения остаются в безопасной
-- подготовке, а уже работающие магазины сохраняют прежнее поведение.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS launch_state text NOT NULL DEFAULT 'setup'
    CHECK (launch_state IN ('setup', 'running', 'paused')),
  ADD COLUMN IF NOT EXISTS fulfillment_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS fulfillment_started_by_user_id bigint
    REFERENCES seller.users(id) ON DELETE SET NULL,
  ADD COLUMN IF NOT EXISTS exclusive_control_confirmed_at timestamptz;

-- Время первого появления позиции в Seller отделяет исторический снимок от
-- заказов, которые действительно поступили после запуска выдачи.
ALTER TABLE seller.order_items
  ADD COLUMN IF NOT EXISTS first_seen_at timestamptz NOT NULL DEFAULT now();

CREATE TABLE IF NOT EXISTS seller.marketplace_connection_launch_events (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  from_state text NOT NULL CHECK (from_state IN ('setup', 'running', 'paused')),
  to_state text NOT NULL CHECK (to_state IN ('setup', 'running', 'paused')),
  automatic_stock_enabled boolean NOT NULL DEFAULT false,
  actor_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  readiness_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_marketplace_connection_launch_events_history
  ON seller.marketplace_connection_launch_events(connection_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_marketplace_connections_launch_poll
  ON seller.marketplace_connections(next_orders_poll_at, id)
  WHERE status='active' AND launch_state='running' AND orders_polling_enabled=true;

-- Любой ранее включённый рабочий контур означает, что магазин уже был введён
-- в эксплуатацию. Такой backfill не останавливает JoyCards, ASAT или Ozon.
UPDATE seller.marketplace_connections
SET launch_state='running',
    -- Момент миграции отделяет уже загруженный снимок от следующих заказов;
    -- ранее созданные fulfillment-записи продолжают жить независимо.
    -- clock_timestamp(), в отличие от now(), меняется внутри одной миграционной
    -- транзакции и гарантированно позже backfill first_seen_at выше.
    fulfillment_started_at=COALESCE(fulfillment_started_at, clock_timestamp()),
    exclusive_control_confirmed_at=COALESCE(exclusive_control_confirmed_at, created_at),
    updated_at=now()
WHERE launch_state='setup'
  AND (
    webhook_processing_enabled=true
    OR fulfillment_reservation_enabled=true
    OR fulfillment_outbound_enabled=true
    OR stock_outbound_enabled=true
    OR supplier_fulfillment_enabled=true
    OR orders_polling_enabled=true
  );
