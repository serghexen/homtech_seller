"""Меняет тариф workspace транзакционно, без рестарта приложений Seller."""

from __future__ import annotations

import argparse
import os

import psycopg


def main() -> int:
    parser = argparse.ArgumentParser(description="Set HomTech Seller workspace plan")
    parser.add_argument("workspace_id", type=int)
    parser.add_argument("plan_code", choices=("basic", "pro"))
    parser.add_argument("--status", choices=("trialing", "active", "past_due", "suspended", "cancelled"), default="active")
    parser.add_argument("--reason", default="Изменение тарифа администратором")
    args = parser.parse_args()

    database_url = str(os.getenv("DATABASE_URL", "")).strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    with psycopg.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT name FROM seller.workspaces WHERE id=%s FOR UPDATE", (args.workspace_id,))
            workspace = cursor.fetchone()
            if not workspace:
                raise RuntimeError(f"Workspace {args.workspace_id} not found")
            cursor.execute("SELECT id, display_name FROM seller.plans WHERE code=%s AND is_active=true", (args.plan_code,))
            plan = cursor.fetchone()
            if not plan:
                raise RuntimeError(f"Active plan {args.plan_code} not found")
            cursor.execute(
                """
                INSERT INTO seller.workspace_subscriptions(
                  workspace_id, plan_id, status, change_source, change_reason
                ) VALUES (%s,%s,%s,'admin_cli',%s)
                ON CONFLICT (workspace_id) DO UPDATE SET
                  plan_id=EXCLUDED.plan_id,
                  status=EXCLUDED.status,
                  started_at=now(),
                  valid_until=NULL,
                  grace_until=NULL,
                  revision=seller.workspace_subscriptions.revision + 1,
                  change_source='admin_cli',
                  change_reason=EXCLUDED.change_reason,
                  updated_at=now()
                RETURNING revision
                """,
                (args.workspace_id, int(plan[0]), args.status, str(args.reason).strip()[:1000]),
            )
            revision = int(cursor.fetchone()[0])

    print(
        f"Workspace {args.workspace_id} ({workspace[0]}): "
        f"plan={args.plan_code}, status={args.status}, revision={revision}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
