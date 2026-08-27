-- Явно фиксирует владельца текущей подготовки: автоматическая цепочка или оператор.
ALTER TABLE seller.order_fulfillments
  ADD COLUMN IF NOT EXISTS handling_mode text NOT NULL DEFAULT 'unassigned';

UPDATE seller.order_fulfillments AS fulfillment
SET handling_mode=CASE
  WHEN EXISTS (
    SELECT 1 FROM seller.supplier_purchase_attempts AS attempt
    WHERE attempt.fulfillment_id=fulfillment.id
      AND attempt.result_key_id IS NULL
      AND (
        attempt.state IN ('queued','created','checked','payment_started','processing','requires_attention')
        OR attempt.blocks_fallback=true
      )
  ) THEN 'automatic'
  WHEN fulfillment.status='manual_required' OR fulfillment.delivery_source='manual' THEN 'manual'
  WHEN fulfillment.status IN ('pending','supplier_required') THEN 'automatic'
  WHEN EXISTS (
    SELECT 1 FROM seller.supplier_purchase_attempts AS attempt
    WHERE attempt.fulfillment_id=fulfillment.id
  ) THEN 'automatic'
  ELSE 'unassigned'
END
WHERE fulfillment.handling_mode='unassigned';

ALTER TABLE seller.order_fulfillments
  DROP CONSTRAINT IF EXISTS order_fulfillments_handling_mode_check;

ALTER TABLE seller.order_fulfillments
  ADD CONSTRAINT order_fulfillments_handling_mode_check
  CHECK (handling_mode IN ('unassigned','automatic','manual'));

CREATE INDEX IF NOT EXISTS idx_order_fulfillments_handling
  ON seller.order_fulfillments(handling_mode, status, updated_at, id);
