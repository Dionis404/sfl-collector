"""
Тесты API на настоящем Postgres (как test_integration_db.py):
    COLLECTOR_TEST_DB="host:port:dbname:user:password" pytest -q
"""
import os
from datetime import date, datetime, timedelta, timezone

import asyncpg
import httpx
import pytest

from collector import config, db, migrate

DSN = os.environ.get("COLLECTOR_TEST_DB")
pytestmark = pytest.mark.skipif(not DSN, reason="COLLECTOR_TEST_DB не задан")


@pytest.fixture
async def client(monkeypatch):
    host, port, name, user, password = DSN.split(":")
    for k, v in dict(DB_HOST=host, DB_PORT=int(port), DB_NAME=name, DB_USER=user, DB_PASSWORD=password).items():
        monkeypatch.setattr(config, k, v)
    monkeypatch.setattr(config, "ADMIN_TOKEN", "secret")

    conn = await asyncpg.connect(host=host, port=int(port), database=name, user=user, password=password)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await migrate.apply_migrations(conn)
    await conn.close()

    pool = await db.get_pool()
    await seed(pool)

    from api.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        c.pool = pool
        yield c
    await db.close_pool()


async def seed(pool):
    t1 = datetime(2026, 10, 28, 10, tzinfo=timezone.utc)
    t2 = t1 + timedelta(hours=1)
    await pool.execute(
        """INSERT INTO cave_merchant_visits (visit_index, opens_at, offers, last_seen)
           VALUES (-3, $1, '[]', $1), (-2, $2, '[{"slot": 1}]', $2)""", t1 - timedelta(days=3), t1,
    )
    await pool.execute(
        """INSERT INTO weekly_chores (started_at, npc, task, last_seen) VALUES
           ($1, 'old', 'old task', $1), ($2, 'peggy', 'Harvest Pepper', $3)""",
        t1 - timedelta(days=7), t1, t2,
    )
    await pool.execute(
        """INSERT INTO bounty_requests (id, name, category, last_seen) VALUES
           ('a', 'Chicken', 'animal', $1), ('b', 'Blue Daffodil', 'poppy', $1), ('old', 'Cow', 'animal', $2)""",
        t2, t1,
    )
    await pool.execute(
        """INSERT INTO ticket_delivery_consensus (taken_at, npc, items, base_tickets, votes, total, share, variants)
           VALUES ($1, 'tywin', '{"Gold": 1}', 10, 1, 1, 1, '[]'),
                  ($2, 'tywin', '{"Gold": 3}', 10, 9, 10, 0.9, '[]')""", t1, t2,
    )
    await pool.execute(
        """INSERT INTO delivery_stats_daily (dump_date, npc, reward_type, item, orders, avg_reward) VALUES
           ('2026-10-27', 'grimbly', 'flower', 'Mashed Potato', 100, 0.35),
           ('2026-10-27', 'tango', 'coins', 'Banana', 50, 634)"""
    )
    # аукцион: победители, затем две проверки владения
    for trig, minutes, owners in (
        ("auction_end", 0, [(10, "auction_winner")]),
        ("auction_plus_1h", 60, [(10, "holdings"), (20, "holdings")]),
        ("auction_plus_24h", 1440, [(10, "holdings"), (20, "holdings"), (30, "holdings")]),
    ):
        cid = await pool.fetchval(
            """INSERT INTO item_checks (item_name, item_type, top_n, trigger, auction_id, checked_at)
               VALUES ('Pumpkin Hat', 'wearable', 200, $1, 'auc1', $2) RETURNING id""",
            trig, t1 + timedelta(minutes=minutes),
        )
        await pool.executemany(
            "INSERT INTO item_check_owners (check_id, farm_id, rank, username, amount, source) VALUES ($1,$2,$3,$4,1,$5)",
            [(cid, fid, fid // 10, f"user{fid}", src) for fid, src in owners],
        )
    await pool.execute("INSERT INTO collector_runs (job, status) VALUES ('top_farms', 'ok')")


async def test_current_data(client):
    r = await client.get("/sfl/merchant/current")
    assert r.status_code == 200 and r.json()["visit_index"] == -2

    assert len((await client.get("/sfl/merchant/history")).json()) == 2

    chores = (await client.get("/sfl/chores/current")).json()
    assert [c["npc"] for c in chores] == ["peggy"]

    bounties = (await client.get("/sfl/bounties/current")).json()
    assert {b["id"] for b in bounties} == {"a", "b"}
    poppy = (await client.get("/sfl/bounties/current", params={"category": "poppy"})).json()
    assert [b["id"] for b in poppy] == ["b"]
    assert (await client.get("/sfl/bounties/current", params={"category": "x"})).status_code == 422

    tickets = (await client.get("/sfl/deliveries/tickets")).json()
    assert tickets[0]["items"] == {"Gold": 3} and float(tickets[0]["share"]) == 0.9

    stats = (await client.get("/sfl/deliveries/stats", params={"type": "flower"})).json()
    assert stats["date"] == "2026-10-27" and [s["npc"] for s in stats["rows"]] == ["grimbly"]

    st = (await client.get("/sfl/status")).json()
    assert st[0]["job"] == "top_farms"
    assert (await client.get("/health")).json() == {"ok": True}


async def test_items_and_auction(client):
    latest = (await client.get("/sfl/items/Pumpkin Hat/owners")).json()
    assert len(latest) == 1 and latest[0]["trigger"] == "auction_plus_24h"
    assert [o["farm_id"] for o in latest[0]["owners"]] == [10, 20, 30]
    assert (await client.get("/sfl/items/Nothing/owners")).status_code == 404

    checks = (await client.get("/sfl/auctions/auc1/top-owners")).json()
    assert [c["trigger"] for c in checks] == ["auction_end", "auction_plus_1h", "auction_plus_24h"]
    assert [c["new_owners"] for c in checks] == [[], [], [30]]
    assert (await client.get("/sfl/auctions/none/top-owners")).status_code == 404


async def test_admin_item_check(client):
    body = {"item_name": "Golden Gnome", "item_type": "collectible", "top_n": 9999}
    assert (await client.post("/admin/item-checks", json=body)).status_code == 401
    assert (await client.post("/admin/item-checks", json=body, headers={"X-Admin-Token": "bad"})).status_code == 401
    bad_type = {**body, "item_type": "nft"}
    assert (await client.post("/admin/item-checks", json=bad_type, headers={"X-Admin-Token": "secret"})).status_code == 422

    r = await client.post("/admin/item-checks", json=body, headers={"X-Admin-Token": "secret"})
    assert r.status_code == 202 and r.json()["status"] == "pending" and r.json()["queue_position"] == 1
    req_id = r.json()["id"]
    assert await client.pool.fetchval("SELECT top_n FROM item_check_requests WHERE id = $1", req_id) == 500

    got = (await client.get(f"/admin/item-checks/{req_id}", headers={"X-Admin-Token": "secret"})).json()
    assert got["item_name"] == "Golden Gnome" and "owners" not in got
