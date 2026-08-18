-- migrate:no-transaction
-- Ускоряет выборки каталога и заказов, не блокируя рабочие таблицы при будущих релизах.
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_seller_connections_workspace
  ON seller.marketplace_connections(workspace_id, created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_seller_catalog_connection_title
  ON seller.catalog_items(connection_id, title);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_seller_orders_connection_created
  ON seller.order_items(connection_id, created_at DESC, external_order_id DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_seller_orders_connection_status_created
  ON seller.order_items(connection_id, normalized_status, created_at DESC);
