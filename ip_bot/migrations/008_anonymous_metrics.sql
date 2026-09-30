CREATE TABLE analytics_counters (
    day DATE NOT NULL DEFAULT CURRENT_DATE,
    category TEXT NOT NULL,
    count BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, category)
);
