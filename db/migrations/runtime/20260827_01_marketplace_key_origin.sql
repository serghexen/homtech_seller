-- Отделяет управляемый оператором пул карточки от ключей, появившихся внутри конкретной выдачи.
ALTER TABLE seller.marketplace_keys
  ADD COLUMN key_origin text NOT NULL DEFAULT 'pool'
    CHECK (key_origin IN ('pool', 'order'));

-- Ключи Supplier Hub всегда покупались для конкретного заказа и не являются остатком ручного пула.
UPDATE seller.marketplace_keys
SET key_origin='order'
WHERE source_system='supplier_hub';

-- Ручные комплекты Seller уже связаны с fulfillment; восстанавливаем их происхождение по журналу событий.
UPDATE seller.marketplace_keys AS key
SET key_origin='order'
WHERE EXISTS (
  SELECT 1
  FROM seller.fulfillment_key_reservations AS reservation
  JOIN seller.fulfillment_events AS event
    ON event.fulfillment_id=reservation.fulfillment_id
   AND event.event_type='manual_keys_prepared'
  WHERE reservation.key_id=key.id
);

CREATE INDEX idx_marketplace_keys_pool_origin_status
  ON seller.marketplace_keys(pool_id, key_origin, status, created_at DESC);
