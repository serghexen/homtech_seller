"""Проверки узкого контракта отчёта без доступа к рабочим системам."""
from datetime import date
import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from domains.voucher_report_api import date_window, fingerprints, mount_voucher_report_routes, load_sales


class VoucherReportTests(unittest.TestCase):
    def setUp(self):
        # Две идентичности позволяют проверить, что область приходит от сервера, а не от запроса.
        self.db=MagicMock();self.conn=self.db.connect.return_value.__enter__.return_value
        self.membership=MagicMock(return_value=SimpleNamespace(workspace_id=11,role_code='owner'))
        app=FastAPI();mount_voucher_report_routes(app,database_url=lambda:'test',psycopg=self.db,user_with_workspace=self.membership)
        self.client=TestClient(app)
        self.path='/internal/reports/voucher-sales?date_from=2026-09-01&date_to=2026-09-30'
        self.headers={'X-Voucher-Report-Token':'a'*32}
        self.env=patch.dict(os.environ,{'SELLER_CRM_REPORT_TOKEN':'a'*32,'SELLER_CRM_REPORT_USER_ID':'7'})
        self.env.start();self.addCleanup(self.env.stop)

    def test_auth_and_revoked_membership(self):
        # Ни отсутствие секрета, ни отозванное членство не разрешают чтение продаж.
        self.assertEqual(self.client.get(self.path).status_code,403)
        self.db.connect.assert_not_called()
        self.membership.return_value=None
        self.assertEqual(self.client.get(self.path,headers=self.headers).status_code,403)

    def test_two_workspaces_and_forged_query(self):
        # Произвольный workspace в URL не меняет проверенное членство машинного пользователя.
        with patch('domains.voucher_report_api.load_sales',return_value=[]) as load:
            for workspace in (11,22):
                self.membership.return_value=SimpleNamespace(workspace_id=workspace,role_code='owner')
                response=self.client.get(self.path+'&workspace_id=999',headers=self.headers)
                self.assertEqual(response.status_code,200)
                self.assertEqual(load.call_args.args[1],workspace)
                self.assertEqual(response.headers['cache-control'],'no-store')
        self.membership.assert_called_with(self.conn,7)
        self.assertTrue(self.conn.read_only)

    def test_query_is_scoped_and_duplicate_keys_fail_closed(self):
        # SQL использует проверенную организацию, а не выводит повторную или неполную выдачу.
        cur=self.conn.cursor.return_value.__enter__.return_value
        cur.fetchall.return_value=[{'key_id':None,'fulfillment_id':1,'requested_quantity':1}]
        with self.assertRaises(Exception):load_sales(self.conn,22,'start','end')
        query,params=cur.execute.call_args.args
        self.assertIn('c.workspace_id=%s',query)
        self.assertIn('p.connection_id=f.connection_id',query)
        self.assertEqual(params[:3],(22,'start','end'))

    def test_dates_include_last_day_in_moscow(self):
        # Правая граница исключает полночь следующего дня и остаётся в московской зоне.
        start,end=date_window(date(2026,9,1),date(2026,9,30))
        self.assertEqual(start.isoformat(),'2026-09-01T00:00:00+03:00')
        self.assertEqual(end.isoformat(),'2026-10-01T00:00:00+03:00')
        self.assertEqual(self.client.get(self.path.replace('2026-09-30','2026-08-01'),headers=self.headers).status_code,422)

    def test_incomplete_quantity_and_batched_key_scope(self):
        # Проверяем полноту комплекта и невозможность подставить ключ другого магазина.
        cur=self.conn.cursor.return_value.__enter__.return_value
        row={'key_id':5,'fulfillment_id':1,'requested_quantity':2,'connection_id':7,'delivery_source':'manual'}
        cur.fetchall.return_value=[row]
        with self.assertRaises(Exception):load_sales(self.conn,22,'start','end')
        row['requested_quantity']=1
        cur.fetchall.side_effect=[[row],[{'id':5,'connection_id':999,'code':'ABCD1234567890'}]]
        with patch('domains.yandex_market_outbound.key_pool_secret',return_value='test-secret'):
            with self.assertRaises(Exception):load_sales(self.conn,22,'start','end')
        sql,params=cur.execute.call_args.args
        self.assertIn('c.workspace_id=%s',sql)
        self.assertEqual(params[1:],(22,[5]))

    def test_fingerprints_do_not_return_codes(self):
        # В ответе остаются только SHA256 полного кода и частей комплекта.
        result=fingerprints('ABCD1234567890\nEFGH1234567890')
        self.assertEqual(len(result['tokens']),2)
        self.assertNotIn('ABCD',str(result))
        self.assertEqual(fingerprints('ABCD-1234567890')['normalized'],fingerprints('abcd1234567890')['normalized'])


if __name__=='__main__':unittest.main()
