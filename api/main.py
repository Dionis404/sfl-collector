"""
API sfl-collector для сайта goblincodex — только читает БД (в SFL API не ходит).

Сайт обращается к нему по shared-net: http://sfl-collector-api:8000.
Наружу порт не публикуется.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import hmac
from contextlib import asynccontextmanager
from datetime import date
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from collector import config, db


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.get_pool()
    yield
    await db.close_pool()


app = FastAPI(title="sfl-collector API", version="0.2.0", lifespan=lifespan)


def rows(records) -> list[dict]:
    return [dict(r) for r in records]


# --- Служебное ---

@app.get("/health")
async def health():
    pool = await db.get_pool()
    await pool.fetchval("SELECT 1")
    return {"ok": True}


@app.get("/sfl/status")
async def status():
    """Когда каждый сборщик последний раз отработал (для «обновлено N минут назад»)."""
    pool = await db.get_pool()
    return rows(await pool.fetch(
        """
        SELECT DISTINCT ON (job) job, started_at, finished_at, status, stats
        FROM collector_runs ORDER BY job, started_at DESC
        """
    ))


# --- Торговец в пещере ---

@app.get("/sfl/merchant/current")
async def merchant_current():
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT * FROM cave_merchant_visits ORDER BY last_seen DESC, visit_index DESC LIMIT 1")
    if row is None:
        raise HTTPException(404, "Данных о торговце пока нет")
    return dict(row)


@app.get("/sfl/merchant/history")
async def merchant_history(limit: int = Query(20, ge=1, le=200)):
    pool = await db.get_pool()
    return rows(await pool.fetch("SELECT * FROM cave_merchant_visits ORDER BY opens_at DESC NULLS LAST LIMIT $1", limit))


# --- Задания недели ---

@app.get("/sfl/chores/current")
async def chores_current():
    pool = await db.get_pool()
    return rows(await pool.fetch(
        """
        SELECT npc, task, reward, started_at, first_seen, last_seen FROM weekly_chores
        WHERE last_seen = (SELECT max(last_seen) FROM weekly_chores)
        ORDER BY npc
        """
    ))


# --- Баунти-борд ---

@app.get("/sfl/bounties/current")
async def bounties_current(category: Literal["animal", "poppy"] | None = None):
    pool = await db.get_pool()
    return rows(await pool.fetch(
        """
        SELECT id, name, category, level, coins, items, first_seen FROM bounty_requests
        WHERE last_seen = (SELECT max(last_seen) FROM bounty_requests)
          AND ($1::text IS NULL OR category = $1)
        ORDER BY category, name, level
        """,
        category,
    ))


# --- Доставки ---

@app.get("/sfl/deliveries/tickets")
async def deliveries_tickets():
    """Текущие билетные доставки: самый частый заказ у топа + доля (share)."""
    pool = await db.get_pool()
    return rows(await pool.fetch(
        """
        SELECT npc, items, base_tickets, votes, total, share, variants, taken_at
        FROM ticket_delivery_consensus
        WHERE taken_at = (SELECT max(taken_at) FROM ticket_delivery_consensus)
        ORDER BY npc
        """
    ))


@app.get("/sfl/deliveries/stats")
async def deliveries_stats(
    type: Literal["coins", "flower", "tickets"] | None = None,
    npc: str | None = None,
    dump_date: date | None = Query(None, alias="date"),
):
    """Статистика доставок по ночному дампу (по умолчанию — последний дамп)."""
    pool = await db.get_pool()
    day = dump_date or await pool.fetchval("SELECT max(dump_date) FROM delivery_stats_daily")
    if day is None:
        return {"date": None, "rows": []}
    data = await pool.fetch(
        """
        SELECT npc, reward_type, item, orders, avg_reward, min_reward, max_reward
        FROM delivery_stats_daily
        WHERE dump_date = $1 AND ($2::text IS NULL OR reward_type = $2) AND ($3::text IS NULL OR npc = $3)
        ORDER BY reward_type, avg_reward DESC
        """,
        day, type, npc,
    )
    return {"date": day, "rows": rows(data)}


# --- Предметы у топа ---

async def _owners(pool, check_ids: list[int]) -> dict[int, list[dict]]:
    data = await pool.fetch(
        """SELECT check_id, farm_id, rank, username, amount, source
           FROM item_check_owners WHERE check_id = ANY($1::int[]) ORDER BY rank NULLS LAST""",
        check_ids,
    )
    out: dict[int, list[dict]] = {i: [] for i in check_ids}
    for r in data:
        d = dict(r)
        out[d.pop("check_id")].append(d)
    return out


@app.get("/sfl/items/{item_name}/owners")
async def item_owners(item_name: str, history: int = Query(1, ge=1, le=50)):
    """Последние проверки владения предметом (по умолчанию одна — самая свежая)."""
    pool = await db.get_pool()
    checks = rows(await pool.fetch(
        "SELECT * FROM item_checks WHERE item_name = $1 ORDER BY checked_at DESC LIMIT $2",
        item_name, history,
    ))
    if not checks:
        raise HTTPException(404, "Проверок по этому предмету ещё не было")
    owners = await _owners(pool, [c["id"] for c in checks])
    for c in checks:
        c["owners"] = owners[c["id"]]
    return checks


@app.get("/sfl/auctions/{auction_id}/top-owners")
async def auction_top_owners(auction_id: str):
    """Все проверки по аукциону: сразу (победители), +1 ч, +24 ч, ночные — с разницей."""
    pool = await db.get_pool()
    checks = rows(await pool.fetch(
        "SELECT * FROM item_checks WHERE auction_id = $1 ORDER BY checked_at", auction_id
    ))
    if not checks:
        raise HTTPException(404, "Проверок по этому аукциону ещё не было")
    owners = await _owners(pool, [c["id"] for c in checks])
    # new_owners — кто появился с прошлой проверки владения. auction_end (победители)
    # в сравнение не входит: это не полный список владельцев.
    prev: set[int] | None = None
    for c in checks:
        c["owners"] = owners[c["id"]]
        if c["trigger"] == "auction_end":
            c["new_owners"] = []
            continue
        current = {o["farm_id"] for o in c["owners"]}
        c["new_owners"] = sorted(current - prev) if prev is not None else []
        prev = current
    return checks


# --- Админка: ручная проверка ---

def require_admin(x_admin_token: str | None = Header(None)) -> None:
    if not config.ADMIN_TOKEN:
        raise HTTPException(503, "ADMIN_TOKEN не настроен")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(401, "Неверный токен")


class ItemCheckRequest(BaseModel):
    item_name: str = Field(min_length=1, max_length=200)
    item_type: Literal["wearable", "collectible"]
    top_n: int = Field(200, ge=1)


@app.post("/admin/item-checks", dependencies=[Depends(require_admin)], status_code=202)
async def create_item_check(req: ItemCheckRequest):
    """Ставит проверку в очередь; коллектор выполнит её (не чаще раза в 10 минут)."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """INSERT INTO item_check_requests (item_name, item_type, top_n)
           VALUES ($1,$2,$3) RETURNING id, status, created_at""",
        req.item_name.strip(), req.item_type, min(req.top_n, config.API_MAX_TOP_N),
    )
    pending = await pool.fetchval("SELECT count(*) FROM item_check_requests WHERE status = 'pending'")
    return {**dict(row), "queue_position": pending}


@app.get("/admin/item-checks/{request_id}", dependencies=[Depends(require_admin)])
async def get_item_check(request_id: int):
    pool = await db.get_pool()
    row = await pool.fetchrow("SELECT * FROM item_check_requests WHERE id = $1", request_id)
    if row is None:
        raise HTTPException(404, "Нет такого запроса")
    result = dict(row)
    if row["check_id"]:
        result["owners"] = (await _owners(pool, [row["check_id"]]))[row["check_id"]]
    return result
