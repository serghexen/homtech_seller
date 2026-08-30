"""Минимальные проверки новых runtime-миграций без подключения к рабочей базе."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.run_migrations import split_sql_statements


class MigrationFilesTests(unittest.TestCase):
    def test_yandex_reviews_are_workspace_scoped_and_replies_start_disabled(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260830_02_yandex_reviews.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("review_reply_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.marketplace_reviews", joined)
        self.assertIn("workspace_id bigint NOT NULL", joined)
        self.assertIn("FOREIGN KEY (review_id, workspace_id, connection_id)", joined)
        self.assertIn("'submitted', 'unknown', 'failed'", joined)
        self.assertIn("WHERE state IN ('preparing', 'sending')", joined)

    def test_ozon_reviews_use_provider_scoped_text_ids(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260830_03_ozon_dashboard_reviews.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("provider_code text NOT NULL DEFAULT 'yandex_market'", joined)
        self.assertIn("external_review_id text", joined)
        self.assertIn(
            "workspace_id, connection_id, provider_code, external_review_id",
            joined,
        )
        self.assertIn("provider_comment_id TYPE text", joined)

    def test_marketplace_dashboard_keeps_orders_unique_and_snapshots_workspace_scoped(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260830_01_marketplace_dashboard.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("PRIMARY KEY (connection_id, external_order_id)", joined)
        self.assertIn("prices,payment,value", joined)
        self.assertIn("prices,cashback,value", joined)
        self.assertIn("prices,subsidy,value", joined)
        self.assertNotIn("prices,delivery,value", joined)
        self.assertIn("workspace_id bigint NOT NULL", joined)
        self.assertIn("unassigned_reviews_count", joined)
        self.assertIn("'catalog', 'orders', 'dashboard'", joined)

    def test_order_activity_events_are_workspace_scoped_and_do_not_backfill_history(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260828_02_order_activity_events.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("CREATE TABLE IF NOT EXISTS seller.order_activity_events", joined)
        self.assertIn("workspace_id bigint NOT NULL", joined)
        self.assertIn("connection_id bigint NOT NULL", joined)
        self.assertIn("AFTER INSERT ON seller.order_items", joined)
        self.assertIn("AFTER UPDATE OF normalized_status ON seller.order_items", joined)
        self.assertNotIn("INSERT INTO seller.order_activity_events SELECT", joined)

    def test_store_launch_migration_preserves_existing_runtime_and_blocks_history(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260828_01_store_launch.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("launch_state text NOT NULL DEFAULT 'setup'", joined)
        self.assertIn("first_seen_at timestamptz NOT NULL DEFAULT now()", joined)
        self.assertIn("marketplace_connection_launch_events", joined)
        self.assertIn("fulfillment_started_at=COALESCE(fulfillment_started_at, clock_timestamp())", joined)
        self.assertIn("orders_polling_enabled=true", joined)

    def test_catalog_presence_migration_contains_schema_and_index_steps(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260824_04_catalog_snapshot_presence.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertIn("ADD COLUMN IF NOT EXISTS is_present", statements[0])
        self.assertIn("WHERE is_present = true", statements[1])

    def test_product_settings_migration_keeps_local_edits_separate(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260824_05_product_card_settings.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.product_card_settings", statements[0])
        self.assertIn("sales_limit_day date NOT NULL DEFAULT CURRENT_DATE", statements[0])
        self.assertIn("activation_instruction", statements[0])
        self.assertIn("idx_product_card_settings_updated", statements[1])

    def test_key_pool_migration_encrypts_values_and_has_no_delivery_tables(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260824_06_marketplace_key_pools.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        joined = "\n".join(statements)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.marketplace_key_pools", joined)
        self.assertIn("code_ciphertext bytea NOT NULL", joined)
        self.assertIn("code_hash text NOT NULL UNIQUE", joined)
        self.assertNotIn("fulfillment", joined.lower())

    def test_catalog_archive_migration_keeps_marketplace_state_separate(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_01_catalog_archive.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))

        self.assertEqual(len(statements), 2)
        self.assertIn("ADD COLUMN IF NOT EXISTS is_archived", statements[0])
        self.assertIn("is_present, is_archived", statements[1])

    def test_yandex_webhook_inbox_starts_paused_and_is_idempotent(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_02_yandex_webhook_inbox.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 5)
        self.assertIn("ADD COLUMN IF NOT EXISTS webhook_processing_enabled", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.yandex_webhook_events", joined)
        self.assertIn("processing_state text NOT NULL DEFAULT 'paused'", joined)
        self.assertIn("CREATE UNIQUE INDEX CONCURRENTLY", joined)
        self.assertIn("event_fingerprint", joined)

    def test_fulfillment_foundation_starts_with_all_reservation_switches_off(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_03_fulfillment_foundation.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 10)
        self.assertIn("fulfillment_reservation_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("pool_issue_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.order_fulfillments", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.fulfillment_key_reservations", joined)
        self.assertIn("WHERE state='reserved'", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.fulfillment_events", joined)

    def test_yandex_outbound_migration_starts_disabled_and_has_uncertain_state(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_04_yandex_fulfillment_outbox.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 4)
        self.assertIn("fulfillment_outbound_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.fulfillment_outbound_jobs", joined)
        self.assertIn("'submitted', 'unknown', 'failed'", joined)
        self.assertIn("fulfillment_id bigint NOT NULL UNIQUE", joined)
        self.assertNotIn("code_ciphertext", joined)

    def test_manual_and_support_migration_keeps_support_snapshot_separate(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_05_manual_and_support_fulfillment.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 6)
        self.assertIn("ADD COLUMN IF NOT EXISTS support_message", joined)
        self.assertIn("ADD COLUMN IF NOT EXISTS support_message_snapshot", joined)
        self.assertIn("'manual', 'support_message'", joined)
        self.assertIn("char_length(support_message_snapshot) <= 2000", joined)

    def test_support_import_migration_preserves_source_enablement_and_local_override(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260825_06_support_message_import.sql"

        statements = split_sql_statements(migration.read_text(encoding="utf-8"))
        joined = "\n".join(statements)

        self.assertEqual(len(statements), 6)
        self.assertIn("yandex_product_settings_snapshot", joined)
        self.assertIn("support_message_delivery_enabled", joined)
        self.assertIn("support_message_overridden", joined)

    def test_supplier_fulfillment_migration_is_off_by_default_and_durable(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_01_supplier_fulfillment.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("supplier_fulfillment_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.product_fulfillment_policies", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.product_supplier_mappings", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.supplier_purchase_attempts", joined)
        self.assertIn("idempotency_key text NOT NULL UNIQUE", joined)
        self.assertIn("'requires_attention'", joined)

    def test_yandex_stock_outbox_is_disabled_and_durable(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_02_yandex_stock_outbox.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("stock_outbound_enabled boolean NOT NULL DEFAULT false", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.yandex_stock_outbound_jobs", joined)
        self.assertIn("fulfillment_id bigint NOT NULL UNIQUE", joined)
        self.assertIn("'queued','preparing','sending','succeeded','failed'", joined)
        self.assertIn("SELECT DISTINCT ON (connection_id, offer_id)", joined)
        self.assertIn("status IN ('submitted','delivered')", joined)

    def test_key_origin_migration_backfills_order_keys_without_touching_pool_history(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260827_01_marketplace_key_origin.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("key_origin text NOT NULL DEFAULT 'pool'", joined)
        self.assertIn("source_system='supplier_hub'", joined)
        self.assertIn("event.event_type='manual_keys_prepared'", joined)
        self.assertIn("key_origin IN ('pool', 'order')", joined)

    def test_workspace_plans_keep_existing_workspaces_on_pro_and_basic_pool_enabled(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260827_02_workspace_plans.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("CREATE TABLE IF NOT EXISTS seller.workspace_subscriptions", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.workspace_entitlement_overrides", joined)
        self.assertIn("'fulfillment.pool'", joined)
        self.assertIn("'supplier_mapping.manage'", joined)
        self.assertIn("WHERE plan.code='basic'", joined)
        self.assertIn("WHERE plan.code='pro'", joined)
        self.assertIn("Сохранение текущих возможностей Seller", joined)

    def test_fulfillment_handling_mode_separates_automation_from_operator(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260827_03_fulfillment_handling_mode.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("handling_mode text NOT NULL DEFAULT 'unassigned'", joined)
        self.assertIn("status='manual_required'", joined)
        self.assertIn("supplier_purchase_attempts", joined)
        self.assertIn("'automatic','manual'", joined)

    def test_marketplace_identity_is_globally_unique_across_workspaces(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260827_04_marketplace_connection_identity.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("uq_marketplace_connections_yandex_campaign_global", joined)
        self.assertIn("provider_code='yandex_market' AND campaign_id<>''", joined)
        self.assertIn("uq_marketplace_connections_ozon_client_global", joined)
        self.assertIn("provider_code='ozon' AND client_id<>''", joined)

    def test_telegram_notifications_are_durable_and_workspace_scoped(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260827_05_telegram_notifications.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("CREATE TABLE IF NOT EXISTS seller.telegram_notification_events", joined)
        self.assertIn("CREATE TABLE IF NOT EXISTS seller.telegram_notification_recipients", joined)
        self.assertIn("workspace_id bigint NOT NULL", joined)
        self.assertIn("UNIQUE (event_id, recipient_id)", joined)
        self.assertIn("state IN ('queued', 'sending', 'retry', 'sent', 'dead')", joined)
        self.assertIn("AFTER INSERT OR UPDATE OF status, last_error", joined)
        self.assertIn("fulfillment_notification_alert_key", joined)

    def test_manual_stock_publication_reuses_durable_outbox(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_06_manual_stock_publication.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("ALTER COLUMN fulfillment_id DROP NOT NULL", joined)
        self.assertIn("job_kind text NOT NULL DEFAULT 'fulfillment'", joined)
        self.assertIn("job_kind IN ('fulfillment','manual')", joined)
        self.assertIn("requested_stock BETWEEN 0 AND 1000000", joined)
        self.assertIn("CREATE UNIQUE INDEX IF NOT EXISTS uq_yandex_stock_manual_active", joined)

    def test_legacy_buyer_text_migration_repairs_both_setting_sources(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_03_normalize_buyer_text.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("UPDATE seller.yandex_product_settings_snapshot", joined)
        self.assertIn("UPDATE seller.product_card_settings", joined)
        self.assertIn("activation_instruction", joined)
        self.assertIn(r"E'\\n'", joined)
        self.assertNotIn(r"E'\\\\n'", joined)

    def test_playstation_instruction_is_a_seller_only_local_override(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_04_playstation_activation_instruction.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("INSERT INTO seller.product_card_settings", joined)
        self.assertIn("campaign_id = '149196813'", joined)
        self.assertEqual(joined.count("('MRKT-"), 25)
        self.assertIn("Как активировать", joined)
        self.assertIn("Redeem Codes", joined)
        self.assertIn("WHERE btrim(seller.product_card_settings.activation_instruction) = ''", joined)
        self.assertNotIn("UPDATE app.", joined)

    def test_playstation_instruction_is_copied_from_reference_card(self) -> None:
        project_root = Path(__file__).resolve().parents[2]
        migration = project_root / "db" / "migrations" / "runtime" / "20260826_05_copy_playstation_instruction.sql"
        joined = "\n".join(split_sql_statements(migration.read_text(encoding="utf-8")))

        self.assertIn("MRKT-9CTX61DE", joined)
        self.assertEqual(joined.count("('MRKT-"), 25)
        self.assertIn("activation_instruction = reference_instruction.value", joined)
        self.assertIn("manual_stock_limit = imported_settings.manual_stock_limit", joined)
        self.assertIn("sales_limit = imported_settings.sales_limit", joined)
        self.assertNotIn("UPDATE app.", joined)


if __name__ == "__main__":
    unittest.main()
