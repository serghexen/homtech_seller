"""Применяет новые миграции самостоятельной базы HomTech Seller."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg


MIGRATIONS_LOCK = "homtech_seller_schema_migrations"
TRACKING_SCHEMA = "seller_system"


def migration_checksum(content: str) -> str:
    # Считает контрольную сумму, чтобы применённую миграцию нельзя было изменить незаметно.
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def split_sql_statements(content: str) -> list[str]:
    # Делит SQL для нетранзакционных индексов, не разрывая строки и dollar-quoted блоки.
    statements: list[str] = []
    current: list[str] = []
    quote: str | None = None
    dollar_quote: str | None = None
    line_comment = False
    block_comment = False
    index = 0
    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""
        if line_comment:
            current.append(char)
            line_comment = char != "\n"
            index += 1
            continue
        if block_comment:
            current.append(char)
            if char == "*" and next_char == "/":
                current.append(next_char)
                index += 2
                block_comment = False
            else:
                index += 1
            continue
        if dollar_quote:
            if content.startswith(dollar_quote, index):
                current.extend(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
            else:
                current.append(char)
                index += 1
            continue
        if quote:
            current.append(char)
            if char == quote:
                if next_char == quote:
                    current.append(next_char)
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if char == "-" and next_char == "-":
            current.extend((char, next_char))
            line_comment = True
            index += 2
            continue
        if char == "/" and next_char == "*":
            current.extend((char, next_char))
            block_comment = True
            index += 2
            continue
        if char in {"'", '"'}:
            current.append(char)
            quote = char
            index += 1
            continue
        if char == "$":
            closing = content.find("$", index + 1)
            tag = content[index : closing + 1] if closing >= 0 else ""
            if tag and all(part.isalnum() or part == "_" for part in tag[1:-1]):
                current.extend(tag)
                dollar_quote = tag
                index += len(tag)
                continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def ensure_tracking_table(connection) -> None:
    # Создаёт служебную схему и журнал миграций до первого изменения бизнес-схемы.
    with connection.cursor() as cursor:
        cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {TRACKING_SCHEMA}")
        cursor.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TRACKING_SCHEMA}.schema_migrations (
              migration_name text PRIMARY KEY,
              checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )


def read_applied_checksum(connection, migration_name: str) -> str | None:
    # Возвращает сохранённую контрольную сумму для идемпотентного запуска.
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT checksum FROM {TRACKING_SCHEMA}.schema_migrations WHERE migration_name=%s",
            (migration_name,),
        )
        row = cursor.fetchone()
    return str(row[0]) if row else None


def apply_migration(connection, migration_path: Path) -> bool:
    # Применяет новый SQL-файл ровно один раз и фиксирует его только после успеха.
    content = migration_path.read_text(encoding="utf-8")
    checksum = migration_checksum(content)
    migration_name = migration_path.name
    applied_checksum = read_applied_checksum(connection, migration_name)
    if applied_checksum:
        if applied_checksum != checksum:
            raise RuntimeError(f"Migration was changed after apply: {migration_name}")
        return False
    is_no_transaction = content.lstrip().startswith("-- migrate:no-transaction")
    if is_no_transaction:
        for statement in split_sql_statements(content):
            statement = statement.removeprefix("-- migrate:no-transaction").strip()
            if statement:
                with connection.cursor() as cursor:
                    cursor.execute(statement)
    else:
        with connection.transaction():
            with connection.cursor() as cursor:
                cursor.execute(content)
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {TRACKING_SCHEMA}.schema_migrations(migration_name, checksum) VALUES (%s, %s)",
            (migration_name, checksum),
        )
    return True


def run_migrations(migrations_root: Path) -> int:
    # Последовательно запускает миграции под advisory lock, исключая параллельные релизы.
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    runtime_dir = migrations_root / "runtime"
    if not runtime_dir.is_dir():
        raise RuntimeError(f"Runtime migrations directory not found: {runtime_dir}")
    with psycopg.connect(database_url, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout TO '5s'")
            cursor.execute("SET statement_timeout TO '15min'")
            cursor.execute("SELECT pg_advisory_lock(hashtext(%s))", (MIGRATIONS_LOCK,))
        try:
            ensure_tracking_table(connection)
            for migration_path in sorted(runtime_dir.glob("*.sql")):
                state = "Applied" if apply_migration(connection, migration_path) else "Already applied"
                print(f"{state} {migration_path.name}")
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtext(%s))", (MIGRATIONS_LOCK,))
    return 0


def main() -> int:
    # Принимает путь тома SQL, чтобы один образ работал в локальном и серверном окружениях.
    parser = argparse.ArgumentParser(description="Apply HomTech Seller migrations")
    parser.add_argument("migrations_root", nargs="?", default="/migrations")
    return run_migrations(Path(parser.parse_args().migrations_root))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Migration failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
