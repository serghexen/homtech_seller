"""Узкий read-only контракт продаж для отчёта CRM. Коды не покидают Seller."""
from datetime import date, datetime, time, timedelta, timezone
from collections import Counter
import hashlib
from hmac import compare_digest
import os
import re
from zoneinfo import ZoneInfo

from fastapi import Header, HTTPException, Query, Response
from psycopg.rows import dict_row

MSK = ZoneInfo('Europe/Moscow')
MAX_ROWS = 20000


def date_window(date_from, date_to):
    # Включаем последний календарный день целиком; сегодняшний ограничиваем моментом запроса.
    now = datetime.now(timezone.utc)
    if date_from > date_to or (date_to - date_from).days >= 366 or date_to > now.astimezone(MSK).date():
        raise HTTPException(422, 'Укажите период до сегодняшнего дня, не более 366 дней')
    return (datetime.combine(date_from, time.min, MSK),
            min(datetime.combine(date_to + timedelta(days=1), time.min, MSK), now))


def fingerprints(code):
    # Полное значение и части комплекта сравниваются в CRM только по отпечаткам.
    digest = lambda value: hashlib.sha256(value.encode()).hexdigest()
    normalize = lambda value: re.sub(r'[\s-]', '', value).upper()
    tokens = set(re.findall(r'[A-Za-z0-9][A-Za-z0-9-]{11,}', code))
    return {'whole': digest(code.strip()), 'normalized': digest(normalize(code)),
            'tokens': sorted({digest(normalize(token)) for token in tokens})}


SALES_SQL = """
SELECT f.id AS fulfillment_id, f.connection_id, c.display_name AS shop,
       f.external_order_id AS order_id, f.external_item_id AS item_id,
       f.requested_quantity, f.delivery_source, f.delivered_at, i.created_at AS order_created_at,
       i.title, k.id AS key_id, k.created_at AS key_created_at,
       a.amount, a.id AS attempt_id, a.hub_purchase_id,
       COALESCE(m.service_id, fallback.service_id) AS service_id,
       COALESCE(m.nominal_id, fallback.nominal_id) AS nominal_id,
       COALESCE(m.provider_code, fallback.provider_code) AS provider_code
FROM seller.order_fulfillments f
JOIN seller.marketplace_connections c ON c.id=f.connection_id AND c.workspace_id=%s
JOIN seller.order_items i ON (i.connection_id,i.external_order_id,i.external_item_id)=
                            (f.connection_id,f.external_order_id,f.external_item_id)
LEFT JOIN seller.fulfillment_key_reservations r ON r.fulfillment_id=f.id AND r.state='consumed'
LEFT JOIN seller.marketplace_key_pools p ON p.connection_id=f.connection_id AND p.external_product_id=f.offer_id
LEFT JOIN seller.marketplace_keys k ON k.id=r.key_id AND k.pool_id=p.id
LEFT JOIN seller.supplier_purchase_attempts a ON a.fulfillment_id=f.id
    AND a.result_key_id=k.id AND a.state='succeeded'
LEFT JOIN seller.product_supplier_mappings m ON m.id=a.supplier_mapping_id AND m.connection_id=f.connection_id
LEFT JOIN LATERAL (
    SELECT pm.service_id,pm.nominal_id,pm.provider_code FROM seller.product_supplier_mappings pm
    WHERE pm.connection_id=f.connection_id AND pm.external_product_id=f.offer_id
    ORDER BY pm.enabled DESC,pm.priority,pm.id LIMIT 1
) fallback ON m.id IS NULL
WHERE f.status='delivered' AND f.delivery_source IN ('supplier','manual','pool')
  AND f.delivered_at >= %s AND f.delivered_at < %s
ORDER BY f.connection_id,f.delivered_at,f.id,k.id LIMIT %s
"""


def load_sales(conn, workspace_id, start, end):
    # Все соединения и расшифровка ограничены одной проверенной организацией и магазином.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(SALES_SQL, (workspace_id, start, end, MAX_ROWS + 1))
        rows = cur.fetchall()
        if len(rows) > MAX_ROWS:
            raise HTTPException(422, 'Слишком много продаж. Уменьшите период отчёта')
        # Нельзя скачать неполную выдачу, если часть ключей заказа не попала в выборку.
        counts = Counter(row['fulfillment_id'] for row in rows)
        if any(counts[row['fulfillment_id']] != row['requested_quantity'] for row in rows):
            raise HTTPException(409, 'Не все ключи выдачи доступны для отчёта')
        seen = set()
        for row in rows:
            if row['key_id'] is None or row['key_id'] in seen:
                raise HTTPException(409, 'Не удалось однозначно сопоставить выдачи. Обратитесь к администратору')
            seen.add(row['key_id'])
        manual = [row for row in rows if row['delivery_source'] in ('manual', 'pool')]
        if manual:
            from domains.yandex_market_outbound import key_pool_secret
            # Одна расшифровка пачки вместо отдельного SQL на каждый проданный ключ.
            cur.execute('''SELECT k.id, p.connection_id, pgp_sym_decrypt(k.code_ciphertext,%s) AS code
                FROM seller.marketplace_keys k
                JOIN seller.marketplace_key_pools p ON p.id=k.pool_id
                JOIN seller.marketplace_connections c ON c.id=p.connection_id
                WHERE c.workspace_id=%s AND k.id=ANY(%s)''',
                (key_pool_secret(), workspace_id, [row['key_id'] for row in manual]))
            materials = {(item['connection_id'], item['id']): fingerprints(item['code']) for item in cur.fetchall()}
            for row in manual:
                material = materials.get((row['connection_id'], row['key_id']))
                if not material:
                    raise HTTPException(409, 'Материал выдачи недоступен')
                row['fingerprints'] = material
        return rows


def mount_voucher_report_routes(app, *, database_url, psycopg, user_with_workspace):
    # Машинный токен привязан на сервере к пользователю; workspace нельзя передать из CRM.
    @app.get('/internal/reports/voucher-sales', include_in_schema=False)
    def report(response: Response, date_from: date = Query(), date_to: date = Query(),
               x_voucher_report_token: str = Header(default='')):
        expected = os.getenv('SELLER_CRM_REPORT_TOKEN', '')
        if len(expected) < 32 or not compare_digest(expected.encode(), x_voucher_report_token.encode()):
            raise HTTPException(403, 'Report access denied')
        try:
            user_id = int(os.getenv('SELLER_CRM_REPORT_USER_ID', '0'))
        except ValueError:
            user_id = 0
        if user_id <= 0:
            raise HTTPException(503, 'Report access is not configured')
        start, end = date_window(date_from, date_to)
        with psycopg.connect(database_url(), connect_timeout=5) as conn:
            conn.read_only = True
            conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout='45s'")
            user = user_with_workspace(conn, user_id)
            if not user or user.role_code != 'owner':
                raise HTTPException(403, 'Report owner membership is unavailable')
            rows = load_sales(conn, user.workspace_id, start, end)
        response.headers['Cache-Control'] = 'no-store'
        return {'version': 1, 'date_from': date_from, 'date_to': date_to,
                'cutoff': end, 'total': len(rows), 'rows': rows}
