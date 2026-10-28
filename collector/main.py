"""
Точка входа sfl-collector: несколько периодических циклов в одном процессе.

Все циклы ходят в SFL API через один RateLimiter (collector/sfl_api.py),
поэтому могут работать параллельно — запросы всё равно выстраиваются в
очередь и не превышают лимит по IP.

Запуск: python -m collector.main
"""
import asyncio
import logging
import signal

from collector import config, db, sfl_api
from collector.jobs import item_checks, nightly_dump, tickets_leaderboard, top_farms

log = logging.getLogger("collector")


async def periodic(name: str, interval: float, func, *, initial_delay: float = 0) -> None:
    if initial_delay:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            await func()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Цикл %s упал, следующая попытка через %.0f с", name, interval)
        await asyncio.sleep(interval)


async def main() -> None:
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    await db.get_pool()

    tasks = []
    if config.TICKETS_ENABLED:
        tasks.append(periodic("tickets", config.TICKETS_INTERVAL_SEC, tickets_leaderboard.run))
    if config.TOP_FARMS_ENABLED:
        # даём лидерборду собраться первым: топ берётся из top500_snapshots
        tasks.append(periodic("top_farms", config.TOP_FARMS_INTERVAL_SEC, top_farms.run, initial_delay=30))
    if config.ITEM_CHECKS_ENABLED:
        tasks.append(periodic("item_checks", config.ITEM_CHECKS_POLL_SEC, item_checks.run_pass, initial_delay=60))
    if config.DUMP_ENABLED:
        tasks.append(periodic("nightly_dump", config.DUMP_POLL_SEC, nightly_dump.run, initial_delay=120))

    if not tasks:
        log.warning("Все циклы выключены в настройках — нечего делать")
        return

    log.info("sfl-collector запущен: %s циклов", len(tasks))
    running = [asyncio.create_task(t) for t in tasks]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    await stop.wait()
    log.info("Останавливаюсь...")
    for t in running:
        t.cancel()
    await asyncio.gather(*running, return_exceptions=True)
    await sfl_api.close()
    await db.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
