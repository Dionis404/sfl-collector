"""
Проверки «у кого из топ-N лидерборда есть предмет X».

Три источника задач:
1. Окончание аукциона (таблица auctions от Аукциониста, та же БД sfl):
   - auction_end: победители из auction_results.leaderboard ∩ топ-N —
     без запросов к API, как только Аукционист сохранил результаты;
   - auction_plus_{H}h: фактическое владение через H часов после конца
     (по умолчанию 1 и 24) — ловит и тех, кто докупил на маркетплейсе.
2. Ручные проверки из админки сайта: строка в item_check_requests.
3. Ночной дамп (jobs/nightly_dump.py) вызывает save_check() с уже готовыми фермами.

Аукционы типа nft (питомцы, buds) пока пропускаются: у них свои структуры
в ферме (farm.pets / farm.buds), их нужно отдельно сверить на реальных данных.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import asyncpg

from collector import config, db, sfl_api
from collector.parsers.items import SUPPORTED_TYPES, auction_winners, find_owners
from collector.runs import latest_top, tracked_run

log = logging.getLogger(__name__)


def delay_trigger(hours: float) -> str:
    return f"auction_plus_{hours:g}h"


async def save_check(
    pool: asyncpg.Pool,
    *,
    item: str,
    item_type: str,
    top: list,
    farms_checked: int,
    owners: dict[int, float],
    trigger: str,
    auction_id: str | None = None,
    source: str = "holdings",
) -> int:
    """Сохраняет результат проверки, возвращает item_checks.id."""
    by_id = {r["farm_id"]: r for r in top}
    async with pool.acquire() as con, con.transaction():
        check_id = await con.fetchval(
            """
            INSERT INTO item_checks (item_name, item_type, top_n, trigger, auction_id,
                                     leaderboard_taken_at, farms_checked, owners_count)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id
            """,
            item, item_type, len(top), trigger, auction_id,
            top[0]["taken_at"] if top else None, farms_checked, len(owners),
        )
        await con.executemany(
            """INSERT INTO item_check_owners (check_id, farm_id, rank, username, amount, source)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            [
                (check_id, fid, by_id[fid]["rank"], by_id[fid]["game_username"], amount, source)
                for fid, amount in owners.items()
                if fid in by_id
            ],
        )
    return check_id


class TopFarmsCache:
    """Фермы топа, загруженные один раз за проход (несколько проверок — одни запросы)."""

    def __init__(self, pool: asyncpg.Pool):
        self._pool = pool
        self._top: list | None = None
        self._farms: dict[int, dict] = {}

    async def get(self, top_n: int) -> tuple[list, dict[int, dict]]:
        if self._top is None or len(self._top) < top_n:
            self._top = await latest_top(self._pool, max(top_n, config.ITEM_CHECKS_TOP_N))
        top = [r for r in self._top if r["rank"] <= top_n]
        missing = [r["farm_id"] for r in top if r["farm_id"] not in self._farms]
        if missing:
            self._farms.update(await sfl_api.get_farms(missing))
        return top, {r["farm_id"]: self._farms[r["farm_id"]] for r in top if r["farm_id"] in self._farms}


async def run_holdings_check(
    pool: asyncpg.Pool, cache: TopFarmsCache, *, item: str, item_type: str, top_n: int,
    trigger: str, auction_id: str | None = None,
) -> int | None:
    top, farms = await cache.get(top_n)
    if not top:
        log.warning("Нет снимка лидерборда — проверка %s отложена", item)
        return None
    owners = find_owners(farms, item, item_type)
    check_id = await save_check(
        pool, item=item, item_type=item_type, top=top, farms_checked=len(farms),
        owners=owners, trigger=trigger, auction_id=auction_id,
    )
    log.info("Проверка #%s: %s (%s) — %s владельцев в топ-%s", check_id, item, trigger, len(owners), top_n)
    return check_id


# --- Аукционы ---

async def due_auction_checks(pool: asyncpg.Pool) -> list[dict]:
    """Какие проверки по аукционам пора сделать и ещё не сделаны."""
    if not (await db.table_exists(pool, "auctions") and await db.table_exists(pool, "auction_results")):
        return []
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=config.ITEM_CHECKS_AUCTION_WINDOW_H)
    auctions = await pool.fetch(
        """
        SELECT a.auction_id, a.item_name, a.item_type, a.supply, a.end_at,
               r.leaderboard AS results_leaderboard, r.supply AS results_supply
        FROM auctions a
        LEFT JOIN auction_results r ON r.auction_id = a.auction_id
        WHERE a.end_at <= $1 AND a.end_at >= $2
          AND a.item_type = ANY($3::text[]) AND a.item_name IS NOT NULL
        """,
        now, window_start, list(SUPPORTED_TYPES),
    )
    if not auctions:
        return []

    done = {
        (r["auction_id"], r["trigger"])
        for r in await pool.fetch(
            "SELECT auction_id, trigger FROM item_checks WHERE auction_id = ANY($1::text[])",
            [a["auction_id"] for a in auctions],
        )
    }
    due = []
    for a in auctions:
        if a["results_leaderboard"] is not None and (a["auction_id"], "auction_end") not in done:
            due.append({**a, "trigger": "auction_end"})
        for h in config.ITEM_CHECKS_AUCTION_DELAYS_H:
            trig = delay_trigger(h)
            if a["end_at"] + timedelta(hours=h) <= now and (a["auction_id"], trig) not in done:
                due.append({**a, "trigger": trig})
    return due


