-- sfl-collector, миграция 002: предметы главы и профили топа лидерборда.

-- Мутанты главы и магазин Стеллы: сколько игроков владеет (по ночному дампу).
CREATE TABLE IF NOT EXISTS chapter_item_stats_daily (
    dump_date    DATE    NOT NULL,
    chapter      TEXT    NOT NULL,
    item         TEXT    NOT NULL,
    item_type    TEXT    NOT NULL,      -- wearable | collectible
    source       TEXT    NOT NULL,      -- mutant | megastore
    kind         TEXT,                  -- для мутантов: chicken | cow | sheep | flower | fish
    tier         TEXT,                  -- для магазина: basic | rare | epic | mega
    holders      INTEGER NOT NULL,      -- активных ферм, у которых предмет есть
    total        NUMERIC NOT NULL,      -- сколько штук всего у активных ферм
    top_holders  INTEGER NOT NULL,      -- из топ-N лидерборда
    top_total    NUMERIC NOT NULL,
    PRIMARY KEY (dump_date, item, item_type)
);

-- Профиль топ-N лидерборда на дату дампа: опыт, уровень, возвышение, отслеживаемые предметы.
CREATE TABLE IF NOT EXISTS top_player_profiles (
    dump_date        DATE    NOT NULL,
    farm_id          BIGINT  NOT NULL,
    rank             INTEGER,
    username         TEXT,
    tickets          INTEGER,
    experience       NUMERIC,
    ascension        INTEGER,           -- уровень возвышения (0 = не возвышался)
    level            INTEGER,           -- уровень внутри возвышения (1..150 при 0, 0..50 после)
    total_level      INTEGER,           -- сквозной уровень (A1 L1 = 151)
    ready_to_ascend  BOOLEAN,
    watched_items    JSONB,             -- {"Grinx's Hammer": 1} — предметы, отслеживаемые в эту главу
    PRIMARY KEY (dump_date, farm_id)
);
CREATE INDEX IF NOT EXISTS idx_top_player_profiles_farm ON top_player_profiles (farm_id, dump_date DESC);

-- Последние известные опыт и возвышение топа — обновляется каждый час из лидерборда
-- (в ответе ticketLeaderboard они уже есть, лишних запросов не нужно).
CREATE TABLE IF NOT EXISTS top_player_latest (
    farm_id      BIGINT PRIMARY KEY,
    rank         INTEGER,
    username     TEXT,
    tickets      INTEGER,
    experience   NUMERIC,
    ascension    INTEGER,
    level        INTEGER,
    total_level  INTEGER,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_top_player_latest_rank ON top_player_latest (rank);

-- Состав баунти-борда во времени: новая строка, только когда меняется набор запросов.
-- Нужна для истории: отдельные запросы в bounty_requests не знают, к какому борду относятся.
CREATE TABLE IF NOT EXISTS bounty_board_snapshots (
    id          SERIAL PRIMARY KEY,
    request_ids TEXT[]      NOT NULL,   -- отсортированные id запросов
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bounty_board_snapshots_seen ON bounty_board_snapshots (first_seen DESC);

-- Борд, уже собранный до этой миграции, — первой записью истории
INSERT INTO bounty_board_snapshots (request_ids, first_seen, last_seen)
SELECT array_agg(id ORDER BY id COLLATE "C"), min(first_seen), max(last_seen)
FROM bounty_requests
WHERE last_seen = (SELECT max(last_seen) FROM bounty_requests)
HAVING count(*) > 0;
