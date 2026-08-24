"""HTTP-контракт постановки синхронизации в долговечную очередь PostgreSQL."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser


SyncKind = Literal["catalog", "orders"]


class MarketplaceSyncJobCreateIn(BaseModel):
    connection_id: int | None = Field(default=None, gt=0)


class MarketplaceSyncJobOut(BaseModel):
    id: int
    connection_id: int
    provider_code: str
    store_name: str
    sync_kind: SyncKind
    status: str
    attempt_count: int
    max_attempts: int
    available_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str = ""
    synced_items: int = 0
    created_at: datetime


class MarketplaceSyncJobListOut(BaseModel):
    items: list[MarketplaceSyncJobOut]


JOB_SELECT = """
    SELECT job.id, job.connection_id, connection.provider_code, connection.display_name,
           job.sync_kind, job.status, job.attempt_count, job.max_attempts,
           job.available_at, job.started_at, job.finished_at, job.error,
           job.synced_items, job.created_at
    FROM seller.marketplace_sync_jobs AS job
    JOIN seller.marketplace_connections AS connection ON connection.id=job.connection_id
"""


def sync_job_out(row: tuple[Any, ...]) -> MarketplaceSyncJobOut:
    # Собирает публичный статус без токена и внутренних данных worker-а.
    return MarketplaceSyncJobOut(
        id=int(row[0]),
        connection_id=int(row[1]),
        provider_code=str(row[2]),
        store_name=str(row[3]),
        sync_kind=str(row[4]),
        status=str(row[5]),
        attempt_count=int(row[6]),
        max_attempts=int(row[7]),
        available_at=row[8],
        started_at=row[9],
        finished_at=row[10],
        error=str(row[11] or ""),
        synced_items=int(row[12] or 0),
        created_at=row[13],
    )


def parse_job_ids(value: str) -> list[int]:
    # Принимает компактный список для polling и ограничивает объём одного запроса.
    if not str(value or "").strip():
        return []
    result: list[int] = []
    for part in value.split(","):
        try:
            job_id = int(part.strip())
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Некорректный список заданий") from exc
        if job_id <= 0:
            raise HTTPException(status_code=400, detail="Некорректный список заданий")
        if job_id not in result:
            result.append(job_id)
        if len(result) > 100:
            raise HTTPException(status_code=400, detail="Слишком много заданий в одном запросе")
    return result


def mount_marketplace_sync_job_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Подключает постановку и чтение заданий в границах workspace текущей сессии."""

    def workspace_for_user(connection, user: AuthenticatedUser):
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        return seller_user

    def enqueue_jobs(sync_kind: SyncKind, payload: MarketplaceSyncJobCreateIn, user: AuthenticatedUser) -> MarketplaceSyncJobListOut:
        # Создаёт не более одного активного задания вида catalog/orders на магазин.
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            connection_filter = "AND id=%s" if payload.connection_id else ""
            params = [seller_user.workspace_id, *([payload.connection_id] if payload.connection_id else [])]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    SELECT id
                    FROM seller.marketplace_connections
                    WHERE workspace_id=%s AND status='active' {connection_filter}
                    ORDER BY created_at, id
                    """,
                    params,
                )
                connection_ids = [int(row[0]) for row in cursor.fetchall()]
                if not connection_ids:
                    raise HTTPException(status_code=404, detail="Активный подключенный магазин не найден")

                job_ids: list[int] = []
                for connection_id in connection_ids:
                    cursor.execute(
                        """
                        INSERT INTO seller.marketplace_sync_jobs(
                            workspace_id, connection_id, sync_kind, requested_by_user_id
                        ) VALUES (%s, %s, %s, %s)
                        ON CONFLICT DO NOTHING
                        RETURNING id
                        """,
                        (seller_user.workspace_id, connection_id, sync_kind, seller_user.id),
                    )
                    row = cursor.fetchone()
                    if not row:
                        cursor.execute(
                            """
                            SELECT id
                            FROM seller.marketplace_sync_jobs
                            WHERE connection_id=%s AND sync_kind=%s AND status IN ('queued', 'running')
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (connection_id, sync_kind),
                        )
                        row = cursor.fetchone()
                    if row:
                        job_ids.append(int(row[0]))

                cursor.execute(
                    f"{JOB_SELECT} WHERE job.workspace_id=%s AND job.id=ANY(%s) ORDER BY job.id",
                    (seller_user.workspace_id, job_ids),
                )
                rows = cursor.fetchall()
        return MarketplaceSyncJobListOut(items=[sync_job_out(row) for row in rows])

    @app.post("/marketplaces/catalog/sync", response_model=MarketplaceSyncJobListOut, status_code=202)
    def enqueue_catalog_sync(
        payload: MarketplaceSyncJobCreateIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceSyncJobListOut:
        return enqueue_jobs("catalog", payload, user)

    @app.post("/marketplaces/orders/sync", response_model=MarketplaceSyncJobListOut, status_code=202)
    def enqueue_orders_sync(
        payload: MarketplaceSyncJobCreateIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceSyncJobListOut:
        return enqueue_jobs("orders", payload, user)

    @app.get("/marketplaces/sync-jobs", response_model=MarketplaceSyncJobListOut)
    def list_sync_jobs(
        job_ids: str = Query(default="", max_length=4096),
        user: AuthenticatedUser = Depends(current_user),
    ) -> MarketplaceSyncJobListOut:
        # Polling возвращает только задания своей рабочей области; без ids — последние 100.
        requested_ids = parse_job_ids(job_ids)
        with psycopg.connect(database_url()) as connection:
            seller_user = workspace_for_user(connection, user)
            where_ids = "AND job.id=ANY(%s)" if requested_ids else ""
            params = [seller_user.workspace_id, *([requested_ids] if requested_ids else [])]
            with connection.cursor() as cursor:
                cursor.execute(
                    f"""
                    {JOB_SELECT}
                    WHERE job.workspace_id=%s {where_ids}
                    ORDER BY job.created_at DESC, job.id DESC
                    LIMIT 100
                    """,
                    params,
                )
                rows = cursor.fetchall()
        return MarketplaceSyncJobListOut(items=[sync_job_out(row) for row in rows])
