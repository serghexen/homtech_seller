-- Новые возможности выключены; применение миграции не меняет остатки магазинов.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN supplier_stock_control_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN supplier_stock_notifications_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN supplier_stock_notifications_until timestamptz,
  ADD COLUMN supplier_stock_next_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN supplier_stock_last_success_at timestamptz,
  ADD COLUMN supplier_stock_last_error text NOT NULL DEFAULT '',
  ADD COLUMN supplier_stock_notice_key text NOT NULL DEFAULT '';

CREATE TABLE seller.product_supplier_stock_state (
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id),
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id),
  external_product_id text NOT NULL,
  mapping_key text NOT NULL DEFAULT '',
  blocked boolean NOT NULL DEFAULT false,
  observation text NOT NULL DEFAULT 'unknown',
  checked_at timestamptz,
  revision bigint NOT NULL DEFAULT 1,
  notice_key text NOT NULL DEFAULT '',
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY(workspace_id,connection_id,external_product_id),
  FOREIGN KEY(connection_id,workspace_id) REFERENCES seller.marketplace_connections(id,workspace_id),
  FOREIGN KEY(connection_id,external_product_id) REFERENCES seller.catalog_items(connection_id,external_product_id)
);

ALTER TABLE seller.yandex_stock_outbound_jobs
  DROP CONSTRAINT yandex_stock_outbound_jobs_kind_check,
  DROP CONSTRAINT yandex_stock_outbound_jobs_source_check,
  ADD CONSTRAINT yandex_stock_outbound_jobs_kind_check
    CHECK(job_kind IN ('fulfillment','manual','daily','reconcile','supplier')),
  ADD CONSTRAINT yandex_stock_outbound_jobs_source_check CHECK (
    (job_kind='fulfillment' AND fulfillment_id IS NOT NULL) OR
    (job_kind='manual' AND fulfillment_id IS NULL AND requested_stock BETWEEN 0 AND 1000000) OR
    (job_kind='daily' AND fulfillment_id IS NULL AND stock_day IS NOT NULL) OR
    (job_kind IN ('reconcile','supplier') AND fulfillment_id IS NULL));

ALTER TABLE seller.telegram_notification_events
  ALTER COLUMN fulfillment_id DROP NOT NULL,
  DROP CONSTRAINT telegram_notification_events_event_type_check,
  ADD CONSTRAINT telegram_notification_events_event_type_check
    CHECK(event_type IN ('manual_required','unknown','error','cancelled','resolved','supplier_stock')),
  ADD CONSTRAINT telegram_notification_events_source_check
    CHECK (fulfillment_id IS NOT NULL OR event_type='supplier_stock');