async def run_auction_end_check(pool: asyncpg.Pool, a: dict, top_n: int) -> int | None:
    """Победители аукциона ∩ топ-N — без запросов к API."""
    top = await latest_top(pool, top_n)
    if not top:
        return None
    top_ids = {r["farm_id"] for r in top}
    winners = auction_winners(a["results_leaderboard"], a["results_supply"] or a["supply"])
    # один победитель = один предмет; фактическое количество покажут проверки владения
    owners = {fid: 1.0 for fid in winners if fid in top_ids}
    check_id = await save_check(
        pool, item=a["item_name"], item_type=a["item_type"], top=top, farms_checked=len(winners),
        owners=owners, trigger="auction_end", auction_id=a["auction_id"], source="auction_winner",
    )
    log.info("Аукцион %s (%s): %s победителей в топ-%s", a["auction_id"], a["item_name"], len(owners), top_n)
    return check_id


# --- Ручные проверки ---

async def _manual_cooldown_left(pool: asyncpg.Pool) -> float:
    last = await pool.fetchval("SELECT max(checked_at) FROM item_checks WHERE trigger = 'manual'")
    if last is None:
        return 0
    passed = (datetime.now(timezone.utc) - last).total_seconds()
    return max(0.0, config.ITEM_CHECKS_MANUAL_COOLDOWN_SEC - passed)


async def take_manual_request(pool: asyncpg.Pool) -> asyncpg.Record | None:
    return await pool.fetchrow(
        """
        UPDATE item_check_requests SET status = 'running'
        WHERE id = (
            SELECT id FROM item_check_requests WHERE status = 'pending'
            ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1
        )
        RETURNING *
        """
    )


# --- Проход планировщика ---

async def run_pass() -> dict:
    pool = await db.get_pool()
    stats = {"auction": 0, "manual": 0, "errors": 0}
    cache = TopFarmsCache(pool)

    due = await due_auction_checks(pool)
    if due:
        async with tracked_run(pool, "item_check") as run_stats:
            for a in due:
                try:
                    if a["trigger"] == "auction_end":
                        await run_auction_end_check(pool, a, config.ITEM_CHECKS_TOP_N)
                    else:
                        await run_holdings_check(
                            pool, cache, item=a["item_name"], item_type=a["item_type"],
                            top_n=config.ITEM_CHECKS_TOP_N, trigger=a["trigger"], auction_id=a["auction_id"],
                        )
                    stats["auction"] += 1
                except asyncpg.UniqueViolationError:
                    pass  # параллельный проход уже сделал эту проверку
                except Exception:
                    stats["errors"] += 1
                    log.exception("Проверка аукциона %s (%s) упала", a["auction_id"], a["trigger"])
            run_stats.update(stats)

    if await _manual_cooldown_left(pool) == 0:
        req = await take_manual_request(pool)
        if req:
            try:
                if req["item_type"] not in SUPPORTED_TYPES:
                    raise ValueError(f"Тип {req['item_type']} не поддерживается")
                check_id = await run_holdings_check(
                    pool, cache, item=req["item_name"], item_type=req["item_type"],
                    top_n=min(req["top_n"], 500), trigger="manual",
                )
                await pool.execute(
                    "UPDATE item_check_requests SET status='done', check_id=$2, processed_at=now() WHERE id=$1",
                    req["id"], check_id,
                )
                stats["manual"] += 1
            except Exception as e:
                stats["errors"] += 1
                log.exception("Ручная проверка #%s упала", req["id"])
                await pool.execute(
                    "UPDATE item_check_requests SET status='error', error=$2, processed_at=now() WHERE id=$1",
                    req["id"], repr(e)[:1000],
                )
    return stats


if __name__ == "__main__":
    async def _main():
        logging.basicConfig(level=logging.INFO)
        try:
            print(await run_pass())
        finally:
            await sfl_api.close()
            await db.close_pool()

    asyncio.run(_main())
