"""Durable, opt-in daily stock renewal for launched Yandex stores."""
from __future__ import annotations

from domains.yandex_market_stock_outbound import yandex_stock_outbound_enabled


def enqueue_due_daily_stock(connection, limit: int = 20) -> int:
    if not yandex_stock_outbound_enabled():
        return 0
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT id, workspace_id FROM seller.marketplace_connections
            WHERE provider_code='yandex_market' AND status='active' AND launch_state='running'
              AND stock_outbound_enabled AND yandex_daily_stock_enabled AND next_daily_stock_at<=now()
            ORDER BY next_daily_stock_at,id FOR UPDATE SKIP LOCKED LIMIT %s
        """, (max(1, min(int(limit), 100)),))
        stores = cursor.fetchall()
        queued = 0
        for connection_id, workspace_id in stores:
            cursor.execute("""
                INSERT INTO seller.yandex_stock_outbound_jobs(
                  workspace_id,connection_id,external_product_id,job_kind,stock_day,business_key,orders_fresh_after,next_attempt_at
                )
                SELECT %s,item.connection_id,item.external_product_id,'daily',
                       (now() AT TIME ZONE 'Europe/Moscow')::date,
                       'daily:' || (now() AT TIME ZONE 'Europe/Moscow')::date || ':' || item.external_product_id, now(),
                       now() + ((hashtextextended(item.external_product_id,0) & 2147483647) %% 60)*interval '1 second'
                FROM seller.catalog_items item
                LEFT JOIN seller.product_card_settings s ON s.connection_id=item.connection_id
                  AND s.external_product_id=item.external_product_id
                LEFT JOIN seller.yandex_product_settings_snapshot imported ON imported.connection_id=item.connection_id
                  AND imported.external_product_id=item.external_product_id
                WHERE item.connection_id=%s AND item.is_present AND NOT item.is_archived
                  AND CASE WHEN s.connection_id IS NOT NULL THEN s.sales_limit ELSE imported.sales_limit END IS NOT NULL
                ON CONFLICT DO NOTHING
            """, (workspace_id, connection_id))
            queued += cursor.rowcount
            # Refresh orders once per store before any daily stock restoration.
            cursor.execute("""
                INSERT INTO seller.marketplace_sync_jobs(workspace_id,connection_id,sync_kind)
                VALUES (%s,%s,'orders') ON CONFLICT DO NOTHING
            """, (workspace_id, connection_id))
            cursor.execute("""
                UPDATE seller.marketplace_connections
                SET next_daily_stock_at=(((now() AT TIME ZONE 'Europe/Moscow')::date+1)::timestamp
                    AT TIME ZONE 'Europe/Moscow') + (id %% 60)*interval '1 second'
                WHERE id=%s AND workspace_id=%s
            """, (connection_id, workspace_id))
    connection.commit()
    return queued
