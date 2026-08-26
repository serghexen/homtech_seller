# Seller → Supplier Hub

Supplier Hub остаётся на VM CRM из-за разрешённого InterHub исходящего IP, но не
использует процессы, сеть Docker или базу CRM. Seller обращается к нему через
отдельный SSH local-forward поверх существующего порта 22.

```text
Seller container -> 172.18.0.1:18010 -> SSH -> Hub 127.0.0.1:8010
Seller host      -> 127.0.0.1:18010 -> SSH -> Hub 127.0.0.1:8010
```

Туннель не создаёт интерфейсы, не меняет маршруты и firewall. Ключ на Hub имеет
`permitopen="127.0.0.1:8010"`, forced `nologin` и не даёт shell-доступ.

## Runtime

- systemd unit: `/etc/systemd/system/homtech-hub-tunnel.service`;
- tracked source: `deploy/systemd/homtech-hub-tunnel.service`;
- Seller key: `/home/adminops/.ssh/homtech_hub_tunnel_ed25519`;
- dedicated host keys: `/home/adminops/.ssh/homtech_hub_known_hosts`;
- Hub marker in `authorized_keys`: `homtech-seller-hub-tunnel`.

Проверка без покупок:

```bash
systemctl is-active homtech-hub-tunnel.service
curl -fsS http://127.0.0.1:18010/live
curl -fsS http://127.0.0.1:18010/ready
```

## Seller configuration

```dotenv
SUPPLIER_HUB_URL=http://172.18.0.1:18010
SUPPLIER_HUB_CLIENT_ID=seller
SUPPLIER_HUB_CLIENT_KEY=<dedicated-secret>
SUPPLIER_HUB_TIMEOUT_SECONDS=10
SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED=false
```

`SELLER_SUPPLIER_HUB_FULFILLMENT_ENABLED=false` остаётся выключенным до
реализации resolver и отдельного контролируемого переключения. На стороне Hub
также остаются выключены `SUPPLIER_HUB_PURCHASES_ENABLED` и
`INTERHUB_PAY_ENABLED`.

Авторизованный диагностический маршрут Seller:

```text
GET /api/integrations/supplier-hub/status
```

Он не возвращает URL или ключ и не делает Hub обязательным для здоровья Seller.

## Откат

Остановка туннеля не затрагивает CRM или Seller:

```bash
sudo systemctl disable --now homtech-hub-tunnel.service
```

При выключенном fulfillment-флаге недоступность Hub не меняет обработку заказов.
