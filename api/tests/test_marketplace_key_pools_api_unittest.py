from __future__ import annotations

import unittest
from contextlib import nullcontext
from datetime import datetime, timezone
from inspect import getsource
from types import SimpleNamespace

from fastapi import FastAPI

from domains.marketplace_key_pools_api import key_hash, masked_code, mount_marketplace_key_pool_routes


class ScriptedCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.current = None
        self.executions = []

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self.current = self.responses.pop(0)

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        return self.current if isinstance(self.current, list) else ([] if self.current is None else [self.current])


class FakePsycopg:
    def __init__(self, responses):
        self.cursor = ScriptedCursor(responses)

    def connect(self, _database_url):
        connection = SimpleNamespace(cursor=lambda: nullcontext(self.cursor))
        return nullcontext(connection)


class MarketplaceKeyPoolsApiTests(unittest.TestCase):
    def test_masks_only_last_four_characters(self) -> None:
        self.assertEqual(masked_code("AAAA-BBBB-CCCC"), "••••CCCC")
        self.assertEqual(masked_code(""), "••••")

    def test_hash_is_stable_and_does_not_contain_key(self) -> None:
        fingerprint = key_hash("AAAA-BBBB-CCCC")
        self.assertEqual(fingerprint, key_hash("AAAA-BBBB-CCCC"))
        self.assertNotIn("AAAA", fingerprint)

    def test_mounts_only_pool_read_and_add_routes(self) -> None:
        app = FastAPI()
        mount_marketplace_key_pool_routes(
            app,
            database_url=lambda: "",
            psycopg=None,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: None,
        )
        methods = {route.path: route.methods for route in app.routes if route.path.startswith("/marketplaces/catalog/key-pool")}
        self.assertEqual(methods["/marketplaces/catalog/key-pool"], {"GET"})
        self.assertEqual(methods["/marketplaces/catalog/key-pool/keys"], {"POST"})
        self.assertEqual(len(methods), 2)

    def test_key_mutation_checks_store_scoped_plan(self) -> None:
        source = getsource(mount_marketplace_key_pool_routes)

        self.assertIn("KEY_POOL_MANAGE", source)
        self.assertIn("seller_user.workspace_id, connection_id", source)
        self.assertIn("connection_allows", source)

    def test_pool_read_excludes_order_keys_and_returns_clean_order_identity(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        psycopg = FakePsycopg([
            (1,),
            (51,),
            (0, 0, 1, 0, 0, 1),
            [(11, "53XH", "delivered", None, "seller:yandex_market:7:59942082307:9", now, now, "59942082307", "9")],
        ])
        app = FastAPI()
        mount_marketplace_key_pool_routes(
            app,
            database_url=lambda: "test",
            psycopg=psycopg,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: SimpleNamespace(workspace_id=4, role_code="owner"),
        )
        endpoint = next(route.endpoint for route in app.routes if route.path == "/marketplaces/catalog/key-pool")

        result = endpoint(
            connection_id=7,
            external_product_id="SKU-1",
            page=1,
            page_size=20,
            user=SimpleNamespace(user_id=9),
        )

        self.assertEqual(result.total, 1)
        self.assertEqual(result.items[0].issued_order_id, "59942082307")
        pool_queries = "\n".join(sql for sql, _params in psycopg.cursor.executions if "marketplace_keys" in sql)
        self.assertEqual(pool_queries.count("key.key_origin='pool'"), 2)


if __name__ == "__main__":
    unittest.main()
