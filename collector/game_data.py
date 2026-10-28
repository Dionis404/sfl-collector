"""
Данные игры из collector/data/game_data.json (генерируется scripts/sync_game_data.py
из исходников sunflower-land): главы, мутанты глав, магазин Стеллы, таблица уровней.

Расчёт уровня — порт src/features/game/lib/level.ts (getAscensionLevel,
getTotalBumpkinLevel, bandXp, levelXp, ascensionBaseline).
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "game_data.json"


@lru_cache(maxsize=1)
def load() -> dict:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- Главы ---

def chapters() -> list[dict]:
    return load()["chapters"]


def chapter_at(when: datetime | None = None) -> dict | None:
    """Глава, идущая в момент `when` (по умолчанию сейчас), или None."""
    when = when or datetime.now(timezone.utc)
    for ch in chapters():
        if _dt(ch["start"]) <= when < _dt(ch["end"]):
            return ch
    return None


def chapter_by_name(name: str) -> dict | None:
    return next((c for c in chapters() if c["name"] == name), None)


def chapter_items(chapter: dict) -> list[dict]:
    """
    Предметы главы для отслеживания: мутанты + магазин Стеллы.
    [{name, type: wearable|collectible, source: mutant|megastore, kind (для мутантов), tier (для магазина)}]
    """
    items = [
        {"name": m["name"], "type": m["type"], "source": "mutant", "kind": m["kind"], "tier": None}
        for m in chapter.get("mutants", [])
    ]
    items += [
        {"name": s["name"], "type": s["type"], "source": "megastore", "kind": None, "tier": s["tier"]}
        for s in chapter.get("megastore", [])
    ]
    return items


# --- Уровни ---

def _const(name: str) -> float:
    return load()["level"]["constants"][name]


@lru_cache(maxsize=1)
def _level_table() -> dict[int, int]:
    return {int(k): int(v) for k, v in load()["level"]["experience"].items()}


def _pre_max() -> int:
    return int(_const("PRE_ASCENSION_MAX_LEVEL"))


def _per_ascension() -> int:
    return int(_const("LEVELS_PER_ASCENSION"))


def bumpkin_level(experience: float) -> int:
    """Уровень до возвышения (1..150) — getBumpkinLevel."""
    table, cap = _level_table(), _pre_max()
    if experience >= table[cap]:
        return cap
    level = 1
    for lvl in range(1, cap + 1):
        if experience >= table[lvl]:
            level = lvl
        else:
            break
    return level


@lru_cache(maxsize=64)
def band_xp(ascension: int) -> int:
    """Опыт на всё возвышение `ascension`, округлённый до 5M (как в игре, ROUND_HALF_UP)."""
    base = Decimal(str(_const("ASCENSION_BAND_XP_BASE")))
    growth = Decimal(str(_const("ASCENSION_BAND_XP_GROWTH")))
    rounding = Decimal(str(_const("ASCENSION_BAND_XP_ROUNDING")))
    raw = base * growth ** (ascension - 1) / rounding
    return int(raw.quantize(Decimal(1), rounding=ROUND_HALF_UP) * rounding)


def _total_weight() -> float:
    ups = _per_ascension() - 1
    return ups + _const("ASCENSION_LEVEL_WEIGHT_PER_LEVEL") * (ups * _per_ascension() / 2)


def level_xp(ascension: int, n: int) -> float:
    return band_xp(ascension) * (1 + _const("ASCENSION_LEVEL_WEIGHT_PER_LEVEL") * n) / _total_weight()


def ascension_baseline(ascension: int) -> int:
    xp = _level_table()[_pre_max()]
    for b in range(1, ascension):
        xp += band_xp(b)
    return xp


@dataclass
class Level:
    ascension: int          # 0 = до возвышения
    level: int              # 1..150 при ascension 0, 0..50 после
    total_level: int        # сквозной уровень: A0 L120 -> 120, A1 L1 -> 151, A2 L25 -> 225
    ready_to_ascend: bool


def player_level(experience: float, ascension_level: int | None) -> Level:
    """getAscensionLevel + getTotalBumpkinLevel."""
    experience = float(experience or 0)
    a = int(ascension_level or 0)
    pre_max, per = _pre_max(), _per_ascension()

    if a < 1:
        lvl = bumpkin_level(experience)
        return Level(0, lvl, lvl, experience >= _level_table()[pre_max])

    baseline = ascension_baseline(a)
    if experience < baseline:
        lvl, ready = 0, False
    elif experience >= baseline + band_xp(a):
        lvl, ready = per, True
    else:
        lvl, start, ready = 1, baseline, False
        for n in range(1, per - 1):
            nxt = start + level_xp(a, n)
            if experience >= nxt:
                lvl, start = n + 1, nxt
            else:
                break
    return Level(a, lvl, pre_max + (a - 1) * per + lvl, ready)
