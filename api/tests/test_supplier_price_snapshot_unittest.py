"""Проверки происхождения цены без обращений к поставщику."""
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.supplier_hub_api import mount_supplier_hub_routes
from domains.supplier_hub_client import SupplierHubError
from domains.supplier_price_snapshot import cached_supplier_price


def snapshot(**changes):
    # Цена и остаток проверялись в разные моменты; последняя попытка цены была позже.
    item = dict(service_id='42', nominal_id='2150', status='active', price='1732.40',
                currency='RUB', price_updated_at='2026-01-01T08:00:00Z',
                price_checked_at='2026-01-02T08:00:00Z', checked_at='2026-01-03T08:00:00Z',
                price_error=False)
    item.update(changes)
    return dict(version=1, generated_at='2026-01-04T08:00:00Z', items=[item])


class SupplierPriceTests(unittest.TestCase):
    def test_failed_attempt_keeps_last_successful_time(self):
        result = cached_supplier_price(snapshot(price_error=True), 42, '2150')
        self.assertEqual(result['amount'], '1732.40')
        self.assertEqual(result['checked_at'], datetime(2026, 1, 1, 8, tzinfo=timezone.utc))
        self.assertEqual(result['last_attempt_at'], datetime(2026, 1, 2, 8, tzinfo=timezone.utc))
        self.assertIn('ошибкой', result['warning'])

    def test_price_requires_valid_amount_currency_and_success_timestamp(self):
        for changes in ({'price': None}, {'price': 'NaN'}, {'price': 'Infinity'}, {'price': '-1'},
                        {'price': '0'}, {'currency': 'USD'}, {'price_updated_at': None},
                        {'price_updated_at': '2026-01-01T08:00:00'},
                        {'price_updated_at': '2999-01-01T08:00:00Z'}):
            with self.subTest(changes=changes):
                self.assertIsNone(cached_supplier_price(snapshot(**changes), 42, '2150')['amount'])

    def test_exact_mapping_old_contract_and_duplicates(self):
        for service, nominal in ((43, '2150'), (42, '950')):
            self.assertIsNone(cached_supplier_price(snapshot(), service, nominal)['amount'])
        self.assertIsNone(cached_supplier_price({'version': 1, 'items': [dict(service_id='42', nominal_id='2150')]}, 42, '2150')['amount'])
        value = snapshot()
        value['items'] *= 2
        with self.assertRaises(ValueError):
            cached_supplier_price(value, 42, '2150')

    def test_errors_do_not_erase_known_price(self):
        value = snapshot(status='unavailable')
        value['catalog_error'] = True
        result = cached_supplier_price(value, 42, '2150')
        self.assertEqual(result['amount'], '1732.40')
        self.assertIn('каталога', result['warning'])
        self.assertIn('не подтверждена', result['warning'])

    def test_cached_api_scopes_access_and_never_calculates_or_buys(self):
        # Второй workspace не может читать снимок под чужим connection_id.
        app = FastAPI()
        db = MagicMock()
        current_workspace = SimpleNamespace(workspace_id=10)
        mount_supplier_hub_routes(app, database_url=lambda: 'test', psycopg=db,
                                  current_user=lambda: SimpleNamespace(user_id=1),
                                  user_with_workspace=lambda conn, uid: current_workspace)
        client = TestClient(app)
        with patch('domains.supplier_hub_api.connection_allows', side_effect=lambda cur, ws, conn, cap: (ws, conn) in {(10, 1), (20, 2)}), \
             patch('domains.supplier_hub_api.SupplierHubClient') as hub:
            hub.return_value.stock_snapshot.return_value = snapshot()
            for ws, connection_id in ((10, 1), (20, 2)):
                current_workspace.workspace_id = ws
                result = client.get(f'/integrations/supplier-hub/cached-price?connection_id={connection_id}&service_id=42&nominal_id=2150')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(datetime.fromisoformat(result.json()['checked_at'].replace('Z', '+00:00')),
                                 datetime(2026, 1, 1, 8, tzinfo=timezone.utc))
                self.assertEqual(result.json()['source'], 'crm')
            count = hub.call_count
            self.assertEqual(client.get('/integrations/supplier-hub/cached-price?connection_id=1&service_id=42').status_code, 403)
            self.assertEqual(hub.call_count, count)
            self.assertEqual([call[0] for call in hub.return_value.method_calls], ['stock_snapshot', 'stock_snapshot'])
            hub.return_value.stock_snapshot.side_effect = SupplierHubError('private transport error')
            result = client.get('/integrations/supplier-hub/cached-price?connection_id=2&service_id=42')
            self.assertEqual(result.status_code, 502)
            self.assertNotIn('private transport error', result.text)
            hub.return_value.quote.assert_not_called()

    def test_live_quote_has_its_own_timestamp(self):
        # Время отдельной проверки приходит с сервера, а не из браузерных часов.
        app = FastAPI()
        mount_supplier_hub_routes(app, database_url=lambda: 'test', psycopg=MagicMock(),
                                  current_user=lambda: SimpleNamespace(user_id=1),
                                  user_with_workspace=lambda conn, uid: SimpleNamespace(workspace_id=10))
        with patch('domains.supplier_hub_api.connection_allows', return_value=True), \
             patch('domains.supplier_hub_api.SupplierHubClient') as hub:
            hub.return_value.quote.return_value = dict(success=True, fixed_amount='1732.40')
            before = datetime.now(timezone.utc)
            result = TestClient(app).post('/integrations/supplier-hub/quote', json=dict(connection_id=1, service_id=42, nominal_id='2150'))
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['source'], 'provider')
            self.assertGreaterEqual(datetime.fromisoformat(result.json()['checked_at'].replace('Z', '+00:00')), before)
            hub.return_value.stock_snapshot.assert_not_called()
