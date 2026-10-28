"""
Интеграционный тест на настоящем Postgres: миграции + все задачи коллектора
с подменённым SFL API. Запускается только если задана переменная:

    COLLECTOR_TEST_DB="host:port:dbname:user:password" pytest -q

ВНИМАНИЕ: схема public указанной БД пересоздаётся — только для тестовой БД!
"""
import os
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from collector import config, db, migrate, sfl_api
from collector.jobs import item_checks, nightly_dump, tickets_leaderboard, top_farms
from tests.conftest import make_farm

DSN = os.environ.get("COLLECTOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DSN, reason="COLLECTOR_TEST_DB не задан")

# Таблицы Аукциониста (Dionis404/auctioneer-bot, migrations) и farmers из goblin-bot — в урезанном виде
EXTERNAL_TABLES = """
CREATE TABLE auctions (
    auction_id text PRIMARY KEY, item_name text, item_type text, supply int, sfl_price numeric,
    ingredients jsonb, start_at timestamptz, end_at timestamptz, chapter_limit int, start_id int,
    raw jsonb, results_fetched boolean DEFAULT false, updated_at timestamptz DEFAULT now());
CREATE TABLE auction_results (
    auction_id text PRIMARY KEY REFERENCES auctions(auction_id), my_status text,
    participant_count int, supply int, leaderboard jsonb, fetched_at timestamptz DEFAULT now());
CREATE TABLE farmers (
    farm_id BIGINT PRIMARY KEY, tickets_tracked BOOLEAN NOT NULL DEFAULT false,
    tickets_excluded BOOLEAN NOT NULL DEFAULT false);
"""

FARMS = {
    10: make_farm(wardrobe={"Pumpkin Hat": 1}),
    20: make_farm(wardrobe={"Pumpkin Hat": 2}),
    30: make_farm(),
}


@pytest.fixture
async def pool(monkeypatch):
    host, port, name, user, password = DSN.split(":")
    for k, v in dict(DB_HOST=host, DB_PORT=int(port), DB_NAME=name, DB_USER=user, DB_PASSWORD=password).items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(config, "ITEM_CHECKS_TOP_N", 3)
    monkeypatch.setattr(config, "TOP_FARMS_N", 3)
    monkeypatch.setattr(config, "DUMP_TOP_N", 3)

    conn = await asyncpg.connect(host=host, port=int(port), database=name, user=user, password=password)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    assert await migrate.apply_migrations(conn) == ["001_init.sql"]
    assert await migrate.apply_migrations(conn) == []  # повторный запуск — ничего нового
    await conn.execute(EXTERNAL_TABLES)
    await conn.close()

    async def fake_get_farms(ids, **_):
        return {i: FARMS[i] for i in ids if i in FARMS}

    async def fake_leaderboard(farm_id, limit=None):
        top = [{"rank": i, "id": f"user{f}", "count": 1000 - i, "farmId": f} for i, f in enumerate(FARMS, 1)]
        if limit:
            return {"topTen": top}
        return {"topTen": top[:1], "farmRankingDetails": [{"rank": 2500, "id": "far", "count": 5, "farmId": farm_id}]}

    monkeypatch.setattr(sfl_api, "get_farms", fake_get_farms)
    monkeypatch.setattr(sfl_api, "get_ticket_leaderboard", fake_leaderboard)

    p = await db.get_pool()
    yield p
    await db.close_pool()


async def test_full_flow(pool, monkeypatch):
    # --- лидерборд + отслеживаемый фермер вне топа ---
    await pool.execute("INSERT INTO farmers (farm_id, tickets_tracked) VALUES (555, true)")
    stats = await tickets_leaderboard.run()
    assert stats["top"] == 3 and stats["excluded"] == 1
    assert await pool.fetchval("SELECT count(*) FROM top500_snapshots") == 3
    assert await pool.fetchval("SELECT tickets_excluded FROM farmers WHERE farm_id = 555") is True

    # --- быстрый цикл по топу ---
    stats = await top_farms.run()
    assert stats == {"farms": 3, "merchant_visit": -2, "chores": 2, "bounties": 3, "ticket_npcs": 1}
    merchant = await pool.fetchrow("SELECT * FROM cave_merchant_visits")
    assert merchant["visit_index"] == -2 and len(merchant["offers"]) == 2
    assert await pool.fetchval("SELECT count(*) FROM weekly_chores") == 2
    assert await pool.fetchval("SELECT category FROM bounty_requests WHERE id = 'ab12cd'") == "poppy"
    tywin = await pool.fetchrow("SELECT * FROM ticket_delivery_consensus WHERE npc = 'tywin'")
    assert tywin["items"] == {"Gold": 3.0} and tywin["votes"] == 3
    # повторный цикл не дублирует торговца/задания/баунти
    await top_farms.run()
    assert await pool.fetchval("SELECT count(*) FROM cave_merchant_visits") == 1
    assert await pool.fetchval("SELECT count(*) FROM bounty_requests") == 3
    assert await pool.fetchval("SELECT count(*) FROM collector_runs WHERE status = 'ok'") == 3

    # --- аукцион закончился 2 часа назад ---
    end = datetime.now(timezone.utc) - timedelta(hours=2)
    await pool.execute(
        "INSERT INTO auctions (auction_id, item_name, item_type, supply, end_at) VALUES ('auc1','Pumpkin Hat','wearable',2,$1)",
        end,
    )
    await pool.execute(
        "INSERT INTO auction_results (auction_id, supply, leaderboard) VALUES ('auc1', 2, $1)",
        [{"rank": 1, "farmId": 10}, {"rank": 2, "farmId": 99}, {"rank": 3, "farmId": 20}],
    )
    stats = await item_checks.run_pass()
    assert stats["auction"] == 2 and stats["errors"] == 0  # auction_end + plus_1h, plus_24h ещё рано

    checks = {r["trigger"]: r for r in await pool.fetch("SELECT * FROM item_checks")}
    assert set(checks) == {"auction_end", "auction_plus_1h"}
    winners = await pool.fetch("SELECT farm_id FROM item_check_owners WHERE check_id = $1", checks["auction_end"]["id"])
    assert [r["farm_id"] for r in winners] == [10]  # 99 вне топа, 20 — за пределами supply
    holders = await pool.fetch(
        "SELECT farm_id, amount FROM item_check_owners WHERE check_id = $1 ORDER BY farm_id",
        checks["auction_plus_1h"]["id"],
    )
    assert [(r["farm_id"], float(r["amount"])) for r in holders] == [(10, 1.0), (20, 2.0)]

    # повторный проход ничего не дублирует
    assert (await item_checks.run_pass())["auction"] == 0

    # --- ручная проверка из админки ---
    req_id = await pool.fetchval(
        "INSERT INTO item_check_requests (item_name, item_type, top_n) VALUES ('Pumpkin Hat','wearable',3) RETURNING id"
    )
    assert (await item_checks.run_pass())["manual"] == 1
    req = await pool.fetchrow("SELECT * FROM item_check_requests WHERE id = $1", req_id)
    assert req["status"] == "done" and req["check_id"] is not None
    # кулдаун: вторая ручная проверка ждёт
    await pool.execute("INSERT INTO item_check_requests (item_name, item_type) VALUES ('X','wearable')")
    assert (await item_checks.run_pass())["manual"] == 0

    # --- ночной дамп ---
    async def fake_manifest():
        return [{"filename": "2026-10-27/active.jsonl.gz", "modifiedAt": "2026-10-27T22:00:59.000Z"}]

    async def fake_stream(_filename):
        for fid, farm in FARMS.items():
            yield {"id": fid, "farm": farm, "isBlacklisted": False}
        yield {"id": 777, "farm": make_farm(), "isBlacklisted": True}

    monkeypatch.setattr(sfl_api, "get_dump_manifest", fake_manifest)
    monkeypatch.setattr(sfl_api, "stream_dump", fake_stream)
    stats = await nightly_dump.run()
    assert stats["farms"] == 4 and stats["top_farms_found"] == 3 and stats["item_checks"] == 1
    tango = await pool.fetchrow("SELECT * FROM delivery_stats_daily WHERE npc = 'tango'")
    assert tango["orders"] == 3 and tango["reward_type"] == "coins"  # забаненная ферма не учтена
    assert (await nightly_dump.run())["skipped"] == "already_processed"
