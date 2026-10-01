import sqlite3
import os
from datetime import datetime, timezone, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JAIL_DB_PATH", os.path.join(BASE_DIR, "jail.db"))

# Локальное время (МСК) для отметок
TZ = timezone(timedelta(hours=3))


# Даты хранятся как «ГГГГ-ММ-ДД ЧЧ:ММ» — так они правильно сортируются и сравниваются прямо в базе.
# На экран выводятся фильтром |dt как «ДД.ММ.ГГГГ ЧЧ:ММ».
DT_FMT = "%Y-%m-%d %H:%M"
SHOW_FMT = "%d.%m.%Y %H:%M"


def now_str():
    return datetime.now(TZ).strftime(DT_FMT)


def parse_dt(sv):
    """Дата из базы (новый или старый формат) или None."""
    for fmt in (DT_FMT, SHOW_FMT):
        try:
            return datetime.strptime(sv or "", fmt)
        except ValueError:
            continue
    return None


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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

CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,               -- 'OUT' (склад→произв), 'RETURN' (произв→склад)
    status       TEXT NOT NULL DEFAULT 'draft', -- draft, sent, accepted
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
    pairs_sent       INTEGER NOT NULL DEFAULT 0,
    pairs_recv       INTEGER,                 -- NULL до приёмки
    discrepancy_note TEXT
);

-- Заявки «чего не хватает»: производство пишет, склад собирает
CREATE TABLE IF NOT EXISTS requests (
    id           INTEGER PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'draft', -- draft, open, progress, done
    urgent       INTEGER NOT NULL DEFAULT 0,
    created_role TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    sent_at      TEXT,
    taken_at     TEXT,        -- склад начал собирать
    done_at      TEXT,        -- склад собрал / отправил
    note         TEXT,
    ship_note    TEXT,        -- примечание кладовщика при отправке
    accepted_at  TEXT,        -- производство приняло передачу
    discr        INTEGER NOT NULL DEFAULT 0, -- приняли с подтверждённым расхождением
    transfer_id  INTEGER      -- заявка производства собрана в эту передачу склада
);

CREATE TABLE IF NOT EXISTS request_items (
    id         INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL,
    item       TEXT NOT NULL,   -- что нужно (модель / материал / размер, свободно)
    qty        INTEGER,         -- сколько нужно (может быть пустым)
    collected  INTEGER,         -- сколько положил кладовщик (может быть больше)
    note       TEXT,
    placed     INTEGER NOT NULL DEFAULT 0,  -- галочка «положил»
    item_type  TEXT,            -- 'material' / 'zagotovka' / 'gotovoe'
    source     TEXT NOT NULL DEFAULT 'req', -- 'req' (из заявки) / 'sklad' (что положил)
    line_kind  TEXT NOT NULL DEFAULT 'need',-- 'need' (нужно) / 'pair' (пары) / 'material' (материал)
    customer_id INTEGER,        -- для строк-пар
    model_id    INTEGER,        -- для строк-пар
    status      TEXT,            -- для строк-пар: zagotovka/upakovka/gotovoe
    unit        TEXT,            -- единица измерения материала
    recv        REAL,            -- сколько принято производством (NULL — ещё не вписано)
    from_item   INTEGER          -- строка передачи создана из позиции заявки (шаг «Сборка»)
);

