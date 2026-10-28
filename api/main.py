"""
API sfl-collector для сайта goblincodex — только читает БД (в SFL API не ходит).

Сайт обращается к нему по shared-net: http://sfl-collector-api:8000.
Наружу порт не публикуется.

Запуск: uvicorn api.main:app --host 0.0.0.0 --port 8000
"""
import hmac
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from collector import config, db, game_data


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await db.get_pool()
    yield
    await db.close_pool()


app = FastAPI(title="sfl-collector API", version="0.3.0", lifespan=lifespan)


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
