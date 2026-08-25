-- Сохраняет CRM-снимок сообщения поддержки отдельно от явного локального переопределения Seller.
ALTER TABLE seller.yandex_product_settings_snapshot
  ADD COLUMN IF NOT EXISTS support_message text NOT NULL DEFAULT '';

ALTER TABLE seller.yandex_product_settings_snapshot
  ADD COLUMN IF NOT EXISTS support_message_delivery_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE seller.product_card_settings
  ADD COLUMN IF NOT EXISTS support_message_delivery_enabled boolean NOT NULL DEFAULT false;

ALTER TABLE seller.product_card_settings
  ADD COLUMN IF NOT EXISTS support_message_overridden boolean NOT NULL DEFAULT false;

ALTER TABLE seller.yandex_product_settings_snapshot
  DROP CONSTRAINT IF EXISTS yandex_product_settings_support_message_length_check;

ALTER TABLE seller.yandex_product_settings_snapshot
  ADD CONSTRAINT yandex_product_settings_support_message_length_check
  CHECK (char_length(support_message) <= 2000);
