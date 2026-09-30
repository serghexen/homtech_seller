-- Opt-in rollout: migration alone never enables a store or sends stock.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN yandex_daily_stock_enabled boolean NOT NULL DEFAULT false,
  ADD COLUMN next_daily_stock_at timestamptz NOT NULL DEFAULT now(),
  ADD COLUMN stock_last_attempt_at timestamptz,
  ADD COLUMN stock_backoff_until timestamptz;

ALTER TABLE seller.yandex_stock_outbound_jobs
  ADD COLUMN workspace_id bigint REFERENCES seller.workspaces(id),
  ADD COLUMN stock_day date,
  ADD COLUMN orders_fresh_after timestamptz,
  ADD COLUMN business_key text NOT NULL DEFAULT gen_random_uuid()::text;
UPDATE seller.yandex_stock_outbound_jobs j
SET connection_id=f.connection_id, external_product_id=f.offer_id
FROM seller.order_fulfillments f WHERE f.id=j.fulfillment_id;
UPDATE seller.yandex_stock_outbound_jobs j
SET workspace_id=c.workspace_id FROM seller.marketplace_connections c WHERE c.id=j.connection_id;
ALTER TABLE seller.yandex_stock_outbound_jobs
  ALTER COLUMN workspace_id SET NOT NULL,
  ALTER COLUMN connection_id SET NOT NULL,
  ALTER COLUMN external_product_id SET NOT NULL,
  DROP CONSTRAINT yandex_stock_outbound_jobs_kind_check,
  DROP CONSTRAINT yandex_stock_outbound_jobs_source_check,
  ADD CONSTRAINT yandex_stock_outbound_jobs_kind_check
    CHECK (job_kind IN ('fulfillment','manual','daily','reconcile')),
  ADD CONSTRAINT yandex_stock_outbound_jobs_source_check CHECK (
    (job_kind='fulfillment' AND fulfillment_id IS NOT NULL) OR
    (job_kind='manual' AND fulfillment_id IS NULL AND requested_stock BETWEEN 0 AND 1000000) OR
    (job_kind='daily' AND fulfillment_id IS NULL AND stock_day IS NOT NULL) OR
    (job_kind='reconcile' AND fulfillment_id IS NULL)
  );
CREATE UNIQUE INDEX uq_yandex_stock_business_key
  ON seller.yandex_stock_outbound_jobs(workspace_id, connection_id, business_key);
CREATE UNIQUE INDEX uq_yandex_stock_daily
  ON seller.yandex_stock_outbound_jobs(connection_id, external_product_id, stock_day) WHERE job_kind='daily';
CREATE UNIQUE INDEX uq_yandex_stock_sending_connection
  ON seller.yandex_stock_outbound_jobs(connection_id) WHERE state IN ('preparing','sending');

-- Keep existing producers compatible, including fulfillment jobs without explicit scope.
CREATE FUNCTION seller.scope_yandex_stock_job() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE scoped_connection bigint; scoped_workspace bigint; scoped_product text;
BEGIN
  IF NEW.fulfillment_id IS NOT NULL THEN
    SELECT connection_id, offer_id INTO STRICT scoped_connection, scoped_product
      FROM seller.order_fulfillments WHERE id=NEW.fulfillment_id;
    IF (NEW.connection_id IS NOT NULL AND NEW.connection_id<>scoped_connection)
       OR (NEW.external_product_id IS NOT NULL AND NEW.external_product_id<>scoped_product) THEN
      RAISE EXCEPTION 'Stock job fulfillment scope mismatch';
    END IF;
    NEW.connection_id := scoped_connection;
    NEW.external_product_id := scoped_product;
    NEW.business_key := 'fulfillment:' || NEW.fulfillment_id;
  END IF;
  SELECT workspace_id INTO STRICT scoped_workspace FROM seller.marketplace_connections
    WHERE id=NEW.connection_id AND provider_code='yandex_market';
  IF NEW.workspace_id IS NOT NULL AND NEW.workspace_id<>scoped_workspace THEN
    RAISE EXCEPTION 'Stock job workspace mismatch';
  END IF;
  NEW.workspace_id := scoped_workspace;
  RETURN NEW;
END $$;
CREATE TRIGGER scope_yandex_stock_job BEFORE INSERT ON seller.yandex_stock_outbound_jobs
  FOR EACH ROW EXECUTE FUNCTION seller.scope_yandex_stock_job();

