# sfl-collector

Сборщик игровых данных Sunflower Land для сайта goblincodex. Единственный сервис,
который ходит в SFL Community API: собирает данные в БД `sfl` и отдаёт их сайту
через собственный API (`sfl-collector-api`).

| Что собирается | Откуда | Как часто | Таблица |
|---|---|---|---|
| Лидерборд тикетов (топ-500 + фермеры бота) | `ticketLeaderboard` | раз в час | `top500_snapshots`, `ticket_leaderboard_snapshots` |
| Торговец в пещере | `farm.cave.merchantVisit` по топ-100 | раз в час | `cave_merchant_visits` |
| Задания недели | `farm.choreBoard.chores` по топ-100 | раз в час | `weekly_chores` |
| Баунти-борд (животные / Поппи) | `farm.bounties.requests` по топ-100 | раз в час | `bounty_requests` |
| Доставки за билеты (общая очередь) | консенсус `farm.delivery.orders` по топ-100 | раз в час | `ticket_delivery_consensus` |
| Доставки за монеты и Flower (статистика) | ночной дамп, все активные фермы | раз в сутки | `delivery_stats_daily` |
| У кого из топа есть предмет | аукционы, ручной запрос, ночной дамп | по событию | `item_checks`, `item_check_owners` |

## Как это работает

```
                ┌──────── sfl-collector (единственный клиент Community API) ────────┐
SFL API  ◄──────┤ RateLimiter: 1 запрос / 6 с, на 429 — пауза и повтор               │
                │  ├─ tickets       лидерборд тикетов             (1+ запрос в час) │
                │  ├─ top_farms     getFarms топ-100              (1–4 запроса в час)│
                │  ├─ item_checks   аукционы + ручные проверки    (по событию)       │
                │  └─ nightly_dump  манифест (1 запрос) + файл с CDN (без лимита)    │
                └──────────────────────────────┬────────────────────────────────────┘
                                               ▼
                                      Postgres, БД "sfl"
                                               ▼
              sfl-collector-api (только чтение БД)  ──shared-net──►  goblincodex (Astro SSR)
```

Один образ — три контейнера: `migrate` (одноразовый), `sfl-collector` (сборщики),
`sfl-collector-api` (FastAPI). Порт API наружу не публикуется: сайт ходит по `shared-net`
на `http://sfl-collector-api:8000`.

**Лимит API — по IP** (~1 запрос в 5 с, при перегрузе 1 в 10 с) и общий для всех сервисов
на VPS. Второй ключ его не увеличивает. Поэтому все запросы идут через один `RateLimiter`,
а остальные сервисы читают данные из БД.

### Общие данные — консенсус по топу
Торговец, задания и баунти одинаковы у всех игроков, но лежат внутри каждой фермы.
Коллектор берёт топ-100 лидерборда (они заходят каждый день) и выбирает самый частый
вариант — так ферма со старым состоянием не подменит текущие данные.
Поля `offers_share` и `generous_share` у торговца показывают, насколько топ согласен.

### Доставки
- `reward.sfl` — Flower, `reward.coins` — монеты: у каждой фермы свои → статистика по дампу.
- Пустой `reward` у NPC из `TICKET_REWARDS` — билеты. Очередь общая (сдал/пропустил — пришла
  следующая), поэтому «текущая» доставка — самый частый открытый заказ у топа (`share` — доля).
- API не отдаёт количество билетов: оно считается в игре
  (`src/features/game/events/landExpansion/deliver.ts`). В БД хранится база
  (`base_tickets`); сверху +2 за VIP, +1 за каждый бонусный предмет главы, x2 в doubleDelivery.

### Предметы у топа
- **`auction_end`** — как только Аукционист сохранил `auction_results`: победители ∩ топ-200.
  Без запросов к API.
- **`auction_plus_1h`, `auction_plus_24h`** — фактическое владение через 1 и 24 часа после конца
  аукциона (ловит покупки на маркетплейсе). Топ-200 = 2–4 запроса `getFarms`.
- **`nightly_YYYY-MM-DD`** — по каждому дампу для аукционов за последние 14 дней, по топ-500.
- **`manual`** — из админки сайта: строка в `item_check_requests` (не чаще раза в 10 минут).
- Тип `nft` (питомцы, buds) пока пропускается — у них свои структуры в ферме.

## Структура

```
collector/
  config.py           настройки из env
  db.py, migrate.py   пул asyncpg и автоприменение миграций
  sfl_api.py          клиент API: лимит, повторы на 429, getFarms с дозапросом skipped, стрим дампа
  runs.py             журнал запусков (collector_runs), последний топ лидерборда
  parsers/            чистые функции разбора фермы (покрыты тестами)
    deliveries.py     типы доставок, консенсус по билетам, статистика
    global_state.py   торговец, задания, баунти
    items.py          поиск предмета, победители аукциона
  jobs/               задачи: tickets_leaderboard, top_farms, item_checks, nightly_dump
  main.py             запуск всех циклов
api/main.py           API для сайта (FastAPI, только чтение + очередь ручных проверок)
migrations/001_init.sql
tests/
```

