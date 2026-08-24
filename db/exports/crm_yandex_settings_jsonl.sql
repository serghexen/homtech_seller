BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL lock_timeout TO '1s';
SET LOCAL statement_timeout TO '10s';

COPY (
  SELECT jsonb_build_object(
    'source_store_code', settings.store_code,
    'offer_id', settings.offer_id,
    'manual_stock_limit', settings.manual_stock_limit,
    'published_stock', settings.published_stock,
    'activation_instruction', settings.activation_instruction,
    'sales_limit', settings.sales_limit,
    'sales_limit_daily_extra', settings.sales_limit_daily_extra,
    'sales_limit_day', settings.sales_limit_day,
    'sales_limit_revision', settings.sales_limit_revision,
    'sales_limit_used', CASE WHEN settings.sales_limit IS NULL THEN 0 ELSE totals.used END,
    'sales_limit_reserved', CASE WHEN settings.sales_limit IS NULL THEN 0 ELSE totals.reserved END,
    'sales_limit_remaining', CASE
      WHEN settings.sales_limit IS NULL THEN NULL
      ELSE GREATEST(0, settings.sales_limit + settings.sales_limit_daily_extra - totals.used - totals.reserved)
    END,
    'sales_limit_exhausted_at', settings.sales_limit_exhausted_at,
    'archived_by_sales_limit', settings.archived_by_sales_limit,
    'last_stock_sync_at', settings.last_stock_sync_at,
    'source_updated_at', settings.updated_at
  )::text
  FROM app.marketplace_yandex_stock_settings AS settings
  LEFT JOIN LATERAL (
    SELECT
      COALESCE(SUM(reservation.quantity) FILTER (WHERE reservation.state='consumed'), 0)::integer AS used,
      COALESCE(SUM(reservation.quantity) FILTER (WHERE reservation.state='reserved'), 0)::integer AS reserved
    FROM app.marketplace_yandex_sales_limit_reservations AS reservation
    WHERE reservation.store_code=settings.store_code
      AND reservation.offer_id=settings.offer_id
      AND reservation.limit_revision=settings.sales_limit_revision
  ) AS totals ON true
  WHERE settings.store_code='joycards'
  ORDER BY settings.offer_id
) TO STDOUT;

COMMIT;
