-- Неоплаченный DBS-заказ Яндекса остаётся в read-only снимке, но уведомление
-- создаётся только в момент появления локального fulfillment: это точная граница,
-- после которой Seller действительно взял позицию в работу.
ALTER TABLE seller.order_activity_events
  DROP CONSTRAINT IF EXISTS order_activity_events_event_type_check;

ALTER TABLE seller.order_activity_events
  ADD CONSTRAINT order_activity_events_event_type_check
  CHECK (event_type IN ('new_order', 'fulfillment_started', 'status_changed'));

CREATE OR REPLACE FUNCTION seller.capture_yandex_fulfillment_started_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  event_workspace_id bigint;
  event_provider_code text;
  event_order_status text;
BEGIN
  SELECT connection.workspace_id, connection.provider_code, item.normalized_status
  INTO event_workspace_id, event_provider_code, event_order_status
  FROM seller.marketplace_connections AS connection
  JOIN seller.order_items AS item
    ON item.connection_id=connection.id
   AND item.external_order_id=NEW.external_order_id
   AND item.external_item_id=NEW.external_item_id
  WHERE connection.id=NEW.connection_id;

  IF event_workspace_id IS NULL THEN
    RAISE EXCEPTION 'Workspace or order item for fulfillment % is unavailable', NEW.id;
  END IF;

  IF event_provider_code='yandex_market' THEN
    INSERT INTO seller.order_activity_events(
      workspace_id, connection_id, external_order_id, external_item_id,
      event_type, order_status, previous_status
    ) VALUES (
      event_workspace_id, NEW.connection_id, NEW.external_order_id, NEW.external_item_id,
      'fulfillment_started', COALESCE(event_order_status, ''), ''
    );
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_yandex_fulfillment_started_activity
  ON seller.order_fulfillments;
CREATE TRIGGER trg_yandex_fulfillment_started_activity
AFTER INSERT ON seller.order_fulfillments
FOR EACH ROW EXECUTE FUNCTION seller.capture_yandex_fulfillment_started_event();
