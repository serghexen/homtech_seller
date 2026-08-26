-- Политика выдачи карточки, связь с Supplier Hub и долговечные попытки покупки.
-- Все магазинные переключатели по умолчанию выключены, поэтому одна миграция
-- не может начать покупку или отправку цифрового товара.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS supplier_fulfillment_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS seller.product_fulfillment_policies (
  connection_id bigint NOT NULL,
  external_product_id text NOT NULL,
  supplier_issue_enabled boolean NOT NULL DEFAULT false,
  pool_issue_enabled boolean NOT NULL DEFAULT false,
  support_message_delivery_enabled boolean NOT NULL DEFAULT false,
  source_system text NOT NULL DEFAULT 'seller'
    CHECK (source_system IN ('seller', 'crm')),
  source_updated_at timestamptz,
  updated_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, external_product_id),
  FOREIGN KEY (connection_id, external_product_id)
    REFERENCES seller.catalog_items(connection_id, external_product_id)
    ON DELETE RESTRICT
);

-- Сохраняем уже настроенные в Seller локальные способы выдачи.
INSERT INTO seller.product_fulfillment_policies(
  connection_id, external_product_id, pool_issue_enabled,
  support_message_delivery_enabled, source_system, updated_by_user_id,
  source_updated_at, updated_at
)
SELECT connection_id, external_product_id, pool_issue_enabled,
       support_message_delivery_enabled, 'seller', updated_by_user_id,
       updated_at, updated_at
FROM seller.product_card_settings
WHERE pool_issue_enabled=true OR support_message_delivery_enabled=true
ON CONFLICT (connection_id, external_product_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS seller.product_supplier_mappings (
  id bigserial PRIMARY KEY,
  connection_id bigint NOT NULL,
  external_product_id text NOT NULL,
  provider_code text NOT NULL,
  priority integer NOT NULL DEFAULT 1 CHECK (priority > 0),
  enabled boolean NOT NULL DEFAULT false,
  service_id integer NOT NULL CHECK (service_id > 0),
  nominal_id text NOT NULL DEFAULT '',
  params jsonb NOT NULL DEFAULT '{}'::jsonb,
  max_amount numeric(18,6) NOT NULL CHECK (max_amount > 0),
  quoted_amount numeric(18,6) CHECK (quoted_amount IS NULL OR quoted_amount > 0),
  quoted_at timestamptz,
  source_system text NOT NULL DEFAULT 'seller'
    CHECK (source_system IN ('seller', 'crm')),
  source_updated_at timestamptz,
  updated_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, external_product_id, provider_code, priority),
  FOREIGN KEY (connection_id, external_product_id)
    REFERENCES seller.catalog_items(connection_id, external_product_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_product_supplier_mappings_enabled
  ON seller.product_supplier_mappings(connection_id, enabled, priority, external_product_id);

ALTER TABLE seller.marketplace_keys
  DROP CONSTRAINT IF EXISTS marketplace_keys_source_system_check;

ALTER TABLE seller.marketplace_keys
  ADD CONSTRAINT marketplace_keys_source_system_check
  CHECK (source_system IN ('seller', 'crm', 'supplier_hub'));

ALTER TABLE seller.marketplace_keys
  ADD COLUMN IF NOT EXISTS source_reference text;

CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_keys_source_reference
  ON seller.marketplace_keys(source_system, source_reference)
  WHERE source_reference IS NOT NULL;

ALTER TABLE seller.order_fulfillments
  ADD COLUMN IF NOT EXISTS next_resolve_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN IF NOT EXISTS resolver_lock_token uuid,
  ADD COLUMN IF NOT EXISTS resolver_locked_until timestamptz;

CREATE INDEX IF NOT EXISTS idx_order_fulfillments_resolver_queue
  ON seller.order_fulfillments(next_resolve_at, updated_at, id)
  WHERE status IN ('pending', 'manual_required', 'supplier_required');

CREATE TABLE IF NOT EXISTS seller.supplier_purchase_attempts (
  id bigserial PRIMARY KEY,
  fulfillment_id bigint NOT NULL
    REFERENCES seller.order_fulfillments(id) ON DELETE RESTRICT,
  supplier_mapping_id bigint NOT NULL
    REFERENCES seller.product_supplier_mappings(id) ON DELETE RESTRICT,
  unit_index integer NOT NULL CHECK (unit_index > 0),
  idempotency_key text NOT NULL UNIQUE,
  request_id text NOT NULL DEFAULT '',
  hub_purchase_id uuid UNIQUE,
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN (
      'queued', 'created', 'checked', 'payment_started', 'processing',
      'succeeded', 'failed', 'requires_attention'
    )),
  max_amount numeric(18,6) NOT NULL CHECK (max_amount > 0),
  amount numeric(18,6),
  blocks_fallback boolean NOT NULL DEFAULT false,
  result_available boolean NOT NULL DEFAULT false,
  result_key_id bigint UNIQUE REFERENCES seller.marketplace_keys(id) ON DELETE RESTRICT,
  provider_status integer,
  provider_message text NOT NULL DEFAULT '',
  last_error text NOT NULL DEFAULT '',
  next_poll_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  UNIQUE (fulfillment_id, unit_index)
);

CREATE INDEX IF NOT EXISTS idx_supplier_purchase_attempts_poll
  ON seller.supplier_purchase_attempts(next_poll_at, id)
  WHERE state IN ('queued', 'created', 'checked', 'payment_started', 'processing', 'succeeded');
