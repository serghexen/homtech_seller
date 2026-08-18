-- Добавляет самостоятельную авторизацию Seller до подключения единого SSO.
ALTER TABLE seller.users
  ADD COLUMN IF NOT EXISTS password_hash text NOT NULL DEFAULT '';
