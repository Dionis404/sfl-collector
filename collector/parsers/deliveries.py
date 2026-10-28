"""
Доставки (farm.delivery.orders).

Тип доставки определяется по reward:
  reward.sfl    -> Flower (гоблины: grimbly, grimtooth, gordo, ...)
  reward.coins  -> монеты
  reward пустой и NPC в TICKET_REWARDS -> билеты. Количество билетов API
  не отдаёт: игра считает его на клиенте (см. sunflower-land
  src/features/game/events/landExpansion/deliver.ts, generateDeliveryTickets):
  база из TICKET_REWARDS + 2 за VIP + 1 за каждый бонусный предмет главы,
  и x2 в событие doubleDelivery.

Билетные доставки идут по общей очереди у всех игроков (сдал/пропустил —
пришла следующая), поэтому "текущая" доставка = самый частый открытый
заказ у NPC среди активных игроков (консенсус по топу лидерборда).
Монетные и Flower-доставки у каждой фермы свои — по ним считаем статистику.
"""
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

TICKET_REWARDS: dict[str, int] = {
    "pumpkin' pete": 1,
    "bert": 2,
    "miranda": 2,
    "finley": 2,
    "raven": 4,
    "finn": 5,
    "timmy": 5,
    "cornwell": 3,
    "tywin": 10,
    "jester": 4,
    "pharaoh": 6,
}
VIP_TICKET_BONUS = 2


def _num(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def classify_order(order: dict) -> tuple[str, float]:
    """('flower'|'coins'|'tickets'|'other', награда). Для билетов — база без бонусов."""
    reward = order.get("reward") or {}
    if _num(reward.get("sfl")):
        return "flower", _num(reward["sfl"])
    if _num(reward.get("coins")):
        return "coins", _num(reward["coins"])
    npc = order.get("from")
    if npc in TICKET_REWARDS:
        return "tickets", float(TICKET_REWARDS[npc])
    return "other", 0.0


def is_open(order: dict) -> bool:
    return not order.get("completedAt")


def items_key(items: dict) -> tuple:
    """Нормализованный ключ «что просят»: (('Banana', 10.0), ...)."""
    return tuple(sorted((name, _num(qty)) for name, qty in (items or {}).items()))


def items_label(items: dict) -> str:
    return " + ".join(sorted((items or {}).keys())) or "?"


def farm_orders(farm: dict) -> list[dict]:
    return ((farm or {}).get("delivery") or {}).get("orders") or []


@dataclass
class TicketConsensus:
    npc: str
    items: dict
    base_tickets: int
    votes: int
    total: int
    share: float
    variants: list[dict] = field(default_factory=list)


def ticket_consensus(farms: Iterable[dict]) -> list[TicketConsensus]:
    """Самый частый открытый билетный заказ у каждого NPC среди переданных ферм."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for farm in farms:
        for order in farm_orders(farm):
            npc = order.get("from")
            if npc in TICKET_REWARDS and is_open(order):
                votes[npc][items_key(order.get("items"))] += 1

    result = []
    for npc, counter in sorted(votes.items()):
        total = sum(counter.values())
        (top_key, top_votes) = counter.most_common(1)[0]
        result.append(
            TicketConsensus(
                npc=npc,
                items={name: qty for name, qty in top_key},
                base_tickets=TICKET_REWARDS[npc],
                votes=top_votes,
                total=total,
                share=round(top_votes / total, 3),
                variants=[
                    {"items": {n: q for n, q in key}, "votes": v} for key, v in counter.most_common()
                ],
            )
        )
    return result


class DeliveryStats:
    """Накопитель статистики доставок по многим фермам (для ночного дампа)."""

    def __init__(self) -> None:
        # (npc, reward_type, item_label) -> [orders, sum, min, max]
        self._acc: dict[tuple[str, str, str], list[float]] = {}

    def add_farm(self, farm: dict) -> None:
        for order in farm_orders(farm):
            kind, reward = classify_order(order)
            if kind == "other":
                continue
            key = (order.get("from") or "?", kind, items_label(order.get("items")))
            acc = self._acc.get(key)
            if acc is None:
                self._acc[key] = [1, reward, reward, reward]
            else:
                acc[0] += 1
                acc[1] += reward
                acc[2] = min(acc[2], reward)
                acc[3] = max(acc[3], reward)

    def rows(self) -> list[dict]:
        return [
            {
                "npc": npc,
                "reward_type": kind,
                "item": item,
                "orders": int(n),
                "avg_reward": round(total / n, 4),
                "min_reward": mn,
                "max_reward": mx,
            }
            for (npc, kind, item), (n, total, mn, mx) in sorted(self._acc.items())
        ]
