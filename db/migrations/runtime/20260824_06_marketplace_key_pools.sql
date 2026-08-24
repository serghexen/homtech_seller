-- Локальные пулы ключей Seller. Таблицы пока не участвуют в автоматической выдаче.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS seller.marketplace_key_pools (
  id bigserial PRIMARY KEY,
  connection_id bigint NOT NULL,
  external_product_id text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, external_product_id),
  FOREIGN KEY (connection_id, external_product_id)
    REFERENCES seller.catalog_items(connection_id, external_product_id)
    ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS seller.marketplace_keys (
  id bigserial PRIMARY KEY,
  pool_id bigint NOT NULL REFERENCES seller.marketplace_key_pools(id) ON DELETE RESTRICT,
  code_ciphertext bytea NOT NULL,
  code_hash text NOT NULL UNIQUE,
  code_suffix text NOT NULL DEFAULT '',
  status text NOT NULL DEFAULT 'free'
    CHECK (status IN ('free', 'reserved', 'sending', 'delivered', 'expired', 'disabled')),
  expires_at date,
  issued_order_ref text NOT NULL DEFAULT '',
  reserved_at timestamptz,
  issued_at timestamptz,
  source_system text NOT NULL DEFAULT 'seller'
    CHECK (source_system IN ('seller', 'crm')),
  source_key_id bigint,
  created_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source_system, source_key_id)
);

CREATE INDEX IF NOT EXISTS idx_marketplace_keys_pool_status
  ON seller.marketplace_keys(pool_id, status, created_at DESC);
