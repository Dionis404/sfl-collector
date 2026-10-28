-- sfl-collector, миграция 001: все таблицы коллектора. БД: sfl.
-- Таблицы лидерборда (top500_snapshots, ticket_leaderboard_snapshots) принадлежат
-- goblin-bot и уже существуют; IF NOT EXISTS — чтобы коллектор поднимался и на пустой БД.

-- ===== Лидерборд тикетов (те же таблицы, что у goblin-bot) =====
CREATE TABLE IF NOT EXISTS top500_snapshots (
    id            SERIAL PRIMARY KEY,
    rank          INTEGER     NOT NULL,
    farm_id       BIGINT      NOT NULL,
    game_username TEXT,
    tickets       INTEGER     NOT NULL,
    taken_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_top500_snapshots_taken_at ON top500_snapshots (taken_at DESC);
CREATE INDEX IF NOT EXISTS idx_top500_snapshots_taken_rank ON top500_snapshots (taken_at, rank);

CREATE TABLE IF NOT EXISTS ticket_leaderboard_snapshots (
    id            SERIAL PRIMARY KEY,
    farm_id       BIGINT      NOT NULL,
    rank          INTEGER     NOT NULL,
    tickets       INTEGER     NOT NULL,
    game_username TEXT,
    taken_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ticket_snapshots_farm_taken
    ON ticket_leaderboard_snapshots (farm_id, taken_at DESC);

-- ===== Журнал запусков (что и когда собрали) =====
CREATE TABLE IF NOT EXISTS collector_runs (
    id          SERIAL PRIMARY KEY,
    job         TEXT        NOT NULL,          -- tickets | top_farms | item_check | nightly_dump
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT        NOT NULL DEFAULT 'running',  -- running | ok | error
    stats       JSONB,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS idx_collector_runs_job ON collector_runs (job, started_at DESC);

-- ===== Торговец в пещере =====
-- Одна строка на визит торговца. offers — консенсус по топу.
CREATE TABLE IF NOT EXISTS cave_merchant_visits (
    visit_index     INTEGER PRIMARY KEY,
    opens_at        TIMESTAMPTZ,
    closes_at       TIMESTAMPTZ,
    next_opens_at   TIMESTAMPTZ,
    offers          JSONB       NOT NULL,
    generous_share  NUMERIC(4,3),          -- доля ферм топа с generous=true
    offers_share    NUMERIC(4,3),          -- доля ферм топа, у которых такой же набор предложений
    farms_seen      INTEGER,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Задания недели (choreBoard) =====
CREATE TABLE IF NOT EXISTS weekly_chores (
    started_at  TIMESTAMPTZ NOT NULL,      -- начало недели заданий
    npc         TEXT        NOT NULL,
    task        TEXT        NOT NULL,
    reward      JSONB,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (started_at, npc)
);
CREATE INDEX IF NOT EXISTS idx_weekly_chores_last_seen ON weekly_chores (last_seen DESC);

-- ===== Баунти-борд =====
CREATE TABLE IF NOT EXISTS bounty_requests (
    id          TEXT PRIMARY KEY,
    name        TEXT        NOT NULL,
    category    TEXT        NOT NULL,      -- animal | poppy
    level       INTEGER,
    coins       NUMERIC,
    items       JSONB,
    first_seen  TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bounty_requests_last_seen ON bounty_requests (last_seen DESC);

-- ===== Билетные доставки: консенсус по топу (снимок на каждый запуск) =====
CREATE TABLE IF NOT EXISTS ticket_delivery_consensus (
    id            SERIAL PRIMARY KEY,
    taken_at      TIMESTAMPTZ NOT NULL,
    npc           TEXT        NOT NULL,
    items         JSONB       NOT NULL,    -- что просит NPC (самый частый вариант)
    base_tickets  INTEGER,                 -- базовая награда без VIP/бонусов главы
    votes         INTEGER     NOT NULL,    -- сколько ферм видят этот вариант
    total         INTEGER     NOT NULL,    -- сколько ферм имеют открытый заказ у NPC
    share         NUMERIC(4,3) NOT NULL,
    variants      JSONB       NOT NULL     -- все варианты: [{items, votes}]
);
CREATE INDEX IF NOT EXISTS idx_ticket_consensus_taken ON ticket_delivery_consensus (taken_at DESC);

-- ===== Доставки за монеты и Flower: дневная статистика по ночному дампу =====
CREATE TABLE IF NOT EXISTS delivery_stats_daily (
    dump_date    DATE    NOT NULL,
    npc          TEXT    NOT NULL,
    reward_type  TEXT    NOT NULL,         -- coins | flower | tickets
    item         TEXT    NOT NULL,         -- что просят (для заказов из нескольких предметов — через " + ")
    orders       INTEGER NOT NULL,
    avg_reward   NUMERIC,
    min_reward   NUMERIC,
    max_reward   NUMERIC,
    PRIMARY KEY (dump_date, npc, reward_type, item)
);

CREATE TABLE IF NOT EXISTS nightly_dumps (
    filename      TEXT PRIMARY KEY,
    dump_date     DATE        NOT NULL,
    modified_at   TIMESTAMPTZ,
    farms         INTEGER,
    processed_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===== Проверки предметов у топа лидерборда =====
CREATE TABLE IF NOT EXISTS item_checks (
    id                   SERIAL PRIMARY KEY,
    item_name            TEXT        NOT NULL,
    item_type            TEXT        NOT NULL,   -- wearable | collectible
    top_n                INTEGER     NOT NULL,
    trigger              TEXT        NOT NULL,   -- auction_end | auction_plus_1h | auction_plus_24h | manual | nightly
    auction_id           TEXT,
    leaderboard_taken_at TIMESTAMPTZ,
    farms_checked        INTEGER,
    owners_count         INTEGER,
    checked_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_item_checks_item ON item_checks (item_name, checked_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS uq_item_checks_auction_trigger
    ON item_checks (auction_id, trigger) WHERE auction_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS item_check_owners (
    check_id  INTEGER REFERENCES item_checks(id) ON DELETE CASCADE,
    farm_id   BIGINT  NOT NULL,
    rank      INTEGER,
    username  TEXT,
    amount    NUMERIC,
    source    TEXT    NOT NULL,             -- holdings | auction_winner
    PRIMARY KEY (check_id, farm_id, source)
);

-- Очередь ручных проверок: сайт (goblin-api) кладёт строку, коллектор выполняет.
CREATE TABLE IF NOT EXISTS item_check_requests (
    id           SERIAL PRIMARY KEY,
    item_name    TEXT        NOT NULL,
    item_type    TEXT        NOT NULL,      -- wearable | collectible
    top_n        INTEGER     NOT NULL DEFAULT 200,
    status       TEXT        NOT NULL DEFAULT 'pending',  -- pending | running | done | error
    check_id     INTEGER REFERENCES item_checks(id) ON DELETE SET NULL,
    error        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_item_check_requests_pending
    ON item_check_requests (created_at) WHERE status = 'pending';
