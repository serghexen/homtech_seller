"""Проверки одноразовой ссылки в disposable PostgreSQL, без поставщика."""
import os
import threading
import unittest
from types import SimpleNamespace
from decimal import Decimal
from uuid import uuid4
from unittest.mock import patch

import psycopg
from psycopg.conninfo import conninfo_to_dict
from fastapi import HTTPException
from fastapi import FastAPI
from fastapi.testclient import TestClient

from domains.steam_topups import SteamTopups, SubmitLinkIn, mount_steam_topup_routes

DSN = os.getenv('SELLER_TEST_DATABASE_URL', '')


class FakeHub:
    def __init__(self):
        self.operations = {}
        self.requests = []
        self.lose_response = False
        self.unavailable = False

    def _request(self, path, **kwargs):
        if self.unavailable:
            raise RuntimeError('offline')
        payload = kwargs['payload']
        self.requests.append(payload)
        key = payload['idempotency_key']
        self.operations.setdefault(key, {'id':str(uuid4()), 'kind':'steam_topup', 'state':'created',
                                      'requested_amount':payload['requested_amount'], 'payment_started':False})
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError('response lost after commit')
        return self.operations[key].copy()

    def purchase(self, purchase_id):
        return next(item.copy() for item in self.operations.values() if item['id']==purchase_id)


