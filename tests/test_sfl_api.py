import asyncio
import gzip
import json
import time

from collector import sfl_api
from collector.jobs.nightly_dump import dump_date_of, pick_latest
from collector.jobs.tickets_leaderboard import parse_farm_rank, parse_top


async def test_rate_limiter_spaces_requests():
    limiter = sfl_api.RateLimiter(0.2)
    stamps = []

    async def hit():
        async with limiter:
            stamps.append(time.monotonic())

    await asyncio.gather(*(hit() for _ in range(3)))
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 0.19 for g in gaps)


class _FakeResp:
    def __init__(self, data: bytes, chunk: int):
        self._data, self._chunk = data, chunk

    async def aiter_bytes(self, _size):
        for i in range(0, len(self._data), self._chunk):
            yield self._data[i:i + self._chunk]


async def test_gzip_lines_streaming_across_chunks_and_members():
    lines = [json.dumps({"id": i, "farm": {"x": "y" * 50}}) for i in range(200)]
    part1 = gzip.compress(("\n".join(lines[:100]) + "\n").encode())
    part2 = gzip.compress(("\n".join(lines[100:]) + "\n").encode())  # второй gzip-член
    got = [json.loads(line) async for line in sfl_api._AsyncGzipLines(_FakeResp(part1 + part2, 37))]
    assert [g["id"] for g in got] == list(range(200))


def test_pick_latest_dump():
    manifest = [
        {"filename": "2026-10-26/active.jsonl.gz", "modifiedAt": "2026-10-26T22:00:59.000Z"},
        {"filename": "2026-10-27/all.jsonl.gz", "modifiedAt": "2026-10-27T22:00:41.000Z"},
        {"filename": "2026-10-27/active.jsonl.gz", "modifiedAt": "2026-10-27T22:00:59.000Z"},
    ]
    latest = pick_latest(manifest, "active")
    assert latest["filename"] == "2026-10-27/active.jsonl.gz"
    assert dump_date_of(latest["filename"]).isoformat() == "2026-10-27"
    assert pick_latest([], "active") is None


def test_leaderboard_parsing():
    data = {
        "topTen": [{"rank": 1, "id": "gordy", "count": 8420, "farmId": 121500},
                   {"rank": 2, "id": "#77", "count": 8000, "farmId": 77}],
        "farmRankingDetails": [{"rank": 61, "id": "a", "count": 1204, "farmId": 98210},
                               {"rank": 62, "id": "sunny", "count": 1198, "farmId": 62559}],
    }
    top = parse_top(data)
    assert top[0] == {"rank": 1, "farm_id": 121500, "game_username": "gordy", "tickets": 8420,
                      "experience": None, "ascension": None}
    assert parse_farm_rank(data, 62559) == {"rank": 62, "tickets": 1198, "game_username": "sunny"}
    # ферма в топ-10: farmRankingDetails отсутствует — ранг из topTen
    assert parse_farm_rank({"topTen": data["topTen"]}, 77)["rank"] == 2
    assert parse_farm_rank({"topTen": data["topTen"], "farmRankingDetails": None}, 5) is None
