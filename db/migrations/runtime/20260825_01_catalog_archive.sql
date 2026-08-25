ALTER TABLE seller.catalog_items
  ADD COLUMN IF NOT EXISTS is_archived boolean NOT NULL DEFAULT false;

CREATE INDEX IF NOT EXISTS seller_catalog_items_archive_idx
  ON seller.catalog_items(connection_id, is_present, is_archived, title, external_product_id);
