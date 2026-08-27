-- Долговечные multi-tenant уведомления Seller без сетевых вызовов из транзакций выдачи.
CREATE TABLE IF NOT EXISTS seller.telegram_bot_state (
  notifier_code text PRIMARY KEY,
  telegram_update_offset bigint NOT NULL DEFAULT 0 CHECK (telegram_update_offset >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO seller.telegram_bot_state(notifier_code)
VALUES ('seller_fulfillment_alerts')
ON CONFLICT (notifier_code) DO NOTHING;

CREATE TABLE IF NOT EXISTS seller.telegram_notification_recipients (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  chat_id bigint NOT NULL,
  chat_type text NOT NULL DEFAULT '',
  title text NOT NULL DEFAULT '',
  notifications_from_event_id bigint NOT NULL DEFAULT 0 CHECK (notifications_from_event_id >= 0),
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_recipients_active
  ON seller.telegram_notification_recipients(workspace_id, id)
  WHERE is_active=true;

CREATE TABLE IF NOT EXISTS seller.telegram_notification_events (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  fulfillment_id bigint NOT NULL REFERENCES seller.order_fulfillments(id) ON DELETE CASCADE,
  event_type text NOT NULL CHECK (event_type IN (
    'manual_required', 'unknown', 'error', 'cancelled', 'resolved'
  )),
  event_key text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telegram_events_workspace
  ON seller.telegram_notification_events(workspace_id, id);

CREATE TABLE IF NOT EXISTS seller.telegram_notification_deliveries (
  id bigserial PRIMARY KEY,
  event_id bigint NOT NULL REFERENCES seller.telegram_notification_events(id) ON DELETE CASCADE,
  recipient_id bigint NOT NULL REFERENCES seller.telegram_notification_recipients(id) ON DELETE CASCADE,
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued', 'sending', 'retry', 'sent', 'dead')),
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  max_attempts integer NOT NULL DEFAULT 240 CHECK (max_attempts BETWEEN 1 AND 1000),
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_by uuid,
  locked_until timestamptz,
  telegram_message_id bigint,
  last_error text NOT NULL DEFAULT '',
  sent_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (event_id, recipient_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_deliveries_queue
  ON seller.telegram_notification_deliveries(available_at, id)
  WHERE state IN ('queued', 'retry');

CREATE OR REPLACE FUNCTION seller.fulfillment_notification_alert_key(
  fulfillment_status text,
  fulfillment_error text
) RETURNS text
LANGUAGE sql
IMMUTABLE
AS $$
  SELECT CASE
    WHEN COALESCE(fulfillment_status, '')='cancelled' THEN 'cancelled'
    WHEN COALESCE(fulfillment_status, '')='unknown'
      THEN 'unknown:' || md5(COALESCE(fulfillment_error, ''))
    WHEN btrim(COALESCE(fulfillment_error, ''))<>''
      AND COALESCE(fulfillment_status, '') IN ('manual_required', 'reserved', 'failed')
      THEN 'error:' || md5(fulfillment_error)
    WHEN COALESCE(fulfillment_status, '')='manual_required' THEN 'manual_required'
    ELSE ''
  END
$$;

CREATE OR REPLACE FUNCTION seller.enqueue_fulfillment_telegram_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  old_alert_key text := '';
  new_alert_key text := '';
  notification_type text := '';
  notification_key text := '';
  connection_workspace_id bigint;
  connection_provider_code text;
  connection_display_name text;
  item_title text;
  item_sku text;
BEGIN
  new_alert_key := seller.fulfillment_notification_alert_key(NEW.status, NEW.last_error);
  IF TG_OP='UPDATE' THEN
    old_alert_key := seller.fulfillment_notification_alert_key(OLD.status, OLD.last_error);
  END IF;

  IF new_alert_key=old_alert_key THEN
    RETURN NEW;
  END IF;

  IF new_alert_key<>'' THEN
    notification_key := new_alert_key;
    notification_type := split_part(new_alert_key, ':', 1);
  ELSIF old_alert_key<>'' THEN
    notification_key := 'resolved:' || old_alert_key;
    notification_type := 'resolved';
  ELSE
    RETURN NEW;
  END IF;

  SELECT connection.workspace_id, connection.provider_code, connection.display_name,
         item.title, COALESCE(NULLIF(item.sku, ''), item.offer_id)
  INTO connection_workspace_id, connection_provider_code, connection_display_name,
       item_title, item_sku
  FROM seller.marketplace_connections AS connection
  JOIN seller.order_items AS item
    ON item.connection_id=NEW.connection_id
   AND item.external_order_id=NEW.external_order_id
   AND item.external_item_id=NEW.external_item_id
  WHERE connection.id=NEW.connection_id;

  IF connection_workspace_id IS NULL THEN
    RETURN NEW;
  END IF;

  INSERT INTO seller.telegram_notification_events(
    workspace_id, fulfillment_id, event_type, event_key, payload
  ) VALUES (
    connection_workspace_id,
    NEW.id,
    notification_type,
    notification_key,
    jsonb_build_object(
      'connection_id', NEW.connection_id,
      'provider_code', connection_provider_code,
      'store_name', connection_display_name,
      'external_order_id', NEW.external_order_id,
      'external_item_id', NEW.external_item_id,
      'offer_id', NEW.offer_id,
      'sku', COALESCE(item_sku, ''),
      'title', COALESCE(item_title, ''),
      'quantity', NEW.requested_quantity,
      'status', NEW.status,
      'last_error', NEW.last_error,
      'previous_alert_key', old_alert_key
    )
  );
  RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS order_fulfillments_telegram_event ON seller.order_fulfillments;

CREATE TRIGGER order_fulfillments_telegram_event
AFTER INSERT OR UPDATE OF status, last_error
ON seller.order_fulfillments
FOR EACH ROW
EXECUTE FUNCTION seller.enqueue_fulfillment_telegram_event();
