from collector import game_data
from collector.parsers.chapter_items import ChapterItemStats, item_amount, parse_watch_items, top_profile


def test_item_amount_by_type():
    farm = {"inventory": {"Mushroom Chicken": "2", "Grinx's Hammer": "1"}, "wardrobe": {"Duende Hat": 1}}
    assert item_amount(farm, "Mushroom Chicken", "collectible") == 2
    assert item_amount(farm, "Duende Hat", "wearable") == 1
    assert item_amount(farm, "Duende Hat", "collectible") == 0  # не тот тип
    assert item_amount(farm, "Grinx's Hammer", None) == 1       # тип неизвестен — ищем везде
    assert item_amount(farm, "Duende Hat", None) == 1
    assert item_amount({}, "X", None) == 0


def test_chapter_item_stats_all_and_top():
    items = game_data.chapter_items(game_data.chapter_by_name("Cave Dwellers"))
    stats = ChapterItemStats(items)
    stats.add_farm({"inventory": {"Mushroom Chicken": 1}, "wardrobe": {"Duende Hat": 1}}, in_top=True)
    stats.add_farm({"inventory": {"Mushroom Chicken": 3}}, in_top=False)
    stats.add_farm({}, in_top=False)
    rows = {r["name"]: r for r in stats.rows()}
    chick = rows["Mushroom Chicken"]
    assert (chick["holders"], chick["total"], chick["top_holders"], chick["top_total"]) == (2, 4, 1, 1)
    assert chick["source"] == "mutant" and chick["kind"] == "chicken"
    hat = rows["Duende Hat"]
    assert (hat["holders"], hat["top_holders"], hat["source"], hat["tier"]) == (1, 1, "megastore", "basic")
    assert rows["Betta"]["holders"] == 0  # ни у кого нет — строка всё равно есть


def test_parse_watch_items():
    assert parse_watch_items("Cave Dwellers:Grinx's Hammer") == {"Cave Dwellers": ["Grinx's Hammer"]}
    assert parse_watch_items("A:x, y;B:z;;broken") == {"A": ["x", "y"], "B": ["z"]}
    assert parse_watch_items("") == {}


def test_top_profile_level_and_watched():
    b1 = game_data.ascension_baseline(1)
    farm = {"bumpkin": {"experience": b1}, "island": {"ascensionLevel": 1}, "inventory": {"Grinx's Hammer": 1}}
    row = {"rank": 3, "game_username": "goblin", "tickets": 900}
    p = top_profile(farm, row, ["Grinx's Hammer"])
    assert (p["ascension"], p["level"], p["total_level"]) == (1, 1, 151)
    assert p["watched_items"] == {"Grinx's Hammer": 1.0}
    assert (p["rank"], p["username"], p["tickets"]) == (3, "goblin", 900)

    p0 = top_profile({"bumpkin": {"experience": 2155}}, None, [])
    assert (p0["ascension"], p0["level"], p0["rank"], p0["watched_items"]) == (0, 7, None, {})
