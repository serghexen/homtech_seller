"""Проверки решений наличия без сети, покупок и рабочих баз."""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from domains.supplier_stock_control import observation, validate_snapshot, effective_block, SupplierStockController, is_blocked
from domains.yandex_market_stock_outbound import calculate_effective_stock
import notifier


class SupplierStockTests(unittest.TestCase):
    def setUp(self):
        # Один фиксированный момент позволяет воспроизводимо проверять устаревшие снимки.
        self.now = datetime(2026,10,5,9,tzinfo=timezone.utc)
        self.item = dict(service_id='10',nominal_id='20',stock_count=5,status='active',error_scope='',checked_at=self.now.isoformat())

    def test_zero_error_recovery_and_price_independence(self):
        # Только уверенный ноль/ошибка позиции блокируют; изменение цены не является причиной.
        for patch_item, expected in [({},('available',False)),({'stock_count':0},('zero',True)),
                ({'error_scope':'item'},('item_error',True)),({'status':'unavailable'},('unavailable',True)),
                ({'error_scope':'common'},('common_error',None)),({'price_error':'bad price'},('available',False))]:
            self.assertEqual(observation({**self.item,**patch_item},now=self.now),expected)

    def test_missing_stale_and_catalog_failure_never_mean_zero(self):
        # Сбой расписания, формата или каталога не превращается в массовое отсутствие.
        self.assertEqual(observation(None,now=self.now),('unknown',None))
        self.assertEqual(observation(self.item,catalog_error=True,now=self.now),('common_error',None))
        item={**self.item,'stock_count':0,'checked_at':(self.now-timedelta(hours=2)).isoformat()}
        self.assertEqual(observation(item,now=self.now),('stale',None))

    def test_common_failure_preserves_previous_decision_but_not_other_mapping(self):
        # Ни общий сбой не открывает ранее закрытую продажу, ни новая связка не наследует чужой ноль.
        old={'mapping_key':'1:10:20','blocked':True}
        self.assertTrue(effective_block(old,'1:10:20',None))
        self.assertFalse(effective_block(old,'2:10:30',None))
        self.assertFalse(effective_block(old,'1:10:20',False))
        self.assertFalse(effective_block(None,'1:10:20',None))

    def test_recovery_respects_daily_quota_and_reservations(self):
        # Возврат заданных 10 ограничивается остатком квоты: 30-25-2=3.
        self.assertEqual(calculate_effective_stock(10,30,0,0,0,25,2),3)
        self.assertEqual(calculate_effective_stock(10,30,0,0,0,30,0),0)
        self.assertEqual(calculate_effective_stock(0,30,0,0,0,0,0),0)

    def test_rejects_duplicate_ids_malformed_counts_and_scope(self):
        # Некорректный контракт не обрабатывается по частям.
        for items in [[self.item,self.item],[{**self.item,'stock_count':True}],
                      [{**self.item,'stock_count':-1}],[{**self.item,'error_scope':'bad'}]]:
            with self.assertRaises(ValueError):
                validate_snapshot({'version':1,'items':items})
        self.assertEqual(len(validate_snapshot({'version':1,'items':[self.item]})),1)

    @patch('domains.supplier_stock_control.active_mapping',return_value=None)
    def test_disabled_auto_issuance_does_not_restrict_pool(self, mapping):
        # Общий отправитель остаётся свободен публиковать пул при выключенной автовыдаче.
        cur=MagicMock()
        self.assertFalse(is_blocked(cur,1,2,'same-sku'))
        cur.execute.assert_not_called()

    @patch('domains.supplier_stock_control.active_mapping',return_value=('1:10:20','10','20'))
    def test_guard_lookup_is_scoped_to_workspace_connection_product_and_mapping(self,mapping):
        # Одинаковый SKU другой организации не может применить чужую блокировку.
        cur=MagicMock();cur.fetchone.return_value=(True,)
        self.assertTrue(is_blocked(cur,1,2,'same-sku'))
        self.assertEqual(cur.execute.call_args.args[1],(1,2,'same-sku','1:10:20'))

    @patch('domains.supplier_stock_control.enabled',return_value=False)
    def test_disabled_worker_never_opens_database_or_network(self, enabled):
        db,client=MagicMock(),MagicMock()
        c=SupplierStockController(database_url=lambda:'test',psycopg=db,client_factory=client)
        self.assertFalse(c.process_once());db.connect.assert_not_called();client.assert_not_called()

    def test_telegram_distinguishes_request_success_failure_and_global_incident(self):
        # Получение нуля, подтверждённый PUT и неизвестный исход описываются разными сообщениями.
        base={'store_name':'A','offer_id':'sku','observation':'zero'}
        queued=notifier.notification_text('supplier_stock',{**base,'action':'queued'})
        sent=notifier.notification_text('supplier_stock',{**base,'action':'sent','target_stock':0})
        failed=notifier.notification_text('supplier_stock',{**base,'action':'error'})
        self.assertIn('очередь',queued);self.assertNotIn('подтвердил',queued)
        self.assertIn('приём остатка: 0',sent);self.assertIn('не подтверждена',failed)
        common=notifier.notification_text('supplier_stock',{**base,'action':'common_warning','observation':'common_error','affected_count':10})
        self.assertIn('карточек с автовыдачей: 10',common);self.assertIn('обнуление не выполнялось',common)


if __name__=='__main__':
    unittest.main()