## API для сайта

Базовый адрес внутри `shared-net`: `http://sfl-collector-api:8000`. Документация Swagger —
`/docs` (открывается изнутри сети, например через SSH-туннель).

| Метод | Путь | Что отдаёт |
|---|---|---|
| GET | `/health` | проверка живости и доступа к БД |
| GET | `/sfl/status` | последний запуск каждого сборщика (для «обновлено N минут назад») |
| GET | `/sfl/merchant/current` | текущий визит торговца: предложения, `opens_at`, `closes_at`, `next_opens_at` |
| GET | `/sfl/merchant/history?limit=20` | прошлые визиты |
| GET | `/sfl/chores/current` | задания текущей недели |
| GET | `/sfl/bounties/current?category=animal\|poppy` | баунти-борд |
| GET | `/sfl/deliveries/tickets` | текущие билетные доставки (`items`, `base_tickets`, `share`, `variants`) |
| GET | `/sfl/deliveries/stats?type=coins\|flower\|tickets&npc=&date=` | статистика по ночному дампу |
| GET | `/sfl/items/{name}/owners?history=1` | последние проверки владения предметом у топа |
| GET | `/sfl/auctions/{auction_id}/top-owners` | все проверки по аукциону + `new_owners` (кто появился с прошлой проверки) |
| POST | `/admin/item-checks` | поставить ручную проверку в очередь (заголовок `X-Admin-Token`) |
| GET | `/admin/item-checks/{id}` | статус ручной проверки и владельцы (заголовок `X-Admin-Token`) |

Пример тела `POST /admin/item-checks`:
```json
{"item_name": "Pumpkin Hat", "item_type": "wearable", "top_n": 200}
```
Ответ `202`: `{"id": 1, "status": "pending", "queue_position": 1, ...}`. Коллектор выполняет
ручные проверки не чаще раза в 10 минут (`ITEM_CHECKS_MANUAL_COOLDOWN_SEC`).

`ADMIN_TOKEN` знает только сервер сайта: форма в админке Astro отправляет запрос со стороны
SSR, токен не попадает в браузер. Если `ADMIN_TOKEN` не задан, админские эндпоинты отвечают 503.

### Подключение на сайте (Astro SSR)
```ts
// .env сайта: SFL_DATA_API_URL=http://sfl-collector-api:8000
const API = import.meta.env.SFL_DATA_API_URL;

export async function getMerchant() {
  const r = await fetch(`${API}/sfl/merchant/current`);
  return r.ok ? r.json() : null;   // 404 — данных ещё нет
}

// только на сервере (endpoint/action Astro), не в клиентском коде!
export async function requestItemCheck(item_name: string, item_type: "wearable" | "collectible") {
  const r = await fetch(`${API}/admin/item-checks`, {
    method: "POST",
    headers: { "content-type": "application/json", "x-admin-token": import.meta.env.SFL_ADMIN_TOKEN },
    body: JSON.stringify({ item_name, item_type, top_n: 200 }),
  });
  return r.json();
}
```

## Деплой

1. Создать тег: `git tag v0.1.0 && git push --tags` → GitHub Actions прогонит тесты и соберёт
   `ghcr.io/dionis404/sfl-collector`.
2. Пакет в GHCR по умолчанию приватный — Portainer должен иметь доступ к registry
   (как для goblin-bot).
3. Portainer → Stacks → Add stack → Repository → этот репозиторий, compose `docker-compose.yml`.
   Переменные окружения — из `.env.example` (минимум `DB_PASSWORD`, `SFL_API_KEY`, `ADMIN_TOKEN`).
4. Пользователю БД нужны права CREATE TABLE в `sfl` (миграции) и SELECT/INSERT/UPDATE на
   `farmers`, `auctions`, `auction_results`.

### Переключение лидерборда из goblin-bot
Пока обе системы собирают лидерборд, в `top500_snapshots` будут двойные снимки. Порядок:
1. В goblin-bot: выключить `tickets_leaderboard_loop` и снизить `API_RATE_PER_SEC` до `0.15`
   (сейчас по умолчанию 2 запроса в секунду — в 10 раз выше лимита API).
2. Задеплоить goblin-bot, затем sfl-collector.
   До этого можно стартовать коллектор с `TICKETS_ENABLED=false`.

Еженедельный отчёт и сброс `tickets_excluded` в начале главы остаются в goblin-bot — они
работают с теми же таблицами.

## Запуск локально

```bash
pip install -r requirements-dev.txt
cp .env.example .env            # заполнить DB_* и SFL_API_KEY
python -m collector.migrate
python -m collector.jobs.top_farms        # любой job можно запустить отдельно
python -m collector.jobs.nightly_dump --force
python -m collector.main                  # все циклы
uvicorn api.main:app --port 8000          # API для сайта
```

Тесты: `pytest -q`. Интеграционные тесты и тесты API на настоящем Postgres (в CI гоняются автоматически) (пересоздаёт схему — только
тестовая БД!): `COLLECTOR_TEST_DB="127.0.0.1:5432:sfl_test:user:pass" pytest -q`.
