"""
API sfl-collector для сайта goblincodex — единый API сайта (sfl-api).

Только читает БД (в SFL API не ходит). Единственная запись — постановка
farm_id в farm_refresh_queue: реальные запросы к SFL делает collector
(collector/jobs/farm_refresh.py) под общим RateLimiter.

Сайт обращается по shared-net: http://sfl-api:8000.
Наружу порт не публикуется.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Response
from pydantic import BaseModel, Field

from collector import config, db, game_data


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.get_pool()
    yield
    await db.close_pool()


app = FastAPI(title="sfl-collector API", version="0.4.0", lifespan=lifespan)


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


# --- История ---

@app.get("/sfl/chores/history")
async def chores_history(weeks: int = Query(8, ge=1, le=104)):
    """Задания по неделям, новые сверху: [{started_at, tasks: [...]}]."""
    pool = await db.get_pool()
    data = await pool.fetch(
        """
        SELECT npc, task, reward, started_at, first_seen, last_seen FROM weekly_chores
        WHERE started_at IN (SELECT DISTINCT started_at FROM weekly_chores ORDER BY started_at DESC LIMIT $1)
        ORDER BY started_at DESC, npc
        """,
        weeks,
    )
    out: list[dict] = []
    for r in data:
        d = dict(r)
        if not out or out[-1]["started_at"] != d["started_at"]:
            out.append({"started_at": d["started_at"], "tasks": []})
        d.pop("started_at")
        out[-1]["tasks"].append(d)
    return out


@app.get("/sfl/bounties/history")
async def bounties_history(
    boards: int = Query(8, ge=1, le=200),
    category: Literal["animal", "poppy"] | None = None,
):
    """Составы баунти-борда, новые сверху: [{first_seen, last_seen, requests: [...]}]."""
    pool = await db.get_pool()
    snaps = await pool.fetch(
        "SELECT id, request_ids, first_seen, last_seen FROM bounty_board_snapshots "
        "ORDER BY first_seen DESC, id DESC LIMIT $1",
        boards,
    )
    all_ids = sorted({i for s in snaps for i in s["request_ids"]})
    reqs = {
        r["id"]: dict(r)
        for r in await pool.fetch(
            "SELECT id, name, category, level, coins, items FROM bounty_requests WHERE id = ANY($1::text[])",
            all_ids,
        )
    }
    out = []
    for snap in snaps:
        items = [reqs[i] for i in snap["request_ids"] if i in reqs]
        if category:
            items = [r for r in items if r["category"] == category]
        items.sort(key=lambda r: (r["category"], r["name"] or "", r["level"] or 0))
        out.append({"first_seen": snap["first_seen"], "last_seen": snap["last_seen"], "requests": items})
    return out


@app.get("/sfl/deliveries/tickets/history")
async def tickets_history(npc: str | None = None, days: int = Query(14, ge=1, le=120)):
    """
    Как менялся билетный заказ у NPC: подряд идущие одинаковые снимки склеены
    в один отрезок [{npc, items, base_tickets, from, to, share}], новые сверху.
    """
    pool = await db.get_pool()
    data = await pool.fetch(
        """
        SELECT npc, items, base_tickets, share, taken_at FROM ticket_delivery_consensus
        WHERE taken_at >= now() - make_interval(days => $1) AND ($2::text IS NULL OR npc = $2)
        ORDER BY npc, taken_at
        """,
        days, npc,
    )
    segments: list[dict] = []
    for r in data:
        last = segments[-1] if segments else None
        if last and last["npc"] == r["npc"] and last["items"] == r["items"]:
            last["to"] = r["taken_at"]
        else:
            segments.append({"npc": r["npc"], "items": r["items"], "base_tickets": r["base_tickets"],
                             "share": r["share"], "from": r["taken_at"], "to": r["taken_at"]})
    segments.sort(key=lambda s: (s["npc"], s["from"]), reverse=True)
    return segments


# --- Глава: мутанты и магазин Стеллы ---

def _chapter_public(ch: dict) -> dict:
    return {
        "number": ch["number"], "name": ch["name"], "start": ch["start"], "end": ch["end"],
        "items": game_data.chapter_items(ch),
    }


@app.get("/sfl/chapter/current")
async def chapter_current():
    """Текущая глава и её предметы (мутанты + магазин Стеллы) — из данных игры."""
    ch = game_data.chapter_at()
    if ch is None:
        raise HTTPException(404, "Текущая глава не найдена — обнови collector/data/game_data.json")
    return _chapter_public(ch)


@app.get("/sfl/chapter/items")
async def chapter_items_stats(
    dump_date: date | None = Query(None, alias="date"),
    source: Literal["mutant", "megastore"] | None = None,
):
    """Сколько игроков владеет предметами главы — по ночному дампу (все активные и топ)."""
    pool = await db.get_pool()
    day = dump_date or await pool.fetchval("SELECT max(dump_date) FROM chapter_item_stats_daily")
    if day is None:
        return {"date": None, "chapter": None, "rows": []}
    data = rows(await pool.fetch(
        """
        SELECT chapter, item, item_type, source, kind, tier, holders, total, top_holders, top_total
        FROM chapter_item_stats_daily
        WHERE dump_date = $1 AND ($2::text IS NULL OR source = $2)
        ORDER BY source DESC, kind NULLS LAST, tier NULLS LAST, item
        """,
        day, source,
    ))
    return {"date": day, "chapter": data[0]["chapter"] if data else None, "rows": data}


# --- Топ лидерборда: опыт, уровень, возвышение, отслеживаемые предметы ---

def _as_aware(value: datetime, tz: str) -> datetime:
    """Время без часового пояса считаем заданным в `tz` (по умолчанию UTC)."""
    if value.tzinfo is not None:
        return value
    try:
        return value.replace(tzinfo=ZoneInfo(tz))
    except ZoneInfoNotFoundError:
        raise HTTPException(422, f"Неизвестный часовой пояс: {tz}")


async def _snapshot_time(pool, at: datetime) -> datetime | None:
    """Последний снимок топ-500, сделанный не позже `at`."""
    return await pool.fetchval("SELECT max(taken_at) FROM top500_snapshots WHERE taken_at <= $1", at)


@app.get("/sfl/top/snapshot")
async def top_snapshot(
    at: datetime = Query(..., description="Момент: 2026-10-26T03:00 (+ tz) или 2026-10-26T03:00+03:00"),
    tz: str = Query("UTC", description="Часовой пояс для `at` без смещения, напр. Europe/Moscow"),
    limit: int = Query(500, ge=1, le=500),
):
    """
    Как выглядел топ-500 лидерборда тикетов в момент `at` — берётся последний
    почасовой снимок, сделанный не позже этого момента.
    """
    pool = await db.get_pool()
    at = _as_aware(at, tz)
    taken_at = await _snapshot_time(pool, at)
    if taken_at is None:
        raise HTTPException(404, "Снимков лидерборда до этого момента нет")
    data = await pool.fetch(
        """SELECT rank, farm_id, game_username AS username, tickets FROM top500_snapshots
           WHERE taken_at = $1 AND rank <= $2 ORDER BY rank""",
        taken_at, limit,
    )
    return {"requested_at": at, "taken_at": taken_at, "lag_minutes": round((at - taken_at).total_seconds() / 60),
            "rows": rows(data)}


@app.get("/sfl/top/compare")
async def top_compare(
    since: datetime = Query(..., alias="from", description="Начало: напр. 2026-10-25T03:00"),
    until: datetime = Query(..., alias="to", description="Конец: напр. 2026-10-26T03:00"),
    tz: str = Query("UTC", description="Часовой пояс для времени без смещения, напр. Europe/Moscow"),
    limit: int = Query(500, ge=1, le=500),
):
    """
    Сравнение топа в два момента (напр. ночь с вс на пн): для каждого игрока из топа
    на момент `to` — место и билеты тогда и сейчас, изменение места и сколько билетов
    набрал. Кто был в топе в `from`, но выпал к `to`, — в `dropped_out`.
    """
    pool = await db.get_pool()
    since, until = _as_aware(since, tz), _as_aware(until, tz)
    if since >= until:
        raise HTTPException(422, "`from` должен быть раньше `to`")
    t_from, t_to = await _snapshot_time(pool, since), await _snapshot_time(pool, until)
    if t_to is None or t_from is None:
        raise HTTPException(404, "Нет снимка лидерборда на один из моментов")
    if t_from == t_to:
        raise HTTPException(404, "На оба момента приходится один и тот же снимок — между ними обновлений не было")

    before = {r["farm_id"]: r for r in await pool.fetch(
        "SELECT rank, farm_id, game_username, tickets FROM top500_snapshots WHERE taken_at = $1", t_from)}
    after = await pool.fetch(
        "SELECT rank, farm_id, game_username, tickets FROM top500_snapshots WHERE taken_at = $1 AND rank <= $2 ORDER BY rank",
        t_to, limit,
    )
    out = []
    for r in after:
        b = before.get(r["farm_id"])
        out.append({
            "farm_id": r["farm_id"], "username": r["game_username"],
            "rank": r["rank"], "tickets": r["tickets"],
            "rank_before": b["rank"] if b else None, "tickets_before": b["tickets"] if b else None,
            "rank_change": (b["rank"] - r["rank"]) if b else None,      # >0 — поднялся
            "tickets_gained": (r["tickets"] - b["tickets"]) if b else None,
            "new_in_top": b is None,
        })
    after_ids = {r["farm_id"] for r in await pool.fetch("SELECT farm_id FROM top500_snapshots WHERE taken_at = $1", t_to)}
    dropped = [
        {"farm_id": fid, "username": b["game_username"], "rank_before": b["rank"], "tickets_before": b["tickets"]}
        for fid, b in sorted(before.items(), key=lambda kv: kv[1]["rank"]) if fid not in after_ids
    ]
    return {"from": {"requested_at": since, "taken_at": t_from},
            "to": {"requested_at": until, "taken_at": t_to},
            "rows": out, "dropped_out": dropped}


@app.get("/sfl/top/snapshots")
async def top_snapshot_times(
    day: date = Query(..., alias="date", description="День, напр. 2026-10-26"),
    tz: str = Query("UTC", description="Часовой пояс для границ дня, напр. Europe/Moscow"),
):
    """Когда делались снимки топа в этот день — чтобы выбрать момент для /sfl/top/snapshot."""
    pool = await db.get_pool()
    start = _as_aware(datetime.combine(day, datetime.min.time()), tz)
    data = await pool.fetch(
        "SELECT DISTINCT taken_at FROM top500_snapshots WHERE taken_at >= $1 AND taken_at < $2 ORDER BY taken_at",
        start, start + timedelta(days=1),
    )
    return [r["taken_at"] for r in data]


@app.get("/sfl/top/latest")
async def top_latest(limit: int = Query(500, ge=1, le=500)):
    """Опыт/уровень/возвышение топа — обновляется каждый час из лидерборда."""
    pool = await db.get_pool()
    return rows(await pool.fetch("SELECT * FROM top_player_latest ORDER BY rank LIMIT $1", limit))


@app.get("/sfl/top/profiles")
async def top_profiles(
    dump_date: date | None = Query(None, alias="date"),
    limit: int = Query(500, ge=1, le=500),
):
    """Профили топа по ночному дампу: опыт, уровень, возвышение и предметы главы (Grinx's Hammer и т.п.)."""
    pool = await db.get_pool()
    day = dump_date or await pool.fetchval("SELECT max(dump_date) FROM top_player_profiles")
    if day is None:
        return {"date": None, "rows": []}
    data = await pool.fetch(
        "SELECT * FROM top_player_profiles WHERE dump_date = $1 ORDER BY rank NULLS LAST LIMIT $2", day, limit,
    )
    return {"date": day, "rows": rows(data)}


@app.get("/sfl/top/watched/{item_name}")
async def top_watched_owners(item_name: str, dump_date: date | None = Query(None, alias="date")):
    """Кто из топа владеет отслеживаемым предметом (напр. Grinx's Hammer) — по последнему дампу."""
    pool = await db.get_pool()
    day = dump_date or await pool.fetchval(
        "SELECT max(dump_date) FROM top_player_profiles WHERE watched_items ? $1", item_name
    )
    if day is None:
        raise HTTPException(404, "Этот предмет у топа ещё не отслеживался")
    owners = rows(await pool.fetch(
        """
        SELECT farm_id, rank, username, (watched_items ->> $2)::numeric AS amount
        FROM top_player_profiles
        WHERE dump_date = $1 AND (watched_items ->> $2)::numeric > 0
        ORDER BY rank NULLS LAST
        """,
        day, item_name,
    ))
    checked = await pool.fetchval(
        "SELECT count(*) FROM top_player_profiles WHERE dump_date = $1 AND watched_items ? $2", day, item_name
    )
    return {"item": item_name, "date": day, "farms_checked": checked, "owners_count": len(owners), "owners": owners}


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


# =====================================================================
# Цены маркетплейса (sfl_resource_prices пишет collector/jobs/marketplace.py)
# =====================================================================

PRICE_CATEGORIES = ("collectibles", "wearables", "buds", "pets", "economies")
PRICE_NUMERIC_FIELDS = (
    "floor", "low", "high", "latest_sale", "best_offer",
    "volume", "quantity", "flower_price",
)


def _price_row(r) -> dict:
    d = dict(r)
    for f in PRICE_NUMERIC_FIELDS:
        if d.get(f) is not None:
            d[f] = float(d[f])
    floor, flower = d.get("floor"), d.get("flower_price")
    sale = d.get("latest_sale")
    d["floor_usd"] = floor * flower if floor is not None and flower is not None else None
    d["latest_sale_usd"] = sale * flower if sale is not None and flower is not None else None
    return d


def _category_where(category: str | None) -> tuple[str, list]:
    if category:
        return "WHERE item_key LIKE $1 || '-%'", [category]
    return "", []


@app.get("/sfl/prices/resources")
async def price_resources(
    category: Literal["collectibles", "wearables", "buds", "pets", "economies"] | None = None,
    limit: int = Query(5000, ge=1, le=20000),
    offset: int = Query(0, ge=0),
):
    """Список отслеживаемых предметов (item_key + имя), по данным в БД."""
    where, params = _category_where(category)
    pool = await db.get_pool()
    return rows(await pool.fetch(
        f"""SELECT DISTINCT item_key, item_name FROM sfl_resource_prices
            {where} ORDER BY item_name LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
        *params, limit, offset,
    ))


@app.get("/sfl/prices/latest")
async def prices_latest(
    category: Literal["collectibles", "wearables", "buds", "pets", "economies"] | None = None,
    limit: int = Query(5000, ge=1, le=20000),
    offset: int = Query(0, ge=0),
):
    """Последний известный снимок цены по каждому предмету."""
    where, params = _category_where(category)
    pool = await db.get_pool()
    data = await pool.fetch(
        f"""SELECT DISTINCT ON (item_key)
                item_key, item_name, captured_at, snapshot_date,
                floor, low, high, latest_sale, best_offer,
                volume, trades, quantity, listing_count, offer_count, flower_price
            FROM sfl_resource_prices
            {where}
            ORDER BY item_key, captured_at DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}""",
        *params, limit, offset,
    )
    return [_price_row(r) for r in data]


@app.get("/sfl/prices/flower-rate")
async def flower_rate(hours: int = Query(2160, ge=1, le=24 * 365)):
    """История курса $/FLOWER по возрастанию времени (flower_price_history)."""
    pool = await db.get_pool()
    data = await pool.fetch(
        """SELECT captured_at, usd FROM flower_price_history
           WHERE captured_at >= now() - make_interval(hours => $1)
           ORDER BY captured_at""",
        hours,
    )
    return [{"captured_at": r["captured_at"], "usd": float(r["usd"])} for r in data]


@app.get("/sfl/prices/{item_key}/latest")
async def price_latest_for_item(item_key: str):
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """SELECT item_key, item_name, captured_at, snapshot_date,
                  floor, low, high, latest_sale, best_offer,
                  volume, trades, quantity, listing_count, offer_count, flower_price
           FROM sfl_resource_prices
           WHERE item_key = $1
           ORDER BY captured_at DESC LIMIT 1""",
        item_key,
    )
    if row is None:
        raise HTTPException(404, f"Нет данных по {item_key}")
    return _price_row(row)


@app.get("/sfl/prices/{item_key}/history")
async def price_history(
    item_key: str,
    hours: int = Query(24, ge=1, le=24 * 90),
    limit: int = Query(2000, ge=1, le=20000),
):
    pool = await db.get_pool()
    data = await pool.fetch(
        """SELECT item_key, item_name, captured_at, snapshot_date,
                  floor, low, high, latest_sale, best_offer,
                  volume, trades, quantity, listing_count, offer_count, flower_price
           FROM sfl_resource_prices
           WHERE item_key = $1 AND captured_at >= now() - make_interval(hours => $2)
           ORDER BY captured_at LIMIT $3""",
        item_key, hours, limit,
    )
    if not data:
        raise HTTPException(404, f"Нет данных по {item_key} за последние {hours}ч")
    return [_price_row(r) for r in data]


# =====================================================================
# Кэш ферм для калькулятора сайта (farm_cache; обновляет collector из
# farm_refresh_queue — см. collector/jobs/farm_refresh.py)
# =====================================================================

MAX_FARM_ID = 2**63 - 1  # farm_id хранится как BIGINT
FARMS_MAX_IDS = 100
FARM_STALE_AFTER = timedelta(hours=6)
FARM_FORCE_REFRESH_COOLDOWN = timedelta(minutes=5)
FARM_WAIT_SEC = 10.0  # сколько ждём первое появление фермы из очереди

FarmId = Annotated[int, Path(gt=0, le=MAX_FARM_ID)]

# Отрицательный кэш несуществующих ферм (свой в этом процессе; у worker'а —
# свой). Час — как в collector/jobs/farm_refresh.py.
_NOT_FOUND_TTL_SEC = 3600
_not_found_until: dict[int, float] = {}


def _farm_known_missing(farm_id: int) -> bool:
    until = _not_found_until.get(farm_id)
    if until is None:
        return False
    if until < time.monotonic():
        del _not_found_until[farm_id]
        return False
    return True


async def _enqueue_farm(pool, farm_id: int) -> None:
    await pool.execute(
        "INSERT INTO farm_refresh_queue (farm_id) VALUES ($1)"
        " ON CONFLICT (farm_id) DO NOTHING",
        farm_id,
    )


async def _get_farm_row(pool, farm_id: int):
    return await pool.fetchrow("SELECT * FROM farm_cache WHERE farm_id = $1", farm_id)


async def _touch_farm(pool, farm_id: int) -> None:
    await pool.execute(
        "UPDATE farm_cache SET last_requested_at = now() WHERE farm_id = $1", farm_id
    )


def _farm_is_stale(updated_at) -> bool:
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated_at > FARM_STALE_AFTER


@app.get("/farm/{farm_id}")
async def get_farm(farm_id: FarmId):
    """Данные фермы из кэша; null — данных пока нет (обновление поставлено в очередь)."""
    pool = await db.get_pool()
    row = await _get_farm_row(pool, farm_id)

    if row is None or row["data"] is None:
        if _farm_known_missing(farm_id):
            return None
        await _enqueue_farm(pool, farm_id)
        # Первое обращение: ждём worker несколько секунд, отдадим сразу, если успеет.
        deadline = time.monotonic() + FARM_WAIT_SEC
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            row = await _get_farm_row(pool, farm_id)
            if row is not None and row["data"] is not None:
                break
        if row is None or row["data"] is None:
            return None
        await _touch_farm(pool, farm_id)
        return row["data"]

    await _touch_farm(pool, farm_id)
    if _farm_is_stale(row["updated_at"]):
        await _enqueue_farm(pool, farm_id)
    return row["data"]


@app.get("/farms")
async def get_farms(ids: str):
    """Батч до 100 id: {"<id>": {"data": {...}, "updated_at": "..."} | null}."""
    try:
        farm_ids = sorted({int(x) for x in ids.split(",") if x.strip()})
    except ValueError:
        raise HTTPException(400, "ids должен быть списком чисел через запятую")
    if not farm_ids:
        return {}
    if len(farm_ids) > FARMS_MAX_IDS:
        raise HTTPException(400, f"Не больше {FARMS_MAX_IDS} id за запрос")
    if not all(0 < fid <= MAX_FARM_ID for fid in farm_ids):
        raise HTTPException(400, "Некорректный id фермы")

    pool = await db.get_pool()
    data = await pool.fetch(
        "SELECT * FROM farm_cache WHERE farm_id = ANY($1::bigint[])", farm_ids
    )
    by_id = {r["farm_id"]: r for r in data}

    result = {}
    for fid in farm_ids:
        row = by_id.get(fid)
        if row is None or row["data"] is None:
            if not _farm_known_missing(fid):
                await _enqueue_farm(pool, fid)
            result[str(fid)] = None
            continue
        if _farm_is_stale(row["updated_at"]):
            await _enqueue_farm(pool, fid)
        result[str(fid)] = {"data": row["data"], "updated_at": row["updated_at"]}
        await _touch_farm(pool, fid)
    return result


@app.post("/farm/{farm_id}/refresh")
async def force_refresh_farm(farm_id: FarmId):
    """Форс-обновление через очередь; свежую запись (< 5 мин) повторно не тянем."""
    pool = await db.get_pool()
    row = await _get_farm_row(pool, farm_id)
    if row is not None and row["data"] is not None:
        updated_at = row["updated_at"]
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - updated_at < FARM_FORCE_REFRESH_COOLDOWN:
            return {"status": "fresh"}
    if _farm_known_missing(farm_id):
        return {"status": "skipped"}
    await _enqueue_farm(pool, farm_id)
    return {"status": "refreshing"}


# =====================================================================
# Лидерборд тикетов (top500_snapshots пишет collector/jobs/tickets_leaderboard.py)
# =====================================================================

@app.get("/api/tickets/top500")
async def tickets_top500(at: datetime | None = None):
    """
    Снэпшот глобального топ-500 лидерборда тикетов.
    Без `at` — последний; с `at` (ISO 8601) — ближайший, но не позже него.
    """
    pool = await db.get_pool()
    if at is None:
        taken_at = await pool.fetchval("SELECT max(taken_at) FROM top500_snapshots")
    else:
        taken_at = await pool.fetchval(
            "SELECT max(taken_at) FROM top500_snapshots WHERE taken_at <= $1", at
        )
    if taken_at is None:
        return {"updated_at": None, "leaderboard": []}
    data = await pool.fetch(
        """SELECT rank, farm_id, game_username, tickets FROM top500_snapshots
           WHERE taken_at = $1 ORDER BY rank""",
        taken_at,
    )
    return {"updated_at": taken_at, "leaderboard": rows(data)}


# =====================================================================
# Аукционы (auctions / auction_results пишет collector/jobs/auctions.py)
# =====================================================================

@app.get("/api/auctions")
async def api_auctions(
    upcoming: bool = False,
    history: bool = False,
    limit: int = Query(20, ge=1, le=100),
    item_name: str | None = Query(None, max_length=100),
):
    if upcoming and history:
        raise HTTPException(400, "upcoming и history взаимоисключающие")
    if not upcoming and not history:
        raise HTTPException(400, "Укажите upcoming=true или history=true")
    if item_name is not None and upcoming:
        raise HTTPException(400, "item_name поддерживается только с history=true")

    pool = await db.get_pool()
    if upcoming:
        data = await pool.fetch(
            """SELECT auction_id, item_name, item_type, supply,
                      sfl_price, ingredients, start_at, end_at
               FROM auctions WHERE start_at > now() ORDER BY start_at ASC"""
        )
    elif item_name is not None:
        # ILIKE без экранирования превращал "%"/"_" из запроса в wildcard'ы
        pattern = item_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        data = await pool.fetch(
            """SELECT auction_id, item_name, item_type, supply,
                      sfl_price, ingredients, start_at, end_at
               FROM auctions WHERE start_at <= now() AND item_name ILIKE $2
               ORDER BY start_at DESC LIMIT $1""",
            limit, pattern,
        )
    else:
        data = await pool.fetch(
            """SELECT auction_id, item_name, item_type, supply,
                      sfl_price, ingredients, start_at, end_at
               FROM auctions WHERE start_at <= now()
               ORDER BY start_at DESC LIMIT $1""",
            limit,
        )

    return [
        {
            "auction_id": r["auction_id"],
            "item_name": r["item_name"],
            "item_type": r["item_type"],
            "supply": r["supply"],
            "sfl_price": float(r["sfl_price"]) if r["sfl_price"] is not None else None,
            "ingredients": r["ingredients"],
            "start_at": r["start_at"],
            "end_at": r["end_at"],
        }
        for r in data
    ]


@app.get("/api/auctions/{auction_id}/results")
async def api_auction_results(auction_id: str):
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """SELECT a.item_name, a.item_type,
                  r.my_status, r.participant_count, r.supply, r.leaderboard, r.fetched_at
           FROM auction_results r
           JOIN auctions a ON a.auction_id = r.auction_id
           WHERE r.auction_id = $1""",
        auction_id,
    )
    if row is None:
        raise HTTPException(404, "Результаты аукциона ещё не готовы")
    return dict(row)


# =====================================================================
# Сообщество (farmers пишет goblin-bot, статы обновляет коллектор из дампа)
# =====================================================================

@app.get("/community/farmers")
async def community_farmers():
    """Список привязанных фермеров для страницы сообщества, по XP."""
    pool = await db.get_pool()
    data = await pool.fetch("SELECT * FROM farmers ORDER BY xp DESC NULLS LAST")
    return [
        {
            "farm_id": r["farm_id"],
            "game_username": r["game_username"],
            "telegram_username": r["telegram_username"],
            "xp": float(r["xp"]) if r["xp"] is not None else None,
            "balance": float(r["balance"]) if r["balance"] is not None else None,
            "coins": float(r["coins"]) if r["coins"] is not None else None,
            "farm_url": r["farm_url"],
        }
        for r in data
    ]


@app.get("/api/community/posts/{post_id}/image")
async def community_post_image(post_id: Annotated[int, Path(gt=0, le=MAX_FARM_ID)]):
    """Картинка поста канала @URGSFL (telegram_posts пишет goblin-bot)."""
    pool = await db.get_pool()
    row = await pool.fetchrow(
        "SELECT image_data, image_content_type FROM telegram_posts WHERE id = $1",
        post_id,
    )
    if row is None or row["image_data"] is None:
        raise HTTPException(404, "Картинка не найдена")
    return Response(
        content=row["image_data"],
        media_type=row["image_content_type"] or "image/jpeg",
    )


# =====================================================================
# Лидерборд билетов главы: победители аукционов и динамика топа
# =====================================================================

@app.get("/api/auctions/winners")
async def api_auction_winners(start: datetime, end: datetime):
    """Победители аукционов диапазона [start, end) с валютой выигрышной ставки.

    Победители — первые `supply` строк leaderboard (столько экземпляров
    разыгрывается). Валюта ставки: sfl > 0 -> "flower", items.Gem -> "gems",
    иначе название первого ингредиента (или "tickets", если ставка в тикетах).
    Используется вкладкой главы на сайте: пометки, кто уже взял предметы.
    """
    if end <= start:
        raise HTTPException(400, "end должен быть позже start")
    pool = await db.get_pool()
    data = await pool.fetch(
        """SELECT r.auction_id, a.item_name, r.supply, r.leaderboard
           FROM auction_results r
           JOIN auctions a ON a.auction_id = r.auction_id
           WHERE a.start_at >= $1 AND a.start_at < $2
           ORDER BY a.start_at""",
        start, end,
    )
    winners = []
    for r in data:
        supply = r["supply"] or 1
        lb = sorted(r["leaderboard"] or [], key=lambda e: e.get("rank") or 999999)
        for entry in lb[:supply]:
            items = entry.get("items") or {}
            if entry.get("sfl"):
                currency, amount = "flower", entry["sfl"]
            elif items.get("Gem"):
                currency, amount = "gems", items["Gem"]
            elif items:
                currency, amount = next(iter(items.items()))
            else:
                currency, amount = "tickets", entry.get("tickets")
            winners.append({
                "auction_id": r["auction_id"],
                "item_name": r["item_name"],
                "farm_id": entry.get("farmId"),
                "username": entry.get("username"),
                "currency": currency,
                "amount": amount,
            })
    return winners


@app.get("/api/tickets/top500/changes")
async def tickets_top500_changes(hours: int = Query(24, ge=1, le=24 * 14)):
    """Динамика топ-500 билетов за hours часов.

    dropped — были в топ-500 на прошлом снимке, на свежем их нет
    (вылетели из топа). fallen — остались, но просели на >= 50 позиций.
    Сравнение с ближайшим снимком не позже now() - hours.
    """
    pool = await db.get_pool()
    latest = await pool.fetchval("SELECT max(taken_at) FROM top500_snapshots")
    if latest is None:
        return {"updated_at": None, "compared_to": None, "dropped": [], "fallen": []}
    prev = await pool.fetchval(
        "SELECT max(taken_at) FROM top500_snapshots WHERE taken_at <= $1",
        latest - timedelta(hours=hours),
    )
    if prev is None or prev == latest:
        return {"updated_at": latest, "compared_to": None, "dropped": [], "fallen": []}

    new_rows = await pool.fetch(
        "SELECT rank, farm_id, game_username, tickets FROM top500_snapshots WHERE taken_at = $1",
        latest,
    )
    old_rows = await pool.fetch(
        "SELECT rank, farm_id, game_username, tickets FROM top500_snapshots WHERE taken_at = $1",
        prev,
    )
    new_by_farm = {r["farm_id"]: r for r in new_rows}

    dropped, fallen = [], []
    for o in old_rows:
        n = new_by_farm.get(o["farm_id"])
        if n is None:
            dropped.append({
                "farm_id": o["farm_id"],
                "username": o["game_username"],
                "old_rank": o["rank"],
                "old_tickets": o["tickets"],
            })
        elif n["rank"] - o["rank"] >= 50:
            fallen.append({
                "farm_id": o["farm_id"],
                "username": n["game_username"] or o["game_username"],
                "old_rank": o["rank"],
                "rank": n["rank"],
                "tickets": n["tickets"],
            })
    dropped.sort(key=lambda x: x["old_rank"])
    fallen.sort(key=lambda x: x["rank"] - x["old_rank"], reverse=True)
    return {
        "updated_at": latest,
        "compared_to": prev,
        "dropped": dropped,
        "fallen": fallen,
    }


# --- Суточный архив изменений топ-500 (collector/jobs/top500_changes_archive.py) ---

@app.get("/api/tickets/top500/changes/daily")
async def tickets_top500_changes_daily_days(limit: int = Query(30, ge=1, le=90)):
    """Дни, за которые есть архив изменений топ-500, с количеством выбывших/просевших."""
    pool = await db.get_pool()
    return rows(await pool.fetch(
        """SELECT day,
                  count(*) FILTER (WHERE kind = 'dropped') AS dropped,
                  count(*) FILTER (WHERE kind = 'fallen') AS fallen
           FROM top500_daily_changes
           GROUP BY day ORDER BY day DESC LIMIT $1""",
        limit,
    ))


@app.get("/api/tickets/top500/changes/daily/{day}")
async def tickets_top500_changes_daily(day: date = Path(...)):
    """Кто выбыл из топ-500 и кто сильно просел в конкретный день (UTC)."""
    pool = await db.get_pool()
    records = rows(await pool.fetch(
        """SELECT farm_id, username, kind, old_rank, old_tickets, new_rank, new_tickets
           FROM top500_daily_changes WHERE day = $1
           ORDER BY kind, old_rank""",
        day,
    ))
    dropped = [{
        "farm_id": r["farm_id"], "username": r["username"],
        "old_rank": r["old_rank"], "old_tickets": r["old_tickets"],
    } for r in records if r["kind"] == "dropped"]
    fallen = [{
        "farm_id": r["farm_id"], "username": r["username"],
        "old_rank": r["old_rank"], "rank": r["new_rank"], "tickets": r["new_tickets"],
    } for r in records if r["kind"] == "fallen"]
    return {"day": day, "dropped": dropped, "fallen": fallen}
