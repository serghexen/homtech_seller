-- Хранит коммерческие возможности workspace в БД. Существующим кабинетам
-- назначается Pro, чтобы миграция не меняла уже работающую выдачу.
CREATE TABLE IF NOT EXISTS seller.plans (
  id bigserial PRIMARY KEY,
  code text NOT NULL UNIQUE,
  display_name text NOT NULL,
  is_active boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller.capabilities (
  code text PRIMARY KEY,
  display_name text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller.plan_entitlements (
  plan_id bigint NOT NULL REFERENCES seller.plans(id) ON DELETE CASCADE,
  capability_code text NOT NULL REFERENCES seller.capabilities(code) ON DELETE RESTRICT,
  enabled boolean NOT NULL DEFAULT true,
  limit_value jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (plan_id, capability_code)
);

CREATE TABLE IF NOT EXISTS seller.workspace_subscriptions (
  workspace_id bigint PRIMARY KEY REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  plan_id bigint NOT NULL REFERENCES seller.plans(id) ON DELETE RESTRICT,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('trialing','active','past_due','suspended','cancelled')),
  started_at timestamptz NOT NULL DEFAULT now(),
  valid_until timestamptz,
  grace_until timestamptz,
  revision bigint NOT NULL DEFAULT 1 CHECK (revision > 0),
  change_source text NOT NULL DEFAULT 'system',
  change_reason text NOT NULL DEFAULT '',
  updated_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS seller.workspace_entitlement_overrides (
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  capability_code text NOT NULL REFERENCES seller.capabilities(code) ON DELETE RESTRICT,
  enabled boolean NOT NULL,
  expires_at timestamptz,
  reason text NOT NULL DEFAULT '',
  updated_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, capability_code)
);

CREATE TABLE IF NOT EXISTS seller.workspace_subscription_events (
  id bigserial PRIMARY KEY,
  workspace_id bigint NOT NULL REFERENCES seller.workspaces(id) ON DELETE CASCADE,
  old_plan_id bigint REFERENCES seller.plans(id) ON DELETE SET NULL,
  new_plan_id bigint NOT NULL REFERENCES seller.plans(id) ON DELETE RESTRICT,
  old_status text,
  new_status text NOT NULL,
  change_source text NOT NULL,
  change_reason text NOT NULL DEFAULT '',
  changed_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION seller.record_workspace_subscription_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO seller.workspace_subscription_events(
    workspace_id, old_plan_id, new_plan_id, old_status, new_status,
    change_source, change_reason, changed_by_user_id
  ) VALUES (
    NEW.workspace_id,
    CASE WHEN TG_OP='UPDATE' THEN OLD.plan_id ELSE NULL END,
    NEW.plan_id,
    CASE WHEN TG_OP='UPDATE' THEN OLD.status ELSE NULL END,
    NEW.status, NEW.change_source, NEW.change_reason, NEW.updated_by_user_id
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_workspace_subscription_insert_event ON seller.workspace_subscriptions;
CREATE TRIGGER trg_workspace_subscription_insert_event
AFTER INSERT ON seller.workspace_subscriptions
FOR EACH ROW EXECUTE FUNCTION seller.record_workspace_subscription_event();

DROP TRIGGER IF EXISTS trg_workspace_subscription_update_event ON seller.workspace_subscriptions;
CREATE TRIGGER trg_workspace_subscription_update_event
AFTER UPDATE OF plan_id, status, valid_until, grace_until ON seller.workspace_subscriptions
FOR EACH ROW
WHEN (
  OLD.plan_id IS DISTINCT FROM NEW.plan_id OR
  OLD.status IS DISTINCT FROM NEW.status OR
  OLD.valid_until IS DISTINCT FROM NEW.valid_until OR
  OLD.grace_until IS DISTINCT FROM NEW.grace_until
)
EXECUTE FUNCTION seller.record_workspace_subscription_event();

INSERT INTO seller.plans(code, display_name)
VALUES ('basic', 'Basic'), ('pro', 'Pro')
ON CONFLICT (code) DO UPDATE SET display_name=EXCLUDED.display_name, updated_at=now();

INSERT INTO seller.capabilities(code, display_name)
VALUES
  ('fulfillment.manual', 'Ручная выдача'),
  ('key_pool.manage', 'Управление списком ключей'),
  ('fulfillment.pool', 'Выдача из списка ключей'),
  ('supplier_mapping.manage', 'Настройка связок Supplier Hub'),
  ('fulfillment.supplier', 'Автовыдача через Supplier Hub')
ON CONFLICT (code) DO UPDATE SET display_name=EXCLUDED.display_name;

INSERT INTO seller.plan_entitlements(plan_id, capability_code, enabled)
SELECT plan.id, capability.code, true
FROM seller.plans AS plan
JOIN seller.capabilities AS capability
  ON capability.code IN ('fulfillment.manual','key_pool.manage','fulfillment.pool')
WHERE plan.code='basic'
ON CONFLICT (plan_id, capability_code) DO UPDATE SET enabled=true, updated_at=now();

INSERT INTO seller.plan_entitlements(plan_id, capability_code, enabled)
SELECT plan.id, capability.code, true
FROM seller.plans AS plan
CROSS JOIN seller.capabilities AS capability
WHERE plan.code='pro'
ON CONFLICT (plan_id, capability_code) DO UPDATE SET enabled=true, updated_at=now();

INSERT INTO seller.workspace_subscriptions(
  workspace_id, plan_id, status, change_source, change_reason
)
SELECT workspace.id, plan.id, 'active', 'migration', 'Сохранение текущих возможностей Seller'
FROM seller.workspaces AS workspace
CROSS JOIN seller.plans AS plan
WHERE plan.code='pro'
ON CONFLICT (workspace_id) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_workspace_subscriptions_plan
  ON seller.workspace_subscriptions(plan_id, status);

CREATE INDEX IF NOT EXISTS idx_workspace_entitlement_overrides_expires
  ON seller.workspace_entitlement_overrides(workspace_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_workspace_subscription_events_workspace
  ON seller.workspace_subscription_events(workspace_id, created_at DESC, id DESC);
