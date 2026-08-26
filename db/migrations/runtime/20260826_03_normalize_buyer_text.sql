-- Исправляет буквальные legacy-последовательности из CRM, не затрагивая уже настоящие переносы.
UPDATE seller.yandex_product_settings_snapshot
SET activation_instruction = replace(replace(replace(activation_instruction, E'\\r\\n', E'\n'), E'\\n', E'\n'), E'\\r', E'\n'),
    support_message = replace(replace(replace(support_message, E'\\r\\n', E'\n'), E'\\n', E'\n'), E'\\r', E'\n')
WHERE activation_instruction LIKE E'%\\n%'
   OR activation_instruction LIKE E'%\\r%'
   OR support_message LIKE E'%\\n%'
   OR support_message LIKE E'%\\r%';

UPDATE seller.product_card_settings
SET activation_instruction = replace(replace(replace(activation_instruction, E'\\r\\n', E'\n'), E'\\n', E'\n'), E'\\r', E'\n'),
    support_message = replace(replace(replace(support_message, E'\\r\\n', E'\n'), E'\\n', E'\n'), E'\\r', E'\n')
WHERE activation_instruction LIKE E'%\\n%'
   OR activation_instruction LIKE E'%\\r%'
   OR support_message LIKE E'%\\n%'
   OR support_message LIKE E'%\\r%';
