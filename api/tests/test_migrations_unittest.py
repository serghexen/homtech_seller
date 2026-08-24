"""Минимальные проверки новых runtime-миграций без подключения к рабочей базе."""

from __future__ import annotations

import unittest
from pathlib import Path

from scripts.run_migrations import split_sql_statements


class MigrationFilesTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