-- One definition for the UI and the sender. Orders count even before key reservation.
-- Unfinished orders from previous days remain reserved; uncertain sends never release quota.
CREATE FUNCTION seller.yandex_daily_sales(
  p_workspace bigint, p_connection bigint, p_product text,
  p_day date DEFAULT (now() AT TIME ZONE 'Europe/Moscow')::date
) RETURNS TABLE(used bigint, reserved bigint) LANGUAGE sql STABLE AS $$
  SELECT COALESCE(sum(quantity) FILTER (WHERE sold AND order_day=p_day),0)::bigint,
         COALESCE(sum(quantity) FILTER (WHERE NOT sold AND pending),0)::bigint
  FROM (
    SELECT greatest(i.quantity, COALESCE(f.requested_quantity,0)) AS quantity,
           (COALESCE(i.created_at,i.first_seen_at) AT TIME ZONE 'Europe/Moscow')::date AS order_day,
           (i.normalized_status='delivered' OR COALESCE(f.status='delivered',false)) AS sold,
           (i.normalized_status<>'cancelled' OR COALESCE(f.status IN ('sending','submitted','unknown'),false)) AS pending
    FROM seller.order_items i
    JOIN seller.marketplace_connections c ON c.id=i.connection_id AND c.workspace_id=p_workspace
    LEFT JOIN seller.order_fulfillments f ON f.connection_id=i.connection_id
      AND f.external_order_id=i.external_order_id AND f.external_item_id=i.external_item_id
    WHERE i.connection_id=p_connection AND i.offer_id=p_product
      AND c.provider_code='yandex_market' AND upper(i.delivery_type)='DIGITAL'
      AND (COALESCE(i.created_at,i.first_seen_at) AT TIME ZONE 'Europe/Moscow')::date<=p_day
  ) counts;
$$;
CREATE INDEX idx_order_items_daily_quota ON seller.order_items(connection_id, offer_id, created_at);

-- The same transaction that changes quota schedules its stock reconciliation.
CREATE FUNCTION seller.enqueue_yandex_quota_stock(p_connection bigint,p_product text)
RETURNS void LANGUAGE sql AS $$
  INSERT INTO seller.yandex_stock_outbound_jobs(workspace_id,connection_id,external_product_id,job_kind,next_attempt_at)
    SELECT c.workspace_id,c.id,p_product,'reconcile',now()+interval '3 seconds'
    FROM seller.marketplace_connections c
    JOIN seller.catalog_items item ON item.connection_id=c.id AND item.external_product_id=p_product
    LEFT JOIN seller.product_card_settings s ON s.connection_id=c.id AND s.external_product_id=p_product
    LEFT JOIN seller.yandex_product_settings_snapshot imported ON imported.connection_id=c.id AND imported.external_product_id=p_product
    WHERE c.id=p_connection AND c.provider_code='yandex_market'
      AND c.status='active' AND c.launch_state='running' AND c.stock_outbound_enabled AND c.yandex_daily_stock_enabled
      AND item.is_present AND NOT item.is_archived
      AND CASE WHEN s.connection_id IS NOT NULL THEN s.sales_limit ELSE imported.sales_limit END IS NOT NULL;
$$;
CREATE FUNCTION seller.enqueue_yandex_quota_change() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP='UPDATE' AND (OLD.normalized_status,OLD.quantity,OLD.offer_id)
      IS NOT DISTINCT FROM (NEW.normalized_status,NEW.quantity,NEW.offer_id) THEN
    RETURN NEW;
  END IF;
  IF upper(NEW.delivery_type)='DIGITAL' THEN
    PERFORM seller.enqueue_yandex_quota_stock(NEW.connection_id,NEW.offer_id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER enqueue_yandex_quota_change AFTER INSERT OR UPDATE ON seller.order_items
  FOR EACH ROW EXECUTE FUNCTION seller.enqueue_yandex_quota_change();

-- Releasing an uncertain send is an explicit reconciliation and may free quota.
CREATE FUNCTION seller.enqueue_yandex_resolved_quota() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF OLD.status IN ('sending','submitted','unknown') AND
     NEW.status IN ('cancelled','closed_external','pending','manual_required','reserved','failed') THEN
    PERFORM seller.enqueue_yandex_quota_stock(NEW.connection_id,NEW.offer_id);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER enqueue_yandex_resolved_quota AFTER UPDATE OF status ON seller.order_fulfillments
  FOR EACH ROW EXECUTE FUNCTION seller.enqueue_yandex_resolved_quota();

CREATE INDEX idx_yandex_stock_card_history ON seller.yandex_stock_outbound_jobs
  (workspace_id,connection_id,external_product_id,updated_at DESC);
CREATE VIEW seller.yandex_stock_queue_metrics AS
SELECT workspace_id,connection_id,
       count(*) FILTER (WHERE state='queued') AS queue_depth,
       max(EXTRACT(EPOCH FROM now()-created_at)) FILTER (WHERE state='queued') AS oldest_job_seconds,
       avg(EXTRACT(EPOCH FROM succeeded_at-sending_at)) FILTER (WHERE succeeded_at IS NOT NULL) AS mean_duration_seconds,
       sum(greatest(attempt_count-1,0)) AS retries,
       count(*) FILTER (WHERE last_error LIKE '%HTTP 429%') AS rate_limited_jobs,
       count(*) FILTER (WHERE state='failed') AS failed_jobs,
       max(succeeded_at) AS last_success_at,
       min(next_attempt_at) FILTER (WHERE state='queued') AS next_run_at
FROM seller.yandex_stock_outbound_jobs GROUP BY workspace_id,connection_id;
