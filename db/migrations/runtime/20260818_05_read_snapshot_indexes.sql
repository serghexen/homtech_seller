-- migrate:no-transaction
-- Ускоряет фильтрацию локальных снимков каталога и заказов в отдельных workspace.
CREATE INDEX CONCURRENTLY IF NOT EXISTS seller_catalog_items_connection_synced_idx
  ON seller.catalog_items (connection_id, synced_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS seller_order_items_connection_created_idx
  ON seller.order_items (connection_id, created_at DESC);

CREATE INDEX CONCURRENTLY IF NOT EXISTS seller_order_items_connection_status_created_idx
  ON seller.order_items (connection_id, normalized_status, created_at DESC);
