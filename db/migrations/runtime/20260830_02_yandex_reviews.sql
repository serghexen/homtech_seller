-- Локальный снимок отзывов и отдельный outbox ручных ответов.
-- Новая внешняя операция по умолчанию выключена и включается для магазинов отдельно.
ALTER TABLE seller.marketplace_connections
  ADD COLUMN IF NOT EXISTS review_reply_enabled boolean NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS seller.marketplace_reviews (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  business_id text NOT NULL,
  feedback_id bigint NOT NULL,
  external_order_id text NOT NULL DEFAULT '',
  offer_id text NOT NULL DEFAULT '',
  author text NOT NULL DEFAULT '',
  provider_created_at timestamptz,
  need_reaction boolean NOT NULL DEFAULT true,
  rating smallint CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
  comments_count integer NOT NULL DEFAULT 0 CHECK (comments_count >= 0),
  recommended boolean,
  paid_amount numeric(18,2),
  advantages text NOT NULL DEFAULT '',
  disadvantages text NOT NULL DEFAULT '',
  comment_text text NOT NULL DEFAULT '',
  media_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  raw_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, business_id, feedback_id),
  UNIQUE (id, workspace_id, connection_id)
);

CREATE INDEX IF NOT EXISTS idx_marketplace_reviews_workspace_pending
  ON seller.marketplace_reviews(workspace_id, need_reaction, provider_created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_marketplace_reviews_connection
  ON seller.marketplace_reviews(connection_id, provider_created_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS seller.marketplace_review_reply_jobs (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  connection_id bigint NOT NULL REFERENCES seller.marketplace_connections(id) ON DELETE CASCADE,
  review_id bigint NOT NULL,
  actor_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  response_text text NOT NULL CHECK (char_length(response_text) BETWEEN 1 AND 4096),
  state text NOT NULL DEFAULT 'queued'
    CHECK (state IN ('queued', 'preparing', 'sending', 'submitted', 'unknown', 'failed')),
  provider_comment_id bigint,
  provider_status text NOT NULL DEFAULT '',
  attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
  lock_token uuid,
  locked_until timestamptz,
  queued_at timestamptz NOT NULL DEFAULT now(),
  sending_at timestamptz,
  submitted_at timestamptz,
  unknown_at timestamptz,
  failed_at timestamptz,
  last_error text NOT NULL DEFAULT '',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (review_id, workspace_id, connection_id)
    REFERENCES seller.marketplace_reviews(id, workspace_id, connection_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_review_reply_active
  ON seller.marketplace_review_reply_jobs(review_id)
  WHERE state IN ('queued', 'preparing', 'sending', 'unknown');

CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_review_reply_connection_inflight
  ON seller.marketplace_review_reply_jobs(connection_id)
  WHERE state IN ('preparing', 'sending');

CREATE INDEX IF NOT EXISTS idx_marketplace_review_reply_pending
  ON seller.marketplace_review_reply_jobs(queued_at, id)
  WHERE state='queued';
