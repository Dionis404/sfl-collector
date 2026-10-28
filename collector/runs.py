"""Журнал запусков (collector_runs) и общие запросы к лидерборду."""
import logging
import traceback
from contextlib import asynccontextmanager

import asyncpg

log = logging.getLogger(__name__)


@asynccontextmanager
async def tracked_run(pool: asyncpg.Pool, job: str):
    """
    Пишет строку в collector_runs: running -> ok/error.
    Внутри блока можно заполнять словарь stats — он сохранится в конце.
    """
    run_id = await pool.fetchval("INSERT INTO collector_runs (job) VALUES ($1) RETURNING id", job)
    stats: dict = {}
    try:
        yield stats
    except Exception as e:
        await pool.execute(
            "UPDATE collector_runs SET finished_at = now(), status = 'error', stats = $2, error = $3 WHERE id = $1",
            run_id, stats, f"{e!r}\n{traceback.format_exc()[-2000:]}",
        )
        raise
    else:
        await pool.execute(
            "UPDATE collector_runs SET finished_at = now(), status = 'ok', stats = $2 WHERE id = $1",
            run_id, stats,
        )


async def latest_top(pool: asyncpg.Pool, top_n: int) -> list[asyncpg.Record]:
    """Первые top_n мест из последнего снимка top500_snapshots."""
    return await pool.fetch(
        """
        SELECT rank, farm_id, game_username, tickets, taken_at
        FROM top500_snapshots
        WHERE taken_at = (SELECT max(taken_at) FROM top500_snapshots)
          AND rank <= $1
        ORDER BY rank
        """,
        top_n,
    )
