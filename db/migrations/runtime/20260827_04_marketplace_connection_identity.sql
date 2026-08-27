-- Один реальный кабинет маркетплейса может принадлежать только одному workspace.
-- Это гарантирует однозначную маршрутизацию webhook и не позволяет двум worker-ам
-- разных пользователей обрабатывать один и тот же магазин.
CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_connections_yandex_campaign_global
  ON seller.marketplace_connections(campaign_id)
  WHERE provider_code='yandex_market' AND campaign_id<>'';

CREATE UNIQUE INDEX IF NOT EXISTS uq_marketplace_connections_ozon_client_global
  ON seller.marketplace_connections(client_id)
  WHERE provider_code='ozon' AND client_id<>'';
