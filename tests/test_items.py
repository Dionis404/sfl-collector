import pytest

from collector.parsers.items import auction_winners, find_owners, item_amount


def test_find_owners_wearable_and_collectible(farm_factory):
    farms = {
        1: farm_factory(wardrobe={"Pumpkin Hat": 1}),
        2: farm_factory(inventory={"Pumpkin Hat": "2"}),   # не тот тип — не считается
        3: farm_factory(inventory={"Golden Gnome": "3"}),
    }
    assert find_owners(farms, "Pumpkin Hat", "wearable") == {1: 1.0}
    assert find_owners(farms, "Golden Gnome", "collectible") == {3: 3.0}


def test_item_amount_rejects_unknown_type():
    with pytest.raises(ValueError):
        item_amount({}, "X", "nft")


def test_auction_winners_respects_supply():
    lb = [{"rank": 2, "farmId": 20}, {"rank": 1, "farmId": 10}, {"rank": 3, "farmId": 30}]
    assert set(auction_winners(lb, supply=2)) == {10, 20}
    assert set(auction_winners(lb, supply=None)) == {10, 20, 30}
