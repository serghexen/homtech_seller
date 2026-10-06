"""Контроль наличия из готового снимка CRM через Supplier Hub, без покупки и опроса поставщика."""
from datetime import datetime, timedelta, timezone
import json
import os

from domains.connection_entitlements import connection_allows, FULFILLMENT_SUPPLIER
from domains.supplier_hub_client import SupplierHubClient, load_supplier_hub_settings


def enabled():
    # Глобальный выключатель дополняет отдельное разрешение каждого магазина.
    return os.getenv('SELLER_SUPPLIER_STOCK_CONTROL_ENABLED', '').lower() in {'true','1','yes','on'}


def parse_time(value):
    # Не принимаем дату без часового пояса за подтверждение свежего остатка.
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace('Z','+00:00'))
        return result if result.tzinfo else None
    except ValueError:
        return None


def validate_snapshot(payload):
    # Неполный контракт или повторные ID отклоняют весь снимок, сохраняя рабочие продажи.
    if not isinstance(payload, dict) or payload.get('version') != 1 or not isinstance(payload.get('items'), list):
        raise ValueError('Invalid stock snapshot')
    result = {}
    for item in payload['items']:
        if not isinstance(item, dict):
            raise ValueError('Invalid stock item')
        key = (str(item.get('service_id','')), str(item.get('nominal_id','')))
        if not all(key) or key in result or item.get('error_scope') not in {'','item','common'}:
            raise ValueError('Invalid stock identity or error scope')
        count = item.get('stock_count')
        if count is not None and (type(count) is not int or count < 0):
            raise ValueError('Invalid stock count')
        if item.get('status') not in {'active','suspect','unavailable'}:
            raise ValueError('Invalid stock status')
        result[key] = item
    return result


def observation(item, *, catalog_error=False, now=None):
    # Давность и общий сбой дают только предупреждение. Цена на решение не влияет.
    now = now or datetime.now(timezone.utc)
    if catalog_error:
        return 'common_error', None
    checked = parse_time(item.get('checked_at')) if item else None
    if not checked:
        return 'unknown', None
    if checked > now + timedelta(minutes=5) or checked < now - timedelta(minutes=90):
        return 'stale', None
    if item['status'] != 'active':
        return 'unavailable', True
    if item['error_scope'] == 'common':
        return 'common_error', None
    if item['error_scope'] == 'item':
        return 'item_error', True
    if item['stock_count'] is None:
        return 'unknown', None
    return ('zero', True) if item['stock_count'] == 0 else ('available', False)


def effective_block(previous, mapping_key, decision):
    # Ошибка связи не снимает ранее подтверждённый ноль; другая связка не наследует блокировку.
    if decision is not None:
        return decision
    return bool(previous and previous['mapping_key'] == mapping_key and previous['blocked'])


def active_mapping(cursor, workspace, connection_id, product_id):
    # Проверяем актуальные разрешения и выбираем ту же первую связку, что и исполнитель выдачи.
    if not enabled() or not load_supplier_hub_settings().fulfillment_enabled:
        return None
    cursor.execute('''SELECT mapping.id,mapping.service_id,mapping.nominal_id
        FROM seller.marketplace_connections c
        JOIN seller.product_fulfillment_policies p ON p.connection_id=c.id AND p.external_product_id=%s
        JOIN seller.product_supplier_mappings mapping ON mapping.connection_id=c.id
          AND mapping.external_product_id=p.external_product_id AND mapping.enabled AND mapping.provider_code='interhub'
        WHERE c.id=%s AND c.workspace_id=%s AND c.provider_code='yandex_market'
          AND c.supplier_stock_control_enabled AND c.supplier_fulfillment_enabled
          AND c.status='active' AND c.launch_state='running' AND p.supplier_issue_enabled
          AND mapping.max_amount>0
        ORDER BY mapping.priority,mapping.id LIMIT 1''', (product_id,connection_id,workspace))
    row = cursor.fetchone()
    if not row or not row[1] or not row[2] or not connection_allows(cursor,workspace,connection_id,FULFILLMENT_SUPPLIER):
        return None
    return ':'.join(str(part) for part in row), str(row[1]), str(row[2])


