from collector.parsers.global_state import extract_bounties, extract_chores, extract_merchant


def test_merchant_consensus_ignores_stale_visit(farm_factory):
    farms = [farm_factory(generous=True), farm_factory(), farm_factory(), farm_factory(merchant_index=-3)]
    m = extract_merchant(farms)
    assert m["visit_index"] == -2
    assert m["farms_seen"] == 3
    assert m["generous_share"] == round(1 / 3, 3)
    assert m["offers_share"] == 1.0
    assert [o["slot"] for o in m["offers"]] == [1, 5]
    assert m["opens_at"].year == 2026


def test_merchant_none_without_cave():
    assert extract_merchant([{"cave": {}}, {}]) is None


def test_chores_strip_personal_progress(farm_factory):
    chores = extract_chores([farm_factory(), farm_factory()])
    assert {c["npc"] for c in chores} == {"peggy", "miranda"}
    peggy = next(c for c in chores if c["npc"] == "peggy")
    assert peggy["task"] == "Harvest Pepper 100 times"
    assert "initialProgress" not in peggy and "completedAt" not in peggy
    assert peggy["started_at"] is not None


def test_bounties_majority_board_and_categories(farm_factory):
    farms = [farm_factory(), farm_factory(), farm_factory(bounty_ids=("zz9999",))]
    bounties = {b["id"]: b for b in extract_bounties(farms)}
    assert set(bounties) == {"76abdc", "94cb57", "ab12cd"}
    assert bounties["76abdc"]["category"] == "animal"
    assert bounties["ab12cd"]["category"] == "poppy"
    assert bounties["94cb57"]["items"] == {"Gem": 5}
