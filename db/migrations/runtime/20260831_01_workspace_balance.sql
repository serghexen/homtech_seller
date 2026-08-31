-- Добавляет единый рублёвый баланс workspace и безопасный inbox платежей Т-Банка.
CREATE TABLE IF NOT EXISTS seller.workspace_balance_accounts (
  workspace_id bigint PRIMARY KEY REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  currency text NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
  available_amount bigint NOT NULL DEFAULT 0 CHECK (available_amount >= 0),
  reserved_amount bigint NOT NULL DEFAULT 0 CHECK (reserved_amount >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller.workspace_balance_topups (
  id bigserial PRIMARY KEY,
  public_id uuid NOT NULL DEFAULT gen_random_uuid() UNIQUE,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  created_by_user_id bigint NOT NULL REFERENCES seller.users(id) ON DELETE RESTRICT,
  provider_code text NOT NULL DEFAULT 'tbank' CHECK (provider_code = 'tbank'),
  terminal_key text NOT NULL DEFAULT '',
  order_id text NOT NULL UNIQUE,
  provider_payment_id text UNIQUE,
  amount bigint NOT NULL CHECK (amount > 0),
  currency text NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
  state text NOT NULL DEFAULT 'created' CHECK (
    state IN ('created', 'init_pending', 'init_unknown', 'pending', 'confirmed', 'rejected', 'expired', 'cancelled', 'failed')
  ),
  provider_status text NOT NULL DEFAULT '',
  qr_data_url text NOT NULL DEFAULT '',
  last_error text NOT NULL DEFAULT '',
  next_reconcile_at timestamptz,
  reconcile_attempt_count integer NOT NULL DEFAULT 0 CHECK (reconcile_attempt_count >= 0),
  reconcile_lock_token uuid,
  reconcile_locked_until timestamptz,
  expires_at timestamptz,
  confirmed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_workspace_balance_topups_workspace_created
  ON seller.workspace_balance_topups(workspace_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_workspace_balance_topups_reconcile
  ON seller.workspace_balance_topups(next_reconcile_at, created_at)
  WHERE state IN ('init_unknown', 'pending');

CREATE TABLE IF NOT EXISTS seller.workspace_balance_ledger (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  connection_id bigint,
  topup_id bigint REFERENCES seller.workspace_balance_topups(id) ON DELETE RESTRICT,
  entry_type text NOT NULL CHECK (entry_type IN ('topup', 'reserve', 'capture', 'release', 'refund', 'adjustment')),
  amount bigint NOT NULL CHECK (amount <> 0),
  currency text NOT NULL DEFAULT 'RUB' CHECK (currency = 'RUB'),
  business_key text NOT NULL UNIQUE,
  metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (connection_id, workspace_id)
    REFERENCES seller.marketplace_connections(id, workspace_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_workspace_balance_ledger_workspace_created
  ON seller.workspace_balance_ledger(workspace_id, created_at DESC);

CREATE TABLE IF NOT EXISTS seller.tbank_payment_events (
  id bigserial PRIMARY KEY,
  event_fingerprint text NOT NULL UNIQUE,
  terminal_key text NOT NULL DEFAULT '',
  order_id text NOT NULL DEFAULT '',
  provider_payment_id text NOT NULL DEFAULT '',
  provider_status text NOT NULL DEFAULT '',
  amount bigint,
  signature_valid boolean NOT NULL,
  processing_state text NOT NULL DEFAULT 'received' CHECK (processing_state IN ('received', 'processed', 'ignored', 'failed')),
  last_error text NOT NULL DEFAULT '',
  received_at timestamptz NOT NULL DEFAULT now(),
  processed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_tbank_payment_events_order_received
  ON seller.tbank_payment_events(order_id, received_at DESC);
