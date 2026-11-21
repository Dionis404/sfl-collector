"""
Worker очереди farm_refresh_queue: on-demand обновление farm_cache для
калькулятора ферм на сайте (перенесено из goblin-bot/shared/farm_cache.py).

API (api/main.py) вставляет farm_id в очередь; этот цикл каждые
FARM_REFRESH_POLL_SEC забирает до FARM_REFRESH_BATCH ферм и обновляет их
по одной через общий RateLimiter (GET /community/farms/{id}).

Захват работы — DELETE ... RETURNING (идемпотентно, зависших локов нет):
- успех  -> upsert в farm_cache;
- 404    -> отрицательный кэш на час (перебор несуществующих id не жжёт
            rate limit), из очереди удалено навсегда;
- ошибка -> возвращаем в очередь с attempts+1, после MAX_ATTEMPTS бросаем.
"""
import logging
import time

import httpx

from collector import config, db, sfl_api
from collector.runs import tracked_run

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Отрицательный кэш: farm_id -> monotonic, до которого ферма считается несуществующей.
NOT_FOUND_TTL_SEC = 3600
_NOT_FOUND_MAX_SIZE = 10_000
_not_found_until: dict[int, float] = {}


def is_known_missing(farm_id: int) -> bool:
    until = _not_found_until.get(farm_id)
    if until is None:
        return False
    if until < time.monotonic():
        del _not_found_until[farm_id]
        return False
    return True


def mark_missing(farm_id: int) -> None:
    if len(_not_found_until) >= _NOT_FOUND_MAX_SIZE:
        _not_found_until.clear()
    _not_found_until[farm_id] = time.monotonic() + NOT_FOUND_TTL_SEC


async def _claim(pool) -> list[int]:
    """Забирает пачку id из очереди (захват удалением — повторной обработки нет)."""
    rows = await pool.fetch(
        """
        DELETE FROM farm_refresh_queue
        WHERE farm_id IN (
            SELECT farm_id FROM farm_refresh_queue
            ORDER BY requested_at
            LIMIT $1
        )
        RETURNING farm_id, attempts
        """,
        config.FARM_REFRESH_BATCH,
    )
    return [(r["farm_id"], r["attempts"]) for r in rows]


async def _requeue(pool, farm_id: int, attempts: int) -> None:
    if attempts + 1 >= MAX_ATTEMPTS:
        log.warning("farm_refresh: ферма %s не обновилась за %d попытки, выбывает из очереди",
                    farm_id, attempts + 1)
        return
    await pool.execute(
        """
        INSERT INTO farm_refresh_queue (farm_id, attempts) VALUES ($1, $2)
        ON CONFLICT (farm_id) DO UPDATE SET attempts = EXCLUDED.attempts
        """,
        farm_id, attempts + 1,
    )


async def refresh_one(pool, farm_id: int) -> bool:
    """Одна ферма: SFL -> farm_cache. False = не получилось (404/ошибка)."""
    try:
        data = await sfl_api.get_farm(farm_id)
    except httpx.HTTPError:
        log.warning("farm_refresh: ошибка API для фермы %s", farm_id, exc_info=True)
        raise

    if data is None:  # 404
        mark_missing(farm_id)
        log.info("farm_refresh: ферма %s не найдена в SFL (отрицательный кэш на час)", farm_id)
        return False

    await pool.execute(
        """
        INSERT INTO farm_cache (farm_id, data, tracked, first_seen, last_requested_at, updated_at)
        VALUES ($1, $2, true, now(), now(), now())
        ON CONFLICT (farm_id) DO UPDATE
            SET data = EXCLUDED.data, updated_at = now(), is_refreshing = false
        """,
        farm_id, data,
    )
    return True


async def run() -> dict:
    pool = await db.get_pool()
    claimed = await _claim(pool)
    if not claimed:
        return {"skipped": "queue_empty"}

    async with tracked_run(pool, "farm_refresh") as stats:
        ok = missing = failed = 0
        for farm_id, attempts in claimed:
            if is_known_missing(farm_id):
                missing += 1
                continue
            try:
                if await refresh_one(pool, farm_id):
                    ok += 1
                else:
                    missing += 1
            except Exception:
                failed += 1
                await _requeue(pool, farm_id, attempts)
        stats.update(ok=ok, missing=missing, failed=failed, queue_left=await pool.fetchval(
            "SELECT count(*) FROM farm_refresh_queue"))
        log.info("farm_refresh: ok=%d missing=%d failed=%d", ok, missing, failed)
        return stats