def is_blocked(cursor, workspace, connection_id, product_id):
    # Проверка непосредственно перед любым PUT не позволяет выдаче или полуночи вернуть запрещённый остаток.
    mapping = active_mapping(cursor, workspace, connection_id, product_id)
    if not mapping:
        return False
    cursor.execute('''SELECT blocked FROM seller.product_supplier_stock_state
        WHERE workspace_id=%s AND connection_id=%s AND external_product_id=%s AND mapping_key=%s''',
        (workspace,connection_id,product_id,mapping[0]))
    row = cursor.fetchone()
    return bool(row and row[0])


def notify(cursor, workspace, connection_id, product_id, key, payload):
    # Бот получает только события текущей включённой автовыдачи; повтор одного состояния не размножается.
    if not active_mapping(cursor,workspace,connection_id,product_id):
        return
    cursor.execute('''UPDATE seller.product_supplier_stock_state s SET notice_key=%s
        FROM seller.marketplace_connections c
        WHERE s.workspace_id=%s AND s.connection_id=%s AND s.external_product_id=%s
          AND c.id=s.connection_id AND c.workspace_id=s.workspace_id
          AND c.supplier_stock_notifications_enabled
          AND (c.supplier_stock_notifications_until IS NULL OR c.supplier_stock_notifications_until>now())
          AND s.notice_key<>%s RETURNING s.observation,s.checked_at,c.display_name''',
        (key,workspace,connection_id,product_id,key))
    row = cursor.fetchone()
    if not row:
        return
    payload = {**payload, 'connection_id':connection_id,'offer_id':product_id,
               'store_name':row[2], 'observation':row[0], 'checked_at':str(row[1] or '')}
    cursor.execute('''INSERT INTO seller.telegram_notification_events
        (workspace_id,event_type,event_key,payload) VALUES (%s,'supplier_stock',%s,%s::jsonb)''',
        (workspace,key,json.dumps(payload,ensure_ascii=False)))


def publication_notice(cursor, connection_id, product_id, *, target=None, error=False):
    # «Отправлено» появляется только после успешного PUT; повтор одной ошибки уведомление не дублирует.
    if not enabled():
        return
    cursor.execute('''SELECT s.workspace_id,s.revision,s.blocked,s.notice_key FROM seller.product_supplier_stock_state s
        JOIN seller.marketplace_connections c ON c.id=s.connection_id AND c.workspace_id=s.workspace_id
        WHERE s.connection_id=%s AND s.external_product_id=%s''', (connection_id,product_id))
    row = cursor.fetchone()
    if row:
        # Исходное наличие не является восстановлением; сообщаем только о блокировке или её снятии.
        recovery = ':recovery:' in str(row[3] or '')
        if not row[2] and not recovery:
            return
        action = 'error' if error else 'sent'
        key = f'{row[1]}:recovery:error' if recovery and error else f'{row[1]}:restored' if recovery else f'{row[1]}:{action}'
        notify(cursor,int(row[0]),connection_id,product_id,key,
               {'action':action,'target_stock':target,'transition':'restored' if recovery else 'blocked'})


