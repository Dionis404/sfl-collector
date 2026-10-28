"""
Вытаскивает из исходников Sunflower Land всё, что коллектору нужно знать об игре,
и сохраняет в collector/data/game_data.json:

- главы: номер, название, даты начала/конца      (src/features/game/types/chapters.ts)
- мутанты каждой главы: курица/корова/овца/цветок/рыбы (.../types/chapterMutants.ts)
- магазин Стеллы (megastore) по главам и уровням   (.../types/megastore.ts)
- таблица опыта уровней и константы возвышения     (src/features/game/lib/level.ts)

Запуск (раз в главу или когда в игре обновили магазин):
    python scripts/sync_game_data.py                    # скачать с GitHub (ветка main)
    python scripts/sync_game_data.py --src ../sunflower-land   # из локальной копии

Потом закоммитить collector/data/game_data.json и собрать образ.
"""
import argparse
import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RAW = "https://raw.githubusercontent.com/sunflower-land/sunflower-land/main/"
FILES = {
    "chapters": "src/features/game/types/chapters.ts",
    "mutants": "src/features/game/types/chapterMutants.ts",
    "megastore": "src/features/game/types/megastore.ts",
    "level": "src/features/game/lib/level.ts",
}
OUT = Path(__file__).resolve().parent.parent / "collector" / "data" / "game_data.json"
TIERS = ("basic", "rare", "epic", "mega")


def read_sources(src: str | None) -> dict[str, str]:
    out = {}
    for key, rel in FILES.items():
        if src:
            out[key] = (Path(src) / rel).read_text(encoding="utf-8")
        else:
            with urllib.request.urlopen(RAW + rel, timeout=60) as r:  # noqa: S310 — фиксированный URL
                out[key] = r.read().decode("utf-8")
    return out


def block_after(text: str, marker: str) -> str:
    """Содержимое фигурных/квадратных скобок, открывающихся после marker."""
    start = text.index(marker)
    i = start + len(marker)
    while text[i] not in "{[":
        i += 1
    open_ch = text[i]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for j in range(i, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    raise ValueError(f"Не закрыт блок после {marker!r}")


def top_level_entries(body: str) -> list[tuple[str, str]]:
    """Ключи верхнего уровня объекта: [(ключ, значение-текстом)]."""
    entries, depth, key, val_start, i = [], 0, None, None, 0
    key_re = re.compile(r'\s*(?:"([^"]+)"|([A-Za-z_][\w]*|\d+))\s*:\s*')
    while i < len(body):
        ch = body[i]
        if depth == 0 and key is None:
            m = key_re.match(body, i)
            if m:
                key = m.group(1) or m.group(2)
                i = m.end()
                val_start = i
                continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth -= 1
        elif ch == "," and depth == 0 and key is not None:
            entries.append((key, body[val_start:i].strip()))
            key = None
        elif ch == "/" and body[i:i + 2] == "//":  # комментарий до конца строки
            i = body.index("\n", i) if "\n" in body[i:] else len(body)
            continue
        i += 1
    if key is not None and body[val_start:].strip():
        entries.append((key, body[val_start:].strip().rstrip(",")))
    return entries


def parse_chapters(ts: str) -> list[dict]:
    order = {k: int(v) for k, v in top_level_entries(block_after(ts, "CHAPTER_ORDER: Record<ChapterName, number> ="))}
    dates = {}
    for name, val in top_level_entries(block_after(ts, "CHAPTERS: Record<ChapterName, ChapterDates> =")):
        start = re.search(r'startDate:\s*new Date\("([^"]+)"\)', val).group(1)
        end = re.search(r'endDate:\s*new Date\("([^"]+)"\)', val).group(1)
        dates[name] = (start, end)
    return [
        {"number": order[name], "name": name, "start": dates[name][0], "end": dates[name][1]}
        for name in sorted(order, key=order.get)
    ]


def parse_mutants(ts: str) -> dict[str, list[dict]]:
    body = block_after(ts, "CHAPTER_MUTANTS: Record<MutantsChapterName, ChapterMutantsData> =")
    out = {}
    for chapter, val in top_level_entries(body):
        items = []
        for kind in ("Chicken", "Cow", "Sheep", "Flower"):
            m = re.search(rf'\b{kind}:\s*"([^"]+)"', val)
            if m:
                items.append({"name": m.group(1), "kind": kind.lower(), "type": "collectible"})
        fish = re.search(r"\bFish:\s*\[([^\]]*)\]", val)
        for name in re.findall(r'"([^"]+)"', fish.group(1) if fish else ""):
            items.append({"name": name, "kind": "fish", "type": "collectible"})
        out[chapter] = items
    return out


def _store_items(text: str) -> list[dict]:
    return [
        {"name": name, "type": typ}
        for typ, name in re.findall(r'\b(wearable|collectible):\s*"([^"]+)"', text)
    ]


def parse_megastore(ts: str) -> dict[str, list[dict]]:
    mapping = top_level_entries(block_after(ts, "MEGASTORE: Record<ChapterName, ChapterStore> ="))
    out = {}
    for chapter, val in mapping:
        if val == "EMPTY_SEASONAL_STORE":
            continue
        store_body = block_after(ts, f"const {val}: ChapterStore =") if re.fullmatch(r"[A-Z_]+", val) else val.strip("{}")
        items = []
        for tier, tier_val in top_level_entries(store_body):
            if tier not in TIERS:
                continue
            arr = re.search(r"items:\s*([A-Z_]+)\s*,?", tier_val)
            text = block_after(ts, f"const {arr.group(1)}: ChapterStoreItem[] =") if arr else tier_val
            for it in _store_items(text):
                items.append({**it, "tier": tier})
        # один предмет может стоять в двух уровнях (напр. epic и mega у Pharaoh) — оставляем первый
        seen, unique = set(), []
        for it in items:
            if (it["name"], it["type"]) not in seen:
                seen.add((it["name"], it["type"]))
                unique.append(it)
        out[chapter] = unique
    return out


def parse_level(ts: str) -> dict:
    body = block_after(ts, "LEVEL_EXPERIENCE: Record<BumpkinLevel, number> =")
    table = {int(k): int(v.replace("_", "")) for k, v in top_level_entries(body)}
    consts = {}
    for name in ("PRE_ASCENSION_MAX_LEVEL", "LEVELS_PER_ASCENSION", "ASCENSION_BAND_XP_BASE",
                 "ASCENSION_BAND_XP_GROWTH", "ASCENSION_BAND_XP_ROUNDING", "ASCENSION_LEVEL_WEIGHT_PER_LEVEL"):
        m = re.search(rf"\b{name}(?::\s*\w+)?\s*=\s*([\d_.]+)", ts)
        consts[name] = float(m.group(1).replace("_", ""))
    return {"experience": table, "constants": consts}


def build(src: str | None) -> dict:
    s = read_sources(src)
    chapters = parse_chapters(s["chapters"])
    mutants = parse_mutants(s["mutants"])
    store = parse_megastore(s["megastore"])
    for ch in chapters:
        ch["mutants"] = mutants.get(ch["name"], [])
        ch["megastore"] = store.get(ch["name"], [])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": src or RAW,
        "chapters": chapters,
        "level": parse_level(s["level"]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", help="путь к локальной копии репозитория sunflower-land")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    data = build(args.src)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    last = data["chapters"][-1]
    print(f"Глав: {len(data['chapters'])}, последняя: {last['number']} {last['name']} "
          f"(мутантов {len(last['mutants'])}, магазин {len(last['megastore'])}), "
          f"уровней в таблице: {len(data['level']['experience'])} -> {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
