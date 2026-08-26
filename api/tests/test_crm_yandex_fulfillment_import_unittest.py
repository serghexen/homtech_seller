from __future__ import annotations

import unittest
from decimal import Decimal

from scripts.import_crm_yandex_fulfillment import max_amount


class FulfillmentImportTests(unittest.TestCase):
    def test_price_buffer_is_rounded_up(self) -> None:
        self.assertEqual(max_amount(Decimal("464.53"), Decimal("5")), Decimal("487.76"))


if __name__ == "__main__":
    unittest.main()
