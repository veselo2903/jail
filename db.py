import sqlite3
import os
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JAIL_DB_PATH", os.path.join(BASE_DIR, "jail.db"))

# Локальное время (МСК) для отметок
TZ = timezone(timedelta(hours=3))


def now_str():
    return datetime.now(TZ).strftime("%d.%m.%Y %H:%M")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS models (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workers (
    id       INTEGER PRIMARY KEY,
    number   TEXT NOT NULL,
    name     TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operations (
    id   INTEGER PRIMARY KEY,
    ord  INTEGER NOT NULL,
    name TEXT NOT NULL
);

-- Этап 2: расценки (модель + операция). Структура заложена заранее.
CREATE TABLE IF NOT EXISTS prices (
    id           INTEGER PRIMARY KEY,
    model_id     INTEGER NOT NULL,
    operation_id INTEGER NOT NULL,
    rate         REAL NOT NULL DEFAULT 0 CHECK(rate >= 0),
    rate_kopeks  INTEGER NOT NULL DEFAULT 0 CHECK(rate_kopeks >= 0),
    UNIQUE(model_id, operation_id),
    FOREIGN KEY(model_id) REFERENCES models(id),
    FOREIGN KEY(operation_id) REFERENCES operations(id)
);

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK(kind IN ('OUT','RETURN')),
    status       TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','sent','accepted')),
    created_role TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    sent_at      TEXT,
    accepted_at  TEXT,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS lines (
    id               INTEGER PRIMARY KEY,
    document_id      INTEGER NOT NULL,
    customer_id      INTEGER NOT NULL,
    model_id         INTEGER NOT NULL,
    status           TEXT,                    -- 'zagotovka' / 'upakovka' (для OUT), 'gotovoe' (RETURN)
    pairs_sent       INTEGER NOT NULL DEFAULT 0 CHECK(pairs_sent > 0),
    pairs_recv       INTEGER CHECK(pairs_recv >= 0),
    discrepancy_note TEXT,
    FOREIGN KEY(document_id) REFERENCES documents(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(model_id) REFERENCES models(id)
);

-- Заявки «чего не хватает»: производство пишет, склад собирает
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','open','progress','done','shipped')),
    urgent       INTEGER NOT NULL DEFAULT 0,
    created_role TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    created_ts   INTEGER,     -- момент создания (unix, сек): окно редактирования и показ складу
    sent_at      TEXT,
    taken_at     TEXT,        -- склад начал собирать
    done_at      TEXT,        -- склад собрал / отправил
    note         TEXT,
    ship_note    TEXT         -- примечание кладовщика при отправке
);

CREATE TABLE IF NOT EXISTS request_items (
    id         INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL,
    item       TEXT NOT NULL,   -- что нужно (модель / материал / размер, свободно)
    qty        INTEGER CHECK(qty > 0),
    collected  INTEGER CHECK(collected >= 0),
    note       TEXT,
    placed     INTEGER NOT NULL DEFAULT 0,  -- галочка «положил»
    item_type  TEXT,            -- 'material' / 'zagotovka' / 'gotovoe'
    source     TEXT NOT NULL DEFAULT 'req', -- 'req' (из заявки) / 'sklad' (что положил)
    line_kind  TEXT NOT NULL DEFAULT 'need',-- 'need' (нужно) / 'pair' (пары) / 'material' (материал)
    customer_id INTEGER,        -- для строк-пар
    model_id    INTEGER,        -- для строк-пар
    status      TEXT,
    operation   TEXT,
    unit        TEXT,
    urgent      INTEGER NOT NULL DEFAULT 0,  -- срочная позиция
    FOREIGN KEY(request_id) REFERENCES requests(id),
    FOREIGN KEY(customer_id) REFERENCES customers(id),
    FOREIGN KEY(model_id) REFERENCES models(id)
);

-- Фактические отправки склада. Одна заявка может быть закрыта несколькими отправками.
CREATE TABLE IF NOT EXISTS shipments (
    id                INTEGER PRIMARY KEY,
    request_id        INTEGER REFERENCES requests(id) ON DELETE SET NULL,
    request_number    INTEGER,
    legacy_request_id INTEGER UNIQUE,
    client_token      TEXT UNIQUE,
    created_at        TEXT NOT NULL,
    note              TEXT,
    CHECK(request_id IS NULL OR request_number IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS shipment_items (
    id              INTEGER PRIMARY KEY,
    shipment_id     INTEGER NOT NULL REFERENCES shipments(id) ON DELETE CASCADE,
    request_item_id INTEGER REFERENCES request_items(id) ON DELETE SET NULL,
    request_number  INTEGER,
    line_kind       TEXT NOT NULL CHECK(line_kind IN ('need','pair','material')),
    item            TEXT NOT NULL DEFAULT '',
    qty             INTEGER NOT NULL CHECK(qty > 0),
    unit            TEXT,
    item_type       TEXT,
    customer_id     INTEGER REFERENCES customers(id),
    model_id        INTEGER REFERENCES models(id),
    operation       TEXT
);

-- Этап 2: внесение бумаг для сдельной зарплаты. Структура заложена заранее.
CREATE TABLE IF NOT EXISTS work_records (
    id           INTEGER PRIMARY KEY,
    worker_id    INTEGER NOT NULL,
    operation_id INTEGER NOT NULL,
    model_id     INTEGER NOT NULL,
    pairs        INTEGER NOT NULL DEFAULT 0 CHECK(pairs > 0),
    created_at   TEXT NOT NULL,
    work_date    TEXT NOT NULL,
    rate_kopeks  INTEGER NOT NULL DEFAULT 0 CHECK(rate_kopeks >= 0),
    amount_kopeks INTEGER NOT NULL DEFAULT 0 CHECK(amount_kopeks >= 0),
    FOREIGN KEY(worker_id) REFERENCES workers(id),
    FOREIGN KEY(operation_id) REFERENCES operations(id),
    FOREIGN KEY(model_id) REFERENCES models(id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor_role TEXT,
    action TEXT NOT NULL,
    target TEXT,
    details TEXT,
    ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_lines_document ON lines(document_id);
CREATE INDEX IF NOT EXISTS idx_request_items_request ON request_items(request_id);
CREATE INDEX IF NOT EXISTS idx_shipments_request ON shipments(request_id);
CREATE INDEX IF NOT EXISTS idx_shipment_items_shipment ON shipment_items(shipment_id);
CREATE INDEX IF NOT EXISTS idx_shipment_items_request_item ON shipment_items(request_item_id);
CREATE INDEX IF NOT EXISTS idx_audit_events_at ON audit_events(at);
"""


def _migrate(conn):
    """Переход на статус-в-строке: добавить lines.status и превратить A/B в OUT."""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(lines)")]
    if "status" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN status TEXT")
    # поля сборки в позициях заявки
    ricols = [r["name"] for r in conn.execute("PRAGMA table_info(request_items)")]
    if ricols:
        if "collected" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN collected INTEGER")
        if "placed" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN placed INTEGER NOT NULL DEFAULT 0")
        if "item_type" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN item_type TEXT")
        if "source" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN source TEXT NOT NULL DEFAULT 'req'")
        if "line_kind" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN line_kind TEXT NOT NULL DEFAULT 'need'")
        if "customer_id" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN customer_id INTEGER")
        if "model_id" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN model_id INTEGER")
        if "status" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN status TEXT")
        if "urgent" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
        if "operation" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN operation TEXT")
        if "unit" not in ricols:
            conn.execute("ALTER TABLE request_items ADD COLUMN unit TEXT")
    rcols = [r["name"] for r in conn.execute("PRAGMA table_info(requests)")]
    if rcols and "ship_note" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN ship_note TEXT")
    if rcols and "created_ts" not in rcols:
        conn.execute("ALTER TABLE requests ADD COLUMN created_ts INTEGER")
    # старые типы документов A/B -> OUT, строкам проставляем статус
    conn.execute("""UPDATE lines SET status='zagotovka'
                    WHERE (status IS NULL OR status='') AND document_id IN
                    (SELECT id FROM documents WHERE kind='A')""")
    conn.execute("""UPDATE lines SET status='upakovka'
                    WHERE (status IS NULL OR status='') AND document_id IN
                    (SELECT id FROM documents WHERE kind='B')""")
    conn.execute("""UPDATE lines SET status='gotovoe'
                    WHERE (status IS NULL OR status='') AND document_id IN
                    (SELECT id FROM documents WHERE kind='RETURN')""")
    conn.execute("UPDATE documents SET kind='OUT' WHERE kind IN ('A','B')")


def _migrate_constraints(conn):
    """Rebuild legacy child tables so old installations gain real foreign keys."""
    if not list(conn.execute("PRAGMA foreign_key_list(lines)")):
        tables = {
            "prices": ("""CREATE TABLE prices (
                id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL, operation_id INTEGER NOT NULL,
                rate REAL NOT NULL DEFAULT 0 CHECK(rate >= 0), UNIQUE(model_id, operation_id),
                FOREIGN KEY(model_id) REFERENCES models(id),
                FOREIGN KEY(operation_id) REFERENCES operations(id))""",
                "id,model_id,operation_id,rate"),
            "lines": ("""CREATE TABLE lines (
                id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, customer_id INTEGER NOT NULL,
                model_id INTEGER NOT NULL, status TEXT,
                pairs_sent INTEGER NOT NULL DEFAULT 0 CHECK(pairs_sent > 0),
                pairs_recv INTEGER CHECK(pairs_recv >= 0), discrepancy_note TEXT,
                FOREIGN KEY(document_id) REFERENCES documents(id),
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(model_id) REFERENCES models(id))""",
                "id,document_id,customer_id,model_id,status,pairs_sent,pairs_recv,discrepancy_note"),
            "request_items": ("""CREATE TABLE request_items (
                id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, item TEXT NOT NULL,
                qty INTEGER CHECK(qty > 0), collected INTEGER CHECK(collected >= 0),
                note TEXT, placed INTEGER NOT NULL DEFAULT 0, item_type TEXT,
                source TEXT NOT NULL DEFAULT 'req', line_kind TEXT NOT NULL DEFAULT 'need',
                customer_id INTEGER, model_id INTEGER, status TEXT,
                operation TEXT, unit TEXT,
                FOREIGN KEY(request_id) REFERENCES requests(id),
                FOREIGN KEY(customer_id) REFERENCES customers(id),
                FOREIGN KEY(model_id) REFERENCES models(id))""",
                "id,request_id,item,qty,collected,note,placed,item_type,source,line_kind,customer_id,model_id,status,operation,unit"),
            "work_records": ("""CREATE TABLE work_records (
                id INTEGER PRIMARY KEY, worker_id INTEGER NOT NULL, operation_id INTEGER NOT NULL,
                model_id INTEGER NOT NULL, pairs INTEGER NOT NULL DEFAULT 0 CHECK(pairs > 0),
                created_at TEXT NOT NULL,
                FOREIGN KEY(worker_id) REFERENCES workers(id),
                FOREIGN KEY(operation_id) REFERENCES operations(id),
                FOREIGN KEY(model_id) REFERENCES models(id))""",
                "id,worker_id,operation_id,model_id,pairs,created_at"),
        }
        conn.commit()
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table, (ddl, columns) in tables.items():
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_legacy")
                conn.execute(ddl)
                conn.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {table}_legacy")
                conn.execute(f"DROP TABLE {table}_legacy")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_document ON lines(document_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_request_items_request ON request_items(request_id)")

    conn.executescript("""
    CREATE TRIGGER IF NOT EXISTS valid_document_insert BEFORE INSERT ON documents
      WHEN NEW.kind NOT IN ('OUT','RETURN') OR NEW.status NOT IN ('draft','sent','accepted')
      BEGIN SELECT RAISE(ABORT, 'invalid document kind/status'); END;
    CREATE TRIGGER IF NOT EXISTS valid_document_update BEFORE UPDATE ON documents
      WHEN NEW.kind NOT IN ('OUT','RETURN') OR NEW.status NOT IN ('draft','sent','accepted')
      BEGIN SELECT RAISE(ABORT, 'invalid document kind/status'); END;
    CREATE TRIGGER IF NOT EXISTS valid_request_insert BEFORE INSERT ON requests
      WHEN NEW.status NOT IN ('draft','open','progress','done','shipped')
      BEGIN SELECT RAISE(ABORT, 'invalid request status'); END;
    CREATE TRIGGER IF NOT EXISTS valid_request_update BEFORE UPDATE ON requests
      WHEN NEW.status NOT IN ('draft','open','progress','done','shipped')
      BEGIN SELECT RAISE(ABORT, 'invalid request status'); END;
    """)
    bad = list(conn.execute("PRAGMA foreign_key_check"))
    if bad:
        raise RuntimeError(f"foreign key violations after migration: {bad[:3]}")


def _migrate_payroll(conn):
    price_cols = {r["name"] for r in conn.execute("PRAGMA table_info(prices)")}
    if "rate_kopeks" not in price_cols:
        conn.execute("ALTER TABLE prices ADD COLUMN rate_kopeks INTEGER NOT NULL DEFAULT 0")
        conn.execute("UPDATE prices SET rate_kopeks=ROUND(rate * 100)")
    work_cols = {r["name"] for r in conn.execute("PRAGMA table_info(work_records)")}
    if "work_date" not in work_cols:
        conn.execute("ALTER TABLE work_records ADD COLUMN work_date TEXT")
        conn.execute("UPDATE work_records SET work_date=substr(created_at,7,4)||'-'||"
                     "substr(created_at,4,2)||'-'||substr(created_at,1,2)")
    if "rate_kopeks" not in work_cols:
        conn.execute("ALTER TABLE work_records ADD COLUMN rate_kopeks INTEGER NOT NULL DEFAULT 0")
    if "amount_kopeks" not in work_cols:
        conn.execute("ALTER TABLE work_records ADD COLUMN amount_kopeks INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_records_date ON work_records(work_date)")


def _migrate_shipments(conn):
    """Перенести старые уже отправленные заявки в журнал отправок один раз."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(shipments)")}
    if "client_token" not in cols:
        conn.execute("ALTER TABLE shipments ADD COLUMN client_token TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_shipments_token ON shipments(client_token)")
    item_cols = {r["name"] for r in conn.execute("PRAGMA table_info(shipment_items)")}
    if "request_number" not in item_cols:
        conn.execute("ALTER TABLE shipment_items ADD COLUMN request_number INTEGER")
    old = conn.execute(
        "SELECT * FROM requests WHERE status='shipped' AND NOT EXISTS "
        "(SELECT 1 FROM shipments WHERE legacy_request_id=requests.id)"
    ).fetchall()
    for r in old:
        linked = r["created_role"] == "proizv"
        cur = conn.execute(
            "INSERT INTO shipments (request_id, request_number, legacy_request_id, created_at, note) "
            "VALUES (?, ?, ?, ?, ?)",
            (r["id"] if linked else None, r["id"] if linked else None, r["id"],
             r["done_at"] or r["created_at"], r["ship_note"]))
        items = conn.execute(
            "SELECT * FROM request_items WHERE request_id=? AND placed=1 "
            "AND COALESCE(collected, 0)>0", (r["id"],)).fetchall()
        for i in items:
            kind = i["line_kind"] if i["line_kind"] in ("pair", "material") else "need"
            conn.execute(
                "INSERT INTO shipment_items "
                "(shipment_id, request_item_id, request_number, line_kind, item, qty, unit, item_type, "
                "customer_id, model_id, operation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cur.lastrowid, i["id"] if kind == "need" else None,
                 r["id"] if kind == "need" else None, kind,
                 i["item"], i["collected"], i["unit"], i["item_type"],
                 i["customer_id"], i["model_id"], i["operation"]))


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate(conn)
    _migrate_constraints(conn)
    _migrate_payroll(conn)
    _migrate_shipments(conn)

    # 6 операций (названия временные, правятся позже)
    cur = conn.execute("SELECT COUNT(*) AS c FROM operations")
    if cur.fetchone()["c"] == 0:
        for i in range(1, 7):
            conn.execute(
                "INSERT INTO operations (ord, name) VALUES (?, ?)",
                (i, f"Операция {i}"),
            )

    # Стартовые заглушки (правятся/архивируются через интерфейс)
    cur = conn.execute("SELECT COUNT(*) AS c FROM customers")
    if cur.fetchone()["c"] == 0:
        for n in ("Заказчик 1", "Заказчик 2", "Заказчик 3"):
            conn.execute("INSERT INTO customers (name) VALUES (?)", (n,))

    cur = conn.execute("SELECT COUNT(*) AS c FROM models")
    if cur.fetchone()["c"] == 0:
        for n in ("Модель 1", "Модель 2", "Модель 3"):
            conn.execute("INSERT INTO models (name) VALUES (?)", (n,))

    cur = conn.execute("SELECT COUNT(*) AS c FROM workers")
    if cur.fetchone()["c"] == 0:
        conn.execute("INSERT INTO workers (number, name) VALUES (?, ?)", ("1", "Работник 1"))
        conn.execute("INSERT INTO workers (number, name) VALUES (?, ?)", ("2", "Работник 2"))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized:", DB_PATH)
