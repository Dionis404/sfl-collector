"""
Поиск предмета в ферме.

wearable    -> farm.wardrobe[name]   (надетые тоже числятся в wardrobe)
collectible -> farm.inventory[name]  (поставленные на ферму тоже числятся в inventory)
"""
from collections.abc import Mapping

SUPPORTED_TYPES = {"wearable", "collectible"}


def item_amount(farm: dict, item: str, item_type: str) -> float:
    if item_type == "wearable":
        source = (farm or {}).get("wardrobe") or {}
    elif item_type == "collectible":
        source = (farm or {}).get("inventory") or {}
    else:
        raise ValueError(f"Неподдерживаемый тип предмета: {item_type}")
    try:
        return float(source.get(item, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def find_owners(farms: Mapping[int, dict], item: str, item_type: str) -> dict[int, float]:
    """{farm_id: количество} для ферм, у которых предмет есть."""
    owners = {}
    for farm_id, farm in farms.items():
        amount = item_amount(farm, item, item_type)
        if amount > 0:
            owners[int(farm_id)] = amount
    return owners


def auction_winners(leaderboard: list[dict], supply: int | None) -> dict[int, dict]:
    """
    Победители аукциона из auction_results.leaderboard (пишет Аукционист):
    первые `supply` мест. {farm_id: строка лидерборда}.
    """
    rows = sorted(leaderboard or [], key=lambda r: r.get("rank") or 10**9)
    if supply:
        rows = [r for r in rows if (r.get("rank") or 10**9) <= supply]
    return {int(r["farmId"]): r for r in rows if r.get("farmId") is not None}
