# JoyCards: подготовка и контролируемое переключение

## Что разрешено сделать заранее

1. Развернуть Supplier Hub и Seller с покупками и выдачей, выключенными во всех окружениях.
2. Применить миграцию `20260826_01_supplier_fulfillment.sql`.
3. Повторно импортировать из CRM инструкции, сообщения поддержки и пул ключей.
4. Выполнить dry-run и затем импорт политики выдачи:

   ```bash
   python scripts/import_crm_yandex_fulfillment.py \
     --source-store-code joycards \
     --target-campaign-id 149196813 \
     --expected-count 371
   ```

   Для записи добавляется только `--apply`. CRM читается с `SET TRANSACTION READ ONLY`,
   `statement_timeout=5s` и `lock_timeout=1s`.

5. При необходимости вручную перепроверить отдельные цены без покупки через Supplier Hub:

   ```bash
   python scripts/refresh_supplier_quotes.py --campaign-id 149196813
   ```

   Повторять расчёт для всего каталога перед переключением не требуется: Seller
   использует последние вручную подтверждённые цены, перенесённые из CRM. Давность
   цены в итоговом аудите является предупреждением, а не блокировкой. Отсутствующая
   цена или защитный лимит остаются блокирующей ошибкой. Endpoint вызывает только
   Interhub `calculate`; `check/pay` не вызываются.
6. Визуально сверить карточки: поставщик, ID услуги, номинал, ценовой предел,
   пул, сообщение поддержки и итоговый порядок выдачи.
7. Выполнить единый read-only аудит (ненулевой exit code означает наличие
   блокирующих условий):

   ```bash
   python scripts/check_joycards_cutover_readiness.py \
     --source-store-code joycards \
     --campaign-id 149196813 \
     --expected-count 371
   ```

   Аудит читает агрегаты Seller, CRM и Supplier Hub, не расшифровывает ключи,
   не обновляет данные и не вызывает методы поставщика.

## Обязательное состояние до команды на переключение

- CRM продолжает принимать единственный webhook Яндекса и выполнять выдачу.
- `YANDEX_MARKET_WEBHOOK_PROCESSING_ENABLED=false`.
- `SELLER_FULFILLMENT_RESOLVER_ENABLED=false`.
- `SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED=false`.
- `SELLER_POOL_RESERVATION_ENABLED=false`.
- `SELLER_YANDEX_OUTBOUND_ENABLED=false`.
- У JoyCards в Seller выключены `webhook_processing_enabled`,
  `supplier_fulfillment_enabled`, `fulfillment_reservation_enabled` и
  `fulfillment_outbound_enabled`.
- В Hub выключены `SUPPLIER_HUB_PURCHASES_ENABLED` и
  `SUPPLIER_HUB_INTERHUB_PAYMENT_ENABLED`.

## Стоп-условия

Переключение запрещено, если в CRM есть незавершённая выдача или ключи
`reserved/sending`, в Hub есть `processing/requires_attention`, в Seller есть
несверенные политики либо отсутствует сохранённая цена/защитный лимит. Переключение webhook и включение
любого боевого флага выполняются только по отдельному явному подтверждению владельца.
