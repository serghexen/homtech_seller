-- Пилот вне маркетплейса: workspace обязателен, связь с магазином отсутствует явно.
CREATE TABLE seller.steam_topup_settings (
    workspace_id bigint PRIMARY KEY REFERENCES seller.workspaces(id),
    enabled boolean NOT NULL DEFAULT false,
    last_worker_at timestamptz,
    max_amount numeric(12,2) NOT NULL DEFAULT 100 CHECK (max_amount >= 16.99),
    budget_amount numeric(12,2) NOT NULL DEFAULT 100 CHECK (budget_amount >= 0)
);

CREATE TABLE seller.steam_topup_links (
    id uuid PRIMARY KEY,
    workspace_id bigint NOT NULL REFERENCES seller.workspaces(id),
    connection_id bigint CHECK (connection_id IS NULL),
    source text NOT NULL DEFAULT 'manual_pilot' CHECK (source='manual_pilot'),
    created_by_user_id bigint NOT NULL REFERENCES seller.users(id),
    creation_key uuid NOT NULL,
    token_hash text NOT NULL UNIQUE,
    amount numeric(12,2) NOT NULL CHECK (amount >= 16.99),
    currency text NOT NULL DEFAULT 'RUB' CHECK (currency='RUB'),
    state text NOT NULL DEFAULT 'ready' CHECK (state IN
      ('ready','queued','processing','succeeded','failed','attention','cancelled')),
    account text NOT NULL DEFAULT '',
    current_attempt uuid,
    attempt_count integer NOT NULL DEFAULT 0,
    public_message text NOT NULL DEFAULT '',
    expires_at timestamptz NOT NULL DEFAULT now()+interval '7 days',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, creation_key),
    UNIQUE (workspace_id, id)
);

CREATE TABLE seller.steam_topup_attempts (
    id uuid PRIMARY KEY,
    workspace_id bigint NOT NULL,
    connection_id bigint CHECK (connection_id IS NULL),
    link_id uuid NOT NULL,
    operation_kind text NOT NULL DEFAULT 'steam_topup' CHECK (operation_kind='steam_topup'),
    request_key uuid NOT NULL,
    account text NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    hub_purchase_id uuid,
    payment_started boolean NOT NULL DEFAULT false,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    retry_count integer NOT NULL DEFAULT 0,
    last_error text NOT NULL DEFAULT '',
    last_success_at timestamptz,
    last_duration_ms integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id,link_id) REFERENCES seller.steam_topup_links(workspace_id,id),
    UNIQUE (link_id, request_key)
);
CREATE UNIQUE INDEX steam_topup_one_active_attempt ON seller.steam_topup_attempts(link_id)
    WHERE state IN ('queued','created','checked','payment_started','processing');
CREATE INDEX steam_topup_due ON seller.steam_topup_attempts(next_attempt_at)
    WHERE state IN ('queued','created','checked','payment_started','processing');

CREATE TABLE seller.steam_topup_events (
    id bigserial PRIMARY KEY,
    workspace_id bigint NOT NULL,
    link_id uuid NOT NULL,
    attempt_id uuid,
    state text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id,link_id) REFERENCES seller.steam_topup_links(workspace_id,id)
);
