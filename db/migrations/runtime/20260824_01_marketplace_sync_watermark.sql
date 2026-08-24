-- Хранит безопасную границу последней полностью успешной синхронизации заказов.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS last_successful_sync_at timestamptz;
