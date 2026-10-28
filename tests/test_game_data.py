"""Сверка с тестами самой игры: src/features/game/lib/level.test.ts и данные глав."""
from datetime import datetime, timezone

from collector import game_data as g


def test_band_xp_matches_game():
    assert [g.band_xp(a) for a in range(1, 7)] == [
        50_000_000, 75_000_000, 105_000_000, 150_000_000, 220_000_000, 320_000_000,
    ]


def test_level_ups_sum_to_band():
    for a in range(1, 5):
        assert abs(sum(g.level_xp(a, n) for n in range(1, 50)) - g.band_xp(a)) < 1e-3


def test_baseline_stacks():
    table = g._level_table()
    assert g.ascension_baseline(1) == table[150]
    for a in range(1, 5):
        assert g.ascension_baseline(a) + g.band_xp(a) == g.ascension_baseline(a + 1)


def test_pre_ascension_levels():
    assert g.player_level(2154, 0).level == 6
    assert g.player_level(2155, 0).level == 7
    assert g.player_level(2156, 0).level == 7
    table = g._level_table()
    assert g.player_level(table[150], 0).level == 150
    assert g.player_level(1e15, 0).level == 150
    assert g.player_level(table[150] - 1, 0).ready_to_ascend is False
    assert g.player_level(table[150], 0).ready_to_ascend is True


def test_ascension_levels():
    b1 = g.ascension_baseline(1)
    assert g.player_level(b1 - 100, 1).level == 0
    assert g.player_level(b1, 1).level == 1
    assert g.player_level(b1 + g.level_xp(1, 1), 1).level == 2
    assert g.player_level(g.ascension_baseline(2) - 1, 1).level == 49
    top = g.player_level(g.ascension_baseline(2), 1)
    assert (top.level, top.ready_to_ascend) == (50, True)
    assert g.player_level(g.ascension_baseline(5), 1).level == 50


def test_total_level():
    b1 = g.ascension_baseline(1)
    assert g.player_level(b1, 1).total_level == 151           # A1 L1
    assert g.player_level(g.ascension_baseline(2), 1).total_level == 200   # A1 L50
    assert g.player_level(2155, 0).total_level == 7
    assert g.player_level(None, None).total_level == 1


def test_chapters():
    cd = g.chapter_by_name("Cave Dwellers")
    assert cd["number"] == 16
    assert g.chapter_at(datetime(2026, 10, 28, tzinfo=timezone.utc))["name"] == "Ascension Age"
    assert g.chapter_at(datetime(2026, 11, 2, tzinfo=timezone.utc))["name"] == "Cave Dwellers"

    items = g.chapter_items(cd)
    mutants = {i["name"]: i for i in items if i["source"] == "mutant"}
    assert mutants["Mushroom Chicken"]["kind"] == "chicken"
    assert {"Manatee", "Betta", "Macropinna"} <= set(mutants)
    store = {i["name"]: i for i in items if i["source"] == "megastore"}
    assert store["Duende Hat"]["type"] == "wearable" and store["Duende Hat"]["tier"] == "basic"
    assert store["Mining Drill"]["type"] == "collectible"
