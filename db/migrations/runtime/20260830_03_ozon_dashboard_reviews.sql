-- Расширяет снимок отзывов до provider-neutral модели и сохраняет Ozon UUID без потери точности.
-- Внешняя публикация по-прежнему выключена глобально и отдельно для каждого магазина.
ALTER TABLE seller.marketplace_reviews
  ADD COLUMN IF NOT EXISTS provider_code text NOT NULL DEFAULT 'yandex_market',
  ADD COLUMN IF NOT EXISTS external_review_id text,
  ADD COLUMN IF NOT EXISTS reply_allowed boolean NOT NULL DEFAULT true;

UPDATE seller.marketplace_reviews
SET external_review_id=feedback_id::text
WHERE external_review_id IS NULL;

ALTER TABLE seller.marketplace_reviews
  ALTER COLUMN external_review_id SET NOT NULL,
  ALTER COLUMN business_id SET DEFAULT '',
  ALTER COLUMN feedback_id DROP NOT NULL;

ALTER TABLE seller.marketplace_reviews
  DROP CONSTRAINT IF EXISTS marketplace_reviews_workspace_id_business_id_feedback_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_reviews_provider_external
  ON seller.marketplace_reviews(workspace_id, connection_id, provider_code, external_review_id);

ALTER TABLE seller.marketplace_review_reply_jobs
  ALTER COLUMN provider_comment_id TYPE text USING provider_comment_id::text;

CREATE INDEX IF NOT EXISTS idx_marketplace_reviews_provider_pending
  ON seller.marketplace_reviews(provider_code, workspace_id, connection_id, need_reaction,
                                provider_created_at DESC, id DESC);
