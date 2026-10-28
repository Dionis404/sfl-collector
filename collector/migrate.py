"""
Автоприменение SQL-миграций из migrations/ (по порядку имён файлов).

Запуск: python -m collector.migrate — в docker-compose это одноразовый сервис
`migrate`, collector стартует только после его успешного завершения.

- Учёт в таблице sfl_collector_migrations (БД sfl общая с другими сервисами).
- Каждая миграция в своей транзакции; упавшая откатывается целиком.
- pg_advisory_lock не даёт двум процессам применять миграции одновременно.
"""
import asyncio
import logging
from pathlib import Path

import asyncpg

from collector import config

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATIONS_TABLE = "sfl_collector_migrations"
ADVISORY_LOCK_KEY = 0x5F1C0


async def apply_migrations(conn: asyncpg.Connection, directory: Path = MIGRATIONS_DIR) -> list[str]:
    await conn.execute("SELECT pg_advisory_lock($1)", ADVISORY_LOCK_KEY)
    try:
        await conn.execute(
            f"""CREATE TABLE IF NOT EXISTS {MIGRATIONS_TABLE} (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"""
        )
        applied = {r["name"] for r in await conn.fetch(f"SELECT name FROM {MIGRATIONS_TABLE}")}
        done = []
        for path in sorted(directory.glob("*.sql"), key=lambda p: p.name):
            if path.name in applied:
                continue
            log.info("Применяю миграцию %s", path.name)
            async with conn.transaction():
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute(f"INSERT INTO {MIGRATIONS_TABLE} (name) VALUES ($1)", path.name)
            done.append(path.name)
        if not done:
            log.info("Новых миграций нет")
        return done
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", ADVISORY_LOCK_KEY)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = await asyncpg.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        database=config.DB_NAME,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
    )
    try:
        await apply_migrations(conn)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(_main())
