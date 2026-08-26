from __future__ import annotations

from datetime import datetime, timezone
import unittest

from scripts.import_crm_yandex_delivery_history import (
    SourceDelivery,
    link_delivery,
    normalized_codes,
    normalized_source_delivery,
    seller_delivery_source,
)


class ScriptedCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.current = None
        self.executions = []

    def execute(self, sql, params=None):
        if params is not None:
            assert sql.count("%s") == len(params), (sql, params)
        self.executions.append((sql, params))
        self.current = self.responses.pop(0)

    def fetchone(self):
        if isinstance(self.current, list):
            return self.current[0] if self.current else None
        return self.current

    def fetchall(self):
        return self.current if isinstance(self.current, list) else ([] if self.current is None else [self.current])


class CrmYandexDeliveryHistoryImportTests(unittest.TestCase):
    def test_normalizes_delivered_key_set(self) -> None:
        self.assertEqual(normalized_codes('[" ONE ", "TWO"]'), ("ONE", "TWO"))
        with self.assertRaises(ValueError):
            normalized_codes(["SAME", "SAME"])

    def test_requires_complete_exact_key_quantity(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        row = (81, 60702723968, 1162720619, "MRKT-L29R57N3", 1, ["Q4M98HPTL6X6"], "pool", now, now, now, now)
        delivery = normalized_source_delivery(row)
        self.assertEqual(delivery.order_id, "60702723968")
        self.assertEqual(delivery.codes, ("Q4M98HPTL6X6",))
        with self.assertRaises(ValueError):
            normalized_source_delivery((*row[:4], 2, *row[5:]))

    def test_maps_crm_sources_without_enabling_any_outbound_action(self) -> None:
        self.assertEqual(seller_delivery_source("pool"), "pool")
        self.assertEqual(seller_delivery_source("manual"), "manual")
        self.assertEqual(seller_delivery_source("interhub"), "supplier")
        self.assertEqual(seller_delivery_source("legacy"), "external")

    def test_links_one_historical_delivery_with_consumed_reservation(self) -> None:
        now = datetime(2026, 8, 27, tzinfo=timezone.utc)
        cursor = ScriptedCursor([
            ("MRKT-L29R57N3", 1, None, None, None, None),
            (90,),
            (50,),
            None,
            (70,),
            None,
            None,
            None,
            None,
            None,
        ])
        result = link_delivery(
            cursor,
            connection_id=7,
            delivery=SourceDelivery(
                source_id=81,
                order_id="60702723968",
                item_id="1162720619",
                offer_id="MRKT-L29R57N3",
                required_qty=1,
                codes=("Q4M98HPTL6X6",),
                delivery_source="pool",
                market_submitted_at=now,
                delivered_at=now,
                created_at=now,
                updated_at=now,
            ),
            target_secret="s" * 32,
        )

        self.assertEqual(result, (1, 1, 1, 0))
        joined = "\n".join(sql for sql, _params in cursor.executions)
        self.assertIn("pgp_sym_encrypt", joined)
        self.assertIn("'consumed'", joined)
        self.assertIn("crm_delivery_history_imported", joined)
        self.assertNotIn("http", joined.lower())


if __name__ == "__main__":
    unittest.main()
