-- Долговечный курсор изменений заказов для интерфейса Seller. События всегда
-- принадлежат workspace и connection; браузер не опрашивает маркетплейс.
CREATE TABLE IF NOT EXISTS seller.order_activity_events (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  external_order_id text NOT NULL,
  external_item_id text NOT NULL,
  event_type text NOT NULL CHECK (event_type IN ('new_order', 'status_changed')),
  order_status text NOT NULL DEFAULT '',
  previous_status text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_order_activity_events_workspace_cursor
  ON seller.order_activity_events(workspace_id, id);

CREATE INDEX IF NOT EXISTS idx_order_activity_events_connection_cursor
  ON seller.order_activity_events(connection_id, id);

CREATE OR REPLACE FUNCTION seller.capture_order_activity_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
  event_workspace_id bigint;
BEGIN
  SELECT connection.workspace_id
  INTO event_workspace_id
  FROM seller.marketplace_connections AS connection
  WHERE connection.id=NEW.connection_id;

  IF event_workspace_id IS NULL THEN
    RAISE EXCEPTION 'Workspace for marketplace connection % is unavailable', NEW.connection_id;
  END IF;

  IF TG_OP='INSERT' THEN
    INSERT INTO seller.order_activity_events(
      workspace_id, connection_id, external_order_id, external_item_id,
      event_type, order_status, previous_status
    ) VALUES (
      event_workspace_id, NEW.connection_id, NEW.external_order_id, NEW.external_item_id,
      'new_order', NEW.normalized_status, ''
    );
  ELSIF NEW.normalized_status IS DISTINCT FROM OLD.normalized_status THEN
    INSERT INTO seller.order_activity_events(
      workspace_id, connection_id, external_order_id, external_item_id,
      event_type, order_status, previous_status
    ) VALUES (
      event_workspace_id, NEW.connection_id, NEW.external_order_id, NEW.external_item_id,
      'status_changed', NEW.normalized_status, OLD.normalized_status
    );
  END IF;

  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_order_items_activity_insert ON seller.order_items;
CREATE TRIGGER trg_order_items_activity_insert
AFTER INSERT ON seller.order_items
FOR EACH ROW EXECUTE FUNCTION seller.capture_order_activity_event();

DROP TRIGGER IF EXISTS trg_order_items_activity_status ON seller.order_items;
CREATE TRIGGER trg_order_items_activity_status
AFTER UPDATE OF normalized_status ON seller.order_items
FOR EACH ROW EXECUTE FUNCTION seller.capture_order_activity_event();
