"""
Клиент SFL Community API — ЕДИНСТВЕННАЯ точка, через которую коллектор
ходит в API.

Лимит community API считается по IP (~1 запрос / 5 с, при перегрузе 1 / 10 с),
поэтому все запросы процесса идут через один RateLimiter: не чаще одного
раза в API_MIN_INTERVAL_SEC и строго по одному. На 429 — пауза с ростом
и повтор. Файлы ночного дампа (CDN) лимитом не ограничены и идут мимо него.
"""
import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

import httpx

from collector import config

log = logging.getLogger(__name__)


class RateLimiter:
    """Не чаще одного запроса раз в `min_interval` секунд и по одному за раз."""

    def __init__(self, min_interval: float):
        self._min_interval = min_interval
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def __aenter__(self):
        await self._lock.acquire()
        wait = self._min_interval - (time.monotonic() - self._last)
        if wait > 0:
            await asyncio.sleep(wait)
        return self

    async def __aexit__(self, *exc):
        self._last = time.monotonic()
        self._lock.release()


class SflApiError(Exception):
    pass


_limiter = RateLimiter(config.API_MIN_INTERVAL_SEC)
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        if not config.SFL_API_KEY:
            raise SflApiError("SFL_API_KEY не задан")
        _client = httpx.AsyncClient(
            base_url=config.SFL_API_BASE,
            headers={"x-api-key": config.SFL_API_KEY},
            timeout=config.API_TIMEOUT_SEC,
            proxy=config.SFL_PROXY,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def _request(method: str, path: str, **kwargs) -> httpx.Response:
    client = _get_client()
    for attempt in range(1, config.API_MAX_RETRIES + 1):
        async with _limiter:
            resp = await client.request(method, path, **kwargs)
        if resp.status_code != 429:
            return resp
        wait = config.API_RETRY_BASE_SEC * attempt
        log.warning("429 от SFL API (%s %s), попытка %s/%s, ждём %.0f с",
                    method, path, attempt, config.API_MAX_RETRIES, wait)
        await asyncio.sleep(wait)
    return resp


# --- Одна ферма ---

async def get_farm(farm_id: int) -> dict | None:
    """Ответ /community/farms/{id} целиком ({farm, id, nft_id, updatedAt, ...}) или None."""
    resp = await _request("GET", f"/community/farms/{farm_id}")
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


# --- Пачка ферм по id (legacy getFarms, до 100 id) ---

async def _get_farms_once(ids: list[int]) -> tuple[dict[str, dict], list[int]]:
    resp = await _request("POST", "/community/getFarms", json={"ids": ids})
    resp.raise_for_status()
    data = resp.json()
    return data.get("farms") or {}, [int(x) for x in data.get("skipped") or []]


async def get_farms(ids: list[int], *, fallback_single: bool = True) -> dict[int, dict]:
    """
    Фермы по списку id: {farm_id: farm_object}.

    getFarms принимает до 100 id, но ответ ограничен ~5.5 МБ — лишние фермы
    приходят в `skipped`. Их дозапрашиваем, деля пополам: вернувшиеся —
    это был лимит размера, оставшиеся в skipped поодиночке — фермы нет.

    Эндпоинт помечен deprecated: если он перестал работать (4xx/5xx),
    при fallback_single=True переходим на /community/farms/{id} по одной.
    """
    result: dict[int, dict] = {}

    async def fetch(batch: list[int]) -> None:
        farms, skipped = await _get_farms_once(batch)
        for fid, farm in farms.items():
            result[int(fid)] = farm
        if skipped and len(batch) > 1:
            half = (len(skipped) + 1) // 2
            for part in (skipped[:half], skipped[half:]):
                if part:
                    await fetch(part)

    try:
        for i in range(0, len(ids), 100):
            await fetch(ids[i:i + 100])
        return result
    except httpx.HTTPStatusError as e:
        if not fallback_single:
            raise
        log.warning("getFarms недоступен (%s) — перехожу на запросы по одной ферме", e.response.status_code)

    for fid in ids:
        if fid in result:
            continue
        try:
            data = await get_farm(fid)
        except httpx.HTTPError:
            log.exception("Не удалось получить ферму %s", fid)
            continue
        if data:
            result[fid] = data["farm"]
    return result


# --- /community/data ---

async def get_data(type_: str, **params) -> dict | list | None:
    resp = await _request("GET", "/community/data", params={"type": type_, **params})
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("data")


async def get_ticket_leaderboard(farm_id: int, limit: int | None = None) -> dict | None:
    params = {"farmId": farm_id}
    if limit:
        params["limit"] = limit
    return await get_data("ticketLeaderboard", **params)


async def get_dump_manifest() -> list[dict]:
    return await get_data("nightlyDump") or []


# --- Ночной дамп (CDN, без ключа и без лимита) ---

class _AsyncGzipLines:
    """Потоковая распаковка gzip-ответа построчно — память не растёт с размером файла."""

    def __init__(self, resp: httpx.Response):
        self._resp = resp

    async def __aiter__(self) -> AsyncIterator[bytes]:
        import zlib

        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)  # формат gzip
        buf = b""
        async for chunk in self._resp.aiter_bytes(1 << 20):
            buf += decomp.decompress(chunk)
            *lines, buf = buf.split(b"\n")
            for line in lines:
                if line:
                    yield line
            # gzip может состоять из нескольких членов подряд
            while decomp.eof and decomp.unused_data:
                rest = decomp.unused_data
                decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
                buf += decomp.decompress(rest)
        buf += decomp.flush()
        for line in buf.split(b"\n"):
            if line:
                yield line


async def stream_dump(filename: str) -> AsyncIterator[dict]:
    """Строки дампа: {id, nftId, farm, isBlacklisted, lastActivity}."""
    url = f"{config.SFL_DUMP_BASE}/{filename}"
    async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=300), proxy=config.SFL_PROXY) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            async for line in _AsyncGzipLines(resp):
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    log.warning("Битая строка в дампе, пропускаю")


__all__ = [
    "RateLimiter", "SflApiError", "close", "get_farm", "get_farms", "get_data",
    "get_ticket_leaderboard", "get_dump_manifest", "stream_dump",
]
