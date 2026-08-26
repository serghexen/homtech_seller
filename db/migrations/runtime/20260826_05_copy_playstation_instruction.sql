-- Исправляет локальные настройки, созданные предыдущей миграцией:
-- 1) копирует точную инструкцию из эталонной карточки Seller MRKT-9CTX61DE;
-- 2) сохраняет прежние эффективные остатки и лимиты из снимка Seller.
-- CRM не читается и не изменяется.
WITH target_skus(external_product_id) AS (
    VALUES
        ('MRKT-K0MLQFA4'),
        ('MRKT-K0MLQFA41'),
        ('MRKT-K0MLQFA42'),
        ('MRKT-K0MLQFA43'),
        ('MRKT-K0MLQFA44'),
        ('MRKT-K0MLQFA45'),
        ('MRKT-K0MLQFA46'),
        ('MRKT-4TPT2H0Z'),
        ('MRKT-4TPT2H0Z1'),
        ('MRKT-4TPT2H0Z2'),
        ('MRKT-4TPT2H0Z3'),
        ('MRKT-4TPT2H0Z4'),
        ('MRKT-4TPT2H0Z5'),
        ('MRKT-4TPT2H0Z6'),
        ('MRKT-4TPT2H0Z7'),
        ('MRKT-4TPT2H0Z8'),
        ('MRKT-4TPT2H0Z9'),
        ('MRKT-4TPT2H0Z10'),
        ('MRKT-4TPT2H0Z11'),
        ('MRKT-4TPT2H0Z12'),
        ('MRKT-4TPT2H0Z13'),
        ('MRKT-G9NVOGWH'),
        ('MRKT-G9NVOGWH1'),
        ('MRKT-G9NVOGWH2'),
        ('MRKT-DWQQUUEL')
),
reference_instruction AS (
    SELECT CASE
             WHEN local_settings.connection_id IS NOT NULL
               THEN local_settings.activation_instruction
             ELSE imported_settings.activation_instruction
           END AS value
    FROM seller.marketplace_connections AS connection
    JOIN seller.catalog_items AS item
      ON item.connection_id = connection.id
     AND item.external_product_id = 'MRKT-9CTX61DE'
    LEFT JOIN seller.product_card_settings AS local_settings
      ON local_settings.connection_id = item.connection_id
     AND local_settings.external_product_id = item.external_product_id
    LEFT JOIN seller.yandex_product_settings_snapshot AS imported_settings
      ON imported_settings.connection_id = item.connection_id
     AND imported_settings.external_product_id = item.external_product_id
    WHERE connection.provider_code = 'yandex_market'
      AND connection.campaign_id = '149196813'
      AND btrim(CASE
                  WHEN local_settings.connection_id IS NOT NULL
                    THEN local_settings.activation_instruction
                  ELSE COALESCE(imported_settings.activation_instruction, '')
                END) <> ''
)
UPDATE seller.product_card_settings AS local_settings
SET manual_stock_limit = imported_settings.manual_stock_limit,
    sales_limit = imported_settings.sales_limit,
    sales_limit_daily_extra = imported_settings.sales_limit_daily_extra,
    sales_limit_day = imported_settings.sales_limit_day,
    activation_instruction = reference_instruction.value,
    updated_at = now()
FROM seller.marketplace_connections AS connection,
     seller.yandex_product_settings_snapshot AS imported_settings,
     target_skus,
     reference_instruction
WHERE local_settings.connection_id = connection.id
  AND connection.provider_code = 'yandex_market'
  AND connection.campaign_id = '149196813'
  AND local_settings.external_product_id = target_skus.external_product_id
  AND imported_settings.connection_id = local_settings.connection_id
  AND imported_settings.external_product_id = local_settings.external_product_id;
