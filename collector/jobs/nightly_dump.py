"""
Ночной дамп SFL (active.jsonl.gz, ~780 МБ gzip) — раз в сутки, ~22:00 UTC.

Один проход по файлу (потоково, память не растёт):
1. Статистика доставок по всем активным фермам -> delivery_stats_daily
   (монеты и Flower у каждого свои, поэтому смотрим распределение:
   кто что просит, средняя/мин/макс награда).
2. Предметы текущей главы (мутанты + магазин Стеллы, из collector/data/game_data.json):
   сколько активных ферм и сколько игроков топа ими владеют -> chapter_item_stats_daily.
3. Профили топ-N: опыт, уровень, возвышение и отслеживаемые в эту главу предметы
   (TOP_WATCH_ITEMS, напр. Grinx's Hammer в главе 16) -> top_player_profiles.
4. Для топ-N лидерборда сохраняем только wardrobe + inventory и после
   прохода перепроверяем владение предметами недавних аукционов
   (trigger = nightly_YYYY-MM-DD) — без единого запроса к API.

Манифест — 1 запрос к API; сам файл качается с CDN без ключа и лимита.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone

import asyncpg

from collector import config, db, game_data, sfl_api
from collector.jobs.item_checks import save_check
from collector.parsers.chapter_items import ChapterItemStats, parse_watch_items, top_profile
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
    modified = latest.get("modifiedAt")
    dumped_at = datetime.fromisoformat(modified.replace("Z", "+00:00")) if modified else None
    # глава, в которую снят дамп (а не «сейчас»: дамп за 1 ноября — ещё прошлая глава)
    chapter = game_data.chapter_at(dumped_at or datetime.combine(dump_day, datetime.min.time(), timezone.utc))
    watched = parse_watch_items(config.TOP_WATCH_ITEMS).get(chapter["name"], []) if chapter else []

    async with tracked_run(pool, "nightly_dump") as stats:
        stats["filename"] = filename
        stats["chapter"] = chapter["name"] if chapter else None
        top = await latest_top(pool, config.DUMP_TOP_N)
        top_by_id = {r["farm_id"]: r for r in top}

        delivery_stats = DeliveryStats()
        item_stats = ChapterItemStats(game_data.chapter_items(chapter) if chapter else [])
        top_farms: dict[int, dict] = {}
        profiles: dict[int, dict] = {}
        farms = 0
        async for line in sfl_api.stream_dump(filename):
            farm = line.get("farm") or {}
            farms += 1
            fid = line.get("id")
            in_top = fid in top_by_id
            if not line.get("isBlacklisted"):
                delivery_stats.add_farm(farm)
                item_stats.add_farm(farm, in_top=in_top)
            if in_top:
                top_farms[fid] = {"wardrobe": farm.get("wardrobe") or {}, "inventory": farm.get("inventory") or {}}
                profiles[fid] = top_profile(farm, top_by_id[fid], watched)
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

        item_rows = item_stats.rows()
        async with pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM chapter_item_stats_daily WHERE dump_date = $1", dump_day)
            await con.executemany(
                """INSERT INTO chapter_item_stats_daily
                   (dump_date, chapter, item, item_type, source, kind, tier, holders, total, top_holders, top_total)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                [(dump_day, chapter["name"], r["name"], r["type"], r["source"], r["kind"], r["tier"],
                  r["holders"], r["total"], r["top_holders"], r["top_total"]) for r in item_rows],
            )
            await con.execute("DELETE FROM top_player_profiles WHERE dump_date = $1", dump_day)
            await con.executemany(
                """INSERT INTO top_player_profiles
                   (dump_date, farm_id, rank, username, tickets, experience, ascension, level,
                    total_level, ready_to_ascend, watched_items)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
                [(dump_day, fid, p["rank"], p["username"], p["tickets"], p["experience"], p["ascension"],
                  p["level"], p["total_level"], p["ready_to_ascend"], p["watched_items"])
                 for fid, p in profiles.items()],
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

        await pool.execute(
            """INSERT INTO nightly_dumps (filename, dump_date, modified_at, farms)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (filename) DO UPDATE SET farms = EXCLUDED.farms, processed_at = now()""",
            filename, dump_day, dumped_at, farms,
        )
        stats.update(
            farms=farms, delivery_rows=len(rows), top_farms_found=len(top_farms), item_checks=checks,
            chapter_items=len(item_rows), top_profiles=len(profiles), watched=watched,
        )
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