@unittest.skipUnless(DSN, 'Requires isolated SELLER_TEST_DATABASE_URL')
class SteamTopupIntegrationTests(unittest.TestCase):
    def setUp(self):
        if not conninfo_to_dict(DSN).get('dbname','').startswith('seller_test_'):
            raise RuntimeError('Refusing non-test DB')
        env=patch.dict(os.environ, {'SELLER_STEAM_TOPUPS_ENABLED':'true','SELLER_STEAM_LINK_SECRET':'test-only-'+'s'*32})
        env.start(); self.addCleanup(env.stop)
        self.hub=FakeHub()
        self.service=SteamTopups(lambda:DSN, lambda:self.hub)
        with psycopg.connect(DSN) as c:
            c.execute('TRUNCATE seller.users CASCADE')
            self.user=c.execute("INSERT INTO seller.users(email) VALUES ('steam@example.test') RETURNING id").fetchone()[0]
            self.workspaces=[]
            for n in range(2):
                w=c.execute('INSERT INTO seller.workspaces(name,owner_user_id) VALUES (%s,%s) RETURNING id',
                            (f'steam{n}',self.user)).fetchone()[0]
                self.workspaces.append(w)
                c.execute('INSERT INTO seller.steam_topup_settings(workspace_id,enabled,max_amount,budget_amount) VALUES (%s,true,200,400)',(w,))

    def create(self, workspace=None, key=None, amount='100.00'):
        return self.service.create(workspace or self.workspaces[0],self.user,Decimal(amount),key or uuid4())

    def due(self):
        with psycopg.connect(DSN) as c:
            c.execute("UPDATE seller.steam_topup_attempts SET next_attempt_at=now()-interval '1 second',lease_until=NULL")

    def count(self, table):
        with psycopg.connect(DSN) as c:
            return c.execute('SELECT count(*) FROM '+table).fetchone()[0]

    def test_creation_retry_and_amount_tampering(self):
        key=uuid4(); link=self.create(key=key)
        self.assertEqual(link['token'],self.create(key=key)['token'])
        with self.assertRaises(HTTPException): self.create(key=key,amount='200')
        self.assertEqual(self.count('seller.steam_topup_links'),1)
        with self.assertRaises(ValueError): SubmitLinkIn(token=link['token'],account='test_login',request_key=uuid4(),amount=200)

    def test_http_requires_owner_and_never_accepts_workspace_or_amount_from_buyer(self):
        app=FastAPI()
        context=SimpleNamespace(id=self.user,workspace_id=self.workspaces[0],role_code='viewer')
        mount_steam_topup_routes(app,database_url=lambda:DSN,
            current_user=lambda:SimpleNamespace(user_id=self.user),user_with_workspace=lambda *_:context)
        client=TestClient(app)
        self.assertEqual(client.post('/steam-topups',json={'amount':'100','request_key':str(uuid4())}).status_code,403)
        context.role_code='owner'
        self.assertEqual(client.post('/steam-topups',json={'amount':'100','request_key':str(uuid4()),'workspace_id':self.workspaces[1]}).status_code,422)
        response=client.post('/steam-topups',json={'amount':'100','request_key':str(uuid4())})
        self.assertEqual(response.status_code,200)
        token=response.json()['token']
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(client.post('/steam-topups/public/submit',json={'token':token,'account':'test_login','request_key':str(uuid4()),'amount':200}).status_code,422)
        status=client.post('/steam-topups/public/status',json={'token':token}).json()
        self.assertEqual(float(status['amount']),100)
        self.assertNotIn('workspace_id',status)

    def test_two_simultaneous_clicks_create_one_attempt(self):
        link=self.create();barrier=threading.Barrier(2);errors=[]
        def click():
            try:
                barrier.wait()
                self.service.submit(link['token'],'test_login',uuid4())
            except Exception as exc: errors.append(exc)
        threads=[threading.Thread(target=click) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertFalse(errors)
        self.assertTrue(all(not t.is_alive() for t in threads))
        self.assertEqual(self.count('seller.steam_topup_attempts'),1)

    def test_two_workers_claim_one_operation(self):
        link=self.create();self.service.submit(link['token'],'test_login',uuid4())
        barrier=threading.Barrier(2);errors=[]
        def work():
            try: barrier.wait();self.service.process_once()
            except Exception as exc:errors.append(exc)
        threads=[threading.Thread(target=work) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(5)
        self.assertFalse(errors)
        self.assertEqual(len(self.hub.requests),1)

    def test_response_lost_after_hub_commit_reuses_operation(self):
        link=self.create();self.service.submit(link['token'],'test_login',uuid4())
        self.hub.lose_response=True
        self.service.process_once();self.due();self.service.process_once()
        self.assertEqual(len(self.hub.operations),1)
        self.assertEqual(len(self.hub.requests),2)
        self.assertEqual(self.hub.requests[0],self.hub.requests[1])

    def test_success_consumes_link_permanently(self):
        link=self.create();self.service.submit(link['token'],'test_login',uuid4())
        self.service.process_once()
        next(iter(self.hub.operations.values())).update(state='succeeded',payment_started=True)
        self.due();self.service.process_once()
        for _ in range(5): self.service.submit(link['token'],'other_login',uuid4())
        self.assertEqual(self.service.read_public(link['token'])['state'],'succeeded')
        self.assertEqual(self.count('seller.steam_topup_attempts'),1)

    def test_failed_check_allows_correction_but_old_request_never_restarts(self):
        link=self.create();request_key=uuid4();self.service.submit(link['token'],'wrong_login',request_key)
        self.service.process_once()
        next(iter(self.hub.operations.values())).update(state='failed',payment_started=False)
        self.due();self.service.process_once()
        self.assertTrue(self.service.read_public(link['token'])['can_submit'])
        self.service.submit(link['token'],'wrong_login',request_key)
        self.assertEqual(self.count('seller.steam_topup_attempts'),1)
        with psycopg.connect(DSN) as c:
            c.execute("UPDATE seller.steam_topup_links SET updated_at=now()-interval '10 seconds'")
        self.service.submit(link['token'],'fixed_login',uuid4())
        self.assertEqual(self.count('seller.steam_topup_attempts'),2)

    def test_payment_started_or_unknown_never_releases_link(self):
        link=self.create();self.service.submit(link['token'],'test_login',uuid4())
        self.service.process_once()
        result=next(iter(self.hub.operations.values()))
        result.update(state='processing',payment_started=True)
        self.due();self.service.process_once()
        result.update(state='failed',payment_started=False)
        self.due();self.service.process_once()
        self.assertFalse(self.service.read_public(link['token'])['can_submit'])
        self.assertEqual(self.service.read_public(link['token'])['state'],'failed')

    def test_two_workspaces_are_isolated_and_failure_does_not_starve_other(self):
        first=self.create();second=self.create(self.workspaces[1])
        self.assertEqual(len(self.service.listing(self.workspaces[0])['items']),1)
        with self.assertRaises(HTTPException):self.service.cancel(self.workspaces[1],first['id'])
        self.service.submit(first['token'],'first_login',uuid4())
        self.service.submit(second['token'],'second_login',uuid4())
        self.hub.unavailable=True;self.service.process_once()
        self.hub.unavailable=False;self.service.process_once()
        self.assertEqual(self.hub.requests[0]['workspace_id'],self.workspaces[1])
        self.assertEqual(self.service.read_public(second['token'])['state'],'processing')

    def test_expired_cancelled_disabled_and_budget_are_enforced(self):
        link=self.create()
        with psycopg.connect(DSN) as c:c.execute("UPDATE seller.steam_topup_links SET expires_at=now()-interval '1 second'")
        with self.assertRaises(HTTPException):self.service.submit(link['token'],'test_login',uuid4())
        self.service.cancel(self.workspaces[0],link['id'])
        self.assertFalse(self.service.submit(link['token'],'test_login',uuid4())['can_submit'])
        self.create(amount='200');self.create(amount='200')
        with self.assertRaises(HTTPException):self.create()
        with patch.dict(os.environ,{'SELLER_STEAM_TOPUPS_ENABLED':'false'}):
            with self.assertRaises(HTTPException):self.create()

    def test_dead_letter_has_explicit_reconcile_without_new_key(self):
        link=self.create();self.service.submit(link['token'],'test_login',uuid4())
        with psycopg.connect(DSN) as c:c.execute('UPDATE seller.steam_topup_attempts SET retry_count=19')
        self.hub.unavailable=True;self.service.process_once()
        self.assertEqual(self.service.read_public(link['token'])['state'],'attention')
        self.service.reconcile(self.workspaces[0],link['id'])
        self.hub.unavailable=False;self.service.process_once()
        self.assertEqual(self.count('seller.steam_topup_attempts'),1)
        self.assertEqual(len(self.hub.operations),1)
