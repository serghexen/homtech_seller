"""Точечное раскрытие уже сохранённых ключей по явному действию оператора."""

from __future__ import annotations

from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from domains.local_auth import AuthenticatedUser
from domains.yandex_market_outbound import key_pool_secret


class KeyRevealItemOut(BaseModel):
    id: int
    code: str


class OrderKeysRevealIn(BaseModel):
    connection_id: int = Field(gt=0)
    external_order_id: str = Field(min_length=1, max_length=128)
    external_item_id: str = Field(min_length=1, max_length=256)


class OrderKeysRevealOut(BaseModel):
    items: list[KeyRevealItemOut] = Field(default_factory=list)


def mount_marketplace_key_reveal_routes(
    app: FastAPI,
    *,
    database_url: Callable[[], str],
    psycopg,
    current_user: Callable[..., AuthenticatedUser],
    user_with_workspace: Callable,
) -> None:
    """Расшифровывает только выбранный ключ или комплект одного заказа."""

    def reveal_context(connection, user: AuthenticatedUser):
        seller_user = user_with_workspace(connection, user.user_id)
        if not seller_user:
            raise HTTPException(status_code=401, detail="Рабочая область недоступна")
        if seller_user.role_code not in {"owner", "operator"}:
            raise HTTPException(status_code=403, detail="Недостаточно прав для просмотра ключей")
        return seller_user

    def encryption_secret() -> str:
        try:
            return key_pool_secret()
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail="Не настроено защищённое хранение ключей Seller") from exc

    @app.post(
        "/marketplaces/catalog/key-pool/keys/{key_id}/reveal",
        response_model=KeyRevealItemOut,
    )
    def reveal_pool_key(
        key_id: int,
        connection_id: int = Query(gt=0),
        external_product_id: str = Query(min_length=1, max_length=256),
        user: AuthenticatedUser = Depends(current_user),
    ) -> KeyRevealItemOut:
        with psycopg.connect(database_url()) as connection:
            seller_user = reveal_context(connection, user)
            secret = encryption_secret()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT key.id, pgp_sym_decrypt(key.code_ciphertext, %s)
                    FROM seller.marketplace_keys AS key
                    JOIN seller.marketplace_key_pools AS pool ON pool.id=key.pool_id
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=pool.connection_id
                    WHERE key.id=%s AND key.key_origin='pool'
                      AND pool.connection_id=%s AND pool.external_product_id=%s
                      AND marketplace_connection.workspace_id=%s
                    """,
                    (secret, key_id, connection_id, str(external_product_id).strip(), seller_user.workspace_id),
                )
                row = cursor.fetchone()
        if not row or not str(row[1] or ""):
            raise HTTPException(status_code=404, detail="Ключ не найден в пуле этой карточки")
        return KeyRevealItemOut(id=int(row[0]), code=str(row[1]))

    @app.post("/marketplaces/orders/fulfillment/reveal", response_model=OrderKeysRevealOut)
    def reveal_order_keys(
        payload: OrderKeysRevealIn,
        user: AuthenticatedUser = Depends(current_user),
    ) -> OrderKeysRevealOut:
        with psycopg.connect(database_url()) as connection:
            seller_user = reveal_context(connection, user)
            secret = encryption_secret()
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT key.id, pgp_sym_decrypt(key.code_ciphertext, %s)
                    FROM seller.order_items AS item
                    JOIN seller.marketplace_connections AS marketplace_connection
                      ON marketplace_connection.id=item.connection_id
                    JOIN seller.order_fulfillments AS fulfillment
                      ON fulfillment.connection_id=item.connection_id
                     AND fulfillment.external_order_id=item.external_order_id
                     AND fulfillment.external_item_id=item.external_item_id
                    JOIN seller.fulfillment_key_reservations AS reservation
                      ON reservation.fulfillment_id=fulfillment.id
                     AND reservation.state IN ('reserved','consumed')
                    JOIN seller.marketplace_keys AS key ON key.id=reservation.key_id
                    WHERE item.connection_id=%s AND item.external_order_id=%s
                      AND item.external_item_id=%s AND marketplace_connection.workspace_id=%s
                      AND key.status IN ('reserved','sending','delivered')
                    ORDER BY reservation.id
                    """,
                    (
                        secret, payload.connection_id, payload.external_order_id.strip(),
                        payload.external_item_id.strip(), seller_user.workspace_id,
                    ),
                )
                rows = cursor.fetchall()
        if not rows:
            raise HTTPException(status_code=404, detail="Для этого заказа нет сохранённых ключей")
        return OrderKeysRevealOut(items=[KeyRevealItemOut(id=int(row[0]), code=str(row[1])) for row in rows])
