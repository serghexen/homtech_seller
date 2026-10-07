"""Одноразовое право на Steam TOP_UP; HTTP никогда не вызывает поставщика."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import dict_row
from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, Field

from domains.supplier_hub_client import SupplierHubClient, load_supplier_hub_settings

ACTIVE = {'queued', 'created', 'checked', 'payment_started', 'processing'}
POLLABLE = ACTIVE | {'requires_attention'}
LOCK_NAMESPACE = 20_261_006


def enabled():
    return os.getenv('SELLER_STEAM_TOPUPS_ENABLED', '').lower() in {'true', '1', 'yes'}


def link_token(link_id):
    secret = os.getenv('SELLER_STEAM_LINK_SECRET', '')
    if len(secret) < 32:
        raise HTTPException(503, 'Ссылки пополнения ещё не настроены')
    signature = hmac.new(secret.encode(), f'steam-link-v1:{link_id}'.encode(), hashlib.sha256).hexdigest()
    return f'{link_id}.{signature}'


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_state(row):
    expired = row['expires_at'] <= datetime.now(timezone.utc)
    state = 'expired' if expired and row['state'] == 'ready' else row['state']
    return {key: row[key] for key in ('id', 'amount', 'currency', 'account', 'public_message')} | {
        'state': state, 'can_submit': state == 'ready', 'expires_at': row['expires_at'],
    }


class SteamTopups:
    def __init__(self, database_url, client_factory=None):
        self.database_url = database_url
        self.client_factory = client_factory or (lambda: SupplierHubClient(load_supplier_hub_settings()))

    def connect(self):
        return psycopg.connect(self.database_url(), row_factory=dict_row)

    @staticmethod
    def event(c, row, state, attempt_id=None):
        c.execute('''INSERT INTO seller.steam_topup_events(workspace_id,link_id,attempt_id,state)
            VALUES (%s,%s,%s,%s)''', (row['workspace_id'], row['id'], attempt_id, state))

    def listing(self, workspace_id):
        with self.connect() as c:
            settings = c.execute('SELECT * FROM seller.steam_topup_settings WHERE workspace_id=%s',
                                 (workspace_id,)).fetchone()
            rows = c.execute('''SELECT id,amount,currency,state,account,public_message,created_at,expires_at
                FROM seller.steam_topup_links WHERE workspace_id=%s ORDER BY created_at DESC LIMIT 50''',
                (workspace_id,)).fetchall()
            queue = c.execute('''SELECT count(*) FILTER (WHERE state=ANY(%s)) AS depth,
                max(EXTRACT(epoch FROM now()-created_at)) FILTER (WHERE state=ANY(%s)) AS oldest_age_seconds,
                max(last_success_at) AS last_success_at, min(next_attempt_at) FILTER (WHERE state=ANY(%s)) AS next_run_at,
                count(*) FILTER (WHERE state IN ('requires_attention','dead')) AS attention_count,
                COALESCE(sum(retry_count),0) AS retries, max(last_duration_ms) AS max_duration_ms
                FROM seller.steam_topup_attempts WHERE workspace_id=%s''',
                (list(POLLABLE), list(POLLABLE), list(POLLABLE), workspace_id)).fetchone()
        return {'enabled': bool(enabled() and settings and settings['enabled']),
                'max_amount': settings['max_amount'] if settings else '100.00', 'items': rows, 'queue': queue}

    def create(self, workspace_id, user_id, amount, creation_key):
        if not enabled():
            raise HTTPException(503, 'Пилот пополнений пока выключен')
        with self.connect() as c:
            # Лимит пилота резервируется при выпуске ссылки, включая ещё не активированные.
            settings = c.execute('SELECT * FROM seller.steam_topup_settings WHERE workspace_id=%s FOR UPDATE',
                                 (workspace_id,)).fetchone()
            if not settings or not settings['enabled']:
                raise HTTPException(403, 'Пилот не включён для вашей рабочей области')
            row = c.execute('SELECT * FROM seller.steam_topup_links WHERE workspace_id=%s AND creation_key=%s',
                            (workspace_id, creation_key)).fetchone()
            if row:
                if row['amount'] != amount:
                    raise HTTPException(409, 'Ссылка уже создана с другой суммой')
            else:
                used = c.execute("SELECT COALESCE(sum(amount),0) AS total FROM seller.steam_topup_links WHERE workspace_id=%s AND state<>'cancelled'",
                                 (workspace_id,)).fetchone()['total']
                if amount > settings['max_amount'] or used + amount > settings['budget_amount']:
                    raise HTTPException(422, 'Превышен лимит суммы или общий бюджет пилота')
                link_id = uuid4()
                token = link_token(link_id)
                row = c.execute('''INSERT INTO seller.steam_topup_links
                    (id,workspace_id,created_by_user_id,creation_key,token_hash,amount)
                    VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
                    (link_id, workspace_id, user_id, creation_key, token_hash(token), amount)).fetchone()
                self.event(c, row, 'created')
        return public_state(row) | {'token': link_token(row['id'])}

    def cancel(self, workspace_id, link_id):
        with self.connect() as c:
            row = c.execute('SELECT * FROM seller.steam_topup_links WHERE workspace_id=%s AND id=%s FOR UPDATE',
                            (workspace_id, link_id)).fetchone()
            if not row:
                raise HTTPException(404, 'Ссылка не найдена')
            if row['state'] != 'ready':
                raise HTTPException(409, 'Начатую операцию нельзя отменить этой кнопкой')
            c.execute("UPDATE seller.steam_topup_links SET state='cancelled',updated_at=now() WHERE id=%s", (link_id,))
            self.event(c, row, 'cancelled')
        return {'state': 'cancelled'}

    def read_public(self, token):
        with self.connect() as c:
            row = c.execute('SELECT * FROM seller.steam_topup_links WHERE token_hash=%s', (token_hash(token),)).fetchone()
            if not row:
                raise HTTPException(404, 'Ссылка не найдена. Откройте ссылку из вашего заказа')
            settings = c.execute('SELECT enabled FROM seller.steam_topup_settings WHERE workspace_id=%s',
                                 (row['workspace_id'],)).fetchone()
        result = public_state(row)
        result['can_submit'] = bool(result['can_submit'] and enabled() and settings and settings['enabled'])
        return result

    def submit(self, token, account, request_key):
        account = account.strip()
        if not re.fullmatch(r'[A-Za-z0-9_]{2,100}', account):
            raise HTTPException(422, 'Введите логин Steam: латинские буквы, цифры или подчёркивание')
        if not enabled():
            raise HTTPException(503, 'Пополнения временно приостановлены')
        with self.connect() as c:
            # Две вкладки блокируют одну запись: вторая получает прежнюю операцию.
            row = c.execute('SELECT * FROM seller.steam_topup_links WHERE token_hash=%s FOR UPDATE',
                            (token_hash(token),)).fetchone()
            if not row:
                raise HTTPException(404, 'Ссылка не найдена')
            previous = c.execute('SELECT id FROM seller.steam_topup_attempts WHERE link_id=%s AND request_key=%s',
                                 (row['id'], request_key)).fetchone()
            if previous or row['state'] != 'ready':
                return public_state(row)
            if row['expires_at'] <= datetime.now(timezone.utc):
                raise HTTPException(410, 'Срок действия ссылки истёк')
            settings = c.execute('SELECT * FROM seller.steam_topup_settings WHERE workspace_id=%s',
                                 (row['workspace_id'],)).fetchone()
            if not settings or not settings['enabled']:
                raise HTTPException(503, 'Пополнения временно приостановлены')
            if row['attempt_count'] >= 10:
                raise HTTPException(429, 'Исчерпан лимит проверок. Обратитесь в поддержку')
            if row['attempt_count'] and (datetime.now(timezone.utc)-row['updated_at']).total_seconds() < 5:
                raise HTTPException(429, 'Подождите несколько секунд перед повторной проверкой')
            attempt = uuid4()
            c.execute('''INSERT INTO seller.steam_topup_attempts(id,workspace_id,link_id,request_key,account)
                VALUES (%s,%s,%s,%s,%s)''', (attempt, row['workspace_id'], row['id'], request_key, account))
            row = c.execute('''UPDATE seller.steam_topup_links SET state='queued',account=%s,current_attempt=%s,
                attempt_count=attempt_count+1,public_message='',updated_at=now() WHERE id=%s RETURNING *''',
                (account, attempt, row['id'])).fetchone()
            self.event(c, row, 'queued', attempt)
        return public_state(row)

    def process_once(self):
        # Отдельный общий worker пилота; session lock сериализует ручные операции workspace.
        with self.connect() as lock:
            candidates = lock.execute('''SELECT s.workspace_id FROM seller.steam_topup_settings s
                WHERE EXISTS (SELECT 1 FROM seller.steam_topup_attempts a WHERE a.workspace_id=s.workspace_id
                AND a.state=ANY(%s) AND a.next_attempt_at<=now() AND (a.lease_until IS NULL OR a.lease_until<now()))
                ORDER BY s.last_worker_at NULLS FIRST,s.workspace_id LIMIT 100''',
                (list(POLLABLE),)).fetchall()
            lock.commit()
            for candidate in candidates:
                workspace_id = candidate['workspace_id']
                locked = lock.execute('SELECT pg_try_advisory_lock(%s,%s) AS ok',
                                      (LOCK_NAMESPACE, workspace_id % 2147483647)).fetchone()['ok']
                lock.commit()
                if not locked:
                    continue
                try:
                    return self._process_workspace(workspace_id)
                finally:
                    lock.execute('SELECT pg_advisory_unlock(%s,%s)', (LOCK_NAMESPACE, workspace_id % 2147483647))
                    lock.commit()
        return False

    def _process_workspace(self, workspace_id):
        lease = uuid4()
        with self.connect() as c:
            row = c.execute('''SELECT a.*,l.amount FROM seller.steam_topup_attempts a
                JOIN seller.steam_topup_links l ON l.id=a.link_id AND l.workspace_id=a.workspace_id AND l.current_attempt=a.id
                WHERE a.workspace_id=%s AND a.state=ANY(%s) AND a.next_attempt_at<=now()
                AND (a.lease_until IS NULL OR a.lease_until<now())
                ORDER BY a.next_attempt_at,a.created_at FOR UPDATE OF a SKIP LOCKED LIMIT 1''',
                (workspace_id, list(POLLABLE))).fetchone()
            if not row:
                return False
            c.execute('''UPDATE seller.steam_topup_attempts SET lease_token=%s,lease_until=now()+interval '1 minute',
                next_attempt_at=now()+interval '15 seconds' WHERE id=%s''', (lease, row['id']))
            c.execute('UPDATE seller.steam_topup_settings SET last_worker_at=now() WHERE workspace_id=%s', (workspace_id,))
        started = time.monotonic()
        try:
            client = self.client_factory()
            if row['hub_purchase_id']:
                result = client.purchase(str(row['hub_purchase_id']))
            else:
                with self.connect() as c:
                    permission = c.execute('SELECT enabled FROM seller.steam_topup_settings WHERE workspace_id=%s',
                                           (workspace_id,)).fetchone()
                if not enabled() or not permission or not permission['enabled']:
                    raise RuntimeError('paused')
                result = client._request('/v1/purchases', authenticated=True, method='POST', payload={
                    'idempotency_key': f'steam:{workspace_id}:{row["link_id"]}:{row["id"]}',
                    'kind': 'steam_topup', 'provider_code': 'interhub', 'service_id': 9361,
                    'workspace_id': workspace_id, 'connection_id': None,
                    'requested_amount': str(row['amount']), 'account': row['account'], 'params': {},
                }, request_id=f'steam-{row["id"]}')
            self._save_result(row, lease, result)
        except Exception:
            # Неопределённый ответ Hub повторяем только с прежним idempotency_key и реквизитами.
            with self.connect() as c:
                c.execute('''UPDATE seller.steam_topup_attempts SET lease_token=NULL,lease_until=NULL,
                    retry_count=retry_count+1,last_error='Hub недоступен или пополнения приостановлены',
                    state=CASE WHEN retry_count>=19 THEN 'dead' ELSE state END,
                    next_attempt_at=now()+make_interval(secs => LEAST(300,15*power(2,LEAST(retry_count,5)))::integer),
                    updated_at=now() WHERE id=%s AND lease_token=%s''', (row['id'], lease))
                if row['retry_count'] >= 19:
                    c.execute("""UPDATE seller.steam_topup_links SET state='attention',
                        public_message='Не удалось сверить операцию. Обратитесь в поддержку.',updated_at=now()
                        WHERE id=%s AND workspace_id=%s AND current_attempt=%s""",
                        (row['link_id'], workspace_id, row['id']))
        finally:
            with self.connect() as c:
                c.execute('UPDATE seller.steam_topup_attempts SET last_duration_ms=%s WHERE id=%s AND workspace_id=%s',
                          (int((time.monotonic()-started)*1000), row['id'], workspace_id))
        return True

    def _save_result(self, attempt, lease, result):
        state = result.get('state')
        if (result.get('kind') != 'steam_topup' or state not in ACTIVE | {'succeeded','failed','requires_attention'}
            or Decimal(str(result.get('requested_amount'))) != attempt['amount']):
            raise ValueError('Unexpected Hub response')
        hub_id = UUID(result['id'])
        # Отсутствие явного false не доказывает, что pay ещё не начинался.
        payment_started = attempt['payment_started'] or result.get('payment_started') is not False
        link_state, message = 'processing', ''
        if state == 'succeeded':
            link_state = 'succeeded'
        elif state == 'failed' and not payment_started:
            link_state, message = 'ready', 'Поставщик не подтвердил пополнение. Проверьте логин или обратитесь в поддержку.'
        elif state == 'failed':
            link_state, message = 'failed', 'Поставщик сообщил об отказе. Обратитесь в поддержку.'
        elif state == 'requires_attention':
            link_state, message = 'attention', 'Уточняем результат у поставщика. Повторное пополнение заблокировано.'
        with self.connect() as c:
            saved = c.execute('''UPDATE seller.steam_topup_attempts SET state=%s,hub_purchase_id=%s,
                payment_started=payment_started OR %s,lease_token=NULL,lease_until=NULL,last_error='',last_success_at=now(),
                next_attempt_at=now()+interval '15 seconds',updated_at=now()
                WHERE id=%s AND workspace_id=%s AND lease_token=%s RETURNING id''',
                (state, hub_id, payment_started, attempt['id'], attempt['workspace_id'], lease)).fetchone()
            if not saved:
                return
            row = c.execute('''UPDATE seller.steam_topup_links SET state=%s,public_message=%s,updated_at=now()
                WHERE id=%s AND workspace_id=%s AND current_attempt=%s RETURNING *''',
                (link_state, message, attempt['link_id'], attempt['workspace_id'], attempt['id'])).fetchone()
            if row and state != attempt['state']:
                self.event(c, row, state, attempt['id'])

    def reconcile(self, workspace_id, link_id):
        # Возвращаем только прежнюю попытку с прежним ключом. Новое пополнение не создаётся.
        with self.connect() as c:
            row = c.execute('SELECT * FROM seller.steam_topup_links WHERE workspace_id=%s AND id=%s FOR UPDATE',
                            (workspace_id, link_id)).fetchone()
            if not row or row['state'] != 'attention':
                raise HTTPException(409, 'Нет операции, ожидающей сверки')
            c.execute("""UPDATE seller.steam_topup_attempts SET state=CASE WHEN hub_purchase_id IS NULL THEN 'queued' ELSE 'processing' END,
                retry_count=0,next_attempt_at=now(),lease_token=NULL,lease_until=NULL
                WHERE id=%s AND workspace_id=%s AND state='dead'""", (row['current_attempt'], workspace_id))
            self.event(c, row, 'reconcile_requested', row['current_attempt'])
        return {'state': 'attention'}


class CreateLinkIn(BaseModel):
    class Config:
        extra = 'forbid'
    amount: Decimal = Field(ge=Decimal('16.99'), le=Decimal('10000'), max_digits=12, decimal_places=2)
    request_key: UUID


class PublicLinkIn(BaseModel):
    class Config:
        extra = 'forbid'
    token: str = Field(min_length=101, max_length=101, pattern=r'^[0-9a-f-]{36}\.[0-9a-f]{64}$')


class SubmitLinkIn(PublicLinkIn):
    account: str = Field(min_length=2, max_length=100)
    request_key: UUID


def mount_steam_topup_routes(app, *, database_url, current_user, user_with_workspace):
    service = SteamTopups(database_url)

    def owner(user=Depends(current_user)):
        with psycopg.connect(database_url()) as c:
            context = user_with_workspace(c, user.user_id)
        if not context or context.role_code != 'owner':
            raise HTTPException(403, 'Только владелец рабочей области может создавать ссылки')
        return context

    @app.get('/steam-topups')
    def listing(context=Depends(owner)):
        return service.listing(context.workspace_id)

    @app.post('/steam-topups')
    def create(payload: CreateLinkIn, response: Response, context=Depends(owner)):
        response.headers['Cache-Control'] = 'no-store'
        return service.create(context.workspace_id, context.id, payload.amount, payload.request_key)

    @app.post('/steam-topups/{link_id}/cancel')
    def cancel(link_id: UUID, context=Depends(owner)):
        return service.cancel(context.workspace_id, link_id)

    @app.post('/steam-topups/{link_id}/reconcile')
    def reconcile(link_id: UUID, context=Depends(owner)):
        return service.reconcile(context.workspace_id, link_id)

    @app.post('/steam-topups/public/status')
    def status(payload: PublicLinkIn, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.read_public(payload.token)

    @app.post('/steam-topups/public/submit')
    def submit(payload: SubmitLinkIn, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        return service.submit(payload.token, payload.account, payload.request_key)
