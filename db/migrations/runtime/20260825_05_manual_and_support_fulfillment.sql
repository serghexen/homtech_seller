-- Два дополнительных источника подготовки используют тот же безопасный outbound-контур.
ALTER TABLE seller.product_card_settings
  ADD COLUMN IF NOT EXISTS support_message text NOT NULL DEFAULT '';

ALTER TABLE seller.order_fulfillments
  ADD COLUMN IF NOT EXISTS support_message_snapshot text NOT NULL DEFAULT '';

ALTER TABLE seller.order_fulfillments
  DROP CONSTRAINT IF EXISTS order_fulfillments_delivery_source_check;

ALTER TABLE seller.order_fulfillments
  ADD CONSTRAINT order_fulfillments_delivery_source_check
  CHECK (delivery_source IN ('unassigned', 'pool', 'supplier', 'manual', 'support_message', 'external'));

ALTER TABLE seller.order_fulfillments
  DROP CONSTRAINT IF EXISTS order_fulfillments_support_message_length_check;

ALTER TABLE seller.order_fulfillments
  ADD CONSTRAINT order_fulfillments_support_message_length_check
  CHECK (char_length(support_message_snapshot) <= 2000);
