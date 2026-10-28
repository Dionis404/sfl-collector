"""
Почасовой сбор лидерборда тикетов (перенесён из goblin-bot).

1. Один запрос забирает топ-500 -> top500_snapshots (нужен сайту и всем
   остальным задачам коллектора: из него берётся «топ-N»).
2. Для фермеров бота (farmers.tickets_tracked = true, tickets_excluded = false):
   ранг берётся из топ-500 бесплатно, если ферма там есть, иначе — отдельный
   запрос. Снимок в ticket_leaderboard_snapshots пишется при rank <= RANK_CUTOFF;
   при rank >= EXCLUDE_RANK ферма помечается tickets_excluded.
   Таблица farmers принадлежит goblin-bot; если её нет — пункт 2 пропускается.

Логика и таблицы те же, что были в goblin-bot (jobs/tickets_leaderboard.py),
поэтому еженедельный отчёт и goblin-api продолжают работать без изменений.
"""
import asyncio
import logging
from datetime import datetime, timezone

from collector import config, db, sfl_api
from collector.runs import tracked_run

log = logging.getLogger(__name__)

RANK_CUTOFF = 1200
EXCLUDE_RANK = 2000
TOP500_LIMIT = 500
TOP500_QUERY_FARM_ID = 1  # любой валидный id: борд общий, farmId обязателен по формату API


def parse_top(data: dict) -> list[dict]:
    return [
        {"rank": i, "farm_id": e["farmId"], "game_username": e.get("id"), "tickets": e["count"]}
        for i, e in enumerate(data.get("topTen") or [], start=1)
        if e.get("farmId") is not None
    ]


def parse_farm_rank(data: dict, farm_id: int) -> dict | None:
    details = data.get("farmRankingDetails")
    rows = details if details else (data.get("topTen") or [])
    for i, e in enumerate(rows, start=1):
        if e.get("farmId") == farm_id:
            return {"rank": e.get("rank") or i, "tickets": e["count"], "game_username": e.get("id")}
    return None


async def run() -> dict:
    pool = await db.get_pool()
    async with tracked_run(pool, "tickets") as stats:
        data = await sfl_api.get_ticket_leaderboard(TOP500_QUERY_FARM_ID, limit=TOP500_LIMIT)
        if not data:
            log.warning("Лидерборд тикетов пуст (нет борда для текущей главы?)")
            stats["top"] = 0
            return stats

        top = parse_top(data)
        now = datetime.now(timezone.utc)
        await pool.executemany(
            "INSERT INTO top500_snapshots (rank, farm_id, game_username, tickets, taken_at) VALUES ($1,$2,$3,$4,$5)",
            [(r["rank"], r["farm_id"], r["game_username"], r["tickets"], now) for r in top],
        )
        stats["top"] = len(top)

        if not config.TICKETS_TRACK_FARMERS or not await db.table_exists(pool, "farmers"):
            return stats

        by_farm = {r["farm_id"]: r for r in top}
        farm_ids = [
            r["farm_id"]
            for r in await pool.fetch(
                "SELECT farm_id FROM farmers WHERE tickets_tracked = true AND tickets_excluded = false"
            )
        ]
        saved = skipped = from_top = excluded = 0
        for farm_id in farm_ids:
            if farm_id in by_farm:
                result = by_farm[farm_id]
                from_top += 1
            else:
                try:
                    lb = await sfl_api.get_ticket_leaderboard(farm_id)
                except Exception:
                    log.exception("Ошибка запроса места фермы %s", farm_id)
                    skipped += 1
                    continue
                result = parse_farm_rank(lb or {}, farm_id)
                if result is None:
                    skipped += 1
                    continue

            if result["rank"] <= RANK_CUTOFF:
                await pool.execute(
                    """INSERT INTO ticket_leaderboard_snapshots (farm_id, rank, tickets, game_username, taken_at)
                       VALUES ($1,$2,$3,$4,$5)""",
                    farm_id, result["rank"], result["tickets"], result["game_username"], now,
                )
                saved += 1
            else:
                skipped += 1
            if result["rank"] >= EXCLUDE_RANK:
                await pool.execute("UPDATE farmers SET tickets_excluded = true WHERE farm_id = $1", farm_id)
                excluded += 1

        stats.update(tracked=len(farm_ids), saved=saved, skipped=skipped, from_top=from_top, excluded=excluded)
        log.info("Лидерборд: топ %s, tracked %s", len(top), stats)
        return stats


if __name__ == "__main__":
    async def _main():
        logging.basicConfig(level=logging.INFO)
        try:
            await run()
        finally:
            await sfl_api.close()
            await db.close_pool()

    asyncio.run(_main())
