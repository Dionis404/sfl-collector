"""
Ночной дамп SFL (active.jsonl.gz, ~780 МБ gzip) — раз в сутки, ~22:00 UTC.

Один проход по файлу (потоково, память не растёт):
1. Статистика доставок по всем активным фермам -> delivery_stats_daily
   (монеты и Flower у каждого свои, поэтому смотрим распределение:
   кто что просит, средняя/мин/макс награда).
2. Для топ-N лидерборда сохраняем только wardrobe + inventory и после
   прохода перепроверяем владение предметами недавних аукционов
   (trigger = nightly_YYYY-MM-DD) — без единого запроса к API.

Манифест — 1 запрос к API; сам файл качается с CDN без ключа и лимита.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import asyncpg

from collector import config, db, sfl_api
from collector.jobs.item_checks import save_check
from collector.parsers.deliveries import DeliveryStats
from collector.parsers.items import SUPPORTED_TYPES, find_owners
from collector.runs import latest_top, tracked_run

log = logging.getLogger(__name__)


def pick_latest(manifest: list[dict], kind: str) -> dict | None:
    files = [f for f in manifest if f.get("filename", "").endswith(f"/{kind}.jsonl.gz")]
    return max(files, key=lambda f: f.get("modifiedAt") or "") if files else None


def dump_date_of(filename: str) -> date:
    return date.fromisoformat(filename.split("/", 1)[0])


async def recent_auction_items(pool: asyncpg.Pool) -> list[tuple[str, str, str]]:
    """(auction_id, item_name, item_type) аукционов, закончившихся за DUMP_AUCTION_DAYS."""
    if not await db.table_exists(pool, "auctions"):
        return []
    since = datetime.now(timezone.utc) - timedelta(days=config.DUMP_AUCTION_DAYS)
    rows = await pool.fetch(
        """SELECT auction_id, item_name, item_type FROM auctions
           WHERE end_at <= now() AND end_at >= $1 AND item_name IS NOT NULL
             AND item_type = ANY($2::text[])""",
        since, list(SUPPORTED_TYPES),
    )
    return [(r["auction_id"], r["item_name"], r["item_type"]) for r in rows]


async def run(force: bool = False) -> dict:
    pool = await db.get_pool()
    manifest = await sfl_api.get_dump_manifest()
    latest = pick_latest(manifest, config.DUMP_FILE_KIND)
    if not latest:
        log.info("Дамп ещё не опубликован")
        return {"skipped": "no_dump"}

    filename = latest["filename"]
    if not force and await pool.fetchval("SELECT 1 FROM nightly_dumps WHERE filename = $1", filename):
        return {"skipped": "already_processed", "filename": filename}

    dump_day = dump_date_of(filename)
    async with tracked_run(pool, "nightly_dump") as stats:
        stats["filename"] = filename
        top = await latest_top(pool, config.DUMP_TOP_N)
        top_ids = {r["farm_id"] for r in top}

        delivery_stats = DeliveryStats()
        top_farms: dict[int, dict] = {}
        farms = 0
        async for line in sfl_api.stream_dump(filename):
            farm = line.get("farm") or {}
            farms += 1
            if not line.get("isBlacklisted"):
                delivery_stats.add_farm(farm)
            fid = line.get("id")
            if fid in top_ids:
                top_farms[fid] = {"wardrobe": farm.get("wardrobe") or {}, "inventory": farm.get("inventory") or {}}
            if farms % 50000 == 0:
                log.info("Дамп: обработано %s ферм", farms)

        rows = delivery_stats.rows()
        async with pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM delivery_stats_daily WHERE dump_date = $1", dump_day)
            await con.executemany(
                """INSERT INTO delivery_stats_daily
                   (dump_date, npc, reward_type, item, orders, avg_reward, min_reward, max_reward)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
                [(dump_day, r["npc"], r["reward_type"], r["item"], r["orders"],
                  r["avg_reward"], r["min_reward"], r["max_reward"]) for r in rows],
            )

        checks = 0
        if top:
            for auction_id, item, item_type in await recent_auction_items(pool):
                owners = find_owners(top_farms, item, item_type)
                try:
                    await save_check(
                        pool, item=item, item_type=item_type, top=top, farms_checked=len(top_farms),
                        owners=owners, trigger=f"nightly_{dump_day.isoformat()}", auction_id=auction_id,
                    )
                    checks += 1
                except asyncpg.UniqueViolationError:
                    pass  # этот дамп уже проверяли для аукциона (force-перезапуск)

        modified = latest.get("modifiedAt")
        await pool.execute(
            """INSERT INTO nightly_dumps (filename, dump_date, modified_at, farms)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (filename) DO UPDATE SET farms = EXCLUDED.farms, processed_at = now()""",
            filename, dump_day,
            datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None,
            farms,
        )
        stats.update(farms=farms, delivery_rows=len(rows), top_farms_found=len(top_farms), item_checks=checks)
        log.info("Дамп обработан: %s", stats)
        return stats


if __name__ == "__main__":
    import sys

    async def _main():
        logging.basicConfig(level=logging.INFO)
        try:
            print(await run(force="--force" in sys.argv))
        finally:
            await sfl_api.close()
            await db.close_pool()

    asyncio.run(_main())
