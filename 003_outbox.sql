CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel TEXT NOT NULL,
    kind TEXT NOT NULL,
    recipient TEXT,
    content TEXT NOT NULL,
    status TEXT NOT NULL,
    note TEXT DEFAULT '',
    ts TEXT NOT NULL
);
