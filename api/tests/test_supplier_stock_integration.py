"""Проверки PostgreSQL только в изолированной seller_test_* БД, без реальной сети."""
import os
import threading
import unittest
from datetime import datetime,timezone,timedelta
from unittest.mock import patch,MagicMock
import psycopg
import test_yandex_daily_stock_integration as daily
TEST_DSN=daily.TEST_DSN
from domains.supplier_stock_control import SupplierStockController


@unittest.skipUnless(TEST_DSN,'Requires isolated SELLER_TEST_DATABASE_URL')
class SupplierStockIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Используем существующий изолированный fixture двух организаций с одинаковым SKU.
        daily.DailyStockIntegrationTests.setUp(self)
        env=patch.dict(os.environ,{'SELLER_SUPPLIER_STOCK_CONTROL_ENABLED':'true','SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED':'true'})
        env.start();self.addCleanup(env.stop)
        access=patch('domains.supplier_stock_control.connection_allows',return_value=True)
        self.access=access.start();self.addCleanup(access.stop)
        self.client=MagicMock()
        self.controller=SupplierStockController(database_url=lambda:TEST_DSN,psycopg=psycopg,client_factory=lambda:self.client)
        with psycopg.connect(TEST_DSN) as c:
            for w,shop in self.stores:
                c.execute("UPDATE seller.marketplace_connections SET supplier_fulfillment_enabled=true,supplier_stock_control_enabled=true,supplier_stock_notifications_enabled=true,supplier_stock_next_at=now()+interval '1 day' WHERE id=%s",(shop,))
                c.execute("INSERT INTO seller.product_fulfillment_policies(connection_id,external_product_id,supplier_issue_enabled,pool_issue_enabled) VALUES (%s,'sku',true,true)",(shop,))
                c.execute("INSERT INTO seller.product_supplier_mappings(connection_id,external_product_id,provider_code,enabled,service_id,nominal_id,max_amount) VALUES (%s,'sku','interhub',true,10,'20',100)",(shop,))

    order=daily.DailyStockIntegrationTests.order
    due=daily.DailyStockIntegrationTests.due

    def snapshot(self,count=5,scope='',age=0):
        # Содержит только публичный контракт снимка, без цены, ключей и сырых данных.
        return {'version':1,'items':[{'service_id':'10','nominal_id':'20','stock_count':count,
          'status':'active','error_scope':scope,'checked_at':(datetime.now(timezone.utc)-timedelta(hours=age)).isoformat()}]}

    def refresh(self,shop,payload):
        self.client.stock_snapshot.side_effect=None
        self.client.stock_snapshot.return_value=payload
        with psycopg.connect(TEST_DSN) as c:
            c.execute("UPDATE seller.marketplace_connections SET supplier_stock_next_at=now() WHERE id=%s",(shop,))
        self.assertTrue(self.controller.process_once())

    def value(self,sql,args=()):
        with psycopg.connect(TEST_DSN) as c:
            return c.execute(sql,args).fetchone()[0]

    def test_zero_recovery_daily_limit_and_duplicate_snapshot(self):
        w,a=self.stores[0];other,b=self.stores[1]
        self.order(a,'sale',12,'delivered')
        self.refresh(a,self.snapshot(0));self.due()
        self.processor.process_pending_jobs(20)
        self.assertTrue(self.sent);self.assertTrue(all(p.target_stock==0 for p in self.sent))
        event_count=self.value('SELECT count(*) FROM seller.telegram_notification_events')
        self.refresh(a,self.snapshot(0))
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),event_count)
        self.assertEqual(self.value('SELECT count(*) FROM seller.product_supplier_stock_state WHERE workspace_id=%s',(other,)),0)
        self.refresh(a,self.snapshot(7));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.sent[-1].target_stock,3)
        self.assertEqual(self.value("SELECT manual_stock_limit FROM seller.product_card_settings WHERE connection_id=%s AND external_product_id='sku'",(a,)),5)

    def test_general_failure_and_stale_do_not_close_sale(self):
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(5));self.due();self.processor.process_pending_jobs(20)
        before=len(self.sent)
        self.refresh(a,self.snapshot(0,'common'))
        self.refresh(a,self.snapshot(0,age=2));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(len(self.sent),before)
        self.assertFalse(self.value('SELECT blocked FROM seller.product_supplier_stock_state WHERE connection_id=%s',(a,)))

    def test_failure_cannot_restore_previously_blocked_card_and_no_auto_means_pool(self):
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0))
        self.refresh(a,self.snapshot(8,'common'));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.sent[-1].target_stock,0)
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.product_fulfillment_policies SET supplier_issue_enabled=false,pool_issue_enabled=false WHERE connection_id=%s',(a,))
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.sent[-1].target_stock,5)
        self.assertFalse(self.value('SELECT blocked FROM seller.product_supplier_stock_state WHERE connection_id=%s',(a,)))

    def test_new_card_and_downgraded_connection(self):
        w,a=self.stores[0]
        self.access.return_value=False
        self.refresh(a,self.snapshot(0))
        self.assertEqual(self.value('SELECT count(*) FROM seller.product_supplier_stock_state'),0)
        self.access.return_value=True
        self.refresh(a,self.snapshot(0))
        self.assertEqual(self.value('SELECT count(*) FROM seller.product_supplier_stock_state'),1)

    def test_worker_crash_after_queue_commit_recovers_without_new_job(self):
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0))
        count=self.value("SELECT count(*) FROM seller.yandex_stock_outbound_jobs WHERE job_kind='supplier'")
        controller=SupplierStockController(database_url=lambda:TEST_DSN,psycopg=psycopg,client_factory=lambda:self.client)
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET supplier_stock_next_at=now() WHERE id=%s',(a,))
        controller.process_once()
        self.assertEqual(self.value("SELECT count(*) FROM seller.yandex_stock_outbound_jobs WHERE job_kind='supplier'"),count)
        self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(len(self.sent),1)

    def test_two_workers_do_not_fetch_same_store_and_other_store_can_continue(self):
        # Сетевой вызов одного магазина удерживает только его lock; другой магазин не ждёт.
        w,a=self.stores[0];other,b=self.stores[1]
        entered=threading.Event();release=threading.Event();errors=[]
        slow=MagicMock()
        def fetch():
            entered.set();release.wait(10);return self.snapshot(0)
        slow.stock_snapshot.side_effect=fetch
        controller=SupplierStockController(database_url=lambda:TEST_DSN,psycopg=psycopg,client_factory=lambda:slow)
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET supplier_stock_next_at=now() WHERE id=%s',(a,))
        def run():
            try: controller.process_once()
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertFalse(controller.process_once())
            self.refresh(b,self.snapshot(4))
            self.assertEqual(slow.stock_snapshot.call_count,1)
        finally:
            release.set();thread.join(10)
        self.assertFalse(errors)
        self.assertEqual(self.value('SELECT count(*) FROM seller.product_supplier_stock_state'),2)

    def test_manual_force_and_old_queued_job_recheck_current_supplier(self):
        # Даже ранее созданная ручная публикация не обходит новый ноль поставщика.
        w,a=self.stores[0]
        with psycopg.connect(TEST_DSN) as c:
            c.execute("INSERT INTO seller.yandex_stock_outbound_jobs(connection_id,external_product_id,job_kind,requested_stock) VALUES (%s,'sku','manual',100)",(a,))
        self.refresh(a,self.snapshot(8,'item'));self.due();self.processor.process_pending_jobs(20)
        self.assertTrue(self.sent)
        self.assertTrue(all(p.target_stock==0 for p in self.sent))

    def test_wrong_workspace_cannot_store_control_state(self):
        # Ограничение БД дополняет область запросов и не допускает чужое подключение в состоянии.
        w,a=self.stores[0];other,b=self.stores[1]
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            with psycopg.connect(TEST_DSN) as c:
                c.execute("INSERT INTO seller.product_supplier_stock_state(workspace_id,connection_id,external_product_id) VALUES (%s,%s,'sku')",(other,a))

    def test_store_without_stock_permission_never_claims_publication(self):
        w,a=self.stores[0]
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET stock_outbound_enabled=false WHERE id=%s',(a,))
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        self.assertFalse(self.sent)

    def test_queued_notification_is_ignored_after_disabling_auto_issuance(self):
        # Даже уже подготовленное уведомление не уходит по выключенной карточке.
        import notifier
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0))
        with psycopg.connect(TEST_DSN) as c:
            c.execute("INSERT INTO seller.telegram_notification_recipients(workspace_id,chat_id) VALUES (%s,123)",(w,))
            notifier.materialize_deliveries(c)
            c.execute('UPDATE seller.product_fulfillment_policies SET supplier_issue_enabled=false WHERE connection_id=%s',(a,));c.commit()
            self.assertIsNone(notifier.claim_delivery(c,90))

    def test_catalog_warning_and_card_state_stay_in_authenticated_workspace(self):
        # Выполняем настоящий SQL списка и предупреждений; чужой магазин не попадает в ответ API.
        from fastapi import FastAPI
        from types import SimpleNamespace
        from domains.marketplace_read_api import mount_marketplace_read_routes
        w,a=self.stores[0];other,b=self.stores[1]
        self.refresh(a,self.snapshot(0));self.refresh(b,self.snapshot(8,'common'))
        app=FastAPI()
        identity=MagicMock()
        mount_marketplace_read_routes(app,database_url=lambda:TEST_DSN,psycopg=psycopg,current_user=lambda:None,user_with_workspace=identity)
        endpoint=next(route.endpoint for route in app.routes if route.path=='/marketplaces/catalog' and 'GET' in route.methods)
        identity.return_value=SimpleNamespace(workspace_id=w,id=1)
        result=endpoint(connection_id=None,query='',state='active',page=1,page_size=100,user=SimpleNamespace(user_id=1))
        self.assertEqual(len(result.items),1)
        self.assertTrue(result.items[0].supplier_stock['blocked'])
        self.assertFalse(result.supplier_stock_warnings)
        identity.return_value=SimpleNamespace(workspace_id=other,id=1)
        result=endpoint(connection_id=None,query='',state='active',page=1,page_size=100,user=SimpleNamespace(user_id=1))
        self.assertEqual(result.supplier_stock_warnings[0]['connection_id'],b)

    def test_recovery_replaces_unsent_zero_and_waits_for_orders(self):
        # Старый ноль не должен превратиться в положительный PUT до свежего снимка заказов.
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0))
        self.refresh(a,self.snapshot(8))
        self.processor.process_pending_jobs(20)
        self.assertFalse(self.sent)
        self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0].target_stock,5)

    def test_disabling_store_control_stops_already_queued_supplier_job(self):
        # Выключатель магазина действует также на задания, созданные до его изменения.
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0))
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET supplier_stock_control_enabled=false WHERE id=%s',(a,))
        self.due();self.processor.process_pending_jobs(20)
        self.assertFalse(self.sent)

    def test_catalog_detects_staleness_even_when_controller_does_not_run(self):
        # Время снимка проверяется при чтении UI, поэтому остановка worker не скрывает предупреждение.
        from fastapi import FastAPI
        from types import SimpleNamespace
        from domains.marketplace_read_api import mount_marketplace_read_routes
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(5))
        with psycopg.connect(TEST_DSN) as c:
            c.execute("UPDATE seller.product_supplier_stock_state SET checked_at=now()-interval '2 hours' WHERE connection_id=%s",(a,))
        app=FastAPI()
        mount_marketplace_read_routes(app,database_url=lambda:TEST_DSN,psycopg=psycopg,current_user=lambda:None,
            user_with_workspace=lambda *args,**kwargs:SimpleNamespace(workspace_id=w,id=1))
        endpoint=next(route.endpoint for route in app.routes if route.path=='/marketplaces/catalog' and 'GET' in route.methods)
        result=endpoint(connection_id=None,query='',state='active',page=1,page_size=100,user=SimpleNamespace(user_id=1))
        self.assertEqual(result.items[0].supplier_stock['observation'],'stale')
        self.assertFalse(result.items[0].supplier_stock['blocked'])
        self.assertEqual(result.supplier_stock_warnings[0]['connection_id'],a)

    def test_zero_can_close_card_even_without_configured_positive_stock(self):
        # Отсутствие положительного заданного остатка не мешает отправить подтверждённый ноль.
        w,a=self.stores[0]
        with psycopg.connect(TEST_DSN) as c:
            c.execute('DELETE FROM seller.product_card_settings WHERE connection_id=%s',(a,))
            c.execute('UPDATE seller.product_fulfillment_policies SET pool_issue_enabled=false WHERE connection_id=%s',(a,))
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(len(self.sent),1)
        self.assertEqual(self.sent[0].target_stock,0)
