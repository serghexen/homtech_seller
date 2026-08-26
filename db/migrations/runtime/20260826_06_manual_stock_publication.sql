-- Позволяет оператору явно опубликовать заданный остаток из карточки товара.
-- Сетевой PUT по-прежнему выполняет только долговечный worker и только при двух включённых kill switch.
ALTER TABLE seller.yandex_stock_outbound_jobs
  ALTER COLUMN fulfillment_id DROP NOT NULL,
  ADD COLUMN IF NOT EXISTS job_kind text NOT NULL DEFAULT 'fulfillment',
  ADD COLUMN IF NOT EXISTS connection_id bigint
    REFERENCES seller.marketplace_connections(id) ON DELETE RESTRICT,
  ADD COLUMN IF NOT EXISTS external_product_id text,
  ADD COLUMN IF NOT EXISTS requested_stock integer,
  ADD COLUMN IF NOT EXISTS requested_by_user_id bigint
    REFERENCES seller.users(id) ON DELETE SET NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='seller.yandex_stock_outbound_jobs'::regclass
      AND conname='yandex_stock_outbound_jobs_kind_check'
  ) THEN
    ALTER TABLE seller.yandex_stock_outbound_jobs
      ADD CONSTRAINT yandex_stock_outbound_jobs_kind_check
      CHECK (job_kind IN ('fulfillment','manual'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conrelid='seller.yandex_stock_outbound_jobs'::regclass
      AND conname='yandex_stock_outbound_jobs_source_check'
  ) THEN
    ALTER TABLE seller.yandex_stock_outbound_jobs
      ADD CONSTRAINT yandex_stock_outbound_jobs_source_check
      CHECK (
        (job_kind='fulfillment' AND fulfillment_id IS NOT NULL)
        OR
        (job_kind='manual' AND fulfillment_id IS NULL
          AND connection_id IS NOT NULL
          AND btrim(external_product_id) <> ''
          AND requested_stock BETWEEN 0 AND 1000000)
      );
  END IF;
END $$;

-- Одновременно у одной карточки может выполняться только одна ручная публикация.
CREATE UNIQUE INDEX IF NOT EXISTS uq_yandex_stock_manual_active
  ON seller.yandex_stock_outbound_jobs(connection_id, external_product_id)
  WHERE job_kind='manual' AND state IN ('queued','preparing','sending');

CREATE INDEX IF NOT EXISTS idx_yandex_stock_manual_history
  ON seller.yandex_stock_outbound_jobs(connection_id, external_product_id, created_at DESC)
  WHERE job_kind='manual';
