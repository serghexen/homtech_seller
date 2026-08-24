-- Сохраняет исчезнувшие карточки как архив, не удаляя связанные настройки и историю.
ALTER TABLE seller.catalog_items
  ADD COLUMN IF NOT EXISTS is_present boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS archived_at timestamptz;

CREATE INDEX IF NOT EXISTS seller_catalog_items_present_idx
  ON seller.catalog_items(connection_id, title, external_product_id)
  WHERE is_present = true;
