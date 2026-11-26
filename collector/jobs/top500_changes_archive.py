"""
Архив суточных изменений топ-500 билетов.

Эндпоинт /api/tickets/top500/changes считает «кто выбыл/просел» на лету за
скользящее окно — вчерашний список перезаписывается новым. Эта задача раз в
час материализует изменения в top500_daily_changes по суткам (UTC): для
каждого дня, у которого есть снимки top500_snapshots, но ещё нет архива,
сравнивает последний снимок предыдущего дня с последним снимком этого дня.

Текущий (незавершённый) день архивируется тоже — по мере появления новых
снимков его строки дополняются (выбывший час назад останется выбывшим,
вернувшийся в топ удаляется), поэтому сайт может показывать «сегодня» сразу.
Бэкфилл истории с начала снимков делается тем же кодом — бесплатно.

Правила те же, что в API: dropped — был в топ-500 вчера, сегодня нет;
fallen — остался, но просел на >= 50 позиций.
"""
import logging
from datetime import date, datetime, timedelta, timezone

from collector import db
from collector.runs import tracked_run

log = logging.getLogger(__name__)

FALLEN_THRESHOLD = 50


async def _archive_day(pool, day: date) -> dict:
    """Diff последнего снимка day-1 и последнего снимка day -> архив за day."""
    prev_taken = await pool.fetchval(
        "SELECT max(taken_at) FROM top500_snapshots WHERE taken_at < $1",
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
    )
    cur_taken = await pool.fetchval(
        "SELECT max(taken_at) FROM top500_snapshots WHERE taken_at < $1",
        datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1),
    )
    if prev_taken is None or cur_taken is None or prev_taken == cur_taken:
        return {"day": str(day), "skipped": True}

    old_rows = await pool.fetch(
        "SELECT rank, farm_id, game_username, tickets FROM top500_snapshots WHERE taken_at = $1",
        prev_taken,
    )
    new_by_farm = {
        r["farm_id"]: r
        for r in await pool.fetch(
            "SELECT rank, farm_id, game_username, tickets FROM top500_snapshots WHERE taken_at = $1",
            cur_taken,
        )
    }

    rows = []
    for o in old_rows:
        n = new_by_farm.get(o["farm_id"])
        if n is None:
            rows.append((day, o["farm_id"], o["game_username"], "dropped",
                         o["rank"], o["tickets"], None, None))
        elif n["rank"] - o["rank"] >= FALLEN_THRESHOLD:
            rows.append((day, o["farm_id"], n["game_username"] or o["game_username"], "fallen",
                         o["rank"], o["tickets"], n["rank"], n["tickets"]))

    async with pool.acquire() as con, con.transaction():
        # Вернувшиеся в топ перестают быть выбывшими — пересчитываем день целиком.
        await con.execute("DELETE FROM top500_daily_changes WHERE day = $1", day)
        if rows:
            await con.executemany(
                """INSERT INTO top500_daily_changes
                   (day, farm_id, username, kind, old_rank, old_tickets, new_rank, new_tickets)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                rows,
            )
    return {"day": str(day), "dropped": sum(1 for r in rows if r[3] == "dropped"),
            "fallen": sum(1 for r in rows if r[3] == "fallen")}


async def run() -> dict:
    pool = await db.get_pool()
    async with tracked_run(pool, "top500_changes_archive") as stats:
        days = [
            r["day"]
            for r in await pool.fetch(
                "SELECT DISTINCT (taken_at AT TIME ZONE 'UTC')::date AS day FROM top500_snapshots"
            )
        ]
        days.sort()
        archived = {
            r["day"] for r in await pool.fetch("SELECT DISTINCT day FROM top500_daily_changes")
        }
        today = datetime.now(timezone.utc).date()
        # Первый день снимков пропускаем — «вчера» для него неизвестно.
        # Старые заархивированные дни неизменны (снимки за них не дописываются),
        # пересчитываем только новые, вчерашний и сегодняшний.
        for day in days[1:]:
            if day in archived and day < today - timedelta(days=1):
                continue
            result = await _archive_day(pool, day)
            if not result.get("skipped"):
                stats[f"day:{result['day']}"] = result
        log.info("Архив изменений топ-500: дней %s, stats %s", len(days), stats)
        return stats


if __name__ == "__main__":
    import asyncio

    async def _main():
        logging.basicConfig(level=logging.INFO)
        try:
            await run()
        finally:
            await db.close_pool()

    asyncio.run(_main())
