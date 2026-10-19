"""Названия в Telegram: точная связка, общий кэш, сбой каталога без потери уведомления."""
import unittest
from unittest.mock import MagicMock, patch

from domains.supplier_catalog_labels import SupplierCatalogLabels, catalog_labels
import notifier


class SupplierCatalogLabelsTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.services.return_value = [
            {'service_id': 10, 'title': 'PlayStation UK', 'fields': [
                {'name': 'nominal', 'value_list': [{'id': 20, 'title': '20 GBP'}]}]},
            {'service_id': 11, 'title': 'Steam', 'fields': [
                {'name': 'nominal', 'value_list': [{'id': 20, 'title': '20 USD'}]}]},
        ]
        self.clock = MagicMock(return_value=100)
        self.catalog = SupplierCatalogLabels(lambda: self.client, self.clock)
        self.payload = dict(service_id='10', nominal_id='20', offer_id='same-sku',
                            store_name='JoyCards', action='sent', observation='available', target_stock=5)

    def test_titles_follow_service_and_nominal_pair_and_keep_article(self):
        for service_id, service_title, nominal_title in [('10', 'PlayStation UK', '20 GBP'), ('11', 'Steam', '20 USD')]:
            payload = self.catalog.enrich('supplier_stock', {**self.payload, 'service_id': service_id})
            text = notifier.notification_text('supplier_stock', payload)
            self.assertIn('Товар: same-sku', text)
            self.assertIn('Услуга: ' + service_title, text)
            self.assertIn('Номинал: ' + nominal_title, text)
            self.assertIn('Яндекс подтвердил приём остатка: 5.', text)
        self.client.services.assert_called_once()
        self.assertNotIn('service_title', self.payload)

    def test_catalog_formats_match_product_card(self):
        labels = catalog_labels([{'service_id': 1, 'title': 'Service', 'fields': [
            {'name': 'NOMINAL', 'value_list': ['100', {'value': 200, 'title': '200 EUR'},
                                              {'title': '300 EUR'}, {'id': 400, 'value': '400 EUR'}]}]}])
        self.assertEqual(labels['1'][1], {'100': '100', '200': '200 EUR', '300 EUR': '300 EUR', '400': '400 EUR'})

    def test_failure_uses_ids_and_retries_after_backoff(self):
        self.client.services.side_effect = TimeoutError()
        payload = self.catalog.enrich('supplier_stock', self.payload)
        text = notifier.notification_text('supplier_stock', payload)
        self.assertIn('Услуга: ID 10', text)
        self.assertIn('Номинал: 20', text)
        self.catalog.enrich('supplier_stock', self.payload)
        self.client.services.assert_called_once()
        self.clock.return_value = 400
        self.client.services.side_effect = None
        self.assertEqual(self.catalog.enrich('supplier_stock', self.payload)['service_title'], 'PlayStation UK')

    def test_failed_refresh_keeps_previous_labels(self):
        self.catalog.enrich('supplier_stock', self.payload)
        self.clock.return_value = 3700
        self.client.services.side_effect = TimeoutError()
        self.assertEqual(self.catalog.enrich('supplier_stock', self.payload)['nominal_title'], '20 GBP')

    def test_orders_and_old_events_do_not_fetch_catalog(self):
        self.catalog.enrich('manual_required', self.payload)
        self.catalog.enrich('supplier_stock', {'offer_id': 'old-sku'})
        self.client.services.assert_not_called()
        text = notifier.notification_text('supplier_stock', {'offer_id': 'old-sku'})
        self.assertIn('Товар: old-sku', text)
        self.assertIn('Услуга: —', text)

    def test_delivery_cycle_sends_enriched_text_even_when_catalog_is_down(self):
        delivery = MagicMock(event_type='supplier_stock', payload=self.payload, chat_id=123)
        settings = MagicMock(batch_size=1, lease_seconds=90)
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                catalog = SupplierCatalogLabels(lambda: self.client, self.clock)
                self.client.services.side_effect = TimeoutError() if unavailable else None
                with patch.object(notifier.psycopg, 'connect'), \
                     patch.object(notifier, 'recover_stale_deliveries', return_value=0), \
                     patch.object(notifier, 'materialize_deliveries', return_value=0), \
                     patch.object(notifier, 'sync_commands'), \
                     patch.object(notifier, 'claim_delivery', return_value=delivery), \
                     patch.object(notifier, 'SUPPLIER_CATALOG_LABELS', catalog), \
                     patch.object(notifier, 'send_text', return_value=456) as send, \
                     patch.object(notifier, 'complete_delivery') as complete, \
                     patch.object(notifier, 'fail_delivery') as fail:
                    self.assertEqual(notifier.run_cycle(settings), 1)
                text = send.call_args.args[2]
                self.assertIn('Услуга: ID 10' if unavailable else 'Услуга: PlayStation UK', text)
                self.assertIn('Товар: same-sku', text)
                complete.assert_called_once()
                fail.assert_not_called()


if __name__ == '__main__':
    unittest.main()
