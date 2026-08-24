from __future__ import annotations

import unittest

from fastapi import FastAPI

from domains.marketplace_key_pools_api import key_hash, masked_code, mount_marketplace_key_pool_routes


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


if __name__ == "__main__":
    unittest.main()
