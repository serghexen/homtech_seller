"""Контракт точечного раскрытия ключей по явному действию оператора."""

from __future__ import annotations

import inspect
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, HTTPException

from domains.marketplace_key_reveals_api import (
    OrderKeysRevealIn,
    mount_marketplace_key_reveal_routes,
)


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


class MarketplaceKeyRevealsApiTests(unittest.TestCase):
    def mount(self, psycopg, role_code="operator"):
        app = FastAPI()
        mount_marketplace_key_reveal_routes(
            app,
            database_url=lambda: "postgresql://seller",
            psycopg=psycopg,
            current_user=lambda: None,
            user_with_workspace=lambda *_args: SimpleNamespace(workspace_id=4, role_code=role_code),
        )
        return app

    def test_mounts_two_post_only_reveal_routes(self) -> None:
        app = self.mount(None)
        methods = {
            route.path: route.methods
            for route in app.routes
            if route.path.endswith("/reveal")
        }
        self.assertEqual(methods["/marketplaces/catalog/key-pool/keys/{key_id}/reveal"], {"POST"})
        self.assertEqual(methods["/marketplaces/orders/fulfillment/reveal"], {"POST"})
        self.assertEqual(len(methods), 2)

    @patch.dict("os.environ", {"SELLER_KEY_POOL_SECRET": "s" * 32})
    def test_pool_reveal_is_scoped_to_pool_origin_card_and_workspace(self) -> None:
        psycopg = FakePsycopg([(11, "AAAA-BBBB-CCCC")])
        app = self.mount(psycopg)
        endpoint = next(
            route.endpoint for route in app.routes
            if route.path == "/marketplaces/catalog/key-pool/keys/{key_id}/reveal"
        )

        result = endpoint(
            key_id=11,
            connection_id=7,
            external_product_id="SKU-1",
            user=SimpleNamespace(user_id=9),
        )

        self.assertEqual(result.code, "AAAA-BBBB-CCCC")
        sql, params = psycopg.cursor.executions[0]
        self.assertIn("pgp_sym_decrypt", sql)
        self.assertIn("key.key_origin='pool'", sql)
        self.assertIn("marketplace_connection.workspace_id=%s", sql)
        self.assertEqual(params[2:4], (7, "SKU-1"))

    @patch.dict("os.environ", {"SELLER_KEY_POOL_SECRET": "s" * 32})
    def test_order_reveal_uses_only_reservations_of_exact_order_item(self) -> None:
        psycopg = FakePsycopg([[(31, "KEY-ONE"), (32, "KEY-TWO")]])
        app = self.mount(psycopg)
        endpoint = next(
            route.endpoint for route in app.routes
            if route.path == "/marketplaces/orders/fulfillment/reveal"
        )

        result = endpoint(
            payload=OrderKeysRevealIn(
                connection_id=7,
                external_order_id="59942082307",
                external_item_id="1162720619",
            ),
            user=SimpleNamespace(user_id=9),
        )

        self.assertEqual([item.code for item in result.items], ["KEY-ONE", "KEY-TWO"])
        sql, params = psycopg.cursor.executions[0]
        self.assertIn("reservation.state IN ('reserved','consumed')", sql)
        self.assertIn("item.external_order_id=%s", sql)
        self.assertIn("item.external_item_id=%s", sql)
        self.assertEqual(params[1:4], (7, "59942082307", "1162720619"))

    @patch.dict("os.environ", {"SELLER_KEY_POOL_SECRET": "s" * 32})
    def test_viewer_cannot_reveal_keys(self) -> None:
        app = self.mount(FakePsycopg([]), role_code="viewer")
        endpoint = next(
            route.endpoint for route in app.routes
            if route.path == "/marketplaces/orders/fulfillment/reveal"
        )
        with self.assertRaises(HTTPException) as raised:
            endpoint(
                payload=OrderKeysRevealIn(connection_id=7, external_order_id="1", external_item_id="2"),
                user=SimpleNamespace(user_id=9),
            )
        self.assertEqual(raised.exception.status_code, 403)

    def test_reveal_module_does_not_call_marketplace_or_supplier(self) -> None:
        source = inspect.getsource(mount_marketplace_key_reveal_routes)
        self.assertNotIn("urllib", source)
        self.assertNotIn("deliver_yandex", source)
        self.assertNotIn("supplier", source)


if __name__ == "__main__":
    unittest.main()
