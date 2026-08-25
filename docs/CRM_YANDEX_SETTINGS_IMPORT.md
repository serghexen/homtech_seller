# Перенос read-only настроек Яндекс Маркета из CRM

Перенос устроен как односторонний снимок: CRM только читается коротким MVCC-запросом,
а изменения выполняются исключительно в базе Seller. Скрипт импорта не содержит кода
подключения к CRM и по умолчанию работает в режиме dry-run.

## 1. Подготовка Seller

Сначала применяется миграция `20260824_03_yandex_product_settings_snapshot.sql`.
Она создаёт отдельную таблицу и не изменяет `catalog_items`, заказы или подключения.

## 2. Read-only экспорт CRM

Перед экспортом проверяем, что нет активного дневного лимита, который ещё не перешёл
на текущий московский день. Если запрос возвращает значение больше нуля, перенос
останавливаем и отдельно согласовываем состояние лимита.

```sql
SELECT COUNT(*)
FROM app.marketplace_yandex_stock_settings
WHERE store_code = 'joycards'
  AND sales_limit IS NOT NULL
  AND sales_limit_day < (CURRENT_TIMESTAMP AT TIME ZONE 'Europe/Moscow')::date;
```

Экспорт выполняется в короткой read-only транзакции без `FOR UPDATE` и без DDL:

```sql
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;
SET LOCAL lock_timeout TO '1s';
SET LOCAL statement_timeout TO '10s';

COPY (
  SELECT jsonb_build_object(
    'source_store_code', settings.store_code,
    'offer_id', settings.offer_id,
    'manual_stock_limit', settings.manual_stock_limit,
    'published_stock', settings.published_stock,
    'activation_instruction', settings.activation_instruction,
    'sales_limit', settings.sales_limit,
    'sales_limit_daily_extra', settings.sales_limit_daily_extra,
    'sales_limit_day', settings.sales_limit_day,
    'sales_limit_revision', settings.sales_limit_revision,
    'sales_limit_used', CASE WHEN settings.sales_limit IS NULL THEN 0 ELSE totals.used END,
    'sales_limit_reserved', CASE WHEN settings.sales_limit IS NULL THEN 0 ELSE totals.reserved END,
    'sales_limit_remaining', CASE
      WHEN settings.sales_limit IS NULL THEN NULL
      ELSE GREATEST(0, settings.sales_limit + settings.sales_limit_daily_extra - totals.used - totals.reserved)
    END,
    'sales_limit_exhausted_at', settings.sales_limit_exhausted_at,
    'archived_by_sales_limit', settings.archived_by_sales_limit,
    'last_stock_sync_at', settings.last_stock_sync_at,
    'source_updated_at', settings.updated_at
  )::text
  FROM app.marketplace_yandex_stock_settings AS settings
  LEFT JOIN LATERAL (
    SELECT
      COALESCE(SUM(reservation.quantity) FILTER (WHERE reservation.state='consumed'), 0)::integer AS used,
      COALESCE(SUM(reservation.quantity) FILTER (WHERE reservation.state='reserved'), 0)::integer AS reserved
    FROM app.marketplace_yandex_sales_limit_reservations AS reservation
    WHERE reservation.store_code=settings.store_code
      AND reservation.offer_id=settings.offer_id
      AND reservation.limit_revision=settings.sales_limit_revision
  ) AS totals ON true
  WHERE settings.store_code='joycards'
  ORDER BY settings.offer_id
) TO STDOUT;

COMMIT;
```

Результат — JSONL: одна самостоятельная JSON-строка на товар. Тексты инструкций
экранируются JSON и не выводятся в журнал импорта.

## 3. Проверка и применение

Сначала запускается dry-run без `--apply`. Он проверяет:

- единственность Campaign ID в Seller;
- активность подключения;
- ожидаемое число строк;
- отсутствие дублей и отрицательных значений;
- наличие каждого `offer_id` в каталоге Seller;
- количество новых, изменённых и неизменившихся строк.

Фактическая запись разрешается только флагом `--apply`. Она выполняется одной короткой
транзакцией с `lock_timeout=3s` и `statement_timeout=30s`. Удаление строк не выполняется.

```bash
python scripts/import_crm_yandex_settings.py \
  --input /secure/path/joycards-settings.jsonl \
  --source-store-code joycards \
  --campaign-id 149196813 \
  --expected-count 371

python scripts/import_crm_yandex_settings.py \
  --input /secure/path/joycards-settings.jsonl \
  --source-store-code joycards \
  --campaign-id 149196813 \
  --expected-count 371 \
  --apply
```

Повторный запуск идемпотентен. Пока CRM остаётся источником редактирования, экспорт и
импорт можно безопасно повторять как одностороннюю синхронизацию CRM → Seller.

Перед финальным импортом нужно выполнить новую синхронизацию каталога: Seller получает
как активные, так и архивные предложения Яндекс Маркета. Если после этого CRM всё ещё
содержит настройки карточек, отсутствующих в полном снимке Seller, строгий dry-run
завершится ошибкой. После проверки списка можно явно добавить `--skip-missing`:
импортёр перенесёт только точно сопоставленные карточки и выведет количество и первые
идентификаторы пропущенных. Эти строки не удаляются из CRM и могут быть доимпортированы
после появления товара в каталоге Seller.
