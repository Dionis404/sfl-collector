-- Миграция 003: цены маркетплейса (перенос из sfl-price-service) + курс Flower.
-- БД: sfl.

-- Таблица уже существует в проде (её создавал sfl-price-service/collector).
-- Схема идентична — CREATE IF NOT EXISTS здесь no-op, нужен чтобы свежая
-- инсталляция поднималась без старого сервиса.
CREATE TABLE IF NOT EXISTS sfl_resource_prices (
    id              BIGSERIAL PRIMARY KEY,
    captured_at     TIMESTAMPTZ NOT NULL,
    snapshot_date   DATE        NOT NULL,
    item_key        TEXT        NOT NULL,
    item_name       TEXT        NOT NULL,
    floor           NUMERIC,
    low             NUMERIC,
    high            NUMERIC,
    latest_sale     NUMERIC,
    best_offer      NUMERIC,
    volume          NUMERIC,
    trades          BIGINT,
    quantity        NUMERIC,
    listing_count   INTEGER,
    offer_count     INTEGER,
    flower_price    NUMERIC     NOT NULL,
    UNIQUE (item_key, captured_at)
);
CREATE INDEX IF NOT EXISTS idx_sfl_resource_prices_item_time
    ON sfl_resource_prices (item_key, captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_sfl_resource_prices_date
    ON sfl_resource_prices (snapshot_date);

-- Курс $/FLOWER как первоклассные данные: сайт считает по нему цены ставок
-- аукционов в $ (медиана ставок × курс на момент старта дропа).
CREATE TABLE IF NOT EXISTS flower_price_history (
    captured_at TIMESTAMPTZ PRIMARY KEY,
    usd         NUMERIC NOT NULL
);

-- Разовый бэкофилл истории курса из уже накопленных снимков цен.
INSERT INTO flower_price_history (captured_at, usd)
SELECT DISTINCT captured_at, flower_price
FROM sfl_resource_prices
WHERE flower_price IS NOT NULL
ON CONFLICT (captured_at) DO NOTHING;
