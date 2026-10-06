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
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
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

    def test_notifier_moves_past_materialized_history(self):
        # Уже созданные доставки не закрывают лимит выборки для следующей страницы событий.
        import notifier
        w,a=self.stores[0]
        with psycopg.connect(TEST_DSN) as c:
            c.execute("INSERT INTO seller.telegram_notification_recipients(workspace_id,chat_id) VALUES (%s,123)",(w,))
            for n in range(3):
                c.execute("INSERT INTO seller.telegram_notification_events(workspace_id,event_type,event_key,payload) VALUES (%s,'supplier_stock',%s,'{}')",(w,str(n)))
            self.assertEqual(notifier.materialize_deliveries(c,limit=1),1)
            self.assertEqual(notifier.materialize_deliveries(c,limit=1),1)
            self.assertEqual(notifier.materialize_deliveries(c,limit=1),1)
            self.assertEqual(notifier.materialize_deliveries(c,limit=1),0)

    def test_order_notification_precedes_stock_backlog(self):
        # Новый заказ не ждёт сотни старых информационных уведомлений об остатках.
        import notifier
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        with psycopg.connect(TEST_DSN) as c:
            c.execute("INSERT INTO seller.telegram_notification_recipients(workspace_id,chat_id) VALUES (%s,123)",(w,))
            notifier.materialize_deliveries(c)
        self.order(a,'urgent-order',1)
        with psycopg.connect(TEST_DSN) as c:
            c.execute("INSERT INTO seller.order_fulfillments(connection_id,external_order_id,external_item_id,offer_id,requested_quantity,reservation_ref,status) VALUES (%s,'urgent-order','1','sku',1,'urgent-order','manual_required')",(a,))
            notifier.materialize_deliveries(c,limit=1)
            delivery=notifier.claim_delivery(c,90)
            self.assertIsNotNone(delivery)
            self.assertEqual(delivery.event_type,'manual_required')

    def test_only_confirmed_zero_and_recovery_notify_not_initial_availability(self):
        # Первый положительный снимок и постановка в очередь не засоряют Telegram.
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(5));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events"),0)
        self.refresh(a,self.snapshot(0))
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events"),0)
        self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events WHERE payload->>'transition'='blocked' AND payload->>'action'='sent'"),1)
        self.refresh(a,self.snapshot(5))
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events"),1)
        self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events WHERE payload->>'transition'='restored' AND payload->>'action'='sent'"),1)
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events WHERE payload->>'action'='queued'"),0)
        from domains.supplier_stock_control import publication_notice
        with psycopg.connect(TEST_DSN) as c:
            with c.cursor() as cur: publication_notice(cur,a,'sku',target=5)
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events"),2)

    def test_notifier_filters_legacy_available_queue_messages(self):
        # Даже старое событие из очереди не должно вернуть отключённый информационный спам.
        import notifier,json
        w,a=self.stores[0]
        with psycopg.connect(TEST_DSN) as c:
            c.execute("INSERT INTO seller.telegram_notification_recipients(workspace_id,chat_id) VALUES (%s,123)",(w,))
            for action in ('queued','sent'):
                c.execute("INSERT INTO seller.telegram_notification_events(workspace_id,event_type,event_key,payload) VALUES (%s,'supplier_stock',%s,%s::jsonb)",(w,action,json.dumps({'connection_id':a,'offer_id':'sku','observation':'available','action':action})))
            notifier.materialize_deliveries(c)
            self.assertIsNone(notifier.claim_delivery(c,90))


    def test_disabled_notifications_consume_success_without_replay_on_enable(self):
        # Подтверждённые при выключенном боте остатки не превращаются в стартовую рассылку.
        from domains.supplier_stock_control import publication_notice
        w,a=self.stores[0]
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET supplier_stock_notifications_enabled=false WHERE id=%s AND workspace_id=%s',(a,w))
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),0)
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET supplier_stock_notifications_enabled=true WHERE id=%s AND workspace_id=%s',(a,w))
            with c.cursor() as cur: publication_notice(cur,a,'sku',target=0)
        self.refresh(a,self.snapshot(0))
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),0)
        self.refresh(a,self.snapshot(8));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value("SELECT count(*) FROM seller.telegram_notification_events WHERE payload->>'transition'='restored'"),1)

    def test_reason_change_and_manual_zero_do_not_repeat_confirmed_block(self):
        # Смена причины zero → unavailable при прежнем блоке не порождает второе сообщение.
        from domains.supplier_stock_control import publication_notice
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        changed=self.snapshot(0);changed['items'][0]['status']='unavailable'
        self.refresh(a,changed)
        with psycopg.connect(TEST_DSN) as c:
            with c.cursor() as cur: publication_notice(cur,a,'sku',target=0)
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),1)

    def test_errors_and_general_warning_stay_silent_until_confirmed_transition(self):
        # Ошибка отправки и сбой снимка не уведомляют, успешный повтор даёт одно событие.
        from domains.supplier_stock_control import publication_notice
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0))
        with psycopg.connect(TEST_DSN) as c:
            with c.cursor() as cur: publication_notice(cur,a,'sku',target=0,error=True)
        self.refresh(a,self.snapshot(0,'common'))
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),0)
        self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),1)
        self.refresh(a,self.snapshot(0))
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),1)

    def test_recovery_at_zero_due_to_quota_waits_for_positive_publication(self):
        # Наличие поставщика без реального положительного PUT ещё не является восстановлением продажи.
        from domains.supplier_stock_control import publication_notice
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        with psycopg.connect(TEST_DSN) as c:
            c.execute("UPDATE seller.product_card_settings SET manual_stock_limit=0 WHERE connection_id=%s AND external_product_id='sku'",(a,))
        self.refresh(a,self.snapshot(5));self.due();self.processor.process_pending_jobs(20)
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),1)
        with psycopg.connect(TEST_DSN) as c:
            with c.cursor() as cur:
                publication_notice(cur,a,'sku',target=5)
                publication_notice(cur,a,'sku',target=5)
        self.assertEqual(self.value('SELECT count(*) FROM seller.telegram_notification_events'),2)

    def test_each_transition_has_one_delivery_per_chat_and_old_warnings_are_filtered(self):
        # Два разрешённых чата получают по одной доставке; повторы и старые предупреждения не проходят.
        import notifier,json
        w,a=self.stores[0]
        self.refresh(a,self.snapshot(0));self.due();self.processor.process_pending_jobs(20)
        self.refresh(a,self.snapshot(0))
        with psycopg.connect(TEST_DSN) as c:
            for chat in (123,456):
                c.execute('INSERT INTO seller.telegram_notification_recipients(workspace_id,chat_id) VALUES (%s,%s)',(w,chat))
            for action in ('queued','error','common_warning','common_restored'):
                payload={'connection_id':a,'offer_id':'sku','observation':'zero','transition':'blocked','target_stock':0,'action':action}
                c.execute("INSERT INTO seller.telegram_notification_events(workspace_id,event_type,event_key,payload) VALUES (%s,'supplier_stock',%s,%s::jsonb)",(w,action,json.dumps(payload)))
            notifier.materialize_deliveries(c)
            self.assertEqual(notifier.materialize_deliveries(c),0)
            deliveries=[notifier.claim_delivery(c,90),notifier.claim_delivery(c,90)]
            self.assertEqual({d.chat_id for d in deliveries},{123,456})
            self.assertTrue(all(d.payload['action']=='sent' for d in deliveries))
            self.assertEqual(len({d.event_id for d in deliveries}),1)
            self.assertIsNone(notifier.claim_delivery(c,90))
