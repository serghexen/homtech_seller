-- Помечает ограниченный счётчик чатов: UI показывает 99+, не выгружая тысячи
-- ожидающих диалогов Яндекс Маркета каждые десять минут.
ALTER TABLE seller.marketplace_dashboard_snapshots
  ADD COLUMN IF NOT EXISTS pending_chats_capped boolean NOT NULL DEFAULT false;
