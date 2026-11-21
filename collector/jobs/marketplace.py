"""
Цены маркетплейса (перенесено из sfl-price-service/collector/collector.py).

Раз в MARKETPLACE_INTERVAL_SEC делает ОДИН запрос
`community/data?type=marketplaceActivity&date=<UTC>` через общий RateLimiter
и пишет в `sfl_resource_prices` только изменившиеся предметы (change-log):
у большинства редких предметов цена не меняется часами, дубли не нужны.

Попутно сохраняет курс $/FLOWER из того же ответа в `flower_price_history` —
сайт считает по нему цены ставок аукционов в $.

При старте подтягивает последнее состояние каждого предмета из БД, чтобы
перезапуск контейнера не порождал лавину "всё изменилось".
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from collector import config, db, sfl_api
from collector.runs import tracked_run

log = logging.getLogger(__name__)

# Поля, по которым отслеживаем изменение (хоть одно не совпало — пишем строку)
TRACKED_FIELDS = [
    "floor", "low", "high", "latestSale", "bestOffer",
    "volume", "trades", "quantity", "listingCount", "offerCount",
]

ITEM_NAMES_FILE = Path(__file__).resolve().parent.parent / "data" / "item_names.json"

# Кэш последнего состояния по предметам: {item_key: tuple(TRACKED_FIELDS)}
_last_state: dict[str, tuple] = {}
_state_loaded = False


def load_item_names() -> dict:
    """Известные имена (collectibles-*, из KNOWN_IDS игры). Для остальных
    категорий маппинга нет — используем сам item_key."""
    if ITEM_NAMES_FILE.exists():
        return json.loads(ITEM_NAMES_FILE.read_text(encoding="utf-8"))
    return {}


async def _load_last_state(pool) -> None:
    global _state_loaded
    rows = await pool.fetch(
        """
        SELECT DISTINCT ON (item_key)
            item_key, floor, low, high, latest_sale, best_offer,
            volume, trades, quantity, listing_count, offer_count
        FROM sfl_resource_prices
        ORDER BY item_key, captured_at DESC
        """
    )
    for r in rows:
        _last_state[r["item_key"]] = (
            r["floor"], r["low"], r["high"], r["latest_sale"], r["best_offer"],
            r["volume"], r["trades"], r["quantity"], r["listing_count"], r["offer_count"],
        )
    _state_loaded = True
    log.info("marketplace: подтянуто последнее состояние по %d предметам", len(_last_state))


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def run() -> dict:
    pool = await db.get_pool()
    if not _state_loaded:
        await _load_last_state(pool)

    date_str = _today_utc()
    captured_at = datetime.now(timezone.utc)
    payload = await sfl_api.get_data("marketplaceActivity", date=date_str)

    async with tracked_run(pool, "marketplace") as stats:
        report = (payload or {}).get("reports", {}).get(date_str)
        if report is None:
            log.warning("marketplace: в ответе нет данных за %s", date_str)
            stats["skipped"] = "no_report"
            return stats

        flower_price = (payload or {}).get("flowerPrice")
        items = report.get("items", {})
        item_names = load_item_names()

        changed = []
        for item_key, v in items.items():
            new_tuple = tuple(v.get(f) for f in TRACKED_FIELDS)
            if _last_state.get(item_key) == new_tuple:
                continue
            changed.append((
                captured_at, date_str, item_key, item_names.get(item_key, item_key),
                v.get("floor"), v.get("low"), v.get("high"),
                v.get("latestSale"), v.get("bestOffer"),
                v.get("volume"), v.get("trades"), v.get("quantity"),
                v.get("listingCount"), v.get("offerCount"),
                flower_price,
            ))
            _last_state[item_key] = new_tuple

        async with pool.acquire() as con, con.transaction():
            if changed:
                await con.executemany(
                    """
                    INSERT INTO sfl_resource_prices (
                        captured_at, snapshot_date, item_key, item_name,
                        floor, low, high, latest_sale, best_offer,
                        volume, trades, quantity, listing_count, offer_count,
                        flower_price
                    ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                    ON CONFLICT (item_key, captured_at) DO NOTHING
                    """,
                    changed,
                )
            if flower_price is not None:
                await con.execute(
                    "INSERT INTO flower_price_history (captured_at, usd) VALUES ($1, $2)"
                    " ON CONFLICT (captured_at) DO NOTHING",
                    captured_at, flower_price,
                )

        stats.update(total=len(items), changed=len(changed), flower_price=flower_price)
        log.info("marketplace: опрошено предметов %d, записано изменений %d", len(items), len(changed))
        return stats
