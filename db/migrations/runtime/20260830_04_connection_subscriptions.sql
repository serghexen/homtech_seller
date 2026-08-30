-- Переносит коммерческий доступ с workspace на конкретное подключение магазина.
-- Старые таблицы workspace остаются на время совместимого развёртывания, но
-- рабочий код после этой миграции читает только connection-scoped подписки.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname='marketplace_connections_id_workspace_unique'
      AND conrelid='seller.marketplace_connections'::regclass
  ) THEN
    ALTER TABLE seller.marketplace_connections
      ADD CONSTRAINT marketplace_connections_id_workspace_unique UNIQUE (id, workspace_id);
  END IF;
END;
$$;

CREATE TABLE IF NOT EXISTS seller.marketplace_connection_subscriptions (
  connection_id bigint PRIMARY KEY,
  workspace_id bigint NOT NULL,
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
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT marketplace_connection_subscriptions_connection_workspace_fk
    FOREIGN KEY (connection_id, workspace_id)
    REFERENCES seller.marketplace_connections(id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS seller.marketplace_connection_entitlement_overrides (
  connection_id bigint NOT NULL,
  workspace_id bigint NOT NULL,
  capability_code text NOT NULL REFERENCES seller.capabilities(code) ON DELETE RESTRICT,
  enabled boolean NOT NULL,
  expires_at timestamptz,
  reason text NOT NULL DEFAULT '',
  updated_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (connection_id, capability_code),
  CONSTRAINT marketplace_connection_overrides_connection_workspace_fk
    FOREIGN KEY (connection_id, workspace_id)
    REFERENCES seller.marketplace_connections(id, workspace_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS seller.marketplace_connection_subscription_events (
  id bigserial PRIMARY KEY,
  connection_id bigint NOT NULL,
  workspace_id bigint NOT NULL,
  old_plan_id bigint REFERENCES seller.plans(id) ON DELETE SET NULL,
  new_plan_id bigint NOT NULL REFERENCES seller.plans(id) ON DELETE RESTRICT,
  old_status text,
  new_status text NOT NULL,
  change_source text NOT NULL,
  change_reason text NOT NULL DEFAULT '',
  changed_by_user_id bigint REFERENCES seller.users(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT marketplace_connection_events_connection_workspace_fk
    FOREIGN KEY (connection_id, workspace_id)
    REFERENCES seller.marketplace_connections(id, workspace_id) ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION seller.record_marketplace_connection_subscription_event()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO seller.marketplace_connection_subscription_events(
    connection_id, workspace_id, old_plan_id, new_plan_id, old_status, new_status,
    change_source, change_reason, changed_by_user_id
  ) VALUES (
    NEW.connection_id,
    NEW.workspace_id,
    CASE WHEN TG_OP='UPDATE' THEN OLD.plan_id ELSE NULL END,
    NEW.plan_id,
    CASE WHEN TG_OP='UPDATE' THEN OLD.status ELSE NULL END,
    NEW.status, NEW.change_source, NEW.change_reason, NEW.updated_by_user_id
  );
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_marketplace_connection_subscription_insert_event
  ON seller.marketplace_connection_subscriptions;
CREATE TRIGGER trg_marketplace_connection_subscription_insert_event
AFTER INSERT ON seller.marketplace_connection_subscriptions
FOR EACH ROW EXECUTE FUNCTION seller.record_marketplace_connection_subscription_event();

DROP TRIGGER IF EXISTS trg_marketplace_connection_subscription_update_event
  ON seller.marketplace_connection_subscriptions;
CREATE TRIGGER trg_marketplace_connection_subscription_update_event
AFTER UPDATE OF plan_id, status, valid_until, grace_until
ON seller.marketplace_connection_subscriptions
FOR EACH ROW
WHEN (
  OLD.plan_id IS DISTINCT FROM NEW.plan_id OR
  OLD.status IS DISTINCT FROM NEW.status OR
  OLD.valid_until IS DISTINCT FROM NEW.valid_until OR
  OLD.grace_until IS DISTINCT FROM NEW.grace_until
)
EXECUTE FUNCTION seller.record_marketplace_connection_subscription_event();

-- Каждый существующий магазин получает точную копию прежнего workspace-тарифа.
-- Если старая подписка отсутствует, доступ остаётся закрытым базовым тарифом.
-- Блокировки не дают старому admin CLI изменить источник между копированием
-- подписки и overrides; таблица подключений уже заблокирована DDL этой транзакции.
LOCK TABLE seller.workspace_subscriptions IN SHARE MODE;
LOCK TABLE seller.workspace_entitlement_overrides IN SHARE MODE;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM seller.plans WHERE code='basic' AND is_active=true
  ) THEN
    RAISE EXCEPTION 'Active Basic plan is required for connection subscription migration';
  END IF;
END;
$$;

INSERT INTO seller.marketplace_connection_subscriptions(
  connection_id, workspace_id, plan_id, status, started_at, valid_until,
  grace_until, revision, change_source, change_reason, updated_by_user_id, updated_at
)
SELECT connection.id, connection.workspace_id,
       COALESCE(subscription.plan_id, basic.id),
       COALESCE(subscription.status, 'suspended'),
       COALESCE(subscription.started_at, connection.created_at),
       subscription.valid_until, subscription.grace_until,
       COALESCE(subscription.revision, 1),
       'workspace_migration', 'Перенос тарифа workspace на подключенный магазин',
       subscription.updated_by_user_id, COALESCE(subscription.updated_at, now())
FROM seller.marketplace_connections AS connection
CROSS JOIN seller.plans AS basic
LEFT JOIN seller.workspace_subscriptions AS subscription
  ON subscription.workspace_id=connection.workspace_id
WHERE basic.code='basic' AND basic.is_active=true
ON CONFLICT (connection_id) DO NOTHING;

DO $$
BEGIN
  IF EXISTS (
    SELECT 1
    FROM seller.marketplace_connections AS connection
    LEFT JOIN seller.marketplace_connection_subscriptions AS subscription
      ON subscription.connection_id=connection.id
     AND subscription.workspace_id=connection.workspace_id
    WHERE subscription.connection_id IS NULL
  ) THEN
    RAISE EXCEPTION 'Connection subscription backfill is incomplete';
  END IF;
END;
$$;

-- Старые точечные разрешения копируются каждому существующему магазину workspace.
INSERT INTO seller.marketplace_connection_entitlement_overrides(
  connection_id, workspace_id, capability_code, enabled, expires_at, reason,
  updated_by_user_id, created_at, updated_at
)
SELECT connection.id, connection.workspace_id, override.capability_code,
       override.enabled, override.expires_at, override.reason,
       override.updated_by_user_id, override.created_at, override.updated_at
FROM seller.marketplace_connections AS connection
JOIN seller.workspace_entitlement_overrides AS override
  ON override.workspace_id=connection.workspace_id
ON CONFLICT (connection_id, capability_code) DO NOTHING;

-- Гарантирует строку подписки и для магазина, созданного в коротком окне
-- между применением миграции и перезапуском API. Новый код сразу читает её.
CREATE OR REPLACE FUNCTION seller.ensure_marketplace_connection_subscription()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
  INSERT INTO seller.marketplace_connection_subscriptions(
    connection_id, workspace_id, plan_id, status, change_source, change_reason,
    updated_by_user_id
  )
  SELECT NEW.id, NEW.workspace_id, plan.id, 'active',
         'connection_created', 'Тариф нового подключенного магазина', NEW.created_by_user_id
  FROM seller.plans AS plan
  WHERE plan.code='basic' AND plan.is_active=true
  ON CONFLICT (connection_id) DO NOTHING;

  IF NOT FOUND AND NOT EXISTS (
    SELECT 1 FROM seller.marketplace_connection_subscriptions
    WHERE connection_id=NEW.id
  ) THEN
    RAISE EXCEPTION 'Active Basic plan is required for marketplace connection %', NEW.id;
  END IF;
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_marketplace_connection_default_subscription
  ON seller.marketplace_connections;
CREATE TRIGGER trg_marketplace_connection_default_subscription
AFTER INSERT ON seller.marketplace_connections
FOR EACH ROW EXECUTE FUNCTION seller.ensure_marketplace_connection_subscription();

CREATE INDEX IF NOT EXISTS idx_marketplace_connection_subscriptions_workspace
  ON seller.marketplace_connection_subscriptions(workspace_id, connection_id);

CREATE INDEX IF NOT EXISTS idx_marketplace_connection_subscriptions_plan
  ON seller.marketplace_connection_subscriptions(plan_id, status);

CREATE INDEX IF NOT EXISTS idx_marketplace_connection_overrides_expires
  ON seller.marketplace_connection_entitlement_overrides(connection_id, expires_at);

CREATE INDEX IF NOT EXISTS idx_marketplace_connection_subscription_events_connection
  ON seller.marketplace_connection_subscription_events(connection_id, created_at DESC, id DESC);
