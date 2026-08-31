# Пополнение баланса Seller через СБП Т-Банка

## Что реализовано

Баланс принадлежит всему `workspace` и доступен всем его магазинам. Клиент не передаёт
`workspace_id`: API получает его из авторизованной сессии. Создавать пополнение могут
`owner` и `operator`, просматривать остаток — любой участник workspace.

Сценарий динамического QR:

1. Seller фиксирует попытку пополнения в PostgreSQL до внешнего запроса.
2. Вызывает `POST /v2/Init` с уникальным `OrderId` вида `seller_<uuid>`.
3. Вызывает `POST /v2/GetQr` с `PaymentMethod=SBP` и `DataType=IMAGE`.
4. Показывает SVG QR как безопасный `img`, не исполняя разметку через `v-html`.
5. Принимает подписанное уведомление Т-Банка.
6. Начисляет деньги только при `CONFIRMED` и только один раз по уникальному
   `business_key` журнала движений.
7. Пока платёж не завершён, общий worker периодически вызывает read-only `GetState`.

Точный webhook Seller:

`https://seller.homtech.app/api/payments/tbank/notifications`

Его не обязательно сохранять глобально в кабинете: Seller передаёт URL в каждом
запросе `Init`. Поэтому другой сайт или CRM может использовать свой URL с тем же
терминалом. Для независимых production-систем всё же предпочтительны отдельные
терминалы: так проще раздельно менять ключи, лимиты и расследовать ошибки.

## Переменные окружения для демо

Добавить в серверный `.env`, не в git:

```dotenv
SELLER_TBANK_TOPUPS_ENABLED=false
SELLER_TBANK_DEMO_MODE=true
SELLER_TBANK_TOPUP_MIN_AMOUNT=10000
SELLER_TBANK_TOPUP_MAX_AMOUNT=10000000
SELLER_TBANK_RECONCILIATION_BATCH_SIZE=5
TBANK_BASE_URL=https://securepay.tinkoff.ru/v2
TBANK_TERMINAL_KEY=<тестовый TerminalKey с DEMO>
TBANK_PASSWORD=<пароль тестового терминала>
TBANK_NOTIFICATION_URL=https://seller.homtech.app/api/payments/tbank/notifications
TBANK_SUCCESS_URL=https://seller.homtech.app/
TBANK_FAIL_URL=https://seller.homtech.app/
TBANK_REQUEST_TIMEOUT_SECONDS=15
TBANK_NOTIFICATION_MAX_BODY_BYTES=65536
```

Сначала оставить `SELLER_TBANK_TOPUPS_ENABLED=false`, применить миграцию и
перезапустить API/worker. После проверки конфигурации поменять значение на `true` и
перезапустить только API/worker. Терминал с `DEMO` отправляет запросы на production URL
Т-Банка, но не списывает реальные деньги.

## TLS-сертификаты Т-Банка

API и worker получают Russian Trusted Root CA и Russian Trusted Sub CA во время
сборки образа из `api/certs`. Docker добавляет их в стандартное системное хранилище,
не удаляя публичные CA, а клиент Т-Банка использует объединённый bundle через
`TBANK_CA_BUNDLE`. Отключать проверку TLS нельзя.

Файлы взяты по официальным ссылкам Госуслуг, указанным в инструкции Т-Банка. Перед
обновлением нужно повторно проверить цепочку и SHA-256. Текущие отпечатки:

- Root: `D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31`, до 27.02.2032;
- Sub: `BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B`, до 06.03.2027.

## Демо-сценарии СБП

В окне QR для DEMO-терминала доступны три серверных сценария:

- успех — `SbpPayTest` без дополнительных признаков, ожидаемый итог `CONFIRMED`;
- отказ — `SbpPayTest` с `IsRejected=true`, ожидаемый итог `REJECTED`;
- таймаут — `SbpPayTest` с `IsDeadlineExpired=true`, ожидаемый итог `DEADLINE_EXPIRED`.

Эти кнопки защищены авторизацией и сервер дополнительно запрещает их, если TerminalKey
не содержит `DEMO`. Номера тестовых банковских карт для QR-сценария не используются.

## Что сознательно отложено

- `Receipt` и интеграция онлайн-кассы;
- возврат через `Cancel` и обратное движение по балансу;
- расходование и резервирование баланса при покупке товара у поставщика;
- production-реквизиты и production-включатель;
- мобильный deeplink с выбором банка вместо отображения QR на том же телефоне.

Перед production-запуском нужно отдельно определить правила фискализации пополнения,
возврата уже потраченного остатка и отрицательного баланса/долга.
