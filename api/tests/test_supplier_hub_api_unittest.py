"""Контракт connection-scoped доступа к диагностике Supplier Hub."""

from __future__ import annotations

import inspect
import unittest

from pydantic import ValidationError

from domains.supplier_hub_api import SupplierHubQuoteIn, mount_supplier_hub_routes


class SupplierHubApiTests(unittest.TestCase):
    def test_quote_requires_store_connection(self) -> None:
        with self.assertRaises(ValidationError):
            SupplierHubQuoteIn(service_id=11125)

    def test_supplier_access_is_checked_for_connection_inside_authenticated_workspace(self) -> None:
        source = inspect.getsource(mount_supplier_hub_routes)

        self.assertIn("connection_id: int = Query(gt=0)", source)
        self.assertIn("seller_user.workspace_id, connection_id", source)
        self.assertIn("connection_allows", source)


if __name__ == "__main__":
    unittest.main()
