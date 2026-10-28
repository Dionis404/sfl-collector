"""
Предметы главы (мутанты + магазин Стеллы) и профили топа — разбор ферм из дампа.
"""
from collections.abc import Iterable

from collector import game_data


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def item_amount(farm: dict, name: str, item_type: str | None) -> float:
    """wearable -> wardrobe, collectible -> inventory; тип не указан — ищем в обоих."""
    farm = farm or {}
    if item_type == "wearable":
        return _num((farm.get("wardrobe") or {}).get(name))
    if item_type == "collectible":
        return _num((farm.get("inventory") or {}).get(name))
    return _num((farm.get("inventory") or {}).get(name)) or _num((farm.get("wardrobe") or {}).get(name))


class ChapterItemStats:
    """Сколько ферм владеет каждым предметом главы — по всем активным и отдельно по топу."""

    def __init__(self, items: list[dict]):
        self.items = items
        self._acc = {(i["name"], i["type"]): [0, 0.0, 0, 0.0] for i in items}  # holders, total, top_h, top_t

    def add_farm(self, farm: dict, *, in_top: bool) -> None:
        for (name, typ), acc in self._acc.items():
            amount = item_amount(farm, name, typ)
            if amount > 0:
                acc[0] += 1
                acc[1] += amount
                if in_top:
                    acc[2] += 1
                    acc[3] += amount

    def rows(self) -> list[dict]:
        out = []
        for it in self.items:
            h, t, th, tt = self._acc[(it["name"], it["type"])]
            out.append({**it, "holders": h, "total": t, "top_holders": th, "top_total": tt})
        return out


def parse_watch_items(spec: str) -> dict[str, list[str]]:
    """'Cave Dwellers:Grinx's Hammer,Other;Ascension Age:X' -> {глава: [предметы]}."""
    out: dict[str, list[str]] = {}
    for part in (spec or "").split(";"):
        if ":" not in part:
            continue
        chapter, items = part.split(":", 1)
        names = [i.strip() for i in items.split(",") if i.strip()]
        if chapter.strip() and names:
            out[chapter.strip()] = names
    return out


def top_profile(farm: dict, top_row: dict | None, watched: Iterable[str]) -> dict:
    """Профиль игрока из топа: опыт, возвышение, уровень и отслеживаемые предметы."""
    farm = farm or {}
    experience = _num((farm.get("bumpkin") or {}).get("experience"))
    ascension = int((farm.get("island") or {}).get("ascensionLevel") or 0)
    lvl = game_data.player_level(experience, ascension)
    return {
        "rank": top_row["rank"] if top_row else None,
        "username": top_row["game_username"] if top_row else None,
        "tickets": top_row["tickets"] if top_row else None,
        "experience": experience,
        "ascension": lvl.ascension,
        "level": lvl.level,
        "total_level": lvl.total_level,
        "ready_to_ascend": lvl.ready_to_ascend,
        "watched_items": {name: item_amount(farm, name, None) for name in watched},
    }
