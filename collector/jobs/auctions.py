"""
Аукционы SFL (перенесено из auctioneer-bot/app/sync.py).

Два цикла, оба через общий RateLimiter:

1. Список аукционов — раз в AUCTIONS_LIST_INTERVAL_SEC (сутки):
   `community/data?type=auctions` -> upsert в `auctions` + `item_total_supply`.

2. Результаты — каждые AUCTIONS_RESULTS_POLL_SEC (~2.5 мин):
   один завершившийся аукцион без результатов ->
   `community/data?type=auctionResults&auctionId=...`. Пока status != complete
   (или результат ещё не отдан) — откладываем до следующего цикла.

Таблицы `auctions`, `auction_results`, `item_total_supply` принадлежат
auctioneer-bot (схема — его migrations/001_init.sql): пишем по
договорённости, ALTER не делаем. После сохранения результатов срабатывает
item_checks с trigger='auction_end' (см. jobs/item_checks.py).
"""
import logging
from datetime import datetime, timezone

from collector import db, sfl_api
from collector.runs import tracked_run

log = logging.getLogger(__name__)

_ITEM_NAME_FIELDS = ("wearable", "collectible", "nft")


def _ms_to_dt(ms) -> datetime | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _item_name(item: dict) -> str | None:
    for field in _ITEM_NAME_FIELDS:
        value = item.get(field)
        if value:
            return value
    return None


async def sync_list() -> dict:
    """Список аукционов -> auctions + item_total_supply."""
    pool = await db.get_pool()
    data = await sfl_api.get_data("auctions")
    items = (data or {}).get("auctions", [])
    total_supply = (data or {}).get("totalSupply", {})

    async with tracked_run(pool, "auctions_list") as stats:
        affected = 0
        async with pool.acquire() as con, con.transaction():
            for item in items:
                await con.execute(
                    """
                    INSERT INTO auctions (
                        auction_id, item_name, item_type, supply, sfl_price,
                        ingredients, start_at, end_at, chapter_limit, start_id,
                        raw, updated_at
                    )
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now())
                    ON CONFLICT (auction_id) DO UPDATE SET
                        item_name     = EXCLUDED.item_name,
                        item_type     = EXCLUDED.item_type,
                        supply        = EXCLUDED.supply,
                        sfl_price     = EXCLUDED.sfl_price,
                        ingredients   = EXCLUDED.ingredients,
                        start_at      = EXCLUDED.start_at,
                        end_at        = EXCLUDED.end_at,
                        chapter_limit = EXCLUDED.chapter_limit,
                        start_id      = EXCLUDED.start_id,
                        raw           = EXCLUDED.raw,
                        updated_at    = now()
                    """,
                    item.get("auctionId"),
                    _item_name(item),
                    item.get("type"),
                    item.get("supply"),
                    item.get("sfl"),
                    item.get("ingredients"),
                    _ms_to_dt(item.get("startAt")),
                    _ms_to_dt(item.get("endAt")),
                    item.get("chapterLimit"),
                    item.get("startId"),
                    item,
                )
                affected += 1

            for item_name, supply in total_supply.items():
                await con.execute(
                    """
                    INSERT INTO item_total_supply (item_name, total_supply, updated_at)
                    VALUES ($1, $2, now())
                    ON CONFLICT (item_name) DO UPDATE SET
                        total_supply = EXCLUDED.total_supply,
                        updated_at   = now()
                    """,
                    item_name, supply,
                )

        stats.update(auctions=affected, total_supply=len(total_supply))
        log.info("auctions: список обновлён — %d аукционов, %d позиций totalSupply",
                 affected, len(total_supply))
        return stats


async def sync_results() -> dict:
    """Один завершившийся аукцион без результатов -> auction_results.

    Приоритет — свежим аукционам (последние 3 дня): результаты нужны на
    сайте сразу после окончания. Исторический бэклог (2024+) догоняем
    в хвосте очереди, не блокируя свежие.
    """
    pool = await db.get_pool()
    row = await pool.fetchrow(
        """
        SELECT auction_id FROM auctions
        WHERE end_at <= now() AND NOT results_fetched
        ORDER BY (end_at > now() - interval '3 days') DESC, end_at
        LIMIT 1
        """
    )
    if row is None:
        return {"skipped": "nothing_pending"}

    auction_id = row["auction_id"]
    async with tracked_run(pool, "auction_results") as stats:
        stats["auction_id"] = auction_id
        result = await sfl_api.get_data("auctionResults", auctionId=auction_id)

        status = (result or {}).get("status")
        if status != "complete":
            # Результаты ещё не готовы — подождём следующий цикл.
            log.info("auctions: результаты %s ещё не готовы (status=%s)", auction_id, status)
            stats["skipped"] = f"not_complete:{status}"
            return stats

        async with pool.acquire() as con, con.transaction():
            await con.execute(
                """
                INSERT INTO auction_results (
                    auction_id, my_status, participant_count, supply,
                    leaderboard, fetched_at
                )
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (auction_id) DO UPDATE SET
                    my_status         = EXCLUDED.my_status,
                    participant_count = EXCLUDED.participant_count,
                    supply            = EXCLUDED.supply,
                    leaderboard       = EXCLUDED.leaderboard,
                    fetched_at        = now()
                """,
                auction_id,
                status,
                result.get("participantCount"),
                result.get("supply"),
                result.get("leaderboard"),
            )
            await con.execute(
                "UPDATE auctions SET results_fetched = true WHERE auction_id = $1",
                auction_id,
            )

        stats["saved"] = True
        log.info("auctions: результаты %s сохранены", auction_id)
        return stats
