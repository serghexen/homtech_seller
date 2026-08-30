"""Меняет тариф конкретного магазина транзакционно, без рестарта Seller."""

from __future__ import annotations

import argparse
import os

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser(description="Set HomTech Seller marketplace connection plan")
    parser.add_argument("connection_id", type=int)
    parser.add_argument("plan_code", choices=("basic", "pro"))
    parser.add_argument(
        "--status",
        choices=("trialing", "active", "past_due", "suspended", "cancelled"),
        default="active",
    )
    parser.add_argument("--reason", default="Изменение тарифа магазина администратором")
    args = parser.parse_args()

    database_url = str(os.getenv("DATABASE_URL", "")).strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT connection.workspace_id, connection.display_name, workspace.name
                FROM seller.marketplace_connections AS connection
                JOIN seller.workspaces AS workspace ON workspace.id=connection.workspace_id
                WHERE connection.id=%s
                FOR UPDATE OF connection
                """,
                (args.connection_id,),
            )
            store = cursor.fetchone()
            if not store:
                raise RuntimeError(f"Marketplace connection {args.connection_id} not found")
            cursor.execute(
                "SELECT id, display_name FROM seller.plans WHERE code=%s AND is_active=true",
                (args.plan_code,),
            )
            plan = cursor.fetchone()
            if not plan:
                raise RuntimeError(f"Active plan {args.plan_code} not found")
            cursor.execute(
                """
                INSERT INTO seller.marketplace_connection_subscriptions(
                  connection_id, workspace_id, plan_id, status, change_source, change_reason
                ) VALUES (%s,%s,%s,%s,'admin_cli',%s)
                ON CONFLICT (connection_id) DO UPDATE SET
                  plan_id=EXCLUDED.plan_id,
                  status=EXCLUDED.status,
                  started_at=now(),
                  valid_until=NULL,
                  grace_until=NULL,
                  revision=seller.marketplace_connection_subscriptions.revision + 1,
                  change_source='admin_cli',
                  change_reason=EXCLUDED.change_reason,
                  updated_at=now()
                RETURNING revision
                """,
                (
                    args.connection_id,
                    int(store[0]),
                    int(plan[0]),
                    args.status,
                    str(args.reason).strip()[:1000],
                ),
            )
            revision = int(cursor.fetchone()[0])

    print(
        f"Connection {args.connection_id} ({store[1]}, workspace {store[2]}): "
        f"plan={args.plan_code}, status={args.status}, revision={revision}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
