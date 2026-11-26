-- Архив суточных изменений топ-500 билетов: кто выбыл из топа и кто сильно
-- просел (>= 50 позиций) за день. Наполняется коллектором
-- (collector/jobs/top500_changes_archive.py) из снимков top500_snapshots.
CREATE TABLE IF NOT EXISTS top500_daily_changes (
    day DATE NOT NULL,
    farm_id BIGINT NOT NULL,
    username TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('dropped', 'fallen')),
    old_rank INT NOT NULL,
    old_tickets INT NOT NULL,
    new_rank INT,
    new_tickets INT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, farm_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_top500_daily_changes_day ON top500_daily_changes (day DESC);
