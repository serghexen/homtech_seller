-- Создаёт независимую модель пользователей, организаций и read-only данных Seller.
CREATE SCHEMA IF NOT EXISTS seller;

CREATE TABLE IF NOT EXISTS seller.users (
  id bigserial PRIMARY KEY,
  email text NOT NULL UNIQUE,
  display_name text NOT NULL DEFAULT '',
  auth_subject text UNIQUE,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller.workspaces (
  id bigserial PRIMARY KEY,
  name text NOT NULL,
  owner_user_id bigint NOT NULL REFERENCES seller.users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller.workspace_members (
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  user_id bigint NOT NULL REFERENCES seller.users(id) ON DELETE RESTRICT,
  role_code text NOT NULL CHECK (role_code IN ('owner', 'operator', 'viewer')),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE IF NOT EXISTS seller.marketplace_connections (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  provider_code text NOT NULL CHECK (provider_code IN ('ozon', 'yandex_market')),
  display_name text NOT NULL,
  client_id text NOT NULL DEFAULT '',
  business_id text NOT NULL DEFAULT '',
  campaign_id text NOT NULL DEFAULT '',
  token_ciphertext bytea NOT NULL,
  token_suffix text NOT NULL DEFAULT '',
  encryption_key_version smallint NOT NULL DEFAULT 1,
  status text NOT NULL DEFAULT 'saved' CHECK (status IN ('saved', 'active', 'error', 'disabled')),
  last_checked_at timestamptz,
  last_error text NOT NULL DEFAULT '',
  created_by_user_id bigint NOT NULL REFERENCES seller.users(id) ON DELETE RESTRICT,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, provider_code, client_id, campaign_id)
);

CREATE TABLE IF NOT EXISTS seller.catalog_items (
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  external_product_id text NOT NULL,
  offer_id text NOT NULL DEFAULT '',
  sku text NOT NULL DEFAULT '',
  title text NOT NULL DEFAULT '',
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  synced_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, external_product_id)
);

CREATE TABLE IF NOT EXISTS seller.order_items (
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  external_order_id text NOT NULL,
  external_item_id text NOT NULL,
  offer_id text NOT NULL DEFAULT '',
  sku text NOT NULL DEFAULT '',
  title text NOT NULL DEFAULT '',
  quantity integer NOT NULL DEFAULT 1 CHECK (quantity >= 0),
  provider_status text NOT NULL DEFAULT '',
  provider_substatus text NOT NULL DEFAULT '',
  normalized_status text NOT NULL DEFAULT 'problem' CHECK (normalized_status IN ('processing', 'in_delivery', 'delivered', 'cancelled', 'problem')),
  delivery_type text NOT NULL DEFAULT '',
  created_at timestamptz,
  updated_at timestamptz,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  synced_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, external_order_id, external_item_id)
);
