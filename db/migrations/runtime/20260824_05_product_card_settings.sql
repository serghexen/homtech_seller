-- Локальные редактируемые настройки карточки Seller.
-- Они перекрывают импортированный снимок CRM, но никогда не отправляются в маркетплейс.
CREATE TABLE IF NOT EXISTS seller.product_card_settings (
  connection_id bigint NOT NULL,
  external_product_id text NOT NULL,
  manual_stock_limit integer NOT NULL DEFAULT 0 CHECK (manual_stock_limit BETWEEN 0 AND 1000000),
  sales_limit integer CHECK (sales_limit IS NULL OR sales_limit BETWEEN 1 AND 1000000),
  sales_limit_daily_extra integer NOT NULL DEFAULT 0 CHECK (sales_limit_daily_extra BETWEEN 0 AND 1000000),
  sales_limit_day date NOT NULL DEFAULT CURRENT_DATE,
  activation_instruction text NOT NULL DEFAULT '' CHECK (char_length(activation_instruction) <= 10000),
  updated_by_user_id bigint,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, external_product_id),
  FOREIGN KEY (connection_id, external_product_id)
    REFERENCES seller.catalog_items(connection_id, external_product_id)
    ON DELETE CASCADE,
  FOREIGN KEY (updated_by_user_id)
    REFERENCES seller.users(id)
    ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_product_card_settings_updated
  ON seller.product_card_settings(updated_at DESC);
