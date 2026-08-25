-- Безопасная основа выдачи: локальная запись на позицию заказа и резерв ключей без их раскрытия или отправки.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS fulfillment_reservation_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE seller.product_card_settings
  ADD COLUMN IF NOT EXISTS pool_issue_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS seller.order_fulfillments (
  id bigserial PRIMARY KEY,
  public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  connection_id bigint NOT NULL,
  external_order_id text NOT NULL,
  external_item_id text NOT NULL,
  offer_id text NOT NULL,
  requested_quantity integer NOT NULL CHECK (requested_quantity > 0),
  status text NOT NULL DEFAULT 'pending'
    CHECK (status IN (
      'pending', 'reserved', 'manual_required', 'supplier_required',
      'sending', 'submitted', 'unknown', 'delivered', 'cancelled',
      'closed_external', 'failed'
    )),
  delivery_source text NOT NULL DEFAULT 'unassigned'
    CHECK (delivery_source IN ('unassigned', 'pool', 'supplier', 'manual', 'external')),
  reservation_ref text NOT NULL UNIQUE,
  last_error text NOT NULL DEFAULT '',
  reserved_at timestamptz,
  submitted_at timestamptz,
  delivered_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, external_order_id, external_item_id),
  FOREIGN KEY (connection_id, external_order_id, external_item_id)
    REFERENCES seller.order_items(connection_id, external_order_id, external_item_id)
    ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_order_fulfillments_queue
  ON seller.order_fulfillments(status, updated_at, id);

CREATE TABLE IF NOT EXISTS seller.fulfillment_key_reservations (
  id bigserial PRIMARY KEY,
  fulfillment_id bigint NOT NULL REFERENCES seller.order_fulfillments(id) ON DELETE RESTRICT,
  key_id bigint NOT NULL REFERENCES seller.marketplace_keys(id) ON DELETE RESTRICT,
  state text NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved', 'released', 'consumed')),
  order_ref text NOT NULL,
  reserved_at timestamptz NOT NULL DEFAULT now(),
  released_at timestamptz,
  consumed_at timestamptz,
  release_reason text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_fulfillment_key_active_reservation
  ON seller.fulfillment_key_reservations(key_id)
  WHERE state='reserved';

CREATE UNIQUE INDEX IF NOT EXISTS uq_fulfillment_active_key
  ON seller.fulfillment_key_reservations(fulfillment_id, key_id)
  WHERE state='reserved';

CREATE INDEX IF NOT EXISTS idx_fulfillment_reservations_fulfillment
  ON seller.fulfillment_key_reservations(fulfillment_id, state, id);

CREATE TABLE IF NOT EXISTS seller.fulfillment_events (
  id bigserial PRIMARY KEY,
  fulfillment_id bigint NOT NULL REFERENCES seller.order_fulfillments(id) ON DELETE CASCADE,
  event_type text NOT NULL,
  from_status text NOT NULL DEFAULT '',
  to_status text NOT NULL DEFAULT '',
  details jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fulfillment_events_history
  ON seller.fulfillment_events(fulfillment_id, created_at, id);
