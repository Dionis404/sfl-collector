from collector.parsers.deliveries import DeliveryStats, classify_order, ticket_consensus


def test_classify_order_types():
    assert classify_order({"from": "grimbly", "reward": {"sfl": 0.35}}) == ("flower", 0.35)
    assert classify_order({"from": "tango", "reward": {"coins": 634, "items": {}}}) == ("coins", 634.0)
    assert classify_order({"from": "tywin", "reward": {}}) == ("tickets", 10.0)
    assert classify_order({"from": "pumpkin' pete", "reward": None}) == ("tickets", 1.0)
    assert classify_order({"from": "unknown", "reward": {}}) == ("other", 0.0)


def test_ticket_consensus_majority_and_completed_ignored(farm_factory):
    farms = [farm_factory() for _ in range(3)] + [farm_factory(tywin_items={"Iron": 7})]
    result = {r.npc: r for r in ticket_consensus(farms)}

    tywin = result["tywin"]
    assert tywin.items == {"Gold": 3.0}
    assert (tywin.votes, tywin.total, tywin.share) == (3, 4, 0.75)
    assert len(tywin.variants) == 2
    assert tywin.base_tickets == 10
    # у bert заказ выполнен — в консенсус не попадает
    assert "bert" not in result
    # монетные/Flower NPC не считаются билетными
    assert "tango" not in result and "grimbly" not in result


def test_delivery_stats(farm_factory):
    stats = DeliveryStats()
    for _ in range(2):
        stats.add_farm(farm_factory())
    rows = {(r["npc"], r["reward_type"]): r for r in stats.rows()}
    assert rows[("tango", "coins")]["orders"] == 2
    assert rows[("tango", "coins")]["avg_reward"] == 634
    assert rows[("grimbly", "flower")]["item"] == "Mashed Potato"
    assert rows[("tywin", "tickets")]["orders"] == 2
