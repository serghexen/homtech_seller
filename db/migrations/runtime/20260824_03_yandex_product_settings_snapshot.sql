-- Хранит read-only снимок настроек карточки, перенесённый из CRM.
-- Таблица не участвует в публикации остатков и не содержит внешних токенов.
CREATE TABLE IF NOT EXISTS seller.yandex_product_settings_snapshot (
  connection_id bigint NOT NULL,
  external_product_id text NOT NULL,
  source_store_code text NOT NULL,
  manual_stock_limit integer NOT NULL DEFAULT 0 CHECK (manual_stock_limit >= 0),
  published_stock integer NOT NULL DEFAULT 0 CHECK (published_stock >= 0),
  activation_instruction text NOT NULL DEFAULT '',
  sales_limit integer CHECK (sales_limit IS NULL OR sales_limit > 0),
  sales_limit_daily_extra integer NOT NULL DEFAULT 0 CHECK (sales_limit_daily_extra >= 0),
  sales_limit_day date,
  sales_limit_revision bigint NOT NULL DEFAULT 0 CHECK (sales_limit_revision >= 0),
  sales_limit_used integer NOT NULL DEFAULT 0 CHECK (sales_limit_used >= 0),
  sales_limit_reserved integer NOT NULL DEFAULT 0 CHECK (sales_limit_reserved >= 0),
  sales_limit_remaining integer CHECK (sales_limit_remaining IS NULL OR sales_limit_remaining >= 0),
  sales_limit_exhausted_at timestamptz,
  archived_by_sales_limit boolean NOT NULL DEFAULT false,
  last_stock_sync_at timestamptz,
  source_updated_at timestamptz NOT NULL,
  imported_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, external_product_id),
  FOREIGN KEY (connection_id, external_product_id)
    REFERENCES seller.catalog_items(connection_id, external_product_id)
    ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_yandex_product_settings_snapshot_source
  ON seller.yandex_product_settings_snapshot(source_store_code, source_updated_at DESC);
