"""
Разовая выгрузка статистики игроков из последнего ночного дампа SFL.

По каждой ферме: уровень (из опыта бампкина), тип острова, число расширений,
навыки бампкина. Результат — CSV (stdout в файл): дальше конвертируется в
Excel локально.

Запуск внутри контейнера sfl-collector:
    python /tmp/dump_player_stats.py /tmp/player_stats.csv
"""
import asyncio
import csv
import sys
from collections import Counter

from collector import db, game_data, sfl_api


def pick_latest(manifest: list[dict], kind: str) -> dict | None:
    files = [f for f in manifest if f.get("filename", "").endswith(f"/{kind}.jsonl.gz")]
    return max(files, key=lambda f: f.get("modifiedAt") or "") if files else None


async def main(path: str) -> None:
    await db.get_pool()
    manifest = await sfl_api.get_dump_manifest()
    latest = pick_latest(manifest, "active")
    if not latest:
        raise SystemExit("Дамп ещё не опубликован")
    filename = latest["filename"]
    print(f"Дамп: {filename}", file=sys.stderr)

    skill_popularity: Counter[str] = Counter()
    island_counts: Counter[str] = Counter()

    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["farm_id", "username", "level", "total_level", "ascension",
                    "island", "expansions", "skills_count", "skills"])
        farms = 0
        async for line in sfl_api.stream_dump(filename):
            farm = line.get("farm") or {}
            fid = line.get("id")
            bumpkin = farm.get("bumpkin") or {}
            island = farm.get("island") or {}
            lvl = game_data.player_level(bumpkin.get("experience") or 0, island.get("ascensionLevel"))
            skills = bumpkin.get("skills") or {}
            skills_str = "; ".join(
                f"{name}{f' ×{n}' if isinstance(n, int) and n > 1 else ''}"
                for name, n in sorted(skills.items())
            )
            w.writerow([
                fid,
                farm.get("username") or "",
                lvl.level,
                lvl.total_level,
                lvl.ascension,
                island.get("type") or "basic",
                island.get("previousExpansions") or 0,
                len(skills),
                skills_str,
            ])
            for name in skills:
                skill_popularity[name] += 1
            island_counts[island.get("type") or "basic"] += 1
            farms += 1
            if farms % 20000 == 0:
                print(f"обработано {farms}", file=sys.stderr)

    # Сводки — отдельными файлами рядом.
    with open(path.replace(".csv", "_skills.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["skill", "players"])
        w.writerows(skill_popularity.most_common())
    with open(path.replace(".csv", "_islands.csv"), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["island", "farms"])
        w.writerows(island_counts.most_common())

    print(f"Готово: {farms} ферм -> {path}", file=sys.stderr)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/player_stats.csv"
    try:
        asyncio.run(main(out))
    finally:
        asyncio.run(sfl_api.close())
        asyncio.run(db.close_pool())