class SupplierStockController:
    def __init__(self, *, database_url, psycopg, client_factory=None):
        # Один stateless обработчик обслуживает очередь магазинов, не создавая таймеров на каждый магазин.
        self.database_url, self.db = database_url, psycopg
        self.client_factory = client_factory or (lambda: SupplierHubClient(load_supplier_hub_settings()))

    def process_once(self):
        # Общий session lock сериализует обновление решения с заказами и отправкой остатков этого магазина.
        if not enabled():
            return False
        with self.db.connect(self.database_url()) as conn:
            with conn.cursor() as cur:
                cur.execute('''SELECT id,workspace_id FROM seller.marketplace_connections
                    WHERE provider_code='yandex_market' AND supplier_stock_control_enabled
                      AND status='active' AND launch_state='running' AND supplier_stock_next_at<=now()
                    ORDER BY supplier_stock_next_at,id FOR UPDATE SKIP LOCKED LIMIT 1''')
                shop = cur.fetchone()
                if not shop:
                    return False
                cid, workspace = map(int,shop)
                cur.execute('SELECT pg_try_advisory_lock(20260824,%s)',(cid % 2147483647,))
                if not cur.fetchone()[0]:
                    cur.execute("UPDATE seller.marketplace_connections SET supplier_stock_next_at=now()+interval '5 seconds' WHERE id=%s AND workspace_id=%s",(cid,workspace))
                    conn.commit()
                    return False
                # Резервируем следующий проход до сети: рестарт не создаст шквал повторов.
                cur.execute("UPDATE seller.marketplace_connections SET supplier_stock_next_at=now()+interval '60 seconds'+(%s %% 15)*interval '1 second' WHERE id=%s AND workspace_id=%s",(cid,cid,workspace))
                conn.commit()
                try:
                    payload = self.client_factory().stock_snapshot()
                    snapshots = validate_snapshot(payload)
                    source_error = bool(payload.get('catalog_error'))
                    transport_error = False
                except Exception:
                    snapshots,source_error,transport_error = {},True,True
                # Перечитываем настройки после сети; новые/выключенные карточки обрабатываются без ручного списка.
                cur.execute('''SELECT item.external_product_id FROM seller.catalog_items item
                    JOIN seller.marketplace_connections c ON c.id=item.connection_id
                    WHERE c.id=%s AND c.workspace_id=%s AND c.supplier_stock_control_enabled
                      AND item.is_present AND NOT item.is_archived
                      AND (EXISTS(SELECT 1 FROM seller.product_fulfillment_policies p
                        WHERE p.connection_id=c.id AND p.external_product_id=item.external_product_id AND p.supplier_issue_enabled)
                        OR EXISTS(SELECT 1 FROM seller.product_supplier_stock_state s WHERE s.workspace_id=c.workspace_id
                          AND s.connection_id=c.id AND s.external_product_id=item.external_product_id))
                      ORDER BY item.external_product_id''',(cid,workspace))
                products = [str(r[0]) for r in cur.fetchall()]
                warnings, active_count = [], 0
                for product in products:
                    state = self.apply(cur,workspace,cid,product,snapshots,source_error)
                    if state is not None:
                        active_count += 1
                    if state in {'common_error','unknown','stale'}:
                        warnings.append(state)
                self.common_notice(cur,workspace,cid,warnings,active_count)
                cur.execute('''UPDATE seller.marketplace_connections SET
                    supplier_stock_last_success_at=CASE WHEN %s THEN supplier_stock_last_success_at ELSE now() END,
                    supplier_stock_last_error=%s WHERE id=%s AND workspace_id=%s''',
                    (transport_error or source_error,'Не получен актуальный снимок остатков' if transport_error or source_error else '',cid,workspace))
                conn.commit()
        return True

    def apply(self, cur, workspace, cid, product, snapshots, source_error):
        # Решение и задание фиксируются вместе; сбой после commit безопасно продолжит существующая outbox.
        mapping = active_mapping(cur,workspace,cid,product)
        cur.execute('''SELECT mapping_key,blocked,observation,revision FROM seller.product_supplier_stock_state
            WHERE workspace_id=%s AND connection_id=%s AND external_product_id=%s FOR UPDATE''',(workspace,cid,product))
        row = cur.fetchone()
        previous = dict(zip(('mapping_key','blocked','observation','revision'),row)) if row else None
        if not mapping and not previous:
            return
        key = mapping[0] if mapping else ''
        item = snapshots.get((mapping[1],mapping[2])) if mapping else None
        state,decision = observation(item,catalog_error=source_error) if mapping else ('disabled',False)
        blocked = effective_block(previous,key,decision)
        changed = not previous or (previous['mapping_key'],previous['blocked'],previous['observation']) != (key,blocked,state)
        rev = (previous['revision'] if previous else 0) + int(changed)
        # Общий сбой/давность сами по себе не ставят публикацию; подтверждённый ноль сохраняется.
        publish = decision is not None and (not previous or previous['mapping_key']!=key or previous['blocked']!=blocked)
        cur.execute('''INSERT INTO seller.product_supplier_stock_state AS s
            (workspace_id,connection_id,external_product_id,mapping_key,blocked,observation,checked_at,revision)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(workspace_id,connection_id,external_product_id)
            DO UPDATE SET mapping_key=excluded.mapping_key,blocked=excluded.blocked,observation=excluded.observation,
              checked_at=excluded.checked_at,revision=excluded.revision,updated_at=now()''',
            (workspace,cid,product,key,blocked,state,parse_time(item.get('checked_at')) if item else None,rev))
        queued = False
        if publish:
            # Новое решение заменяет неотправленное старое: восстановление дождётся свежих заказов.
            cur.execute('''UPDATE seller.yandex_stock_outbound_jobs SET state='failed',failed_at=now(),
                last_error='Заменено новым решением по остатку поставщика',updated_at=now()
                WHERE workspace_id=%s AND connection_id=%s AND external_product_id=%s
                  AND job_kind='supplier' AND state='queued' ''',(workspace,cid,product))
            cur.execute('''INSERT INTO seller.yandex_stock_outbound_jobs
                (workspace_id,connection_id,external_product_id,job_kind,business_key,orders_fresh_after)
                SELECT %s,%s,%s,'supplier',%s,CASE WHEN %s THEN NULL ELSE now() END
                FROM seller.marketplace_connections WHERE id=%s AND workspace_id=%s AND stock_outbound_enabled
                ON CONFLICT DO NOTHING''', (workspace,cid,product,f'supplier:{product}:{rev}',blocked,cid,workspace))
            queued = cur.rowcount > 0
            if queued and not blocked:
                cur.execute("INSERT INTO seller.marketplace_sync_jobs(workspace_id,connection_id,sync_kind) VALUES (%s,%s,'orders') ON CONFLICT DO NOTHING",(workspace,cid))
        if publish and mapping:
            # Запоминаем восстановление до PUT, но не отправляем промежуточные сообщения об очереди.
            if not blocked and previous and previous['blocked']:
                notice_key = f'{rev}:recovery:pending'
            elif blocked:
                notice_key = f'{rev}:blocked:pending'
            else:
                notice_key = ''
            cur.execute('''UPDATE seller.product_supplier_stock_state SET notice_key=%s
                WHERE workspace_id=%s AND connection_id=%s AND external_product_id=%s''',
                (notice_key,workspace,cid,product))

        return state if mapping else None

    def common_notice(self, cur, workspace, cid, warnings, active_count):
        # Один общий сбой даёт одно сообщение на магазин, а не сотни одинаковых сообщений по карточкам.
        key = ':'.join(sorted(set(warnings))) if warnings else ''
        cur.execute('''UPDATE seller.marketplace_connections SET supplier_stock_notice_key=%s
            WHERE id=%s AND workspace_id=%s AND supplier_stock_notice_key<>%s
            RETURNING supplier_stock_notifications_enabled,
              supplier_stock_notifications_until IS NULL OR supplier_stock_notifications_until>now(),display_name''',
            (key,cid,workspace,key))
        row = cur.fetchone()
        if row and row[0] and row[1] and active_count:
            payload = {'connection_id':cid,'store_name':row[2], 'offer_id':'',
                       'observation':warnings[0] if warnings else 'available',
                       'action':'common_warning' if warnings else 'common_restored','affected_count':len(warnings)}
            cur.execute('''INSERT INTO seller.telegram_notification_events(workspace_id,event_type,event_key,payload)
                VALUES (%s,'supplier_stock',%s,%s::jsonb)''', (workspace,'common:'+key,json.dumps(payload,ensure_ascii=False)))
