"""
Быстрый цикл: топ-N ферм лидерборда одним массовым запросом (getFarms),
из них — торговец в пещере, задания недели, баунти-борд и консенсус по
билетным доставкам.

Почему топ лидерборда: эти игроки заходят каждый день, у них актуальное
состояние. 100 ферм = 1–4 запроса (лимит ответа 5.5 МБ, остаток дозапрашиваем).
"""
import asyncio
import logging
from datetime import datetime, timezone

import asyncpg

from collector import config, db, sfl_api
from collector.parsers import deliveries, global_state
from collector.runs import latest_top, tracked_run

log = logging.getLogger(__name__)


async def save_merchant(con: asyncpg.Connection, m: dict, now: datetime) -> None:
    await con.execute(
        """
        INSERT INTO cave_merchant_visits
            (visit_index, opens_at, closes_at, next_opens_at, offers,
             generous_share, offers_share, farms_seen, first_seen, last_seen)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$9)
        ON CONFLICT (visit_index) DO UPDATE SET
            opens_at = EXCLUDED.opens_at, closes_at = EXCLUDED.closes_at,
            next_opens_at = EXCLUDED.next_opens_at, offers = EXCLUDED.offers,
            generous_share = EXCLUDED.generous_share, offers_share = EXCLUDED.offers_share,
            farms_seen = EXCLUDED.farms_seen, last_seen = EXCLUDED.last_seen
        """,
        m["visit_index"], m["opens_at"], m["closes_at"], m["next_opens_at"], m["offers"],
        m["generous_share"], m["offers_share"], m["farms_seen"], now,
    )


async def save_chores(con: asyncpg.Connection, chores: list[dict], now: datetime) -> None:
    await con.executemany(
        """
        INSERT INTO weekly_chores (started_at, npc, task, reward, first_seen, last_seen)
        VALUES ($1,$2,$3,$4,$5,$5)
        ON CONFLICT (started_at, npc) DO UPDATE SET
            task = EXCLUDED.task, reward = EXCLUDED.reward, last_seen = EXCLUDED.last_seen
        """,
        [(c["started_at"], c["npc"], c["task"], c["reward"], now) for c in chores if c["started_at"]],
    )


async def save_bounties(con: asyncpg.Connection, bounties: list[dict], now: datetime) -> None:
    await con.executemany(
        """
        INSERT INTO bounty_requests (id, name, category, level, coins, items, first_seen, last_seen)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$7)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name, category = EXCLUDED.category, level = EXCLUDED.level,
            coins = EXCLUDED.coins, items = EXCLUDED.items, last_seen = EXCLUDED.last_seen
        """,
        [(b["id"], b["name"], b["category"], b["level"], b["coins"], b["items"], now) for b in bounties],
    )


async def save_ticket_consensus(con: asyncpg.Connection, rows: list, now: datetime) -> None:
    await con.executemany(
        """
        INSERT INTO ticket_delivery_consensus
            (taken_at, npc, items, base_tickets, votes, total, share, variants)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
        """,
        [(now, r.npc, r.items, r.base_tickets, r.votes, r.total, r.share, r.variants) for r in rows],
    )


async def run() -> dict:
    pool = await db.get_pool()
    async with tracked_run(pool, "top_farms") as stats:
        top = await latest_top(pool, config.TOP_FARMS_N)
        if not top:
            log.warning("Нет снимка top500_snapshots — пропускаю цикл по топу")
            stats["farms"] = 0
            return stats

        farms = await sfl_api.get_farms([r["farm_id"] for r in top])
        farm_list = list(farms.values())
        stats["farms"] = len(farm_list)

        merchant = global_state.extract_merchant(farm_list)
        chores = global_state.extract_chores(farm_list)
        bounties = global_state.extract_bounties(farm_list)
        consensus = deliveries.ticket_consensus(farm_list)

        # все строки одного запуска получают одинаковый last_seen —
        # «текущие» данные = строки с максимальным last_seen
        now = datetime.now(timezone.utc)
        async with pool.acquire() as con, con.transaction():
            if merchant:
                await save_merchant(con, merchant, now)
            if chores:
                await save_chores(con, chores, now)
            if bounties:
                await save_bounties(con, bounties, now)
            if consensus:
                await save_ticket_consensus(con, consensus, now)

        stats.update(
            merchant_visit=merchant["visit_index"] if merchant else None,
            chores=len(chores),
            bounties=len(bounties),
            ticket_npcs=len(consensus),
        )
        log.info("Цикл по топу: %s", stats)
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
