-- Локальное переопределение только в Seller. CRM и импортированный снимок не изменяются.
-- Если оператор уже успел заполнить инструкцию вручную, миграция сохраняет его значение.
WITH target_connection AS (
    SELECT id
    FROM seller.marketplace_connections
    WHERE provider_code = 'yandex_market'
      AND campaign_id = '149196813'
),
target_skus(external_product_id) AS (
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
instruction(value) AS (
    VALUES (E'Инструкция:\n\nКак активировать:\n\n1. Войдите в аккаунт на консоли PlayStation или через сайт\n2. Перейдите в PlayStation Store\n3. Выберите «Redeem Codes» / «Погасить код»\n4. Введите полученный код\n5. Подтвердите активацию')
)
INSERT INTO seller.product_card_settings (
    connection_id,
    external_product_id,
    activation_instruction
)
SELECT connection.id, sku.external_product_id, instruction.value
FROM target_connection AS connection
CROSS JOIN target_skus AS sku
CROSS JOIN instruction
JOIN seller.catalog_items AS item
  ON item.connection_id = connection.id
 AND item.external_product_id = sku.external_product_id
ON CONFLICT (connection_id, external_product_id) DO UPDATE
SET activation_instruction = EXCLUDED.activation_instruction,
    updated_at = now()
WHERE btrim(seller.product_card_settings.activation_instruction) = '';
