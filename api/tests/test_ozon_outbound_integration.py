"""Run only against a disposable seller_test_* database; external calls are fake."""
import json
import os
import threading
import unittest
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict

from domains.ozon_outbound import OzonOutboundError, OzonOutboundProcessor

TEST_DSN = os.getenv('SELLER_TEST_DATABASE_URL', '')


@unittest.skipUnless(TEST_DSN, 'Requires isolated SELLER_TEST_DATABASE_URL')
class OzonPostingIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not conninfo_to_dict(TEST_DSN).get('dbname', '').startswith('seller_test_'):
            raise RuntimeError('Refusing non-test database')
        self.secret = 'test-encryption-key-12345678901234567890'
        env = patch.dict(os.environ, {'SELLER_OZON_OUTBOUND_ENABLED': 'true',
            'MARKETPLACE_CREDENTIALS_SECRET': self.secret, 'SELLER_KEY_POOL_SECRET': self.secret})
        env.start(); self.addCleanup(env.stop)
        self.stores = []
        with psycopg.connect(TEST_DSN) as c:
            c.execute('TRUNCATE seller.users CASCADE')
            user = c.execute("INSERT INTO seller.users(email) VALUES ('ozon@test.invalid') RETURNING id").fetchone()[0]
            for n in range(2):
                w = c.execute("INSERT INTO seller.workspaces(name,owner_user_id) VALUES (%s,%s) RETURNING id", (f'w{n}',user)).fetchone()[0]
                shop = c.execute("""INSERT INTO seller.marketplace_connections(workspace_id,provider_code,display_name,
                    client_id,token_ciphertext,created_by_user_id,status,fulfillment_outbound_enabled)
                    VALUES (%s,'ozon','test',%s,pgp_sym_encrypt('fake-token',%s),%s,'active',true) RETURNING id""",
                    (w,str(n),self.secret,user)).fetchone()[0]
                self.stores.append((w,shop))
        self.sent = []
        self.processor = OzonOutboundProcessor(database_url=lambda: TEST_DSN, psycopg=psycopg, sender=self.sent.append)

    def posting(self, shop, posting='same-posting', second_queued=True):
        products = [{'sku':9911,'quantity':1,'required_qty_for_digital_code':1},
                    {'sku':9922,'quantity':2,'required_qty_for_digital_code':2}]
        ids = []
        with psycopg.connect(TEST_DSN) as c:
            for index, p in enumerate(products):
                sku = str(p['sku']); qty=p['quantity']; ref=f'{shop}:{posting}:{sku}'
                c.execute("INSERT INTO seller.catalog_items(connection_id,external_product_id,sku) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",(shop,sku,sku))
                c.execute("""INSERT INTO seller.order_items(connection_id,external_order_id,external_item_id,offer_id,
                    sku,quantity,normalized_status,provider_status,delivery_type,raw_payload)
                    VALUES (%s,%s,%s,%s,%s,%s,'processing','awaiting_packaging','DIGITAL',%s::jsonb)""",
                    (shop,posting,sku,sku,sku,qty,json.dumps({'products':products})))
                f = c.execute("""INSERT INTO seller.order_fulfillments(connection_id,external_order_id,external_item_id,
                    offer_id,requested_quantity,reservation_ref,status) VALUES (%s,%s,%s,%s,%s,%s,'reserved') RETURNING id""",
                    (shop,posting,sku,sku,qty,ref)).fetchone()[0]
                pool=c.execute("""INSERT INTO seller.marketplace_key_pools(connection_id,external_product_id) VALUES (%s,%s)
                    ON CONFLICT (connection_id,external_product_id) DO UPDATE SET updated_at=now() RETURNING id""",(shop,sku)).fetchone()[0]
                for k in range(qty):
                    code=f'FAKE-{ref}-{k}'
                    key=c.execute("""INSERT INTO seller.marketplace_keys(pool_id,code_ciphertext,code_hash,status,issued_order_ref)
                        VALUES (%s,pgp_sym_encrypt(%s,%s),%s,'reserved',%s) RETURNING id""",(pool,code,self.secret,code,ref)).fetchone()[0]
                    c.execute("INSERT INTO seller.fulfillment_key_reservations(fulfillment_id,key_id,order_ref) VALUES (%s,%s,%s)",(f,key,ref))
                if index==0 or second_queued:
                    c.execute("INSERT INTO seller.fulfillment_outbound_jobs(fulfillment_id) VALUES (%s)",(f,))
                ids.append(f)
        return ids

    def states(self, ids):
        with psycopg.connect(TEST_DSN) as c:
            return [r[0] for r in c.execute('SELECT status FROM seller.order_fulfillments WHERE id=ANY(%s) ORDER BY id',(ids,))]

    def test_whole_posting_and_duplicate_job_with_two_workspaces(self):
        for _,shop in self.stores: self.posting(shop)
        self.assertEqual(self.processor.process_pending_jobs(5),2)
        self.assertEqual(self.processor.process_pending_jobs(5),0)
        self.assertEqual({p.workspace_id for p in self.sent},{w for w,_ in self.stores})
        for p in self.sent:
            parts=(p,*p.siblings)
            self.assertEqual([(x.sku,len(x.codes)) for x in parts],[(9911,1),(9922,2)])
            self.assertTrue(all(f':{p.posting_number}:' in code and code.startswith(f'FAKE-{p.connection_id}:') for x in parts for code in x.codes))
            self.assertEqual(self.states([x.fulfillment_id for x in parts]),['submitted','submitted'])

    def test_waiting_for_other_position_does_not_block_other_store(self):
        a=self.posting(self.stores[0][1],second_queued=False)
        self.posting(self.stores[1][1])
        self.processor.process_pending_jobs(5)
        self.assertEqual([p.connection_id for p in self.sent],[self.stores[1][1]])
        self.assertEqual(self.states(a),['reserved','reserved'])
        with psycopg.connect(TEST_DSN) as c:
            c.execute('INSERT INTO seller.fulfillment_outbound_jobs(fulfillment_id) VALUES (%s)',(a[1],))
        self.processor.process_pending_jobs(5)
        self.assertEqual(self.states(a),['submitted','submitted'])

    def test_two_workers_do_not_send_same_store_twice(self):
        a=self.stores[0][1]; b=self.stores[1][1]
        self.posting(a); self.posting(a,'next-posting'); self.posting(b)
        entered=threading.Event(); release=threading.Event(); errors=[]
        def sender(payload):
            self.assertEqual(self.states([payload.fulfillment_id,payload.siblings[0].fulfillment_id]),['sending','sending'])
            entered.set()
            if not release.wait(10): raise TimeoutError()
        worker=OzonOutboundProcessor(database_url=lambda:TEST_DSN,psycopg=psycopg,sender=sender)
        def run():
            try: worker.process_pending_jobs(1)
            except Exception as exc: errors.append(exc)
        thread=threading.Thread(target=run);thread.start()
        try:
            self.assertTrue(entered.wait(10))
            self.processor.process_pending_jobs(5)
            self.assertEqual([p.connection_id for p in self.sent],[b])
        finally:
            release.set();thread.join(10)
        self.assertEqual(errors,[])
        self.assertFalse(thread.is_alive())
        self.processor.process_pending_jobs(5)
        self.assertEqual([p.connection_id for p in self.sent],[b,a])

    def test_crash_after_commit_marks_whole_posting_unknown(self):
        ids=self.posting(self.stores[0][1])
        with psycopg.connect(TEST_DSN) as lock:
            self.assertIsNotNone(self.processor._claim_and_prepare(lock))
            with psycopg.connect(TEST_DSN) as c:
                c.execute("UPDATE seller.fulfillment_outbound_jobs SET locked_until=now()-interval '1 second'")
            self.assertEqual(self.processor.recover_stale(),(0,0))
        self.assertEqual(self.processor.recover_stale(),(0,2))
        self.processor.process_pending_jobs(5)
        self.assertEqual(self.states(ids),['unknown','unknown'])
        self.assertEqual(self.sent,[])

    def test_timeout_in_one_store_does_not_block_other_store(self):
        ids=self.posting(self.stores[0][1]);self.posting(self.stores[1][1])
        def sender(payload):
            if payload.connection_id==self.stores[0][1]: raise OzonOutboundError('timeout',definite=False)
            self.sent.append(payload)
        self.processor._sender=sender
        self.processor.process_pending_jobs(5)
        self.assertEqual(self.states(ids),['unknown','unknown'])
        self.assertEqual([p.connection_id for p in self.sent],[self.stores[1][1]])

    def test_400_returns_entire_bundle_to_reserve(self):
        ids=self.posting(self.stores[0][1])
        self.processor._sender=lambda _: (_ for _ in ()).throw(OzonOutboundError('HTTP 400',definite=True))
        self.processor.process_pending_jobs(5)
        self.assertEqual(self.states(ids),['reserved','reserved'])
        with psycopg.connect(TEST_DSN) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM seller.marketplace_keys WHERE status='reserved'").fetchone()[0],3)
            self.assertEqual(c.execute("SELECT count(*) FROM seller.fulfillment_outbound_jobs WHERE state='failed'").fetchone()[0],2)

    def test_missing_local_position_and_quantity_drift_fail_closed(self):
        ids=self.posting(self.stores[0][1])
        with psycopg.connect(TEST_DSN) as c:
            c.execute('UPDATE seller.order_items SET quantity=3 WHERE connection_id=%s AND sku=\'9922\'',(self.stores[0][1],))
        self.processor.process_pending_jobs(5)
        self.assertEqual(self.sent,[])
        self.assertEqual(self.states(ids),['reserved','reserved'])