-- Операции, выбранные складом для строки пар (цена фиксируется в момент выбора)
CREATE TABLE IF NOT EXISTS item_ops (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,     -- 'req' (request_items) / 'line' (lines)
    item_id      INTEGER NOT NULL,
    operation_id INTEGER NOT NULL,
    name         TEXT NOT NULL,
    price        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_item_ops ON item_ops(kind, item_id);

-- Склад готовой обуви (у нас): приход от производства, списания
CREATE TABLE IF NOT EXISTS wh_moves (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- in (принято от производства), writeoff, inv
    customer_id INTEGER NOT NULL,
    model_id    INTEGER NOT NULL,
    quality     TEXT NOT NULL DEFAULT 'ready',  -- ready (готовая) / brak
    qty         REAL NOT NULL,
    reason      TEXT,
    note        TEXT,
    ref_id      INTEGER,         -- документ «передача на склад»
    role        TEXT
);
CREATE INDEX IF NOT EXISTS ix_wh_ref ON wh_moves(ref_id);

-- Справочник материалов: одно название — одна единица измерения
CREATE TABLE IF NOT EXISTS materials (
    id       INTEGER PRIMARY KEY,
    name     TEXT NOT NULL,
    unit     TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0
);

-- Служебные отметки (однократные пересчёты и т. п.)
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Оплаты производству (уменьшают долг)
CREATE TABLE IF NOT EXISTS payments (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    amount     REAL NOT NULL,
    note       TEXT
);

-- Склад производства: все движения товара (остаток = сумма qty)
CREATE TABLE IF NOT EXISTS stock_moves (
    id          INTEGER PRIMARY KEY,
    created_at  TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- in (приход по передаче), out (передано на склад),
                                 -- writeoff (списание), inv (инвентаризация), add (оприходование)
    item_type   TEXT NOT NULL,   -- pair / material
    customer_id INTEGER,
    model_id    INTEGER,
    name        TEXT,            -- материал
    unit        TEXT,
    qty         REAL NOT NULL,   -- + приход, − расход
    reason      TEXT,
    note        TEXT,
    ref_type    TEXT,            -- req / doc
    ref_id      INTEGER,
    role        TEXT
);
CREATE INDEX IF NOT EXISTS ix_stock_ref ON stock_moves(ref_type, ref_id);

-- Этап 2: внесение бумаг для сдельной зарплаты. Структура заложена заранее.
-- Расхождения: закрытие (разобрались — что выяснили)
CREATE TABLE IF NOT EXISTS discr_close (
    id         INTEGER PRIMARY KEY,
    ref_type   TEXT NOT NULL,            -- 'doc' (передача на склад) / 'req' (передача на производство)
    ref_id     INTEGER NOT NULL,
    resolution TEXT NOT NULL,
    note       TEXT,
    role       TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(ref_type, ref_id)
);

-- Расхождения: разбор двумя сторонами (предложение итогов → согласие / возврат)
CREATE TABLE IF NOT EXISTS discr_case (
    id          INTEGER PRIMARY KEY,
    ref_type    TEXT NOT NULL,
    ref_id      INTEGER NOT NULL,
    status      TEXT NOT NULL,           -- proposed / dispute / closed
    proposed_by TEXT,
    proposal    TEXT,                    -- JSON {строка: {found, left, lost}}
    note        TEXT,
    comment     TEXT,                    -- почему вернули
    created_at  TEXT NOT NULL,
    updated_at  TEXT,
    closed_at   TEXT,
    closed_by   TEXT,
    UNIQUE(ref_type, ref_id)
);

-- Материалы на складе склада: приход от поставщиков, расход в передачи на производство
CREATE TABLE IF NOT EXISTS mat_moves (
    id         INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    kind       TEXT NOT NULL,           -- in / out / writeoff
    name       TEXT NOT NULL,
    unit       TEXT,
    qty        REAL NOT NULL,           -- + приход, − расход
    reason     TEXT,
    note       TEXT,
    ref_id     INTEGER,                 -- передача
    role       TEXT
);

-- Задел под сдельную оплату по работникам (этап «зарплата по номерам»)
CREATE TABLE IF NOT EXISTS work_records (
    id           INTEGER PRIMARY KEY,
    worker_id    INTEGER NOT NULL,
    operation_id INTEGER NOT NULL,
    model_id     INTEGER NOT NULL,
    pairs        INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);
"""


def _migrate(conn):
    """Переход на статус-в-строке: добавить lines.status и превратить A/B в OUT."""
    ocols = [r["name"] for r in conn.execute("PRAGMA table_info(operations)")]
    if "price" not in ocols:
        conn.execute("ALTER TABLE operations ADD COLUMN price REAL NOT NULL DEFAULT 100")
    dcols = [r["name"] for r in conn.execute("PRAGMA table_info(discr_case)")]
    if dcols and "counter" not in dcols:   # 1.08: вариант несогласной стороны; спор сразу у директора
        conn.execute("ALTER TABLE discr_case ADD COLUMN counter TEXT")
        conn.execute("ALTER TABLE discr_case ADD COLUMN counter_by TEXT")
        conn.execute("UPDATE discr_case SET status='dispute' WHERE status='returned'")
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(lines)")]
    if "status" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN status TEXT")
    if "note" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN note TEXT")
    if "lot" not in cols:
        conn.execute("ALTER TABLE lines ADD COLUMN lot TEXT")   # партия: какие операции делались (JSON)
    # поля сборки в позициях заявки
    try:
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
            if "unit" not in ricols:
                conn.execute("ALTER TABLE request_items ADD COLUMN unit TEXT")
            if "from_item" not in ricols:
                conn.execute("ALTER TABLE request_items ADD COLUMN from_item INTEGER")
            if "recv" not in ricols:
                conn.execute("ALTER TABLE request_items ADD COLUMN recv REAL")
            if "urgent" not in ricols:
                conn.execute("ALTER TABLE request_items ADD COLUMN urgent INTEGER NOT NULL DEFAULT 0")
            if "delivered" not in ricols:   # сколько уже пришло по строке заказа (для «Пришла часть»)
                conn.execute("ALTER TABLE request_items ADD COLUMN delivered REAL NOT NULL DEFAULT 0")
        rcols = [r["name"] for r in conn.execute("PRAGMA table_info(requests)")]
        if rcols and "ship_note" not in rcols:
            conn.execute("ALTER TABLE requests ADD COLUMN ship_note TEXT")
        if rcols and "accepted_at" not in rcols:
            conn.execute("ALTER TABLE requests ADD COLUMN accepted_at TEXT")
        if rcols and "transfer_id" not in rcols:
            conn.execute("ALTER TABLE requests ADD COLUMN transfer_id INTEGER")
        if rcols and "discr" not in rcols:
            conn.execute("ALTER TABLE requests ADD COLUMN discr INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    # Черновиков больше нет: пустые недооформленные документы и заявки удаляем.
    conn.execute("""DELETE FROM documents WHERE status='draft'
                    AND id NOT IN (SELECT document_id FROM lines)""")
    conn.execute("""DELETE FROM requests WHERE (status='draft' OR (created_role='sklad' AND status!='shipped'))
                    AND id NOT IN (SELECT request_id FROM request_items)""")
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
    conn.execute("DROP TABLE IF EXISTS prices")   # не использовалась
    for sql in INDEXES:                            # индексы для частых выборок
        try:
            conn.execute(sql)
        except sqlite3.Error:
            pass
    _migrate_dates(conn)


INDEXES = [
    "CREATE INDEX IF NOT EXISTS ix_lines_doc ON lines(document_id)",
    "CREATE INDEX IF NOT EXISTS ix_ri_req ON request_items(request_id)",
    "CREATE INDEX IF NOT EXISTS ix_req_status ON requests(status, created_role)",
    "CREATE INDEX IF NOT EXISTS ix_req_transfer ON requests(transfer_id)",
    "CREATE INDEX IF NOT EXISTS ix_docs_status ON documents(status, kind)",
    "CREATE INDEX IF NOT EXISTS ix_wh_cm ON wh_moves(customer_id, model_id, quality)",
    "CREATE INDEX IF NOT EXISTS ix_stock_item ON stock_moves(item_type, customer_id, model_id)",
]


DATE_COLS = {
    "documents": ["created_at", "sent_at", "accepted_at"],
    "requests": ["created_at", "sent_at", "taken_at", "done_at", "accepted_at"],
    "stock_moves": ["created_at"],
    "wh_moves": ["created_at"],
    "payments": ["created_at"],
    "work_records": ["created_at"],
    "discr_close": ["created_at"],
    "discr_case": ["created_at", "updated_at", "closed_at"],
    "mat_moves": ["created_at"],
}


def _migrate_dates(conn):
    """Однократно: «ДД.ММ.ГГГГ ЧЧ:ММ» → «ГГГГ-ММ-ДД ЧЧ:ММ» во всех датах."""
    if conn.execute("SELECT 1 FROM meta WHERE key='iso_dates_v1'").fetchone():
        return
    for table, cols in DATE_COLS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for c in cols:
            if c not in have:
                continue
            # ДД.ММ.ГГГГ ЧЧ:ММ: символы 7-10 год, 4-5 месяц, 1-2 день, 12-16 время
            conn.execute(
                f"UPDATE {table} SET {c} = substr({c},7,4)||'-'||substr({c},4,2)||'-'||substr({c},1,2)"
                f"||substr({c},11) WHERE {c} GLOB '[0-9][0-9].[0-9][0-9].[0-9][0-9][0-9][0-9]*'")
    conn.execute("INSERT INTO meta (key, value) VALUES ('iso_dates_v1', ?)", (now_str(),))


REAL_OPERATIONS = [
    ("Упаковка", 18),
    ("Покраска", 7),
    ("Шьёт Штробель", 9),
    ("Затяжка Штробель", 65),
    ("Прошивка", 10),
    ("Вставка обуви в колодку", 15),
    ("Вклейка вставки", 15),
    ("Плетение", 75),
]


def init_db():
    conn = get_db()
    # Обычный журнал вместо WAL: на сетевом диске PythonAnywhere WAL заметно тормозит.
    try:
        conn.execute("PRAGMA journal_mode = DELETE")
    except sqlite3.Error:
        pass
    conn.executescript(SCHEMA)
    _migrate(conn)

    # Операции и сдельные цены (один раз; дальше правятся в «Справочниках»)
    if not conn.execute("SELECT 1 FROM meta WHERE key='real_operations_v1'").fetchone():
        for i, (name, price) in enumerate(REAL_OPERATIONS, start=1):
            if conn.execute("SELECT 1 FROM operations WHERE ord=?", (i,)).fetchone():
                conn.execute("UPDATE operations SET name=?, price=? WHERE ord=?", (name, price, i))
            else:
                conn.execute("INSERT INTO operations (ord, name, price) VALUES (?, ?, ?)", (i, name, price))
        conn.execute("INSERT INTO meta (key, value) VALUES ('real_operations_v1', ?)", (now_str(),))

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("DB initialized:", DB_PATH)
