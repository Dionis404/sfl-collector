"""
Регрессия: на проде таблицы лидерборда (goblin-bot) принадлежат другому
пользователю Postgres, а коллектор работает под обычным пользователем с
правами только на чтение/запись. Миграция не должна пытаться менять
чужие таблицы (раньше падала с "must be owner of table ...").

Нужен COLLECTOR_TEST_DB с пользователем-суперпользователем (в CI так и есть):
тест сам создаёт роли «владелец» и «коллектор».
"""
import os

import asyncpg
import pytest

from collector import migrate

DSN = os.environ.get("COLLECTOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DSN, reason="COLLECTOR_TEST_DB не задан")

OWNER, COLLECTOR, PASSWORD = "legacy_owner_t", "collector_t", "collector_t_pw"


async def test_migration_skips_tables_owned_by_another_role():
    host, port, name, user, password = DSN.split(":")
    admin = await asyncpg.connect(host=host, port=int(port), database=name, user=user, password=password)
    try:
        if not await admin.fetchval("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"):
            pytest.skip("нужен суперпользователь для создания тестовых ролей")

        await admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for role in (OWNER, COLLECTOR):
            await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.execute(f"CREATE ROLE {OWNER} NOLOGIN")
        await admin.execute(f"CREATE ROLE {COLLECTOR} LOGIN PASSWORD '{PASSWORD}'")
        await admin.execute(f"GRANT ALL ON SCHEMA public TO {OWNER}, {COLLECTOR}")

        # как на проде: таблицы лидерборда созданы другим пользователем
        await admin.execute(f"SET ROLE {OWNER}")
        await admin.execute(
            """
            CREATE TABLE top500_snapshots (id SERIAL PRIMARY KEY, rank INT NOT NULL, farm_id BIGINT NOT NULL,
                game_username TEXT, tickets INT NOT NULL, taken_at TIMESTAMPTZ NOT NULL DEFAULT now());
            CREATE TABLE ticket_leaderboard_snapshots (id SERIAL PRIMARY KEY, farm_id BIGINT NOT NULL,
                rank INT NOT NULL, tickets INT NOT NULL, game_username TEXT,
                taken_at TIMESTAMPTZ NOT NULL DEFAULT now());
            """
        )
        await admin.execute(
            f"""GRANT SELECT, INSERT, UPDATE ON top500_snapshots, ticket_leaderboard_snapshots TO {COLLECTOR};
                GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {COLLECTOR};"""
        )
        await admin.execute("RESET ROLE")

        conn = await asyncpg.connect(host=host, port=int(port), database=name, user=COLLECTOR, password=PASSWORD)
        try:
            assert await migrate.apply_migrations(conn) == sorted(p.name for p in migrate.MIGRATIONS_DIR.glob("*.sql"))
            # коллектор может писать в чужую таблицу (нужны только GRANT, не владение)
            await conn.execute("INSERT INTO top500_snapshots (rank, farm_id, tickets) VALUES (1, 1, 1)")
            # свои таблицы созданы
            assert await conn.fetchval("SELECT to_regclass('cave_merchant_visits') IS NOT NULL")
        finally:
            await conn.close()

        # чужие таблицы так и остались у прежнего владельца
        owner = await admin.fetchval(
            "SELECT tableowner FROM pg_tables WHERE tablename = 'ticket_leaderboard_snapshots'"
        )
        assert owner == OWNER
    finally:
        await admin.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
        for role in (OWNER, COLLECTOR):
            await admin.execute(f"DROP OWNED BY {role}") if await admin.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", role) else None
            await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()
