"""PostgreSQL integration checks. Requires a disposable seller_test_* database.

Never connects to DATABASE_URL; no network marketplace sender is used.
"""
import os
import threading
import unittest
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict

from domains.yandex_daily_stock import enqueue_due_daily_stock
from domains.yandex_market_stock_outbound import YandexStockOutboundProcessor, YandexStockOutboundError

TEST_DSN = os.getenv('SELLER_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_DSN, 'Requires isolated SELLER_TEST_DATABASE_URL')
class DailyStockIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not conninfo_to_dict(TEST_DSN).get('dbname', '').startswith('seller_test_'):
            raise RuntimeError('Refusing non-test database')
        self.env = patch.dict(os.environ, {'SELLER_YANDEX_STOCK_OUTBOUND_ENABLED':'true',
                                          'MARKETPLACE_CREDENTIALS_SECRET':'test-encryption-key-12345678901234567890'})
        self.env.start()
        self.addCleanup(self.env.stop)
        with psycopg.connect(TEST_DSN) as c:
            c.execute('TRUNCATE seller.users CASCADE')
            u = c.execute("INSERT INTO seller.users(email) VALUES ('quota@example.test') RETURNING id").fetchone()[0]
            self.stores = []
            for n in range(2):
                w = c.execute("INSERT INTO seller.workspaces(name,owner_user_id) VALUES (%s,%s) RETURNING id",(f'w{n}',u)).fetchone()[0]
                shop = c.execute("""INSERT INTO seller.marketplace_connections(workspace_id,provider_code,display_name,
                  campaign_id,token_ciphertext,created_by_user_id,status,launch_state,fulfillment_started_at,
                  stock_outbound_enabled,yandex_daily_stock_enabled)
                  VALUES (%s,'yandex_market','test',%s,pgp_sym_encrypt('fake-token',%s),%s,'active','running',now()-interval '2 days',true,true) RETURNING id""",
                  (w,str(n+1),os.environ['MARKETPLACE_CREDENTIALS_SECRET'],u)).fetchone()[0]
                c.execute("INSERT INTO seller.catalog_items(connection_id,external_product_id,title) VALUES (%s,'sku','test')",(shop,))
                c.execute("INSERT INTO seller.product_card_settings(connection_id,external_product_id,manual_stock_limit,sales_limit) VALUES (%s,'sku',5,15)",(shop,))
                self.stores.append((w,shop))
        self.sent = []
        self.processor = YandexStockOutboundProcessor(database_url=lambda:TEST_DSN,psycopg=psycopg,sender=self.sent.append)

    def order(self, shop, key, quantity, status='processing', days=0):
        with psycopg.connect(TEST_DSN) as c:
            c.execute("""INSERT INTO seller.order_items(connection_id,external_order_id,external_item_id,offer_id,
                 quantity,normalized_status,delivery_type,created_at)
                 VALUES (%s,%s,'1','sku',%s,%s,'DIGITAL',now()+%s*interval '1 day')
                 ON CONFLICT (connection_id,external_order_id,external_item_id) DO UPDATE
                   SET normalized_status=EXCLUDED.normalized_status,quantity=EXCLUDED.quantity""",(shop,key,quantity,status,days))

    def due(self):
        with psycopg.connect(TEST_DSN) as c:
            c.execute("UPDATE seller.yandex_stock_outbound_jobs SET next_attempt_at=now()-interval '1 second'")
            c.execute("UPDATE seller.marketplace_connections SET last_successful_sync_at=now()")

    def counts(self, workspace, shop):
        with psycopg.connect(TEST_DSN) as c:
            return c.execute("SELECT * FROM seller.yandex_daily_sales(%s,%s,'sku')",(workspace,shop)).fetchone()

    def test_daily_catchup_idempotency_and_tenant_isolation(self):
        w,a = self.stores[0]; other,b = self.stores[1]
        self.order(a,'same-order',13,'delivered')
        self.order(b,'same-order',1,'delivered')
        with psycopg.connect(TEST_DSN) as c:
            self.assertEqual(enqueue_due_daily_stock(c),2)
            self.assertEqual(enqueue_due_daily_stock(c),0)
            c.execute('UPDATE seller.marketplace_connections SET next_daily_stock_at=now()')
            c.commit()
            self.assertEqual(enqueue_due_daily_stock(c),0)
        self.assertEqual(self.counts(w,a),(13,0))
        self.assertEqual(self.counts(w,b),(0,0))
        self.due(); self.processor.process_pending_jobs(20)
        self.assertEqual({p.connection_id:p.target_stock for p in self.sent},{a:2,b:5})

    def test_new_day_ignores_old_sales_and_extra_but_keeps_unfinished_reserves(self):
        w,a=self.stores[0]
        self.order(a,'old-sale',15,'delivered',-1)
        self.order(a,'old-pending',2,'processing',-1)
        self.order(a,'today',12,'delivered')
        with psycopg.connect(TEST_DSN) as c:
            c.execute("UPDATE seller.product_card_settings SET sales_limit_daily_extra=20,sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date-1 WHERE connection_id=%s",(a,))
            enqueue_due_daily_stock(c)
        self.due(); self.processor.process_pending_jobs(20)
        self.assertEqual(self.counts(w,a),(12,2))
        self.assertTrue(all(p.target_stock==1 for p in self.sent if p.connection_id==a))

    def test_cancel_releases_quota_and_duplicate_snapshot_does_not_create_job(self):
        w,a=self.stores[0]
        self.order(a,'order',15)
        with psycopg.connect(TEST_DSN) as c:
            before=c.execute('SELECT count(*) FROM seller.yandex_stock_outbound_jobs').fetchone()[0]
        self.order(a,'order',15)
        with psycopg.connect(TEST_DSN) as c:
            self.assertEqual(before,c.execute('SELECT count(*) FROM seller.yandex_stock_outbound_jobs').fetchone()[0])
        self.due(); self.processor.process_pending_jobs(); self.assertEqual(self.sent[-1].target_stock,0)
        self.order(a,'order',15,'cancelled')
        self.due(); self.processor.process_pending_jobs(); self.assertEqual(self.sent[-1].target_stock,5)
        self.assertEqual(self.counts(w,a),(0,0))

    def test_cancel_with_unknown_send_keeps_quota(self):
        w,a=self.stores[0]
        self.order(a,'unknown',15)
        with psycopg.connect(TEST_DSN) as c:
            c.execute("""INSERT INTO seller.order_fulfillments(connection_id,external_order_id,external_item_id,
              offer_id,requested_quantity,reservation_ref,status) VALUES (%s,'unknown','1','sku',15,'test-unknown','unknown')""",(a,))
        self.order(a,'unknown',15,'cancelled')
        self.assertEqual(self.counts(w,a),(0,15))

    def test_two_workers_serialize_store_while_other_workspace_progresses(self):
        a=self.stores[0][1]; b=self.stores[1][1]
        self.order(a,'a1',1); self.order(a,'a2',1); self.order(b,'b1',1)
        self.due()
        entered=threading.Event(); release=threading.Event(); outcomes=[]
        def sender(payload):
            outcomes.append(payload.connection_id)
            entered.set()
            if not release.wait(10): raise RuntimeError('test timeout')
        worker=YandexStockOutboundProcessor(database_url=lambda:TEST_DSN,psycopg=psycopg,sender=sender)
        thread=threading.Thread(target=worker.process_pending_jobs,args=(1,))
        thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.processor.process_pending_jobs(2)
            self.assertEqual([p.connection_id for p in self.sent],[b])
            self.assertEqual(outcomes,[a])
        finally:
            release.set(); thread.join(10)
        self.assertFalse(thread.is_alive())

    def test_crash_after_sending_commit_recalculates_on_recovery(self):
        a=self.stores[0][1]; self.order(a,'a1',1); self.due()
        with psycopg.connect(TEST_DSN) as lock:
            payload=self.processor._claim_and_prepare(lock)
            self.assertIsNotNone(payload)
            with psycopg.connect(TEST_DSN) as c:
                c.execute("UPDATE seller.yandex_stock_outbound_jobs SET locked_until=now()-interval '1 second' WHERE id=%s",(payload.job_id,))
            self.assertEqual(self.processor.recover_stale(),0)
        self.assertEqual(self.processor.recover_stale(),1)
        self.order(a,'a1',15)
        self.due(); self.processor.process_pending_jobs(10)
        self.assertTrue(all(p.target_stock==0 for p in self.sent))

    def test_failed_store_backoff_does_not_block_other_workspace(self):
        a=self.stores[0][1]; b=self.stores[1][1]
        self.order(a,'a1',1); self.order(b,'b1',1); self.due()
        def sender(payload):
            if payload.connection_id==a: raise YandexStockOutboundError('HTTP 429',definite=False)
            self.sent.append(payload)
        self.processor._sender=sender
        self.processor.process_pending_jobs(5)
        self.assertEqual([p.connection_id for p in self.sent],[b])
        with psycopg.connect(TEST_DSN) as c:
            self.assertEqual(c.execute("SELECT state,next_attempt_at>now() FROM seller.yandex_stock_outbound_jobs WHERE connection_id=%s",(a,)).fetchone(),('queued',True))

    def test_disabled_and_archived_stores_are_not_renewed(self):
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.marketplace_connections SET yandex_daily_stock_enabled=false WHERE id=%s',(self.stores[0][1],))
            c.execute('UPDATE seller.catalog_items SET is_archived=true WHERE connection_id=%s',(self.stores[1][1],))
            self.assertEqual(enqueue_due_daily_stock(c),0)

    def test_scope_mismatch_is_rejected(self):
        with self.assertRaises(psycopg.errors.RaiseException):
            with psycopg.connect(TEST_DSN) as c:
                c.execute("INSERT INTO seller.yandex_stock_outbound_jobs(workspace_id,connection_id,external_product_id,job_kind) VALUES (%s,%s,'sku','reconcile')",(self.stores[0][0],self.stores[1][1]))

    def test_catalog_api_reads_today_metrics_and_isolates_workspace(self):
        from fastapi import FastAPI
        from types import SimpleNamespace
        from domains.local_auth import AuthenticatedUser
        from domains.marketplace_read_api import mount_marketplace_read_routes
        w,a=self.stores[0]
        self.order(a,'today',13,'delivered')
        app=FastAPI()
        user=AuthenticatedUser(user_id=1,email='quota@example.test')
        mount_marketplace_read_routes(app,database_url=lambda:TEST_DSN,psycopg=psycopg,
          current_user=lambda:user,user_with_workspace=lambda *_:SimpleNamespace(workspace_id=w,role_code='owner',id=1))
        endpoint=next(r.endpoint for r in app.routes if r.path=='/marketplaces/catalog')
        result=endpoint(connection_id=a,query='',state='active',page=1,page_size=24,user=user)
        self.assertEqual(len(result.items),1)
        item=result.items[0]
        self.assertEqual((item.sales_limit_used,item.sales_limit_reserved,item.sales_limit_remaining),(13,0,2))
        self.assertTrue(item.sales_metrics_available)
        self.assertTrue(item.daily_stock_enabled)
        result=endpoint(connection_id=self.stores[1][1],query='',state='active',page=1,page_size=24,user=user)
        self.assertEqual(result.items,[])

    def test_parallel_schedulers_create_only_one_job_per_product_and_day(self):
        barrier=threading.Barrier(2); results=[]; errors=[]
        def schedule():
            try:
                with psycopg.connect(TEST_DSN) as c:
                    barrier.wait(timeout=10)
                    results.append(enqueue_due_daily_stock(c))
            except Exception as exc:
                errors.append(exc)
        threads=[threading.Thread(target=schedule) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(10)
        self.assertEqual(errors,[])
        self.assertEqual(sum(results),2)

    def test_today_extra_increases_quota(self):
        w,a=self.stores[0]; self.order(a,'sold',15,'delivered')
        with psycopg.connect(TEST_DSN) as c:
            c.execute("UPDATE seller.product_card_settings SET sales_limit_daily_extra=5,sales_limit_day=(now() AT TIME ZONE 'Europe/Moscow')::date WHERE connection_id=%s",(a,))
        self.due(); self.processor.process_pending_jobs()
        self.assertEqual(self.sent[-1].target_stock,5)

    def test_daily_restoration_waits_for_fresh_orders(self):
        with psycopg.connect(TEST_DSN) as c:
            enqueue_due_daily_stock(c)
            c.execute("UPDATE seller.yandex_stock_outbound_jobs SET next_attempt_at=now()-interval '1 second'")
        self.assertEqual(self.processor.process_pending_jobs(),0)
        self.assertEqual(self.sent,[])
        self.due()
        self.assertEqual(self.processor.process_pending_jobs(),2)

    def test_archived_card_is_checked_again_before_sending(self):
        with psycopg.connect(TEST_DSN) as c:
            enqueue_due_daily_stock(c)
            c.execute('UPDATE seller.catalog_items SET is_archived=true')
        self.due()
        self.processor.process_pending_jobs()
        self.assertEqual(self.sent,[])
