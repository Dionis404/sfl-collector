-- Миграция 004: данные ферм — статы из ночного дампа + очередь обновления farm_cache.
-- БД: sfl.

-- Статы всех активных ферм из ночного дампа (0 запросов к API).
-- Из них коллектор обновляет farmers (goblin-bot) и farm_cache.
CREATE TABLE IF NOT EXISTS farm_stats_daily (
    farm_id    BIGINT  NOT NULL,
    dump_date  DATE    NOT NULL,
    username   TEXT,
    xp         NUMERIC,
    balance    NUMERIC,
    coins      NUMERIC,
    PRIMARY KEY (farm_id, dump_date)
);
CREATE INDEX IF NOT EXISTS idx_farm_stats_daily_date ON farm_stats_daily (dump_date);

-- Очередь on-demand обновления farm_cache (калькулятор ферм на сайте).
-- API вставляет запросы, worker (collector/jobs/farm_refresh.py) выполняет
-- их под общим RateLimiter. Вставка идемпотентна (PK).
CREATE TABLE IF NOT EXISTS farm_refresh_queue (
    farm_id      BIGINT PRIMARY KEY,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts     INT NOT NULL DEFAULT 0
);
