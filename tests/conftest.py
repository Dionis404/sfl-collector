"""Фермы в формате реального ответа /community/farms (урезанные до нужных полей)."""
import pytest

MERCHANT_OFFERS = [
    {"slot": 1, "cost": {"item": "Wood", "amount": 40}, "reward": {"item": "Magic Mushroom", "amount": 4},
     "limit": 3, "curiosity": False},
    {"slot": 5, "cost": {"item": "Wild Mushroom", "amount": 30},
     "reward": {"item": "Spaghetti al Limone", "amount": 5}, "limit": 2, "curiosity": True},
]


def make_farm(*, tywin_items=None, merchant_index=-2, generous=False, chores_started=1792975588460,
              bounty_ids=("76abdc", "94cb57", "ab12cd"), wardrobe=None, inventory=None):
    orders = [
        {"id": "a1", "from": "tango", "items": {"Banana": 10}, "reward": {"coins": 634, "items": {}}},
        {"id": "a2", "from": "grimbly", "items": {"Mashed Potato": 12}, "reward": {"sfl": 0.35}},
        {"id": "a3", "from": "tywin", "items": tywin_items or {"Gold": 3}, "reward": {}},
        {"id": "a4", "from": "bert", "items": {"Wood": 5}, "reward": {}, "completedAt": 1793000000000},
    ]
    bounties = {
        "76abdc": {"level": 12, "id": "76abdc", "name": "Chicken", "coins": 470},
        "94cb57": {"level": 4, "id": "94cb57", "name": "Chicken", "items": {"Gem": 5}},
        "ab12cd": {"id": "ab12cd", "name": "Blue Daffodil", "items": {"Mark": 20}},
        "zz9999": {"id": "zz9999", "name": "Cow", "coins": 200},
    }
    return {
        "delivery": {"orders": orders},
        "cave": {
            "merchantVisit": {
                "index": merchant_index, "opensAt": 1793059200000, "closesAt": 1793318400000,
                "nextOpensAt": 1793318400000, "generous": generous, "offers": list(MERCHANT_OFFERS),
            }
        },
        "choreBoard": {
            "chores": {
                "peggy": {"name": "Harvest Pepper 100 times", "reward": {"items": {"Ticket": 3}},
                          "initialProgress": 8364, "startedAt": chores_started, "completedAt": 1793122273205},
                "miranda": {"name": "Grow Purple Cosmos 6 times", "reward": {"items": {"Ticket": 2}},
                            "initialProgress": 385, "startedAt": chores_started},
            }
        },
        "bounties": {"requests": [bounties[i] for i in bounty_ids]},
        "wardrobe": wardrobe or {},
        "inventory": inventory or {},
    }


@pytest.fixture
def farm_factory():
    return make_farm
