"""
Общие для всех игроков данные, которые лежат внутри каждой фермы:
торговец в пещере (farm.cave.merchantVisit), задания недели
(farm.choreBoard.chores) и баунти-борд (farm.bounties.requests).

Берём не одну ферму, а консенсус по топу: так случайная ферма со старым
состоянием (давно не заходил) не подменит текущие данные.
"""
import json
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone

ANIMAL_BOUNTIES = {"Chicken", "Cow", "Sheep"}


def ms_to_dt(ms) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def _key(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


# --- Торговец ---

def _clean_offers(offers: list[dict]) -> list[dict]:
    return sorted(offers or [], key=lambda o: o.get("slot", 0))


def extract_merchant(farms: Iterable[dict]) -> dict | None:
    visits = []
    for farm in farms:
        visit = ((farm or {}).get("cave") or {}).get("merchantVisit")
        if visit and visit.get("offers") is not None:
            visits.append(visit)
    if not visits:
        return None

    # текущий визит — самый частый index среди ферм (у отставших может быть прошлый)
    index, _ = Counter(v.get("index") for v in visits).most_common(1)[0]
    same_visit = [v for v in visits if v.get("index") == index]

    offers_counter = Counter(_key(_clean_offers(v["offers"])) for v in same_visit)
    offers_key, offers_votes = offers_counter.most_common(1)[0]
    sample = same_visit[0]
    return {
        "visit_index": int(index),
        "opens_at": ms_to_dt(sample.get("opensAt")),
        "closes_at": ms_to_dt(sample.get("closesAt")),
        "next_opens_at": ms_to_dt(sample.get("nextOpensAt")),
        "offers": json.loads(offers_key),
        "generous_share": round(sum(1 for v in same_visit if v.get("generous")) / len(same_visit), 3),
        "offers_share": round(offers_votes / len(same_visit), 3),
        "farms_seen": len(same_visit),
    }


# --- Задания недели ---

def extract_chores(farms: Iterable[dict]) -> list[dict]:
    """[{npc, task, reward, started_at}] — консенсус набора заданий."""
    boards = []
    for farm in farms:
        chores = ((farm or {}).get("choreBoard") or {}).get("chores")
        if chores:
            # убираем личный прогресс игрока, оставляем только общее
            board = {
                npc: {"task": c.get("name"), "reward": c.get("reward"), "startedAt": c.get("startedAt")}
                for npc, c in chores.items()
            }
            boards.append(board)
    if not boards:
        return []

    def shape(board):  # набор заданий без времени старта
        return _key({npc: (c["task"], c["reward"]) for npc, c in board.items()})

    best_shape, _ = Counter(shape(b) for b in boards).most_common(1)[0]
    matching = [b for b in boards if shape(b) == best_shape]
    started = Counter(c["startedAt"] for b in matching for c in b.values() if c["startedAt"])
    started_at = ms_to_dt(started.most_common(1)[0][0]) if started else None

    sample = matching[0]
    return [
        {"npc": npc, "task": c["task"], "reward": c["reward"], "started_at": started_at}
        for npc, c in sorted(sample.items())
    ]


# --- Баунти-борд ---

def bounty_category(name: str) -> str:
    return "animal" if name in ANIMAL_BOUNTIES else "poppy"


def extract_bounties(farms: Iterable[dict]) -> list[dict]:
    boards = []
    for farm in farms:
        reqs = ((farm or {}).get("bounties") or {}).get("requests")
        if reqs:
            boards.append(reqs)
    if not boards:
        return []

    ids_key = lambda reqs: tuple(sorted(r.get("id") for r in reqs))  # noqa: E731
    best, _ = Counter(ids_key(b) for b in boards).most_common(1)[0]
    sample = next(b for b in boards if ids_key(b) == best)
    return [
        {
            "id": r["id"],
            "name": r.get("name"),
            "category": bounty_category(r.get("name", "")),
            "level": r.get("level"),
            "coins": r.get("coins"),
            "items": r.get("items"),
        }
        for r in sample
    ]
