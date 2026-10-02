import math
import uuid
from decimal import Decimal, ROUND_HALF_UP
import os
import json
from functools import wraps
from flask import (
    Flask, g, session, request, redirect, url_for,
    render_template, abort, flash
)

# Работает и как отдельное приложение (python app.py),
# и как раздел /jail внутри основной системы (from jail.app import app).
if __package__:
    from . import db
else:
    import db

from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_prefix=1)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024   # архив обновления — не больше 50 МБ
app.secret_key = os.environ.get("JAIL_SECRET_KEY") or "jail-dev-secret-change-before-launch"
# Отдельное имя cookie, чтобы сессии jail и основной системы не пересекались.
app.config["SESSION_COOKIE_NAME"] = "jail_session"
app.config["SESSION_COOKIE_PATH"] = os.environ.get("JAIL_COOKIE_PATH", "/")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("JAIL_COOKIE_SECURE") == "1"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# CSS/JS подключаются с номером версии в адресе — браузер может хранить их долго.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 31536000

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
    VERSION = f.read().strip()

if os.environ.get("JAIL_SKIP_MIGRATIONS") != "1":
    db.init_db()

# ---------- Роли ----------
ROLES = {
    "sklad":    "Склад",
    "proizv":   "Производство",
    "director": "Директор",
}

# Тип документа -> (подпись, роль-создатель, роль-приёмщик)
KINDS = {
    "OUT":    ("Передача на производство (склад → производство)", "sklad", "proizv"),
    "RETURN": ("Передача на склад (производство → склад)", "proizv", "sklad"),
}
KIND_SHORT = {"OUT": "На производство", "RETURN": "На склад"}

# Статус товара в строке (что именно передаём)
LINE_STATUSES = {
    "zagotovka": "Заготовки",
    "upakovka":  "На упаковку",
    "gotovoe":   "Готовое",
    "brak":      "Брак",
    "repair":    "Ремонт",
}
# Какие статусы доступны для выбора в документе данного типа
KIND_LINE_STATUSES = {
    "RETURN": ["gotovoe", "brak"],
}

STATUS_LABEL = {"draft": "Создаётся", "sent": "Отправлен", "accepted": "Принят"}

# Заявки «чего не хватает» (производство → склад)
REQ_STATUS = {"draft": "Создаётся", "open": "Новая", "progress": "Собирается",
              "done": "Собрано", "shipped": "Едет", "accepted": "Выполнена"}

# Как статусы называются для производства (раздел «Заявки» производства)
PROD_REQ_STATUS = {"draft": "Ждёт отправки", "open": "Отправлена складу", "progress": "Склад собирает",
                   "done": "Склад собирает", "shipped": "Едет к вам", "accepted": "Получена"}
PROD_DOC_STATUS = {"draft": "Ждёт отправки", "sent": "Едет на склад", "accepted": "Принята складом"}
# Как статусы заказов производства называются для склада
SKLAD_REQ_STATUS = {"open": "Нужно собрать", "progress": "Собирается", "done": "Собирается",
                    "shipped": "Едет", "accepted": "Получена"}

# Тип позиции при сборке (кладовщик отмечает, что это)
ITEM_TYPES = {"material": "Материал", "zagotovka": "Заготовка", "gotovoe": "Готовая обувь"}


# ---------- Кэш тяжёлых расчётов ----------
# Остатки, долг и расхождения считаются по всей истории. Чтобы не пересчитывать их на каждой
# странице, результат запоминается до следующего сохранения в базу (счётчик изменений в файле).
# Пока в текущем запросе есть несохранённые изменения — считаем заново, без кэша.
_CACHE = {}


def _db_stamp():
    """Счётчик изменений из заголовка файла SQLite (байты 24–27): растёт при каждом сохранении."""
    try:
        with open(db.DB_PATH, "rb") as f:
            f.seek(24)
            return f.read(4)
    except OSError:
        return None


def _cached(fn):
    @wraps(fn)
    def w(*a):
        conn = getattr(g, "db", None)
        stamp = _db_stamp()
        if conn is None or conn.in_transaction or stamp is None:
            return fn(*a)
        key = (fn.__name__,) + a
        hit = _CACHE.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        res = fn(*a)
        if not conn.in_transaction:
            _CACHE[key] = (stamp, res)
        return res
    return w


# ---------- Склад производства ----------
def _move(conn, kind, item_type, qty, at, cid=None, mid=None, name=None, unit=None,
          reason=None, note=None, ref_type=None, ref_id=None, role=None):
    conn.execute(
        "INSERT INTO stock_moves (created_at, kind, item_type, customer_id, model_id, name, unit, "
        "qty, reason, note, ref_type, ref_id, role) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (at, kind, item_type, cid, mid, name, unit, qty, reason, note, ref_type, ref_id, role))


def _brak_moved(conn):
    """id строк брака, которые уже списаны с производства при добавлении в передачу."""
    return {r["ref_id"] for r in conn.execute("SELECT ref_id FROM stock_moves WHERE ref_type='docbrak'")}


def stock_from_req(conn, req_id):
    """Принятая передача со склада → приход на склад производства (по факту принятого)."""
    if conn.execute("SELECT 1 FROM stock_moves WHERE ref_type='req' AND ref_id=?", (req_id,)).fetchone():
        return
    r = conn.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "accepted":
        return
    for i in conn.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind IN ('pair','material')",
                          (req_id,)):
        q = i["recv"] if i["recv"] is not None else i["collected"]
        if not q:
            continue
        if i["line_kind"] == "pair":
            _move(conn, "in", "pair", q, r["accepted_at"], cid=i["customer_id"], mid=i["model_id"],
                  reason="Передача со склада", ref_type="req", ref_id=req_id, role="proizv")
        else:
            _move(conn, "in", "material", q, r["accepted_at"], name=i["item"], unit=i["unit"],
                  reason="Передача со склада", ref_type="req", ref_id=req_id, role="proizv")


def stock_from_doc(conn, doc_id):
    """Документы: принятое «на производство» — приход; отправленное «на склад» — расход."""
    if conn.execute("SELECT 1 FROM stock_moves WHERE ref_type='doc' AND ref_id=?", (doc_id,)).fetchone():
        return
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        return
    lines = conn.execute("SELECT * FROM lines WHERE document_id=?", (doc_id,)).fetchall()
    if d["kind"] == "OUT" and d["status"] == "accepted":
        for l in lines:
            if l["pairs_recv"]:
                _move(conn, "in", "pair", l["pairs_recv"], d["accepted_at"], cid=l["customer_id"],
                      mid=l["model_id"], reason="Передача со склада", ref_type="doc", ref_id=doc_id, role="proizv")
    elif d["kind"] == "RETURN" and d["status"] in ("sent", "accepted"):
        moved = _brak_moved(conn)
        for l in lines:
            if l["id"] in moved:
                continue   # брак уже списан с производства при добавлении
            if l["pairs_sent"]:
                _move(conn, "out", "pair", -l["pairs_sent"], d["sent_at"], cid=l["customer_id"],
                      mid=l["model_id"], reason="Передача на склад", ref_type="doc", ref_id=doc_id, role="proizv")


def _once(conn, key):
    """True, если пересчёт с этим ключом ещё не делался (и отмечает его сделанным)."""
    if conn.execute("SELECT 1 FROM meta WHERE key=?", (key,)).fetchone():
        return False
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, db.now_str()))
    return True


def _stock_backfill():
    """Однократно проводит по складу уже существующие передачи. Новые проводятся сразу при приёмке."""
    conn = db.get_db()
    if not _once(conn, "stock_backfill_v1"):
        conn.close()
        return
    for r in conn.execute("SELECT id FROM requests WHERE status='accepted'").fetchall():
        stock_from_req(conn, r["id"])
    for d in conn.execute("SELECT id FROM documents WHERE status IN ('sent','accepted')").fetchall():
        stock_from_doc(conn, d["id"])
    conn.commit()
    conn.close()


if os.environ.get("JAIL_SKIP_MIGRATIONS") != "1":
    _stock_backfill()


# ---------- Склад готовой обуви (у нас) ----------
WH_REASONS = ["Отгружено заказчику", "Брак", "Потеря", "Другое"]
WH_QUALITY = {"ready": "Готовая", "brak": "Брак"}
WH_KINDS = {"in": "Приход", "writeoff": "Списание", "inv": "Инвентаризация", "repair": "В ремонт"}


def wh_from_doc(conn, doc_id):
    """Принятая складом «передача на склад» → приход готовой обуви и брака (однократно)."""
    if conn.execute("SELECT 1 FROM wh_moves WHERE ref_id=? AND kind='in'", (doc_id,)).fetchone():
        return
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d or d["kind"] != "RETURN" or d["status"] != "accepted":
        return
    for l in conn.execute("SELECT * FROM lines WHERE document_id=?", (doc_id,)).fetchall():
        if l["pairs_recv"]:
            conn.execute(
                "INSERT INTO wh_moves (created_at, kind, customer_id, model_id, quality, qty, reason, ref_id, role) "
                "VALUES (?, 'in', ?, ?, ?, ?, 'От производства', ?, 'sklad')",
                (d["accepted_at"], l["customer_id"], l["model_id"],
                 "brak" if l["status"] == "brak" else "ready", l["pairs_recv"], doc_id))


def _wh_backfill():
    conn = db.get_db()
    if not _once(conn, "wh_backfill_v1"):
        conn.close()
        return
    for d in conn.execute("SELECT id FROM documents WHERE kind='RETURN' AND status='accepted'").fetchall():
        wh_from_doc(conn, d["id"])
    conn.commit()
    conn.close()


if os.environ.get("JAIL_SKIP_MIGRATIONS") != "1":
    _wh_backfill()


@_cached
def wh_balances():
    """Остатки на нашем складе по (заказчик, модель): готовая и брак."""
    rows = g.db.execute(
        """SELECT w.customer_id, w.model_id, c.name customer, m.name model, w.quality,
                  SUM(w.qty) q, MAX(w.id) last_id,
                  MAX(CASE WHEN w.kind='in' THEN w.id END) last_in_id
           FROM wh_moves w JOIN customers c ON c.id=w.customer_id JOIN models m ON m.id=w.model_id
           GROUP BY w.customer_id, w.model_id, w.quality""").fetchall()
    agg = {}
    for r in rows:
        e = agg.setdefault((r["customer_id"], r["model_id"]),
                           dict(cid=r["customer_id"], mid=r["model_id"], customer=r["customer"],
                                model=r["model"], ready=0, brak=0, last_id=0, last_in_id=0))
        e[r["quality"]] += r["q"] or 0
        e["last_id"] = max(e["last_id"], r["last_id"] or 0)
        e["last_in_id"] = max(e["last_in_id"], r["last_in_id"] or 0)
    for e in agg.values():
        row = g.db.execute("SELECT created_at FROM wh_moves WHERE id=?", (e["last_id"],)).fetchone()
        e["last"] = row["created_at"] if row else ""
        row = g.db.execute("SELECT created_at, ref_id FROM wh_moves WHERE id=?", (e["last_in_id"],)).fetchone()
        e["accepted"] = row["created_at"] if row else ""
        e["doc_id"] = row["ref_id"] if row else None
    items = [e for e in agg.values() if abs(e["ready"]) > 1e-9 or abs(e["brak"]) > 1e-9]
    items.sort(key=lambda e: ((e["customer"] or "").lower(), (e["model"] or "").lower()))
    return items


def _mat_key(name):
    return " ".join((name or "").split()).lower()


@_cached
def mat_balances():
    """Материалы на складе склада: {ключ: {name, unit, qty, last}}."""
    agg = {}
    for m in g.db.execute("SELECT * FROM mat_moves ORDER BY id").fetchall():
        e = agg.setdefault(_mat_key(m["name"]), dict(name=m["name"], unit=m["unit"] or "", qty=0, last=m["created_at"]))
        e["qty"] += m["qty"]
        e["last"] = m["created_at"]
    for e in agg.values():
        e["qty"] = round(e["qty"], 3)
    return agg


def _mat_move(kind, name, unit, qty, reason, note=None, ref_id=None):
    g.db.execute("INSERT INTO mat_moves (created_at, kind, name, unit, qty, reason, note, ref_id, role) VALUES (?,?,?,?,?,?,?,?,?)",
                 (db.now_str(), kind, name, unit, qty, reason, note, ref_id, session.get("role")))


@app.route("/wh/mat", methods=["POST"])
def wh_mat():
    """Склад: приход материала (от поставщика) или списание."""
    if session.get("role") != "sklad":
        abort(403)
    act = request.form.get("act")
    name = " ".join((request.form.get("name") or "").split())
    row = _find_ref("materials", name) if name else None
    qty = _num(request.form.get("qty"))
    back = redirect(url_for("wh", tab="mats"))
    if not row:
        flash("Выберите материал из справочника." if name else "Укажите материал.")
        return back
    if not qty:
        flash("Укажите количество.")
        return back
    m = g.db.execute("SELECT name, unit FROM materials WHERE id=?", (row["id"],)).fetchone()
    note = (request.form.get("note") or "").strip() or None
    if act == "writeoff":
        bal = mat_balances().get(_mat_key(m["name"]), {}).get("qty", 0)
        if qty - bal > 1e-9:
            flash(f"Нельзя списать больше остатка ({_fmt(bal)} {m['unit']}).")
            return back
        _mat_move("writeoff", m["name"], m["unit"], -qty, request.form.get("reason") or "Списание", note)
        flash(f"Списано: {m['name']} {_fmt(qty)} {m['unit']}.")
    else:
        _mat_move("in", m["name"], m["unit"], qty, "Приход", note)
        flash(f"Приход: {m['name']} {_fmt(qty)} {m['unit']}.")
    g.db.commit()
    return back


@app.route("/wh")
def wh():
    """Склад готовой обуви — роль «Склад» списывает, директор смотрит."""
    if session.get("role") not in ROLES:
        return redirect(url_for("login"))
    if session.get("role") not in ("sklad", "director"):
        abort(403)
    tab = request.args.get("tab", "ready")
    items = wh_balances()
    moves = []
    if tab == "moves":
        moves = g.db.execute(
            """SELECT w.*, c.name customer, m.name model FROM wh_moves w
               JOIN customers c ON c.id=w.customer_id JOIN models m ON m.id=w.model_id
               ORDER BY w.id DESC LIMIT 200""").fetchall()
    mats = sorted(mat_balances().values(), key=lambda e: e["name"].lower())
    mat_moves = []
    if tab == "moves":
        mat_moves = g.db.execute("SELECT * FROM mat_moves ORDER BY id DESC LIMIT 200").fetchall()
    return render_template("wh.html", tab=tab, items=items, moves=moves, reasons=WH_REASONS,
                           mats=[e for e in mats if abs(e["qty"]) > 1e-9], mat_moves=mat_moves,
                           materials=_materials_catalog(),
                           operations=_operations(),
                           can_edit=session.get("role") == "sklad",
                           total_ready=sum(e["ready"] for e in items),
                           total_brak=sum(e["brak"] for e in items))


def _wh_brak_free(cid, mid, exclude_req=None):
    """Брак на складе минус то, что уже положено в ремонт в неотправленную передачу."""
    bal = g.db.execute("SELECT COALESCE(SUM(qty),0) s FROM wh_moves WHERE customer_id=? AND model_id=? "
                       "AND quality='brak'", (cid, mid)).fetchone()["s"]
    queued = g.db.execute(
        """SELECT COALESCE(SUM(ri.collected),0) s FROM request_items ri JOIN requests r ON r.id=ri.request_id
           WHERE ri.status='repair' AND ri.customer_id=? AND ri.model_id=? AND r.status='progress'""",
        (cid, mid)).fetchone()["s"]
    return bal - queued


@app.route("/wh/repair", methods=["POST"])
def wh_repair():
    """Брак со склада — в ремонт: кладётся в текущую передачу на производство."""
    if session.get("role") != "sklad":
        abort(403)
    try:
        cid, mid = int(request.form["cid"]), int(request.form["mid"])
        if not (0 < cid < 2**62 and 0 < mid < 2**62):
            raise ValueError
    except (KeyError, ValueError):
        abort(400)
    qty = _num(request.form.get("qty"))
    free = _wh_brak_free(cid, mid)
    if not qty or qty != int(qty):
        flash("Впишите целое число пар.")
        return redirect(url_for("wh", tab="brak"))
    if qty - free > 1e-9:
        flash(f"Столько брака нет: доступно {_fmt(free)} пар.")
        return redirect(url_for("wh", tab="brak"))
    t = _open_transfer(create=True)
    note = "Ремонт (брак со склада)" + (" · " + request.form["note"].strip() if (request.form.get("note") or "").strip() else "")
    cur = g.db.execute(
        "INSERT INTO request_items (request_id, item, collected, placed, source, line_kind, customer_id, model_id, "
        "status, note) VALUES (?, '', ?, 1, 'sklad', 'pair', ?, ?, 'repair', ?)", (t, int(qty), cid, mid, note))
    _save_item_ops("req", cur.lastrowid)   # за ремонт платим по выбранным операциям (если выбраны)
    g.db.commit()
    flash(f"{int(qty)} пар брака положено в передачу на производство — в ремонт.")
    return redirect(url_for("request_view", req_id=t, step=2))


@app.route("/wh/writeoff", methods=["POST"])
def wh_writeoff():
    """Списание с нашего склада: отгрузка заказчику, брак, потеря."""
    if session.get("role") != "sklad":
        abort(403)
    try:
        cid, mid = int(request.form["cid"]), int(request.form["mid"])
        if not (0 < cid < 2**62 and 0 < mid < 2**62):
            raise ValueError
    except (KeyError, ValueError):
        abort(400)
    quality = request.form.get("quality") if request.form.get("quality") in WH_QUALITY else "ready"
    qty = _int_pairs(request.form.get("qty"))
    back = redirect(url_for("wh", tab=quality))
    if not qty:
        flash("Впишите, сколько списать.")
        return back
    bal = g.db.execute("SELECT COALESCE(SUM(qty),0) s FROM wh_moves WHERE customer_id=? AND model_id=? "
                       "AND quality=?", (cid, mid, quality)).fetchone()["s"]
    if qty - bal > 1e-9:
        flash(f"Нельзя списать больше остатка ({_fmt(bal)}).")
        return back
    reason = request.form.get("reason") if request.form.get("reason") in WH_REASONS else "Другое"
    g.db.execute("INSERT INTO wh_moves (created_at, kind, customer_id, model_id, quality, qty, reason, note, role) "
                 "VALUES (?, 'writeoff', ?, ?, ?, ?, ?, ?, 'sklad')",
                 (db.now_str(), cid, mid, quality, -qty, reason,
                  (request.form.get("note") or "").strip() or None))
    g.db.commit()
    flash(f"Списано: {_fmt(qty)} пар. Остаток {_fmt(bal - qty)}.")
    return back

MAT_UNITS = ["м2", "шт", "пары"]   # единицы измерения материалов (пока только эти)

STOCK_KINDS = {"in": "Приход", "out": "На склад", "writeoff": "Списание",
               "inv": "Инвентаризация", "add": "Добавлено вручную"}
WRITEOFF_REASONS = ["Израсходовано в производстве", "Брак", "Потеря", "Другое"]


def _stock_key(m):
    if m["item_type"] == "pair":
        return f"p-{m['customer_id']}-{m['model_id']}"
    return "m-" + (m["name"] or "").strip().lower() + "|" + (m["unit"] or "").strip().lower()


@_cached
def stock_balances():
    """Остатки на производстве: обувь (заказчик+модель) и материалы (название+ед.)."""
    rows = g.db.execute(
        """SELECT s.*, c.name AS customer, md.name AS model FROM stock_moves s
           LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN models md ON md.id=s.model_id
           ORDER BY s.id""").fetchall()
    agg = {}
    for m in rows:
        k = _stock_key(m)
        e = agg.get(k)
        if not e:
            e = agg[k] = dict(key=k, type=m["item_type"], qty=0, last=m["created_at"],
                              title=(f"{m['customer']} — {m['model']}" if m["item_type"] == "pair" else m["name"]),
                              customer=m["customer"], model=m["model"],
                              unit=("пар" if m["item_type"] == "pair" else (m["unit"] or "")))
        e["qty"] += m["qty"]
        e["last"] = m["created_at"]
    items = [e for e in agg.values() if abs(e["qty"]) > 1e-9]
    items.sort(key=lambda e: e["title"].lower())
    return items


def _fmt(v):
    v = round(v or 0, 3)
    return (str(int(v)) if v == int(v) else str(v)).replace(".", ",")


app.jinja_env.filters["q"] = _fmt


def _can_stock():
    """Склад производства: производство работает, директор и склад («Зона») только смотрят."""
    return session.get("role") in ("proizv", "director", "sklad")


def _can_stock_edit():
    """Списывать, пересчитывать и добавлять может только производство; директор только смотрит."""
    return session.get("role") == "proizv"


def warehouse_ready():
    """Готовая обувь, принятая складом от производства (по заказчику и модели)."""
    rows = g.db.execute(
        """SELECT c.name customer, m.name model, l.customer_id, l.model_id,
                  SUM(CASE WHEN COALESCE(l.status,'')!='brak' THEN l.pairs_recv ELSE 0 END) ready,
                  SUM(CASE WHEN l.status='brak' THEN l.pairs_recv ELSE 0 END) brak,
                  MAX(d.id) last_doc
           FROM lines l JOIN documents d ON d.id=l.document_id
           JOIN customers c ON c.id=l.customer_id JOIN models m ON m.id=l.model_id
           WHERE d.kind='RETURN' AND d.status='accepted' AND l.pairs_recv > 0
           GROUP BY l.customer_id, l.model_id""").fetchall()
    return sorted([dict(r) for r in rows], key=lambda r: ((r["customer"] or "").lower(), (r["model"] or "").lower()))


@app.route("/stock")
def stock():
    if session.get("role") not in ROLES:
        return redirect(url_for("login"))
    if not _can_stock():
        abort(403)
    tab = request.args.get("tab", "ready" if session.get("role") == "director" else "work")
    if tab == "pairs":
        tab = "work"
    items = stock_balances()
    work, fin_ready, fin_brak = _prod_view()
    pairs = [e for e in items if e["type"] == "pair"]
    mats = [e for e in items if e["type"] == "material"]
    moves = []
    if tab == "moves":
        moves = g.db.execute(
            """SELECT s.*, c.name AS customer, md.name AS model FROM stock_moves s
               LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN models md ON md.id=s.model_id
               ORDER BY s.id DESC LIMIT 200""").fetchall()
    ready = wh_balances() if session.get("role") == "director" else []
    return render_template("stock.html", tab=tab, pairs=pairs, mats=mats, moves=moves, ready=ready,
                           work=work, fin_ready=fin_ready, fin_brak=fin_brak, fin_kinds=FIN_KINDS,
                           can_edit=_can_stock_edit(),
                           reasons=WRITEOFF_REASONS, units=MAT_UNITS, materials=_materials_catalog(),
                           total_pairs=sum(e["qty"] for e in pairs),
                           customers=g.db.execute("SELECT * FROM customers WHERE archived=0 ORDER BY name").fetchall(),
                           models=g.db.execute("SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall())


def _parse_key(k):
    if k.startswith("p-"):
        _, c, m = k.split("-", 2)
        return dict(item_type="pair", cid=int(c), mid=int(m))
    name, _, unit = k[2:].partition("|")
    return dict(item_type="material", name=name, unit=unit)


def _item_moves(k):
    p = _parse_key(k)
    if p["item_type"] == "pair":
        return g.db.execute(
            "SELECT * FROM stock_moves WHERE item_type='pair' AND customer_id=? AND model_id=? ORDER BY id DESC",
            (p["cid"], p["mid"])).fetchall()
    # сравниваем в Python: lower() в SQLite не понимает русские буквы
    rows = g.db.execute("SELECT * FROM stock_moves WHERE item_type='material' ORDER BY id DESC").fetchall()
    return [m for m in rows if (m["name"] or "").strip().lower() == p["name"]
            and (m["unit"] or "").strip().lower() == p["unit"]]


@app.route("/stock/item")
def stock_item():
    if session.get("role") not in ROLES:
        return redirect(url_for("login"))
    if not _can_stock():
        abort(403)
    k = request.args.get("k", "")
    try:
        moves = _item_moves(k)
    except (ValueError, IndexError):
        abort(404)
    if not moves:
        return redirect(url_for("stock"))
    e = [x for x in stock_balances() if x["key"] == k]
    m0 = moves[-1]
    if m0["item_type"] == "pair":
        c = g.db.execute("SELECT name FROM customers WHERE id=?", (m0["customer_id"],)).fetchone()
        md = g.db.execute("SELECT name FROM models WHERE id=?", (m0["model_id"],)).fetchone()
        title, unit = f"{c['name'] if c else '?'} — {md['name'] if md else '?'}", "пар"
    else:
        title, unit = m0["name"], (m0["unit"] or "")
    return render_template("stock_item.html", k=k, moves=moves, title=title, unit=unit,
                           qty=(e[0]["qty"] if e else 0), is_pair=(m0["item_type"] == "pair"),
                           reasons=WRITEOFF_REASONS, can_edit=(_can_stock_edit() and m0["item_type"] != "pair"))


@app.route("/stock/item/act", methods=["POST"])
def stock_act():
    """Списание или инвентаризация по позиции склада производства."""
    if not _can_stock_edit():
        abort(403)
    k = request.form.get("k", "")
    act = request.form.get("act")
    try:
        moves = _item_moves(k)
    except (ValueError, IndexError, KeyError):
        abort(404)
    if not moves:
        abort(404)
    m0 = moves[-1]
    if m0["item_type"] == "pair":
        abort(403)   # обувь на производстве меняется только передачами — без ручных списаний
    bal = sum(m["qty"] for m in moves)
    qty = _num(request.form.get("qty")) if act == "writeoff" else None
    note = (request.form.get("note") or "").strip() or None
    base = dict(cid=m0["customer_id"], mid=m0["model_id"], name=m0["name"], unit=m0["unit"],
                role=session.get("role"))
    back = redirect(url_for("stock", tab=request.form.get("back_tab"))
                    if request.form.get("back_tab") else url_for("stock_item", k=k))
    if act == "writeoff":
        if not qty:
            flash("Впишите, сколько списать.")
            return back
        if qty - bal > 1e-9:
            flash(f"Нельзя списать больше остатка ({_fmt(bal)}).")
            return back
        reason = request.form.get("reason") or "Другое"
        if reason not in WRITEOFF_REASONS:
            reason = "Другое"
        _move(g.db, "writeoff", m0["item_type"], -qty, db.now_str(), reason=reason, note=note, **base)
        flash(f"Списано: {_fmt(qty)}. Остаток {_fmt(bal - qty)}.")
    elif act == "inv":
        raw = (request.form.get("fact") or "").replace(",", ".").strip()
        try:
            fact = float(raw)
        except ValueError:
            fact = None
        if fact is None or not math.isfinite(fact) or fact > MAX_QTY:
            flash("Впишите, сколько есть по факту.")
            return back
        if fact < 0:
            flash("Количество не может быть отрицательным.")
            return back
        d = fact - bal
        if abs(d) < 1e-9:
            flash("Факт совпадает с остатком — изменений нет.")
            return back
        _move(g.db, "inv", m0["item_type"], d, db.now_str(), reason="Инвентаризация", note=note, **base)
        flash(f"Инвентаризация: {'+' if d > 0 else ''}{_fmt(d)}. Остаток теперь {_fmt(fact)}.")
    else:
        abort(400)
    g.db.commit()
    return back


@app.route("/stock/add", methods=["POST"])
def stock_add():
    """Ручное оприходование на производстве отключено: всё приходит только передачами склада."""
    abort(403)
    if not _can_stock_edit():
        abort(403)
    t = request.form.get("t")
    if t == "pair":
        abort(403)   # обувь вручную не добавляется — только по передачам склада
    qty = _num(request.form.get("qty"))
    note = (request.form.get("note") or "").strip() or None
    if not qty:
        flash("Впишите количество.")
        return redirect(url_for("stock", tab="pairs" if t == "pair" else "mats"))
    if t == "pair":
        try:
            cid, mid = _ref_from_form("customers", "customer"), _ref_from_form("models", "model")
        except (KeyError, ValueError):
            flash("Впишите заказчика и модель.")
            return redirect(url_for("stock", tab="pairs"))
        _move(g.db, "add", "pair", qty, db.now_str(), cid=cid, mid=mid, reason="Оприходование",
              note=note, role=session.get("role"))
        k = f"p-{cid}-{mid}"
    else:
        name, unit = _material_from_form("name")
        if not name:
            flash(unit)
            return redirect(url_for("stock", tab="mats"))
        _move(g.db, "add", "material", qty, db.now_str(), name=name, unit=unit, reason="Оприходование",
              note=note, role=session.get("role"))
        k = "m-" + name.lower() + "|" + (unit or "").lower()
    g.db.commit()
    flash(f"Добавлено: {_fmt(qty)}.")
    return redirect(url_for("stock_item", k=k))


@app.after_request
def _no_stale_pages(resp):
    """Страницы всегда свежие: после обновления браузер не показывает старую версию из кэша."""
    if resp.mimetype == "text/html":
        resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/version")
def version_now():
    """Версия, которая сейчас реально работает (для страницы обновления)."""
    resp = app.response_class(VERSION, mimetype="text/plain")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- Соединение с БД ----------
@app.before_request
def _open_db():
    g.db = db.get_db()
    # Любое сохранение — под замком записи с самого начала: проверка («хватает ли остатка», «ещё не
    # отправлено») и запись идут одним целым. Два нажатия с разных устройств выполняются по очереди,
    # и второе уже видит результат первого — без двойных отгрузок и списаний в минус.
    if request.method == "POST" and not request.path.startswith("/update"):
        try:
            g.db.execute("BEGIN IMMEDIATE")
        except Exception:
            g.db.close()
            abort(503, description="База занята. Повторите действие через несколько секунд.")


@app.teardown_request
def _close_db(exc):
    d = getattr(g, "db", None)
    if d is not None:
        d.close()


def _pending_accept_count(role):
    """Сколько документов ждёт приёмки этой ролью."""
    if role not in ("sklad", "proizv"):
        return 0
    kinds = [k for k, v in KINDS.items() if v[2] == role]
    if not kinds:
        return 0
    q = ("SELECT COUNT(*) c FROM documents WHERE status='sent' AND kind IN (%s)"
         % ",".join("?" * len(kinds)))
    n = g.db.execute(q, kinds).fetchone()["c"]
    if role == "proizv":
        n += g.db.execute("SELECT COUNT(*) c FROM requests WHERE status='shipped' AND transfer_id IS NULL").fetchone()["c"]
    return n


def _open_requests_count():
    """Сколько заявок ждёт склад (новые + собираются)."""
    if getattr(g, "db", None) is None:
        return 0
    return g.db.execute(
        "SELECT COUNT(*) c FROM requests WHERE created_role='proizv' AND status IN ('open','progress','done')"
    ).fetchone()["c"]


def _req_todo_count(role):
    """Бейдж «Заявки»: сколько дел ждут эту роль. Уходит только когда дело сделано."""
    if getattr(g, "db", None) is None:
        return 0
    q = lambda sql: g.db.execute(sql).fetchone()["c"]
    if role == "sklad":   # = синие строки в «Заявках» склада
        return _open_requests_count() + q(
            "SELECT COUNT(DISTINCT r.id) c FROM requests r JOIN request_items ri ON ri.request_id=r.id "
            "AND ri.line_kind IN ('pair','material') WHERE r.created_role='sklad' AND r.status='progress' "
            "AND NOT EXISTS (SELECT 1 FROM requests x WHERE x.transfer_id=r.id)")
    if role == "proizv":
        return q("SELECT COUNT(DISTINCT d.id) c FROM documents d JOIN lines l ON l.document_id=d.id "
                 "WHERE d.kind='RETURN' AND d.status='draft'") + q(
            "SELECT COUNT(DISTINCT r.id) c FROM requests r JOIN request_items ri ON ri.request_id=r.id "
            "WHERE r.created_role='proizv' AND r.status='draft'")
    return 0


@app.context_processor
def _inject():
    role = session.get("role")
    pending = 0
    req_open = 0
    if role in ("sklad", "proizv") and getattr(g, "db", None) is not None:
        pending = _pending_accept_count(role)
    if role in ("sklad", "proizv", "director") and getattr(g, "db", None) is not None:
        req_open = _open_requests_count()
    req_todo = _req_todo_count(role) if role in ("sklad", "proizv") else 0
    disc_open = _open_discr_count(role) if role in ("sklad", "proizv", "director") and getattr(g, "db", None) is not None else 0
    return dict(
        VERSION=VERSION, ROLES=ROLES, KINDS=KINDS, STOCK_KINDS=STOCK_KINDS, WH_KINDS=WH_KINDS,
        KIND_SHORT=KIND_SHORT, STATUS_LABEL=STATUS_LABEL,
        LINE_STATUSES=LINE_STATUSES, KIND_LINE_STATUSES=KIND_LINE_STATUSES,
        REQ_STATUS=REQ_STATUS, ITEM_TYPES=ITEM_TYPES,
        PROD_REQ_STATUS=PROD_REQ_STATUS, PROD_DOC_STATUS=PROD_DOC_STATUS, SKLAD_REQ_STATUS=SKLAD_REQ_STATUS,
        role=role, role_name=ROLES.get(role), pending_accept=pending,
        req_open=req_open, req_todo=req_todo, disc_open=disc_open,
        web_updates_enabled=os.environ.get("JAIL_ALLOW_WEB_UPDATES") == "1",
    )


# ---------- Операции и долг ----------
def _operations():
    return g.db.execute("SELECT * FROM operations ORDER BY ord, id").fetchall()


def _save_item_ops(kind, item_id):
    """Сохранить выбранные операции строки с текущей ценой."""
    ids = []
    for v in request.form.getlist("ops"):
        try:
            ids.append(int(v))
        except ValueError:
            pass
    for o in _operations():
        if o["id"] in ids:
            g.db.execute(
                "INSERT INTO item_ops (kind, item_id, operation_id, name, price) VALUES (?,?,?,?,?)",
                (kind, item_id, o["id"], o["name"], o["price"]))


def _selected_ops_ok():
    valid = {o["id"] for o in _operations()}
    for v in request.form.getlist("ops"):
        try:
            if int(v) in valid:
                return True
        except ValueError:
            pass
    return False


def _ops_map(kind, item_ids):
    """{item_id: [операции]} для списка строк."""
    item_ids = list(item_ids)
    res = {i: [] for i in item_ids}
    if not item_ids:
        return res
    q = ("SELECT * FROM item_ops WHERE kind=? AND item_id IN (%s) ORDER BY id"
         % ",".join("?" * len(item_ids)))
    for r in g.db.execute(q, [kind] + item_ids):
        res[r["item_id"]].append(r)
    return res


def _del_item_ops(kind, item_ids):
    item_ids = list(item_ids)
    if item_ids:
        g.db.execute("DELETE FROM item_ops WHERE kind=? AND item_id IN (%s)"
                     % ",".join("?" * len(item_ids)), [kind] + item_ids)


def _parse_dt(sv):
    return db.parse_dt(sv) or db.datetime.max


def _sig(ops):
    """Подпись партии: какие операции и по какой цене делались с парами (одинаково для одинаковых партий)."""
    return json.dumps(sorted([[o["name"], float(o["price"])] for o in ops]), ensure_ascii=False)


def _sig_ops(sig):
    try:
        return [dict(name=n, price=p) for n, p in json.loads(sig or "[]")]
    except (ValueError, TypeError):
        return []


def _plural_ops(n):
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} операция"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} операции"
    return f"{n} операций"


@app.template_filter("ops_short")
def _ops_short(ops):
    """Операции коротко: «4 операции», список — при наведении и по клику. Одна операция — её название."""
    from markupsafe import Markup, escape
    names = [(o["name"] if not isinstance(o, str) else o) for o in (ops or [])]
    if not names:
        return Markup('<span class="small">—</span>')
    if len(names) == 1:
        return Markup(f'<span class="ops-one">{escape(names[0])}</span>')
    full = ", ".join(names)
    items = "".join(f"<div>{escape(n)}</div>" for n in names)
    return Markup(f'<details class="ops-n" onclick="event.stopPropagation()"><summary title="{escape(full)}">'
                  f'{_plural_ops(len(names))}</summary><div class="ops-list">{items}</div></details>')


@app.template_filter("lot_ops")
def _lot_ops_filter(sig):
    return _sig_ops(sig)


def _received_chunks():
    """Пары, которые производство получило от склада, по партиям (в порядке поступления)."""
    rows = g.db.execute(
        """SELECT ri.id, ri.customer_id, ri.model_id, COALESCE(ri.recv, ri.collected) AS q,
                  COALESCE(r.accepted_at, r.done_at) AS t
           FROM request_items ri JOIN requests r ON r.id=ri.request_id
           WHERE ri.line_kind='pair' AND r.status='accepted'""").fetchall()   # только реально принятое
    om = _ops_map("req", [r["id"] for r in rows])
    src = [(r, om[r["id"]]) for r in rows]
    rows = g.db.execute(
        """SELECT l.id, l.customer_id, l.model_id,
                  COALESCE(l.pairs_recv, l.pairs_sent) AS q, d.sent_at AS t
           FROM lines l JOIN documents d ON d.id=l.document_id
           WHERE d.kind='OUT' AND d.status='accepted'""").fetchall()
    om2 = _ops_map("line", [r["id"] for r in rows])
    src += [(r, om2[r["id"]]) for r in rows]
    src.sort(key=lambda x: _parse_dt(x[0]["t"]))
    chunks = {}
    for r, ops in src:
        if r["q"]:
            chunks.setdefault((r["customer_id"], r["model_id"]), []).append(
                dict(left=r["q"], ops=ops, sig=_sig(ops), cost=sum(_cents(o["price"]) for o in ops) / 100))
    return chunks


def _take(chunks, need, sig=None):
    """Забрать пары из партий по очереди (только своей партии, если sig задан)."""
    parts = []
    for ch in chunks:
        if need <= 0:
            break
        if sig is not None and ch["sig"] != sig:
            continue
        t = min(need, ch["left"])
        if t > 0:
            ch["left"] -= t
            need -= t
            parts.append(dict(pairs=t, ops=ch["ops"], cost=ch["cost"]))
    return parts, need


@_cached
def compute_debt():
    """Долг производству: принятые складом пары × операции ИМЕННО ИХ партии.

    Производство, отправляя на склад, выбирает партию (какие операции делались), поэтому
    долг считается точно. Старые строки без партии — по очереди отправки, как раньше.
    """
    chunks = _received_chunks()
    rets = g.db.execute(
        """SELECT l.*, c.name AS customer, m.name AS model, d.id AS doc_id, d.accepted_at
           FROM lines l JOIN documents d ON d.id=l.document_id
           JOIN customers c ON c.id=l.customer_id JOIN models m ON m.id=l.model_id
           WHERE d.kind='RETURN' AND d.status='accepted' AND l.pairs_recv > 0""").fetchall()
    rets = sorted(rets, key=lambda l: (_parse_dt(l["accepted_at"]), l["id"]))
    items, by_op = [], {}
    total, uncovered = 0, 0
    for l in rets:
        pool = chunks.get((l["customer_id"], l["model_id"]), [])
        brak = l["status"] == "brak"
        if l["lot"] is not None:
            _take(pool, l["pairs_recv"], l["lot"])          # партия уходит со склада производства
            ops = _sig_ops(l["lot"])
            cost = sum(_cents(o["price"]) for o in ops) / 100
            parts, need = [dict(pairs=l["pairs_recv"], ops=ops, cost=cost)], 0
        else:
            parts, need = _take(pool, l["pairs_recv"])
        if brak:
            continue   # за брак не платим, но пары из партии ушли
        amount = sum(int(p_["pairs"]) * _cents(p_["cost"]) for p_ in parts) / 100
        for p_ in parts:
            for o in p_["ops"]:
                e = by_op.setdefault(o["name"], dict(name=o["name"], pairs=0, sum=0))
                e["pairs"] += p_["pairs"]
                e["sum"] = (_cents(e["sum"]) + int(p_["pairs"]) * _cents(o["price"])) / 100
        total = (_cents(total) + _cents(amount)) / 100
        uncovered += need
        items.append(dict(l=l, amount=amount, parts=parts, uncovered=need))
    items.reverse()
    return dict(total=total, items=items, uncovered=uncovered,
                by_op=sorted(by_op.values(), key=lambda e: e["name"]))


@app.template_filter("rub")
def _rub(v):
    v = round(v or 0, 2)
    s = f"{v:,.2f}".replace(",", " ").replace(".00", "").replace(".", ",")
    return s + " ₽"


@app.route("/debt")
def debt():
    """Долг производству: начислено за принятую складом обувь минус оплаты."""
    if session.get("role") not in ROLES:
        return redirect(url_for("login"))
    if session.get("role") != "director":
        abort(403)
    d = compute_debt()
    # за что долг: принятые складом модели (заказчик + модель)
    by_model = {}
    for x in d["items"]:
        l = x["l"]
        e = by_model.setdefault((l["customer_id"], l["model_id"]),
                                dict(customer=l["customer"], model=l["model"], pairs=0, sum=0, last=l["accepted_at"],
                                     docs=[]))
        e["pairs"] += l["pairs_recv"]
        e["sum"] = (_cents(e["sum"]) + _cents(x["amount"])) / 100
        if l["doc_id"] not in e["docs"]:
            e["docs"].append(l["doc_id"])
    pays = g.db.execute("SELECT * FROM payments ORDER BY id DESC").fetchall()
    paid = sum(p["amount_kopeks"] for p in pays if not p["cancelled_at"]) / 100
    return render_template("debt.html", accrued=d["total"], paid=paid, left=(_cents(d["total"]) - _cents(paid)) / 100,
                           by_model=sorted(by_model.values(), key=lambda e: -e["sum"]),
                           pays=pays, uncovered=d["uncovered"], payment_token=uuid.uuid4().hex, today=db.datetime.now(db.TZ).date().isoformat())


def _cents(value):
    return int((Decimal(str(value or 0)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@app.route("/debt/pay", methods=["POST"])
def debt_pay():
    if session.get("role") != "director":
        abort(403)
    token = (request.form.get("action_token") or "").strip()
    if not token or len(token) > 100:
        flash("Обновите страницу оплаты и повторите ввод.")
        return redirect(url_for("debt"))
    if g.db.execute("SELECT 1 FROM payments WHERE action_token=?", (token,)).fetchone():
        flash("Эта оплата уже записана.")
        return redirect(url_for("debt"))
    amount = _num(request.form.get("amount"), cap=1e10)
    cents = _cents(amount)
    if cents <= 0:
        flash("Впишите сумму оплаты.")
        return redirect(url_for("debt"))
    date = request.form.get("payment_date") or db.datetime.now(db.TZ).date().isoformat()
    try:
        db.datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        flash("Проверьте дату оплаты.")
        return redirect(url_for("debt"))
    g.db.execute("INSERT INTO payments(created_at,amount,amount_kopeks,note,action_token,payment_date) VALUES(?,?,?,?,?,?)",
                 (db.now_str(), cents/100, cents, (request.form.get("note") or "").strip() or None, token, date))
    g.db.commit()
    flash("Оплата записана.")
    return redirect(url_for("debt"))


@app.route("/debt/pay/<int:pid>/delete", methods=["POST"])
def debt_pay_delete(pid):
    if session.get("role") != "director":
        abort(403)
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("Укажите причину отмены оплаты.")
        return redirect(url_for("debt"))
    g.db.execute("UPDATE payments SET cancelled_at=?, cancel_reason=?, cancelled_by='director' WHERE id=? AND cancelled_at IS NULL", (db.now_str(), reason, pid))
    g.db.commit()
    flash("Оплата отменена. Запись сохранена в истории.")
    return redirect(url_for("debt"))


# ---------- Приёмка передач на производстве ----------
def _placed_items(req_id):
    return [i for i in _load_req_items(req_id) if i["line_kind"] in ("pair", "material")]


def _req_diff(req_id):
    """Сумма расхождений (принято − отправлено) по передаче."""
    t = 0
    for i in _placed_items(req_id):
        if i["recv"] is not None and i["collected"] is not None:
            t += i["recv"] - i["collected"]
    return int(t) if t == int(t) else t


# ---------- Доступ ----------
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if session.get("role") not in ROLES:
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def can_edit_refs(table=None):
    """Справочники правят склад и директор; материалы — ещё и производство."""
    role = session.get("role")
    return role in ("sklad", "director") or (role == "proizv" and table == "materials")


def can_see_discrepancies():
    return session.get("role") in ("sklad", "proizv", "director")


# ---------- Вход ----------
@app.route("/login")
def login():
    if session.get("role") in ROLES:
        return redirect(url_for("documents"))
    return render_template("login.html")


@app.route("/login/<role>")
def login_as(role):
    if role in ROLES:
        session["role"] = role
        if role == "director":
            return redirect(url_for("overview"))
        return redirect(_start_url(role))
    return redirect(url_for("login"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return redirect(_start_url(session["role"]))


def _start_url(role):
    if role == "director":
        return url_for("overview")
    return url_for("wh") if role == "sklad" else url_for("stock")


@app.route("/home")
@login_required
def home():
    return redirect(_start_url(session["role"]))


# ---------- Документы ----------
def _doc_dir(kind):
    """Роль-создатель и роль-приёмщик для типа."""
    return KINDS[kind][1], KINDS[kind][2]


def _visible_kinds(role):
    """Какие типы документов роль вообще видит."""
    return ["OUT", "RETURN"]


def _load_lines(doc_id):
    return g.db.execute(
        """SELECT l.*, c.name AS customer, m.name AS model
           FROM lines l
           JOIN customers c ON c.id = l.customer_id
           JOIN models    m ON m.id = l.model_id
           WHERE l.document_id = ?
           ORDER BY l.id""",
        (doc_id,),
    ).fetchall()


def _doc_totals(doc_id):
    lines = _load_lines(doc_id)
    sent = sum(l["pairs_sent"] for l in lines)
    recv = sum((l["pairs_recv"] or 0) for l in lines)
    has_recv = any(l["pairs_recv"] is not None for l in lines)
    diff = (recv - sent) if has_recv else 0
    return sent, recv, diff, has_recv


def _sort_key(created_at):
    return db.parse_dt(created_at) or db.datetime.min


@app.template_filter("dt")
def _dt_show(sv):
    """Дата из базы → «ДД.ММ.ГГГГ ЧЧ:ММ» для экрана."""
    d = db.parse_dt(sv)
    return d.strftime(db.SHOW_FMT) if d else (sv or "")


@app.route("/docs")
@login_required
def documents():
    """Раздел «Заявки»: у каждой роли свой вид."""
    role = session["role"]
    if role == "proizv":
        return _prod_requests()
    if role == "sklad":
        return _sklad_requests()
    return _director_requests()


def _director_requests():
    """Директор: всё движение в одном месте — «Материалы» (заказы производства и передачи склада)
    и «Отправка готовой обуви»; только просмотр."""
    tab = request.args.get("tab") if request.args.get("tab") in ("mat", "shoes") else "shoes"
    sub = "done" if request.args.get("sub") == "done" else "work"
    names = {"open": "Отправлена складу", "progress": "Склад собирает", "done": "Склад собирает",
             "shipped": "Едет на производство", "accepted": "Получена"}
    rows = []
    if tab == "mat":
        for r in g.db.execute("SELECT * FROM requests ORDER BY id DESC").fetchall():
            if r["status"] == "draft":
                continue
            by_sklad = r["created_role"] == "sklad"
            if by_sklad:
                if g.db.execute("SELECT 1 FROM requests WHERE transfer_id=? LIMIT 1", (r["id"],)).fetchone():
                    continue
                its = g.db.execute("SELECT ri.*, c.name AS customer, m.name AS model FROM request_items ri "
                                   "LEFT JOIN customers c ON c.id=ri.customer_id LEFT JOIN models m ON m.id=ri.model_id "
                                   "WHERE ri.request_id=? AND ri.line_kind IN ('pair','material') ORDER BY ri.id",
                                   (r["id"],)).fetchall()
                if not its or r["status"] not in ("progress", "shipped", "accepted"):
                    continue
                first = f"{its[0]['customer']} — {its[0]['model']}" if its[0]["line_kind"] == "pair" else its[0]["item"]
            else:
                its = g.db.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='need' ORDER BY id",
                                   (r["id"],)).fetchall()
                first = its[0]["item"] if its else "—"
            disc = bool(r["status"] == "accepted" and r["discr"])
            part = (not by_sklad) and r["status"] == "open" and any((i["delivered"] or 0) > 0 for i in its)
            rows.append(dict(id=r["id"], first=first, more=max(0, len(its) - 1), urgent=r["urgent"],
                             created_at=r["created_at"], action=False, disc=disc, done=(r["status"] == "accepted"),
                             status=("Пришла часть" if part else names.get(r["status"], r["status"]) +
                                     (" с расхождением" if disc else "")),
                             url=url_for("request_view", req_id=r["id"])))
    else:
        for d in g.db.execute("SELECT * FROM documents WHERE kind='RETURN' AND status!='draft' ORDER BY id DESC").fetchall():
            lines = _load_lines(d["id"])
            sent, recv, diff, has_recv = _doc_totals(d["id"])
            disc = bool(d["status"] == "accepted" and has_recv and diff)
            first = lines[0] if lines else None
            rows.append(dict(id=d["id"], customer=(first["customer"] if first else "—"), model=(first["model"] if first else ""),
                             more=max(0, len(lines) - 1), pairs=sent,
                             brak=sum(l["pairs_sent"] for l in lines if l["status"] == "brak"),
                             created_at=d["created_at"], action=False, disc=disc, done=(d["status"] == "accepted"),
                             status=("Принята с расхождением" if disc else PROD_DOC_STATUS.get(d["status"], d["status"])),
                             url=url_for("doc_view", doc_id=d["id"])))
    work = [x for x in rows if not x["done"]]
    done = [x for x in rows if x["done"]]
    return render_template("prod_requests.html", tab=tab, sub=sub, rows=(done if sub == "done" else work),
                           n_work=len(work), n_done=len(done), n_act_mat=0, n_act_shoe=0)


def _prod_requests():
    """Раздел «Заявки» производства: «Материалы» (заказы складу) и «Отправка готовой обуви» (передачи готовой обуви)."""
    tab = request.args.get("tab")
    sub = "done" if request.args.get("sub") == "done" else "work"

    mats = []
    for r in g.db.execute("SELECT * FROM requests WHERE created_role='proizv' ORDER BY id DESC").fetchall():
        its = g.db.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='need' ORDER BY id",
                           (r["id"],)).fetchall()
        if r["status"] == "draft" and not its:
            continue   # пустая — ещё не начинали
        disc = bool(r["status"] == "accepted" and r["discr"])
        part = r["status"] == "open" and any((i["delivered"] or 0) > 0 for i in its)
        mats.append(dict(
            id=r["id"], first=(its[0]["item"] if its else "—"), more=max(0, len(its) - 1),
            urgent=r["urgent"], created_at=r["created_at"],
            status=("Пришла часть" if part else PROD_REQ_STATUS[r["status"]] + (" с расхождением" if disc else "")),
            action=(r["status"] == "draft"), disc=disc, done=(r["status"] == "accepted"),
            url=url_for("request_view", req_id=r["id"])))

    shoes = []
    for d in g.db.execute("SELECT * FROM documents WHERE kind='RETURN' ORDER BY id DESC").fetchall():
        lines = _load_lines(d["id"])
        if d["status"] == "draft" and not lines:
            continue
        sent, recv, diff, has_recv = _doc_totals(d["id"])
        disc = bool(d["status"] == "accepted" and has_recv and diff)
        first = lines[0] if lines else None
        shoes.append(dict(
            id=d["id"], customer=(first["customer"] if first else "—"), model=(first["model"] if first else ""),
            more=max(0, len(lines) - 1), pairs=sent, brak=sum(l["pairs_sent"] for l in lines if l["status"] == "brak"),
            created_at=d["created_at"],
            status=("Принята с расхождением" if disc else PROD_DOC_STATUS.get(d["status"], d["status"])),
            action=(d["status"] == "draft"), disc=disc, done=(d["status"] == "accepted"),
            url=url_for("doc_view", doc_id=d["id"])))

    def order(rows):   # сначала ждущие вас, потом новые первыми
        return sorted(rows, key=lambda x: (not x["action"], -x["id"]))
    n_act_mat = sum(1 for x in mats if x["action"])
    n_act_shoe = sum(1 for x in shoes if x["action"])
    if tab not in ("mat", "shoes"):
        tab = "mat" if (n_act_mat and not n_act_shoe) else "shoes"   # по умолчанию — обувь; иначе вкладка, где ждёт дело
    rows = mats if tab == "mat" else shoes
    work = order([x for x in rows if not x["done"]])
    done = [x for x in rows if x["done"]]
    return render_template("prod_requests.html", tab=tab, sub=sub, rows=(done if sub == "done" else work),
                           n_work=len(work), n_done=len(done), n_act_mat=n_act_mat, n_act_shoe=n_act_shoe)


def _sklad_requests():
    """Раздел «Заявки» склада: заказы производства + свои передачи без заказа, одним списком."""
    sub = "done" if request.args.get("sub") == "done" else "work"
    rows = []
    for r in g.db.execute("SELECT * FROM requests ORDER BY id").fetchall():
        by_sklad = r["created_role"] == "sklad"
        if by_sklad:
            # передача склада: показываем, только если она без заказов (заказы видны сами по себе)
            if g.db.execute("SELECT 1 FROM requests WHERE transfer_id=? LIMIT 1", (r["id"],)).fetchone():
                continue
            its = g.db.execute("SELECT ri.*, c.name AS customer, m.name AS model FROM request_items ri "
                               "LEFT JOIN customers c ON c.id=ri.customer_id LEFT JOIN models m ON m.id=ri.model_id "
                               "WHERE ri.request_id=? AND ri.line_kind IN ('pair','material') ORDER BY ri.id",
                               (r["id"],)).fetchall()
            if not its or r["status"] not in ("progress", "shipped", "accepted"):
                continue
            first = (f"{its[0]['customer']} — {its[0]['model']}" if its[0]["line_kind"] == "pair" else its[0]["item"])
            url = url_for("request_view", req_id=r["id"], step=2) if r["status"] == "progress" else url_for("request_view", req_id=r["id"])
        else:
            if r["status"] == "draft":
                continue   # производство ещё не отправило
            its = g.db.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='need' ORDER BY id",
                               (r["id"],)).fetchall()
            first = its[0]["item"] if its else "—"
            url = url_for("request_view", req_id=r["id"])
        disc = bool(r["status"] == "accepted" and r["discr"])
        part = (not by_sklad) and r["status"] == "open" and any((i["delivered"] or 0) > 0 for i in its)
        rows.append(dict(
            id=r["id"], first=first, more=max(0, len(its) - 1), urgent=r["urgent"], created_at=r["created_at"],
            status=("Нужно дособрать" if part else SKLAD_REQ_STATUS.get(r["status"], r["status"]) + (" с расхождением" if disc else "")),
            action=(r["status"] in ("open", "progress", "done")), disc=disc, done=(r["status"] == "accepted"),
            url=url))
    work = [x for x in rows if not x["done"]]
    # срочные → нужно собрать → по дате (старые первыми — их собирать раньше)
    work.sort(key=lambda x: (not (x["urgent"] and x["action"]), not x["action"], x["id"]))
    done = sorted([x for x in rows if x["done"]], key=lambda x: -x["id"])
    return render_template("sklad_requests.html", sub=sub, rows=(done if sub == "done" else work),
                           n_work=len(work), n_done=len(done), n_act=sum(1 for x in work if x["action"]))


@app.route("/docs/collect")
@login_required
def docs_collect():
    """Склад: общий список позиций всех заявок — идёшь и отмечаешь, что положил."""
    if session["role"] != "sklad":
        abort(403)
    reqs = g.db.execute(
        "SELECT * FROM requests WHERE status IN ('open','progress','done') AND created_role='proizv' "
        "ORDER BY urgent DESC, id DESC").fetchall()
    groups = []
    for r in reqs:
        its = g.db.execute(
            "SELECT * FROM request_items WHERE request_id=? AND line_kind='need' ORDER BY id",
            (r["id"],)).fetchall()
        groups.append(dict(r=r, its=its))
    if not any(gr["its"] for gr in groups):
        # заявок нет — сразу к передаче, без пустой страницы «Что нужно положить»
        t = _open_transfer()
        g.db.commit()
        return redirect(url_for("request_view", req_id=t, step=2))
    return render_template("docs_collect.html", groups=groups)


@app.route("/docs/collect/save", methods=["POST"])
@login_required
def docs_collect_save():
    """Сохранить отмеченное по общему списку сборки (все заявки сразу)."""
    if session["role"] != "sklad":
        abort(403)
    reqs = g.db.execute(
        "SELECT * FROM requests WHERE status IN ('open','progress','done') AND created_role='proizv'").fetchall()
    t = _open_transfer()
    any_placed = False
    missing = []
    for r in reqs:
        items = g.db.execute(
            "SELECT * FROM request_items WHERE request_id=? AND line_kind='need'",
            (r["id"],)).fetchall()
        for it in items:
            iid = it["id"]
            placed = 1 if request.form.get(f"placed_{iid}") else 0
            raw = (request.form.get(f"col_{iid}", "") or "").strip()
            rest = max(0, (it["qty"] or 0) - (it["delivered"] or 0)) if it["qty"] else None
            if raw == "":
                val = rest if placed else it["collected"]
            else:
                val = _num(raw) or 0
            if placed and not val:
                missing.append(it["item"])
            g.db.execute("UPDATE request_items SET collected=?, placed=? WHERE id=?",
                         (val, placed, iid))
            if placed and val and not t:
                t = _open_transfer(create=True)
            _sync_collected(t, it, placed, val, r["id"])
        # заявка, из которой что-то положили, привязывается к одной общей передаче
        has = g.db.execute("SELECT 1 FROM request_items WHERE request_id=? AND line_kind='need' AND placed=1",
                           (r["id"],)).fetchone()
        if has and t:
            any_placed = True
            g.db.execute("UPDATE requests SET status='progress', taken_at=COALESCE(taken_at,?), transfer_id=? "
                         "WHERE id=?", (db.now_str(), t, r["id"]))
        elif r["transfer_id"] == t:
            g.db.execute("UPDATE requests SET status='open', transfer_id=NULL WHERE id=?", (r["id"],))
    if missing:
        g.db.rollback()
        flash("Впишите, сколько положили: " + ", ".join(missing) + ".")
        return redirect(url_for("docs_collect"))
    g.db.commit()
    if any_placed:
        return redirect(url_for("request_view", req_id=t, step=2))
    flash("Отметьте галочкой, что положили, — тогда откроется следующий шаг.")
    return redirect(url_for("docs_collect"))


def _guess_unit(text):
    t = (text or "").lower()
    if "пар" in t:
        return "пары"
    if "м2" in t or "кв" in t or "метр" in t or t.strip() in ("м", "m"):
        return "м2"
    return "шт"


def _sync_collected(t, it, placed, qty, req_id):
    """Отмеченное на шаге «Сборка» само попадает в передачу (шаг 2) — без повторного ввода."""
    ex = g.db.execute("SELECT id FROM request_items WHERE request_id=? AND from_item=?",
                      (t, it["id"])).fetchone()
    if placed and qty:
        note = f"по заявке №{req_id}" + (f" · {it['note']}" if it["note"] else "")
        if ex:
            g.db.execute("UPDATE request_items SET item=?, qty=?, collected=?, note=?, unit=? WHERE id=?",
                         (it["item"], qty, qty, note, it["unit"] or _guess_unit(it["note"]), ex["id"]))
        else:
            g.db.execute(
                "INSERT INTO request_items (request_id, item, qty, collected, placed, item_type, source, "
                "line_kind, unit, note, from_item) VALUES (?, ?, ?, ?, 1, 'material', 'sklad', 'material', ?, ?, ?)",
                (t, it["item"], qty, qty, it["unit"] or _guess_unit(it["note"]), note, it["id"]))
    elif ex:
        g.db.execute("DELETE FROM request_items WHERE id=?", (ex["id"],))


def _open_transfer(create=False):
    """Текущая несобранная передача склада (одна на всех) — или новая."""
    old = g.db.execute(
        "SELECT id FROM requests WHERE created_role='sklad' AND status='progress' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if old:
        return old["id"]
    if not create:
        return 0
    cur = g.db.execute(
        "INSERT INTO requests (status, urgent, created_role, created_at, taken_at) "
        "VALUES ('progress', 0, 'sklad', ?, ?)", (db.now_str(), db.now_str()))
    return cur.lastrowid


@app.route("/docs/collect/blank", methods=["POST"])
@login_required
def docs_collect_blank():
    """Передача без заявки: пустой документ склада, сразу шаг «что положил»."""
    if session["role"] != "sklad":
        abort(403)
    t = _open_transfer()
    g.db.commit()
    return redirect(url_for("request_view", req_id=t, step=2))


# ---------- Склад производства: «В работе» → «Готово к отправке» / «Брак к отправке» → машина ----------
# Кладовщик производства в течение дня переписывает то, что цех сдал готовым (или браком). В машину
# грузится только переписанное — неготовое отправить нельзя. Остаток на производстве не меняется,
# пока пары не уехали: «В работе» = свободно на производстве − переписано и ещё не уехало.
FIN_KINDS = {"ready": "Готово к отправке", "brak": "Брак к отправке"}


@_cached
def _finish_bal():
    """{(заказчик, модель, партия, ready|brak, причина): пар} — переписано и ещё не уехало."""
    bal = {}
    for r in g.db.execute("SELECT customer_id c, model_id m, lot, kind, COALESCE(note,'') n, SUM(pairs) p "
                          "FROM finish GROUP BY 1, 2, 3, 4, 5"):
        k = (r["c"], r["m"], r["lot"], r["kind"], r["n"] if r["kind"] == "brak" else "")
        bal[k] = bal.get(k, 0) + (r["p"] or 0)
    for r in g.db.execute("SELECT l.customer_id c, l.model_id m, l.lot, l.status, COALESCE(l.note,'') n, "
                          "SUM(l.pairs_sent) p FROM lines l JOIN documents d ON d.id=l.document_id "
                          "WHERE l.src='ready' GROUP BY 1, 2, 3, 4, 5"):
        kind = "brak" if r["status"] == "brak" else "ready"
        k = (r["c"], r["m"], r["lot"], kind, r["n"] if kind == "brak" else "")
        bal[k] = bal.get(k, 0) - (r["p"] or 0)
    return {k: v for k, v in bal.items() if v}


def _names():
    return ({r["id"]: r["name"] for r in g.db.execute("SELECT id, name FROM customers")},
            {r["id"]: r["name"] for r in g.db.execute("SELECT id, name FROM models")})


def _prod_view():
    """Три части склада производства: в работе (по партиям), готово к отправке, брак к отправке."""
    lots, _ = _return_lots()
    fb = _finish_bal()
    cn, mn = _names()
    held = {}
    for (c, m, lot, kind, n), q in fb.items():
        held[(c, m, lot)] = held.get((c, m, lot), 0) + q
    work = []
    for (c, m), ls in lots.items():
        for v in ls:
            q = v["qty"] - held.get((c, m, v["sig"]), 0)
            if q > 1e-9:
                work.append(dict(cid=c, mid=m, lot=v["sig"], ops=v["ops"], qty=q,
                                 customer=cn.get(c, "?"), model=mn.get(m, "?")))
    ready, brak = [], []
    for (c, m, lot, kind, n), q in fb.items():
        if q <= 0:
            continue
        (ready if kind == "ready" else brak).append(dict(cid=c, mid=m, lot=lot, ops=_sig_ops(lot), qty=q, note=n,
                                                         kind=kind, customer=cn.get(c, "?"), model=mn.get(m, "?")))
    key = lambda e: ((e["customer"] or "").lower(), (e["model"] or "").lower(), e["lot"], e.get("note") or "")
    return sorted(work, key=key), sorted(ready, key=key), sorted(brak, key=key)


def _fin_key(e, kind=None):
    return json.dumps([kind or e.get("kind", ""), e["cid"], e["mid"], e.get("note") or "", e["lot"]], ensure_ascii=False)


def _parse_fin_key(k):
    if (k or "").startswith("["):
        kind, c, m, note, lot = json.loads(k)
    else:
        kind, c, m, note, lot = (k or "").split("|", 4)
    cid, mid = int(c), int(m)
    if kind not in FIN_KINDS or not (0 < cid < 2**62 and 0 < mid < 2**62):
        raise ValueError
    if not isinstance(note, str) or not isinstance(lot, str):
        raise ValueError
    return kind, cid, mid, note, lot


@app.template_filter("fin_key")
def _fin_key_filter(e, kind=None):
    return _fin_key(e, kind)


@app.route("/stock/finish", methods=["POST"])
@login_required
def stock_finish():
    """Кладовщик производства принял от цеха: готово или брак (с причиной)."""
    if session["role"] != "proizv":
        abort(403)
    try:
        kind, cid, mid, _, lot = _parse_fin_key(request.form.get("k"))
    except (ValueError, TypeError):
        abort(400)
    back = redirect(url_for("stock", tab="work"))
    pairs = _int_pairs(request.form.get("pairs"))
    note = " ".join((request.form.get("note") or "").split()) or None
    if not pairs:
        flash("Впишите, сколько пар.")
        return back
    if kind == "brak" and not note:
        flash("Укажите причину брака.")
        return back
    work, _, _ = _prod_view()
    w = next((e for e in work if e["cid"] == cid and e["mid"] == mid and e["lot"] == lot), None)
    if not w or pairs > w["qty"] + 1e-9:
        flash(f"В работе столько нет: {_fmt(w['qty']) if w else 0} пар.")
        return back
    g.db.execute("INSERT INTO finish (created_at, customer_id, model_id, lot, kind, pairs, note, role) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, 'proizv')",
                 (db.now_str(), cid, mid, lot, kind, pairs, note if kind == "brak" else None))
    g.db.commit()
    flash(f"{w['customer']} — {w['model']}: {pairs} пар → {'готово к отправке' if kind == 'ready' else 'брак к отправке'}.")
    return back


@app.route("/stock/intake", methods=["GET", "POST"])
@login_required
def stock_intake():
    """«Принять готовую обувь»: таблица всего, что в работе, — по строке вписывают, сколько готово; одна кнопка.
    Брак — отдельной формой (stock_intake_brak), чтобы его не вносили случайно."""
    if session["role"] != "proizv":
        abort(403)
    if request.method == "POST":
        back = redirect(url_for("stock_intake"))
        work, _, _ = _prod_view()
        avail = {(e["cid"], e["mid"], e["lot"]): e for e in work}
        rows, total = [], 0
        requested = {}
        for key in request.form:
            if not key.startswith("k_"):
                continue
            i = key[2:]
            raw = (request.form.get(f"q_{i}") or "").strip()
            if not raw or raw == "0":
                continue
            q = _int_pairs(raw)
            try:
                _, cid, mid, _, lot = _parse_fin_key(request.form.get(key))
            except (ValueError, TypeError):
                abort(400)
            w = avail.get((cid, mid, lot))
            batch_key = (cid, mid, lot)
            requested[batch_key] = requested.get(batch_key, 0) + (q or 0)
            if q is None or not w or requested[batch_key] > w["qty"] + 1e-9:
                flash(f"{w['customer']} — {w['model']}: в работе только {_fmt(w['qty'])} пар." if w and q
                      else "Проверьте строки: только целые числа, не больше, чем в работе.")
                return back
            rows.append((cid, mid, lot, q))
            total += q
        if not total:
            flash("Впишите, сколько пар готово.")
            return back
        now = db.now_str()
        for cid, mid, lot, q in rows:
            g.db.execute("INSERT INTO finish (created_at, customer_id, model_id, lot, kind, pairs, note, role) "
                         "VALUES (?, ?, ?, ?, 'ready', ?, NULL, 'proizv')", (now, cid, mid, lot, q))
        g.db.commit()
        flash(f"Принято готовой обуви: {total} пар.")
        return back
    work, _, _ = _prod_view()
    return render_template("intake.html", work=work)


@app.route("/stock/intake/brak", methods=["GET", "POST"])
@login_required
def stock_intake_brak():
    """«Принять брак»: выбрать в списке (с поиском) обувь из «В работе», вписать пары и причину."""
    if session["role"] != "proizv":
        abort(403)
    work, _, _ = _prod_view()
    if request.method == "GET":
        return render_template("intake_brak.html", work=work)
    back = redirect(url_for("stock_intake_brak"))
    try:
        _, cid, mid, _, lot = _parse_fin_key(request.form.get("k"))
    except (ValueError, TypeError):
        flash("Выберите обувь в списке.")
        return back
    pairs = _int_pairs(request.form.get("pairs"))
    note = " ".join((request.form.get("note") or "").split()) or None
    w = next((e for e in work if e["cid"] == cid and e["mid"] == mid and e["lot"] == lot), None)
    if not pairs:
        flash("Впишите, сколько пар брака.")
        return back
    if not w or pairs > w["qty"] + 1e-9:
        flash(f"В работе столько нет: {_fmt(w['qty']) if w else 0} пар.")
        return back
    if not note:
        flash("Укажите причину брака.")
        return back
    g.db.execute("INSERT INTO finish (created_at, customer_id, model_id, lot, kind, pairs, note, role) "
                 "VALUES (?, ?, ?, ?, 'brak', ?, ?, 'proizv')", (db.now_str(), cid, mid, lot, pairs, note))
    g.db.commit()
    flash(f"Брак записан: {w['customer']} — {w['model']}, {pairs} пар.")
    return back


@app.route("/stock/intake/<int:fid>/undo", methods=["POST"])
@login_required
def stock_intake_undo(fid):
    """Отменить запись «Принять от цеха» (пока эти пары не уехали)."""
    if session["role"] != "proizv":
        abort(403)
    r = g.db.execute("SELECT * FROM finish WHERE id=? AND pairs > 0", (fid,)).fetchone()
    if not r:
        abort(404)
    have = _finish_bal().get((r["customer_id"], r["model_id"], r["lot"], r["kind"],
                              (r["note"] or "") if r["kind"] == "brak" else ""), 0)
    if have < r["pairs"]:
        flash("Эти пары уже погружены — отменить нельзя.")
        return redirect(url_for("stock_intake"))
    g.db.execute("DELETE FROM finish WHERE id=?", (fid,))
    g.db.commit()
    flash("Запись отменена, пары снова в работе.")
    return redirect(url_for("stock_intake"))


@app.route("/stock/unfinish", methods=["POST"])
@login_required
def stock_unfinish():
    """Ошиблись — вернуть пары из «Готово/Брак к отправке» обратно в работу (пока не уехали)."""
    if session["role"] != "proizv":
        abort(403)
    try:
        kind, cid, mid, note, lot = _parse_fin_key(request.form.get("k"))
    except (ValueError, TypeError):
        abort(400)
    back = redirect(url_for("stock", tab="fready" if kind == "ready" else "fbrak"))
    pairs = _int_pairs(request.form.get("pairs"))
    have = _finish_bal().get((cid, mid, lot, kind, note if kind == "brak" else ""), 0)
    if not pairs:
        flash("Впишите, сколько пар вернуть.")
        return back
    if pairs > have:
        flash(f"Столько нет: {_fmt(have)} пар.")
        return back
    g.db.execute("INSERT INTO finish (created_at, customer_id, model_id, lot, kind, pairs, note, role) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, 'proizv')",
                 (db.now_str(), cid, mid, lot, kind, -pairs, note or None))
    g.db.commit()
    flash(f"{pairs} пар возвращено в работу.")
    return back


@app.route("/docs/load", methods=["GET", "POST"])
@login_required
def docs_load():
    """Приехала машина: кладовщик вписывает, сколько каждой позиции погрузил. Уезжает только вписанное."""
    if session["role"] != "proizv":
        abort(403)
    _, ready, brak = _prod_view()
    if request.method == "GET":
        return render_template("load.html", ready=ready, brak=brak)
    have = _finish_bal()
    rows, total = [], 0
    requested = {}
    for i in range(len(request.form)):
        k = request.form.get(f"k_{i}")
        if k is None:
            continue
        raw = (request.form.get(f"q_{i}") or "").strip()
        if not raw or raw == "0":
            continue
        q = _int_pairs(raw)
        try:
            kind, cid, mid, note, lot = _parse_fin_key(k)
        except (ValueError, TypeError):
            abort(400)
        bal = have.get((cid, mid, lot, kind, note if kind == "brak" else ""), 0)
        batch_key = (cid, mid, lot, kind, note if kind == "brak" else "")
        requested[batch_key] = requested.get(batch_key, 0) + (q or 0)
        if q is None or requested[batch_key] > bal:
            flash("Проверьте строки: вписано больше, чем есть, или не целое число.")
            return redirect(url_for("docs_load"))
        rows.append((kind, cid, mid, note, lot, q))
        total += q
    if not total:
        flash("Впишите, сколько пар погрузили.")
        return redirect(url_for("docs_load"))
    now = db.now_str()
    cur = g.db.execute("INSERT INTO documents (kind, status, created_role, created_at, sent_at, note) "
                       "VALUES ('RETURN', 'sent', 'proizv', ?, ?, ?)",
                       (now, now, (request.form.get("note") or "").strip()))
    doc_id = cur.lastrowid
    for kind, cid, mid, note, lot, q in rows:
        g.db.execute("INSERT INTO lines (document_id, customer_id, model_id, status, pairs_sent, note, lot, src) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, 'ready')",
                     (doc_id, cid, mid, "brak" if kind == "brak" else "gotovoe", q, note or None, lot))
    stock_from_doc(g.db, doc_id)
    g.db.commit()
    flash(f"Отправлено на склад: {total} пар.")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/docs/new", methods=["GET", "POST"])
@login_required
def doc_new():
    role = session["role"]
    if role == "proizv":
        return redirect(url_for("docs_load"))   # обувь на склад — только погрузкой переписанного
    # какие типы может создавать эта роль
    creatable = [k for k, v in KINDS.items() if v[1] == role]
    if not creatable:
        abort(403)

    # У роли ровно один тип: склад -> OUT, производство -> RETURN.
    kind = creatable[0]
    if kind == "OUT":
        # на производство отправляют только через сборку передачи
        return redirect(url_for("docs_collect"))
    # Неотправленный документ этой роли продолжаем, а не плодим новые.
    old = g.db.execute(
        "SELECT id FROM documents WHERE status='draft' AND kind=? AND created_role=? "
        "ORDER BY id DESC LIMIT 1", (kind, role)).fetchone()
    if old:
        return redirect(url_for("doc_view", doc_id=old["id"]))
    cur = g.db.execute(
        "INSERT INTO documents (kind, status, created_role, created_at, note) "
        "VALUES (?, 'draft', ?, ?, '')",
        (kind, role, db.now_str()),
    )
    g.db.commit()
    return redirect(url_for("doc_view", doc_id=cur.lastrowid))


@app.route("/docs/<int:doc_id>")
@login_required
def doc_view(doc_id):
    role = session["role"]
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d or d["kind"] not in _visible_kinds(role):
        abort(404)
    lines = _load_lines(doc_id)
    sent, recv, diff, has_recv = _doc_totals(doc_id)
    creator, acceptor = _doc_dir(d["kind"])

    can_edit = (d["status"] == "draft" and role == creator)
    can_send = (d["status"] == "draft" and role == creator and len(lines) > 0)
    can_accept = (d["status"] == "sent" and role == acceptor)
    can_delete = (role == "director") or (d["status"] == "draft" and role == creator)

    customers = g.db.execute(
        "SELECT * FROM customers WHERE archived=0 ORDER BY name").fetchall()
    models = g.db.execute(
        "SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall()

    return render_template(
        "document_view.html", d=d, lines=lines,
        operations=_operations(), ops_by_item=_ops_map("line", [l["id"] for l in lines]), sent=sent, recv=recv,
        diff=diff, has_recv=has_recv, creator=creator, acceptor=acceptor,
        can_edit=can_edit, can_send=can_send, can_accept=can_accept,
        can_delete=can_delete,
        customers=customers, models=models,
        stock_choices=(_return_choices(doc_id) if d["kind"] == "RETURN" and can_edit else []),
    )


@_cached
def _return_lots():
    """Что можно отправить на склад, по партиям: {(заказчик, модель): [партии]}.

    Партия = пары с одинаковыми операциями. Свободно = получено − уже в передачах на склад
    (включая неотправленные). Если на производстве меньше (списания), срезаем со старых партий;
    если больше (добавлено вручную) — появляется партия «без операций».
    """
    chunks = _received_chunks()
    names, bal = {}, {}
    for e in stock_balances():
        if e["type"] == "pair":
            _, c, m = e["key"].split("-")
            bal[(int(c), int(m))] = e["qty"]
            names[(int(c), int(m))] = (e["customer"], e["model"])
    lines = g.db.execute(
        """SELECT l.*, d.status AS d_status, d.sent_at FROM lines l JOIN documents d ON d.id=l.document_id
           WHERE d.kind='RETURN' ORDER BY d.id, l.id""").fetchall()
    draft = {}
    brak_moved = _brak_moved(g.db)
    for l in lines:
        k = (l["customer_id"], l["model_id"])
        pool = chunks.setdefault(k, [])
        if l["lot"] is not None:
            left, need = _take(pool, l["pairs_sent"], l["lot"])
            if need > 0:   # из партии «без операций» (добавлено вручную)
                pool.append(dict(left=-need, ops=[], sig=l["lot"], cost=0))
        else:
            _take(pool, l["pairs_sent"])
        if l["d_status"] == "draft" and l["id"] not in brak_moved:
            draft[k] = draft.get(k, 0) + l["pairs_sent"]
    out = {}
    for k, pool in chunks.items():
        free_model = bal.get(k, 0) - draft.get(k, 0)
        lots = {}
        for ch in pool:
            lots.setdefault(ch["sig"], dict(sig=ch["sig"], ops=ch["ops"], qty=0))["qty"] += ch["left"]
        lots = [v for v in lots.values() if abs(v["qty"]) > 1e-9]
        extra = free_model - sum(v["qty"] for v in lots)
        if extra < 0:                 # списано на производстве — срезаем со старых партий
            for v in lots:
                cut = min(v["qty"], -extra)
                if cut > 0:
                    v["qty"] -= cut
                    extra += cut
        elif extra > 0:               # добавлено вручную — «без операций»
            e = next((v for v in lots if v["sig"] == _sig([])), None)
            if e:
                e["qty"] += extra
            else:
                lots.append(dict(sig=_sig([]), ops=[], qty=extra))
        out[k] = [v for v in lots if v["qty"] > 1e-9]
    for k in bal:
        if k not in out and bal[k] - draft.get(k, 0) > 0:
            out[k] = [dict(sig=_sig([]), ops=[], qty=bal[k] - draft.get(k, 0))]
    return out, names


def _return_available(doc_id=None):
    """Свободно по моделям (для проверки перед отправкой)."""
    lots, _ = _return_lots()
    return {k: sum(v["qty"] for v in ls) for k, ls in lots.items()}


def _return_choices(doc_id):
    lots, names = _return_lots()
    res = []
    for (c, m), ls in lots.items():
        cu, mo = names.get((c, m), ("?", "?"))
        for v in ls:
            res.append(dict(key=f"{c}|{m}|{v['sig']}", customer=cu, model=mo, qty=v["qty"],
                            ops=[o["name"] for o in v["ops"]]))
    return sorted(res, key=lambda x: ((x["customer"] or "").lower(), (x["model"] or "").lower(), -x["qty"]))


def _draft_owner(doc_id):
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        abort(404)
    creator, _ = _doc_dir(d["kind"])
    if d["status"] != "draft" or session.get("role") != creator:
        abort(403)
    return d


@app.route("/docs/<int:doc_id>/line/add", methods=["POST"])
@login_required
def line_add(doc_id):
    """Строка передачи на склад (старые передачи «на производство» больше не создаются)."""
    d = _draft_owner(doc_id)
    if d["kind"] != "RETURN":
        abort(403)
    flash("Обувь на склад отправляется погрузкой переписанного: «Склад» → «Готово к отправке».")
    return redirect(url_for("docs_load"))
    try:
        # выбор партии из остатков производства: «заказчик|модель|операции»
        c, m, lot = request.form["stock_key"].split("|", 2)
        cid, mid = int(c), int(m)
        if not (0 < cid < 2**62 and 0 < mid < 2**62):
            raise ValueError
        pairs = _int_pairs(request.form["pairs"])
        if pairs is None:
            raise ValueError
    except (KeyError, ValueError):
        flash("Выберите обувь и впишите целое число пар.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    if pairs <= 0:
        flash("Число пар должно быть больше нуля.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    allowed = KIND_LINE_STATUSES.get(d["kind"], [])
    status = request.form.get("status") or (allowed[0] if allowed else None)
    if status not in allowed:
        status = allowed[0] if allowed else None
    # на склад — только то, что есть на производстве, и именно из выбранной партии
    lots, _ = _return_lots()
    free = sum(v["qty"] for v in lots.get((cid, mid), []) if v["sig"] == lot)
    if pairs > free:
        flash(f"Столько нет на производстве в этой партии: доступно {_fmt(free)} пар.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    note = (request.form.get("note") or "").strip() or None
    if status == "brak" and not note:
        flash("Укажите причину брака.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    cur = g.db.execute(
        "INSERT INTO lines (document_id, customer_id, model_id, status, pairs_sent, note, lot) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", (doc_id, cid, mid, status, pairs, note, lot))
    if status == "brak":   # брак сразу списывается с остатка производства
        _move(g.db, "writeoff", "pair", -pairs, db.now_str(), cid=cid, mid=mid,
              reason=f"Брак в передачу на склад №{doc_id}", note=note, ref_type="docbrak", ref_id=cur.lastrowid,
              role=session["role"])
    g.db.commit()
    if status == "brak":
        flash(f"Брак добавлен: {pairs} пар — списано с остатка производства.")
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/docs/<int:doc_id>/line/<int:line_id>/del", methods=["POST"])
@login_required
def line_del(doc_id, line_id):
    _draft_owner(doc_id)
    if not g.db.execute("SELECT 1 FROM lines WHERE id=? AND document_id=?", (line_id, doc_id)).fetchone():
        abort(404)   # строка не из этой передачи — ничего не трогаем
    _del_item_ops("line", [line_id])
    g.db.execute("DELETE FROM stock_moves WHERE ref_type='docbrak' AND ref_id=?", (line_id,))   # брак — обратно
    g.db.execute("DELETE FROM lines WHERE id=? AND document_id=?", (line_id, doc_id))
    g.db.commit()
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/docs/<int:doc_id>/send", methods=["POST"])
@login_required
def doc_send(doc_id):
    _draft_owner(doc_id)
    n = g.db.execute("SELECT COUNT(*) c FROM lines WHERE document_id=?",
                     (doc_id,)).fetchone()["c"]
    if n == 0:
        flash("Нельзя отправить пустой документ.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if d["kind"] == "RETURN":
        # повторная проверка: остаток мог измениться, пока передача оформлялась
        lots, _ = _return_lots()
        bal = {}
        for e in stock_balances():
            if e["type"] == "pair":
                _, c, m = e["key"].split("-")
                bal[(int(c), int(m))] = e["qty"]
        need = {}
        moved = _brak_moved(g.db)
        for l in _load_lines(doc_id):
            if l["id"] in moved:
                continue
            need[(l["customer_id"], l["model_id"])] = need.get((l["customer_id"], l["model_id"]), 0) + l["pairs_sent"]
        if any(n - bal.get(k, 0) > 1e-9 for k, n in need.items()):
            flash("На производстве уже нет столько обуви — проверьте строки передачи.")
            return redirect(url_for("doc_view", doc_id=doc_id))
    note = (request.form.get("note") or "").strip()
    g.db.execute("UPDATE documents SET status='sent', sent_at=?, note=? WHERE id=?",
                 (db.now_str(), note, doc_id))
    stock_from_doc(g.db, doc_id)
    g.db.commit()
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/docs/<int:doc_id>/accept", methods=["POST"])
@login_required
def doc_accept(doc_id):
    role = session["role"]
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        abort(404)
    _, acceptor = _doc_dir(d["kind"])
    if d["status"] != "sent" or role != acceptor:
        abort(403)

    lines = _load_lines(doc_id)
    vals = {}
    for l in lines:
        raw = (request.form.get(f"recv_{l['id']}") or "").strip()
        if not raw:
            flash("Пересчитайте и впишите, сколько пришло, по каждой строке.")
            return redirect(url_for("doc_view", doc_id=doc_id))
        v = _int_pairs(raw)
        if v is None:
            flash(f"{l['customer']} — {l['model']}: впишите целое число пар (без дробей и букв).")
            return redirect(url_for("doc_view", doc_id=doc_id))
        if v > l["pairs_sent"]:
            flash(f"Нельзя принять больше, чем отправили: {l['customer']} — {l['model']}, "
                  f"отправлено {l['pairs_sent']} пар.")
            return redirect(url_for("doc_view", doc_id=doc_id))
        vals[l["id"]] = v
    # статус меняем условием — повторное нажатие (или второе устройство) ничего не задвоит
    cur = g.db.execute("UPDATE documents SET status='accepted', accepted_at=? WHERE id=? AND status='sent'",
                       (db.now_str(), doc_id))
    if cur.rowcount != 1:
        g.db.rollback()
        flash("Эта передача уже принята.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    for l in lines:
        note = (request.form.get(f"note_{l['id']}", "") or "").strip()
        g.db.execute("UPDATE lines SET pairs_recv=?, discrepancy_note=? WHERE id=?", (vals[l["id"]], note, l["id"]))
    if d["kind"] != "RETURN":   # передача на склад списана с производства ещё при отправке
        stock_from_doc(g.db, doc_id)
    wh_from_doc(g.db, doc_id)
    g.db.commit()
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/docs/<int:doc_id>/delete", methods=["POST"])
@login_required
def doc_delete(doc_id):
    role = session["role"]
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        abort(404)
    creator, _ = _doc_dir(d["kind"])
    # Черновик удаляет создатель или директор; отправленный — только директор; принятый — никто
    if d["status"] == "accepted":
        flash("Принятую передачу удалить нельзя — остатки и долг уже посчитаны. Исправьте через инвентаризацию.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    if d["status"] != "draft":
        flash("Отправленную передачу нельзя удалить. Примите фактическое количество и разберите расхождение.")
        return redirect(url_for("doc_view", doc_id=doc_id))
    allowed = role == "director" or role == creator
    if not allowed:
        abort(403)
    _del_item_ops("line", [x["id"] for x in g.db.execute(
        "SELECT id FROM lines WHERE document_id=?", (doc_id,))])
    g.db.execute("DELETE FROM stock_moves WHERE ref_type='doc' AND ref_id=?", (doc_id,))
    g.db.execute("DELETE FROM stock_moves WHERE ref_type='docbrak' AND ref_id IN (SELECT id FROM lines WHERE document_id=?)",
                 (doc_id,))
    g.db.execute("DELETE FROM wh_moves WHERE ref_id=? AND kind='in'", (doc_id,))
    g.db.execute("DELETE FROM lines WHERE document_id=?", (doc_id,))
    g.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    g.db.commit()
    flash(f"Документ №{doc_id} удалён.")
    return redirect(url_for("documents"))


@app.route("/refs")
@login_required
def refs():
    tab = request.args.get("tab", "customers")
    if tab not in ("customers", "models", "materials", "operations"):
        tab = "customers"
    if tab == "operations" and session.get("role") != "director":
        tab = "customers"
    show_arch = False

    def load(table):
        q = f"SELECT * FROM {table}"
        if not show_arch:
            q += " WHERE archived=0"
        q += " ORDER BY " + ("number+0, number" if table == "workers" else "name")
        return g.db.execute(q).fetchall()

    return render_template(
        "refs.html", tab=tab, show_arch=show_arch,
        customers=load("customers"), models=load("models"),
        workers=load("workers"), materials=load("materials"), units=MAT_UNITS, can_edit=can_edit_refs(tab),
        operations=_operations(),
    )


def _ref_guard(table):
    if not can_edit_refs(table):
        abort(403)
    if table not in ("customers", "models", "workers", "materials"):
        abort(404)


@app.route("/refs/<table>/add", methods=["POST"])
@login_required
def ref_add(table):
    _ref_guard(table)
    proposed = request.form.get("number" if table == "workers" else "name", "")
    key = "number" if table == "workers" else "name"
    rid_value = locals().get("rid", -1)
    if table == "workers" and proposed and g.db.execute(f"SELECT 1 FROM {table} WHERE jail_norm({key})=jail_norm(?) AND id<>?", (proposed, rid_value)).fetchone():
        flash("Такая запись уже существует. Выберите другое название или номер.")
        return redirect(url_for("refs", tab=table))
    if table == "workers":
        number = (request.form.get("number") or "").strip()
        name = (request.form.get("name") or "").strip()
        if number and name:
            g.db.execute("INSERT INTO workers (number, name) VALUES (?, ?)",
                         (number, name))
    elif table == "materials":
        name = " ".join((request.form.get("name") or "").split())
        unit = request.form.get("unit")
        if name and _find_ref("materials", name):
            flash(f"«{name}» уже есть в справочнике.")
        elif name and unit in MAT_UNITS:
            g.db.execute("INSERT INTO materials (name, unit) VALUES (?, ?)", (name, unit))
    else:
        name = (request.form.get("name") or "").strip()
        if name and _find_ref(table, name):
            flash(f"«{name}» уже есть в справочнике.")
        elif name:
            g.db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    g.db.commit()
    return redirect(url_for("refs", tab=table))


def _norm(s):
    return " ".join((s or "").split()).lower()


def _material_from_form(name_field="item", unit_field="unit"):
    """Материал из справочника: одно название — одна единица. Новый — создаётся с выбранной единицей.
    Возвращает (название, единица) или (None, текст ошибки)."""
    name = " ".join((request.form.get(name_field) or "").split())
    if not name:
        return None, "Впишите материал."
    row = _find_ref("materials", name)
    if row:
        m = g.db.execute("SELECT name, unit FROM materials WHERE id=?", (row["id"],)).fetchone()
        return m["name"], m["unit"]
    unit = request.form.get(unit_field)
    if unit not in MAT_UNITS:
        return None, "Выберите единицу измерения: " + ", ".join(MAT_UNITS) + "."
    g.db.execute("INSERT INTO materials (name, unit) VALUES (?, ?)", (name, unit))
    return name, unit


def _materials_catalog():
    return g.db.execute("SELECT name, unit FROM materials WHERE archived=0 ORDER BY name").fetchall()


def _ref_from_form(table, field):
    """Заказчик/модель вписаны руками: находим по названию (без учёта регистра) или создаём.
    Поддерживает и старое поле с id (<field>_id)."""
    name = " ".join((request.form.get(field + "_name") or "").split())
    if not name:
        return int(request.form[field + "_id"])
    row = _find_ref(table, name)
    if row:
        return row["id"]
    return g.db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,)).lastrowid


def _find_ref(table, name):
    """Есть ли уже такая запись (без учёта регистра и лишних пробелов, по-русски тоже).
    Запись из архива возвращается из архива, а не создаётся заново."""
    for r in g.db.execute(f"SELECT id, name, archived FROM {table} ORDER BY archived"):
        if _norm(r["name"]) == _norm(name):
            if r["archived"]:
                g.db.execute(f"UPDATE {table} SET archived=0 WHERE id=?", (r["id"],))
            return r
    return None


@app.route("/refs/<table>/quick_add", methods=["POST"])
@login_required
def ref_quick_add(table):
    """«+ Создать» прямо из поля ввода: заказчик, модель (склад/директор), материал (+ производство)."""
    role = session.get("role")
    if table not in ("customers", "models", "materials"):
        return {"ok": False, "error": "неизвестный справочник"}, 404
    if role not in ("sklad", "director") and not (table == "materials" and role == "proizv"):
        return {"ok": False, "error": "нет прав"}, 403
    name = " ".join((request.form.get("name") or "").split())
    if not name:
        return {"ok": False, "error": "Введите название", "field": "name"}, 400
    row = _find_ref(table, name)
    if row:
        extra = {}
        if table == "materials":
            extra["unit"] = g.db.execute("SELECT unit FROM materials WHERE id=?", (row["id"],)).fetchone()["unit"]
        g.db.commit()
        return dict(ok=True, id=row["id"], name=row["name"], existed=True, **extra)
    if table == "materials":
        unit = request.form.get("unit")
        if unit not in MAT_UNITS:
            return {"ok": False, "error": "Выберите единицу измерения", "field": "unit"}, 400
        cur = g.db.execute("INSERT INTO materials (name, unit) VALUES (?, ?)", (name, unit))
        g.db.commit()
        return {"ok": True, "id": cur.lastrowid, "name": name, "unit": unit, "existed": False}
    cur = g.db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    g.db.commit()
    return {"ok": True, "id": cur.lastrowid, "name": name, "existed": False}


@app.route("/refs/<table>/<int:rid>/edit", methods=["POST"])
@login_required
def ref_edit(table, rid):
    _ref_guard(table)
    proposed = request.form.get("number" if table == "workers" else "name", "")
    key = "number" if table == "workers" else "name"
    rid_value = locals().get("rid", -1)
    if proposed and g.db.execute(f"SELECT 1 FROM {table} WHERE jail_norm({key})=jail_norm(?) AND id<>?", (proposed, rid_value)).fetchone():
        flash("Такая запись уже существует. Выберите другое название или номер.")
        return redirect(url_for("refs", tab=table))
    if table == "workers":
        number = (request.form.get("number") or "").strip()
        name = (request.form.get("name") or "").strip()
        if number and name:
            g.db.execute("UPDATE workers SET number=?, name=? WHERE id=?",
                         (number, name, rid))
    elif table == "materials":
        name = " ".join((request.form.get("name") or "").split())
        unit = request.form.get("unit")
        old = g.db.execute("SELECT * FROM materials WHERE id=?", (rid,)).fetchone()
        if old and name and unit in MAT_UNITS and (name != old["name"] or unit != old["unit"]):
            dup = any(_mat_key(x["name"]) == _mat_key(name) for x in
                      g.db.execute("SELECT name FROM materials WHERE id<>?", (rid,)))   # lower() в SQLite не знает кириллицу
            if dup:
                flash(f"Материал «{name}» уже есть в справочнике.")
                return redirect(url_for("refs", tab=table))
            g.db.execute("UPDATE materials SET name=?, unit=? WHERE id=?", (name, unit, rid))
            # остатки и история привязаны к названию — переименовываем везде, чтобы остаток не «потерялся»
            o, ou = old["name"], old["unit"]
            g.db.execute("UPDATE stock_moves SET name=?, unit=? WHERE item_type='material' AND name=? "
                         "AND COALESCE(unit,'')=COALESCE(?,'')", (name, unit, o, ou))
            for (nm,) in g.db.execute("SELECT DISTINCT name FROM mat_moves").fetchall():
                if _mat_key(nm) == _mat_key(o):
                    g.db.execute("UPDATE mat_moves SET name=?, unit=? WHERE name=?", (name, unit, nm))
            g.db.execute("UPDATE request_items SET item=?, unit=? WHERE item=? AND COALESCE(unit,'')=COALESCE(?,'') "
                         "AND (line_kind='material' OR item_type='material')", (name, unit, o, ou))
    else:
        name = (request.form.get("name") or "").strip()
        if name:
            g.db.execute(f"UPDATE {table} SET name=? WHERE id=?", (name, rid))
    g.db.commit()
    return redirect(url_for("refs", tab=table))


@app.route("/refs/<table>/<int:rid>/arch", methods=["POST"])
@login_required
def ref_arch(table, rid):
    _ref_guard(table)
    val = 0 if request.form.get("restore") else 1
    g.db.execute(f"UPDATE {table} SET archived=? WHERE id=?", (val, rid))
    g.db.commit()
    return redirect(url_for("refs", tab=table, arch=request.args.get("arch")))


@app.route("/refs/operations/save", methods=["POST"])
@login_required
def ops_save():
    """Директор меняет названия и цены операций. Уже отправленные пары — по старой цене."""
    if session.get("role") != "director":
        abort(403)
    for o in _operations():
        name = (request.form.get(f"name_{o['id']}") or "").strip() or o["name"]
        raw = (request.form.get(f"price_{o['id']}") or "").replace(",", ".").replace(" ", "")
        try:
            price = max(0.0, float(raw))
            if not math.isfinite(price) or price > MAX_QTY:
                price = o["price"]
        except ValueError:
            price = o["price"]
        g.db.execute("UPDATE operations SET name=?, price=? WHERE id=?", (name, price, o["id"]))
    g.db.commit()
    flash("Операции сохранены. Новые цены действуют для следующих передач.")
    return redirect(url_for("refs", tab="operations"))


# ---------- Расхождения ----------
# ---------- Расхождения: разбор двумя сторонами ----------
# Итог по каждой строке: не догрузили (осталось у отправителя), нашлось у получателя, потеряно.
# Обычно вся недостача строки — один итог (одна кнопка); при необходимости — «Разделить по количеству».
# Любая сторона предлагает, другая соглашается; не согласна — спор сразу уходит директору.
DISC_PARTS = [("left", "Не догрузили"), ("found", "Нашлось"), ("lost", "Потеряно")]
DISC_STATE = {"open": "Ждёт разбора", "proposed": "Ждёт согласия", "dispute": "У директора", "closed": "Закрыто"}
SIDE_NAME = {"sklad": "склад", "proizv": "производство", "director": "директор"}


def _disc_sides(ref_type, kind=None):
    """(отправитель, получатель)"""
    if ref_type == "doc" and kind == "RETURN":
        return "proizv", "sklad"
    return "sklad", "proizv"


def _disc_lines(ref_type, ref_id):
    """Строки с недостачей: id, заказчик/модель или материал, единица, отправлено, принято, недостача."""
    out = []
    if ref_type == "req":
        for i in _placed_items(ref_id):
            if i["recv"] is None or i["collected"] is None or i["recv"] >= i["collected"] - 1e-9:
                continue
            pair = i["line_kind"] == "pair"
            out.append(dict(id=i["id"], title=(f"{i['customer']} — {i['model']}" if pair else i["item"]),
                            customer=(i["customer"] if pair else None), model=(i["model"] if pair else None),
                            unit=("пар" if pair else (i["unit"] or "")),
                            sent=i["collected"], got=i["recv"], short=round(i["collected"] - i["recv"], 3)))
    else:
        for l in _load_lines(ref_id):
            if l["pairs_recv"] is None or l["pairs_recv"] >= l["pairs_sent"]:
                continue
            out.append(dict(id=l["id"], title=f"{l['customer']} — {l['model']}", customer=l["customer"], model=l["model"],
                            unit="пар", sent=l["pairs_sent"], got=l["pairs_recv"], short=l["pairs_sent"] - l["pairs_recv"],
                            note=l["discrepancy_note"]))
    return out


def _disc_case_row(ref_type, ref_id):
    return g.db.execute("SELECT * FROM discr_case WHERE ref_type=? AND ref_id=?", (ref_type, ref_id)).fetchone()


def _json_or_empty(v):
    try:
        return json.loads(v) if v else {}
    except ValueError:
        return {}


@_cached
def _disc_cases(where="all"):
    """Все передачи, принятые с недостачей, с состоянием разбора. where: prod / sklad / all."""
    legacy = {(r["ref_type"], r["ref_id"]): r for r in g.db.execute("SELECT * FROM discr_close")}
    cases = {(r["ref_type"], r["ref_id"]): r for r in g.db.execute("SELECT * FROM discr_case")}
    res = []
    refs = [("req", r) for r in g.db.execute("SELECT * FROM requests WHERE status='accepted' AND transfer_id IS NULL "
                                             "ORDER BY id DESC").fetchall()]
    refs += [("doc", d) for d in g.db.execute("SELECT * FROM documents WHERE status='accepted' ORDER BY id DESC").fetchall()]
    for t, r in refs:
        kind = r["kind"] if t == "doc" else None
        if where == "prod" and kind == "RETURN":
            continue
        if where == "sklad" and kind != "RETURN":
            continue
        c = cases.get((t, r["id"]))
        lines = _disc_lines(t, r["id"])
        if not lines and not c:
            continue
        sender, receiver = _disc_sides(t, kind)
        state = c["status"] if c else ("closed" if (t, r["id"]) in legacy else "open")
        if state == "returned":
            state = "dispute"
        res.append(dict(t=t, id=r["id"], at=r["accepted_at"], sender=sender, receiver=receiver,
                        direction=("Производство → Склад" if kind == "RETURN" else "Склад → Производство"),
                        lines=lines, case=c, state=state,
                        prop=_json_or_empty(c["proposal"] if c else None),
                        counter=_json_or_empty(c["counter"] if c and "counter" in c.keys() else None),
                        legacy=legacy.get((t, r["id"]))))
    res.sort(key=lambda x: _sort_key(x["at"]), reverse=True)
    return res


def _disc_needs(x, role):
    """Ждёт ли разбор действия этой роли."""
    if role == "director":
        return x["state"] == "dispute"
    if role not in (x["sender"], x["receiver"]):
        return False
    if x["state"] == "open":
        return True
    if x["state"] == "proposed":
        return x["case"]["proposed_by"] != role
    return False


def _disc_tab(x, role):
    """Вкладка страницы «Расхождения» для этой роли."""
    if x["state"] == "closed":
        return "closed"
    if x["state"] == "dispute":
        return "dir"
    if role == "director":
        return "work"
    return "mine" if _disc_needs(x, role) else "wait"


def _open_discr_count(role):
    if role == "director":
        return g.db.execute("SELECT COUNT(*) FROM discr_case WHERE status IN ('dispute','returned')").fetchone()[0]
    if role not in ("sklad", "proizv"):
        return 0
    return g.db.execute("""WITH shortages AS (
        SELECT 'req' t, r.id FROM requests r JOIN request_items i ON i.request_id=r.id
        WHERE r.status='accepted' AND r.transfer_id IS NULL AND i.line_kind IN ('pair','material')
          AND i.recv IS NOT NULL AND i.collected>i.recv+0.000000001 GROUP BY r.id
        UNION
        SELECT 'doc', d.id FROM documents d JOIN lines l ON l.document_id=d.id
        WHERE d.status='accepted' AND l.pairs_recv IS NOT NULL AND l.pairs_sent>l.pairs_recv GROUP BY d.id
    ) SELECT COUNT(*) FROM shortages s
    LEFT JOIN discr_case c ON c.ref_type=s.t AND c.ref_id=s.id
    LEFT JOIN discr_close old ON old.ref_type=s.t AND old.ref_id=s.id
    WHERE (c.id IS NULL AND old.id IS NULL) OR (c.status='proposed' AND c.proposed_by<>?)""", (role,)).fetchone()[0]


def _disc_ref(ref_type, ref_id):
    if ref_type == "req":
        r = g.db.execute("SELECT * FROM requests WHERE id=? AND status='accepted'", (ref_id,)).fetchone()
        return r, None
    r = g.db.execute("SELECT * FROM documents WHERE id=? AND status='accepted'", (ref_id,)).fetchone()
    return r, (r["kind"] if r else None)


def _disc_read(form, lines, optional=False):
    """Итоги из формы: по строке либо одна кнопка (pick_<id>) — вся недостача туда,
    либо «Разделить по количеству» (split_<id>=1 и поля left_/found_/lost_).
    Возвращает (итоги, ошибка). optional: если ничего не выбрано — (None, None)."""
    keys = [k for k, _ in DISC_PARTS]
    prop, missing = {}, []
    for ln in lines:
        lid = ln["id"]
        if form.get(f"split_{lid}") == "1":
            vals = {k: (_recv_value(form.get(f"{k}_{lid}")) or 0) for k in keys}
            if abs(sum(vals.values()) - ln["short"]) > 1e-6:
                return None, f"{ln['title']}: разделите всю недостачу ({_fmt(ln['short'])} {ln['unit']})."
            if ln["unit"] == "пар" and any(abs(v - round(v)) > 1e-9 for v in vals.values()):
                return None, f"{ln['title']}: пары — только целым числом."
        else:
            pick = form.get(f"pick_{lid}")
            if pick not in keys:
                missing.append(ln["title"])
                continue
            vals = {k: (ln["short"] if k == pick else 0) for k in keys}
        # снимок строки — чтобы закрытое расхождение показывалось и после исправления остатков
        vals.update(_t=ln["title"], _c=ln.get("customer"), _m=ln.get("model"), _u=ln["unit"],
                    _sent=ln["sent"], _got=ln["got"], _short=ln["short"])
        prop[str(lid)] = vals
    if optional and not prop:
        return None, None
    if missing:
        return None, "Выберите итог: " + ", ".join(missing) + "."
    return prop, None


def _disc_back():
    b = request.form.get("back") or ""
    return b if b.startswith("/") else url_for("discrepancies")


def _disc_close(ref_type, r, kind, c, prop, by, note=None):
    now = db.now_str()
    if c:
        g.db.execute("UPDATE discr_case SET status='closed', proposal=?, note=COALESCE(?, note), closed_at=?, closed_by=?, "
                     "updated_at=? WHERE id=?", (json.dumps(prop), note, now, by, now, c["id"]))
    else:
        g.db.execute("INSERT INTO discr_case (ref_type, ref_id, status, proposed_by, proposal, note, created_at, updated_at, "
                     "closed_at, closed_by) VALUES (?, ?, 'closed', ?, ?, ?, ?, ?, ?, ?)",
                     (ref_type, r["id"], by, json.dumps(prop), note, now, now, now, by))
    lines = {ln["id"] for ln in _disc_lines(ref_type, r["id"])}
    for sid, v in prop.items():
        if int(sid) in lines:
            _disc_apply(ref_type, r, kind, int(sid), v.get("found") or 0, v.get("left") or 0, now)


@app.route("/discrepancies/<ref_type>/<int:ref_id>/propose", methods=["POST"])
@login_required
def disc_propose(ref_type, ref_id):
    """Сторона выбирает итог по каждой строке и отправляет на согласие другой стороне."""
    role = session["role"]
    back = _disc_back()
    if ref_type not in ("req", "doc"):
        abort(404)
    r, kind = _disc_ref(ref_type, ref_id)
    if not r:
        abort(404)
    sender, receiver = _disc_sides(ref_type, kind)
    if role not in (sender, receiver):
        abort(403)
    c = _disc_case_row(ref_type, ref_id)
    if c and (c["status"] in ("closed", "dispute", "returned") or (c["status"] == "proposed" and c["proposed_by"] != role)):
        flash("Это расхождение уже ждёт вашего ответа, у директора или закрыто.")
        return redirect(back)
    prop, err = _disc_read(request.form, _disc_lines(ref_type, ref_id))
    if err:
        flash(err)
        return redirect(back)
    note = (request.form.get("note") or "").strip() or None
    now = db.now_str()
    if c:
        g.db.execute("UPDATE discr_case SET status='proposed', proposed_by=?, proposal=?, note=?, comment=NULL, "
                     "counter=NULL, counter_by=NULL, updated_at=? WHERE id=?", (role, json.dumps(prop), note, now, c["id"]))
    else:
        g.db.execute("INSERT INTO discr_case (ref_type, ref_id, status, proposed_by, proposal, note, created_at, updated_at) "
                     "VALUES (?, ?, 'proposed', ?, ?, ?, ?, ?)", (ref_type, ref_id, role, json.dumps(prop), note, now, now))
    g.db.commit()
    flash("Отправлено на согласие " + ("производству." if role == "sklad" else "складу."))
    return redirect(back)


@app.route("/discrepancies/<ref_type>/<int:ref_id>/answer", methods=["POST"])
@login_required
def disc_answer(ref_type, ref_id):
    """Вторая сторона: «Согласен» — итоги проводятся по остаткам; «Не согласен» — спор сразу у директора."""
    role = session["role"]
    back = _disc_back()
    r, kind = _disc_ref(ref_type, ref_id) if ref_type in ("req", "doc") else (None, None)
    c = _disc_case_row(ref_type, ref_id)
    if not r or not c or c["status"] != "proposed":
        abort(404)
    sender, receiver = _disc_sides(ref_type, kind)
    if role not in (sender, receiver) or role == c["proposed_by"]:
        abort(403)
    now = db.now_str()
    if request.form.get("action") == "disagree":
        comment = (request.form.get("comment") or "").strip()
        if not comment:
            flash("Напишите, с чем не согласны.")
            return redirect(back)
        counter, err = _disc_read(request.form, _disc_lines(ref_type, ref_id), optional=True)
        if err:
            flash(err)
            return redirect(back)
        g.db.execute("UPDATE discr_case SET status='dispute', comment=?, counter=?, counter_by=?, updated_at=? WHERE id=?",
                     (comment, json.dumps(counter) if counter else None, role if counter else None, now, c["id"]))
        g.db.commit()
        flash("Спор передан директору.")
        return redirect(back)
    _disc_close(ref_type, r, kind, c, json.loads(c["proposal"] or "{}"), role)
    g.db.commit()
    flash("Расхождение закрыто, остатки обновлены.")
    return redirect(back)


@app.route("/discrepancies/<ref_type>/<int:ref_id>/decide", methods=["POST"])
@login_required
def disc_decide(ref_type, ref_id):
    """Решение директора: принять вариант одной из сторон или расписать своё — и закрыть."""
    if session["role"] != "director":
        abort(403)
    back = _disc_back()
    r, kind = _disc_ref(ref_type, ref_id) if ref_type in ("req", "doc") else (None, None)
    if not r:
        abort(404)
    c = _disc_case_row(ref_type, ref_id)
    if c and c["status"] == "closed":
        return redirect(back)
    which = request.form.get("which")
    if which in ("proposal", "counter") and c and c[which]:
        prop = json.loads(c[which])
        who = c["proposed_by"] if which == "proposal" else c["counter_by"]
        note = "Решение директора: вариант " + ("склада" if who == "sklad" else "производства")
    else:
        prop, err = _disc_read(request.form, _disc_lines(ref_type, ref_id))
        if err:
            flash(err)
            return redirect(back)
        note = ((request.form.get("note") or "").strip() + " · Решение директора").strip(" ·")
    _disc_close(ref_type, r, kind, c, prop, "director", note)
    g.db.commit()
    flash("Расхождение закрыто решением директора.")
    return redirect(back)


def _disc_apply(ref_type, r, kind, line_id, found, left, now):
    """Применить итоги к остаткам. Нашлось — получателю (и считается принятым); не догрузили — отправителю
    (считается неотправленным); потеряно — ничего не меняет (за потерянное не платим)."""
    ref = f"Расхождение №{r['id']}"
    if ref_type == "req":   # склад → производство
        i = g.db.execute("SELECT * FROM request_items WHERE id=?", (line_id,)).fetchone()
        if found:
            g.db.execute("UPDATE request_items SET recv=recv+? WHERE id=?", (found, line_id))
            if i["from_item"]:
                _refresh_need(i["from_item"])
            if i["line_kind"] == "pair":
                _move(g.db, "in", "pair", found, now, cid=i["customer_id"], mid=i["model_id"],
                      reason=f"Нашлось · {ref}", ref_type="disc", ref_id=r["id"], role=session["role"])
            else:
                _move(g.db, "in", "material", found, now, name=i["item"], unit=i["unit"],
                      reason=f"Нашлось · {ref}", ref_type="disc", ref_id=r["id"], role=session["role"])
        if left:
            g.db.execute("UPDATE request_items SET collected=collected-? WHERE id=?", (left, line_id))
            if i["line_kind"] == "material":   # материал остался на складе
                _mat_move("in", i["item"], i["unit"], left, f"Не догрузили · {ref}", ref_id=r["id"])
            if i["status"] == "repair":   # брак со склада в ремонт — остался на складе
                g.db.execute("INSERT INTO wh_moves (created_at, kind, customer_id, model_id, quality, qty, reason, ref_id, role) "
                             "VALUES (?, 'repair', ?, ?, 'brak', ?, ?, ?, ?)",
                             (now, i["customer_id"], i["model_id"], left, f"Не догрузили · {ref}", r["id"], session["role"]))
    else:   # передача на склад (или старая «на производство»)
        l = g.db.execute("SELECT * FROM lines WHERE id=?", (line_id,)).fetchone()
        if found:
            g.db.execute("UPDATE lines SET pairs_recv=pairs_recv+? WHERE id=?", (found, line_id))
            if kind == "RETURN":
                g.db.execute("INSERT INTO wh_moves (created_at, kind, customer_id, model_id, quality, qty, reason, ref_id, role) "
                             "VALUES (?, 'in', ?, ?, ?, ?, ?, ?, ?)",
                             (now, l["customer_id"], l["model_id"], "brak" if l["status"] == "brak" else "ready", found,
                              f"Нашлось · {ref}", r["id"], session["role"]))
            else:
                _move(g.db, "in", "pair", found, now, cid=l["customer_id"], mid=l["model_id"],
                      reason=f"Нашлось · {ref}", ref_type="disc", ref_id=r["id"], role=session["role"])
        if left:
            g.db.execute("UPDATE lines SET pairs_sent=pairs_sent-? WHERE id=?", (left, line_id))
            if kind == "RETURN":   # обувь осталась на производстве
                _move(g.db, "in", "pair", left, now, cid=l["customer_id"], mid=l["model_id"],
                      reason=f"Не догрузили · {ref}", ref_type="disc", ref_id=r["id"], role=session["role"])


@app.route("/discrepancies")
@login_required
def discrepancies():
    """Вкладки по тому, чей шаг: у сторон — «Ждут вашего ответа» / «Ждут другой стороны» / «У директора» / «Закрытые»;
    у директора — «У директора» / «Идёт разбор» / «Закрытые»."""
    role = session["role"]
    cases = _disc_cases("all")
    if role == "director":
        tabs = [("dir", "У директора"), ("work", "Идёт разбор"), ("closed", "Закрытые")]
    else:
        tabs = [("mine", "Ждут вашего ответа"), ("wait", "Ждут другой стороны"), ("dir", "У директора"),
                ("closed", "Закрытые")]
    groups = {k: [] for k, _ in tabs}
    for x in cases:
        k = _disc_tab(x, role)
        if k in groups:
            groups[k].append(x)
    tab = request.args.get("tab")
    if tab not in groups:
        tab = next((k for k, _ in tabs[:2] if groups[k]), tabs[0][0])
    return render_template("discrepancies.html", tabs=tabs, groups=groups, tab=tab, items=groups[tab],
                           parts=DISC_PARTS, states=DISC_STATE, needs=_disc_needs, side_name=SIDE_NAME)


# ---------- Статистика ----------
STAT_PERIODS = {"7": "7 дней", "30": "30 дней", "90": "90 дней", "all": "Всё время"}


def _dt(sv):
    d = _sort_key(sv)
    return None if d in (db.datetime.min, db.datetime.max) else d


def _pct(a, b):
    return round(a * 100.0 / b, 1) if b else 0.0


def _delta(cur, prev):
    """Изменение к прошлому периоду в %, None если сравнивать не с чем."""
    if prev is None:
        return None
    if not prev:
        return None   # в прошлом периоде ничего не было — сравнивать не с чем
    return round((cur - prev) * 100.0 / prev, 1)


@app.route("/stats")
@login_required
def stats():
    """Статистика: выпуск, брак, деньги (деньги — только директору)."""
    role = session["role"]
    if role != "director":
        abort(403)
    period = request.args.get("p", "30")
    if period not in STAT_PERIODS:
        period = "30"
    tab = request.args.get("tab", "out")
    if tab == "money" and role != "director":
        tab = "out"
    group = request.args.get("g", "day")          # day / week

    lines = []
    for l in g.db.execute(
            """SELECT l.*, c.name customer, m.name model, d.accepted_at FROM lines l
               JOIN documents d ON d.id=l.document_id
               JOIN customers c ON c.id=l.customer_id JOIN models m ON m.id=l.model_id
               WHERE d.kind='RETURN' AND d.status='accepted' AND l.pairs_recv > 0"""):
        t = _dt(l["accepted_at"])
        if t:
            lines.append(dict(t=t, day=t.date(), brak=l["status"] == "brak", n=l["pairs_recv"],
                              customer=l["customer"], model=l["model"]))
    today = db.datetime.now(db.TZ).date()
    if period == "all":
        first = min([x["day"] for x in lines] or [today])
        days = (today - first).days + 1
        start = first
        prev_start = None
    else:
        days = int(period)
        start = today - db.timedelta(days=days - 1)
        prev_start = start - db.timedelta(days=days)
    cur = [x for x in lines if x["day"] >= start]
    prev = [x for x in lines if prev_start and prev_start <= x["day"] < start]

    def sums(rows):
        r = sum(x["n"] for x in rows if not x["brak"])
        b_ = sum(x["n"] for x in rows if x["brak"])
        return r, b_
    ready, brak = sums(cur)
    p_ready, p_brak = sums(prev) if prev_start else (None, None)

    # ---- выпуск: по дням или неделям (понедельник — начало недели)
    def bucket(d):
        return d if group == "day" else d - db.timedelta(days=d.weekday())
    series = {}
    d = bucket(start)
    while d <= today:
        series[d] = dict(ready=0, brak=0)
        d += db.timedelta(days=1 if group == "day" else 7)
    for x in cur:
        e = series.setdefault(bucket(x["day"]), dict(ready=0, brak=0))
        e["brak" if x["brak"] else "ready"] += x["n"]
    out_bars = [dict(label=(k.strftime("%d.%m") if group == "day" else "с " + k.strftime("%d.%m")),
                     value=v["ready"], brak=v["brak"],
                     rate=_pct(v["brak"], v["ready"] + v["brak"])) for k, v in sorted(series.items())]

    # ---- брак по моделям и заказчикам
    def by(key):
        agg = {}
        for x in cur:
            e = agg.setdefault(x[key] if key != "cm" else (x["customer"], x["model"]),
                               dict(customer=x["customer"], model=x["model"], ready=0, brak=0))
            e["brak" if x["brak"] else "ready"] += x["n"]
        rows = list(agg.values())
        for e in rows:
            e["rate"] = _pct(e["brak"], e["ready"] + e["brak"])
        return sorted(rows, key=lambda e: (-e["rate"], -(e["ready"] + e["brak"])))
    rate = _pct(brak, ready + brak)
    p_rate = _pct(p_brak, p_ready + p_brak) if prev_start else None

    ctx = dict(period=period, periods=STAT_PERIODS, tab=tab, group=group, days=days,
               ready=ready, brak=brak, avg=round(ready / days, 1) if days else 0,
               p_ready=p_ready, d_ready=_delta(ready, p_ready),
               out_bars=out_bars, out_max=max([b_["value"] for b_ in out_bars] or [1]) or 1,
               rate=rate, p_rate=p_rate, d_rate=(round(rate - p_rate, 1) if p_rate is not None else None),
               brak_models=by("cm"), brak_customers=by("customer"),
               rate_max=max([b_["rate"] for b_ in out_bars] or [1]) or 1)

    # ---- деньги (директор): начислено, оплачено, долг
    if role == "director":
        dd = compute_debt()
        acc = [(_dt(x["l"]["accepted_at"]), x["amount"]) for x in dd["items"]]
        pays = [(_dt((p_["payment_date"] or p_["created_at"][:10]) + " 00:00"), p_["amount_kopeks"] / 100) for p_ in g.db.execute("SELECT * FROM payments WHERE cancelled_at IS NULL")]
        in_cur = lambda t: t and t.date() >= start
        in_prev = lambda t: t and prev_start and prev_start <= t.date() < start
        accrued = sum(_cents(a_) for t, a_ in acc if in_cur(t)) / 100
        paid = sum(_cents(a_) for t, a_ in pays if in_cur(t)) / 100
        p_accrued = sum(_cents(a_) for t, a_ in acc if in_prev(t)) / 100 if prev_start else None
        # долг на конец каждой недели
        weeks = []
        w = start - db.timedelta(days=start.weekday())
        while w <= today:
            end = w + db.timedelta(days=6)
            owed = (sum(_cents(a_) for t, a_ in acc if t and t.date() <= end) - sum(_cents(a_) for t, a_ in pays if t and t.date() <= end)) / 100
            a_w = sum(_cents(a_) for t, a_ in acc if t and w <= t.date() <= end) / 100
            p_w = sum(_cents(a_) for t, a_ in pays if t and w <= t.date() <= end) / 100
            weeks.append(dict(label="с " + w.strftime("%d.%m"), debt=owed, accrued=a_w, paid=p_w))
            w += db.timedelta(days=7)
        ctx.update(accrued=accrued, paid=paid, p_accrued=p_accrued, d_accrued=_delta(accrued, p_accrued),
                   debt_now=(_cents(dd["total"]) - sum(_cents(a_) for _, a_ in pays)) / 100, weeks=weeks,
                   debt_max=max([abs(x["debt"]) for x in weeks] + [x["accrued"] for x in weeks] + [1]))
    return render_template("stats.html", **ctx)


# ---------- Приёмка ----------
@app.route("/acceptance")
@login_required
def acceptance():
    role = session["role"]
    if role not in ("sklad", "proizv"):
        abort(403)
    if role == "proizv":
        return _prod_acceptance()
    return _sklad_acceptance()
    # типы, которые принимает эта роль
    kinds = [k for k, v in KINDS.items() if v[2] == role]

    def rows_for(status):
        if not kinds:
            return []
        q = ("SELECT * FROM documents WHERE kind IN (%s) AND status=? "
             "ORDER BY id DESC" % ",".join("?" * len(kinds)))
        docs = g.db.execute(q, kinds + [status]).fetchall()
        out = []
        for d in docs:
            sent, recv, diff, has_recv = _doc_totals(d["id"])
            out.append(dict(d=d, sent=sent, recv=recv, diff=diff, has_recv=has_recv))
        return out

    pending = rows_for("sent")       # ждут приёмки
    done = rows_for("accepted")[:15]  # недавно принятые
    total_pending_pairs = sum(r["sent"] for r in pending)

    return render_template("acceptance.html", pending=pending, done=done,
                           total_pending_pairs=total_pending_pairs)


# ---------- Приёмка на складе: всё, что едет с производства, одним списком (как у производства) ----------
def _sklad_incoming():
    return g.db.execute("SELECT * FROM documents WHERE kind='RETURN' AND status='sent' ORDER BY id").fetchall()


def _sklad_acceptance():
    tab = "done" if request.args.get("tab") == "done" else "wait"
    docs = _sklad_incoming()
    ready, brak = [], []
    for d in docs:
        for l in _load_lines(d["id"]):
            row = dict(l=l, ops=[o["name"] for o in _sig_ops(l["lot"])] if l["lot"] else [], doc=d["id"])
            (brak if l["status"] == "brak" else ready).append(row)
    done = []
    if tab == "done":
        for d in g.db.execute("SELECT * FROM documents WHERE kind='RETURN' AND status='accepted' "
                              "ORDER BY id DESC LIMIT 50").fetchall():
            sent, recv, diff, has_recv = _doc_totals(d["id"])
            done.append(dict(d=d, sent=sent, recv=recv, diff=diff))
    return render_template("sklad_acceptance.html", tab=tab, ready=ready, brak=brak, n_wait=len(docs), done=done)


def _receipt_conflict(rows):
    for row in rows:
        raw=request.form.get(f"rev_{row['id']}")
        if raw is not None and raw != str(row['recv_version']):
            return True
    return False


def _save_receipts(table, rows, target):
    if _receipt_conflict(rows):
        return {"ok":False,"error":"Данные изменились в другой вкладке. Обновите страницу перед продолжением."},409
    updates=[]
    for row in rows:
        raw=request.form.get(f"recv_{row['id']}")
        if raw is None:continue
        parser=_int_pairs if table=='lines' or row['line_kind']=='pair' else _recv_value
        v=parser(raw) if raw.strip() else None
        maximum=row['pairs_sent'] if table=='lines' else (row['collected'] or 0)
        if raw.strip() and (v is None or v>maximum):
            return {"ok":False,"error":"Проверьте количество: оно не должно быть отрицательным или больше отправленного."},400
        updates.append((v,row['id']))
    for value,rid in updates:
        g.db.execute(f"UPDATE {table} SET {target}=?,recv_version=recv_version+1 WHERE id=?",(value,rid))
    revisions={str(rid):g.db.execute(f"SELECT recv_version FROM {table} WHERE id=?",(rid,)).fetchone()[0] for _,rid in updates}
    g.db.commit()
    return {"ok":True,"revisions":revisions}


@app.route("/acceptance/sklad/save", methods=["POST"])
@login_required
def sklad_accept_save():
    """Автосохранение вписанного складом (чтобы не потерять при обновлении страницы)."""
    if session["role"] != "sklad":
        abort(403)
    rows=[l for d in _sklad_incoming() for l in _load_lines(d['id'])]
    return _save_receipts('lines',rows,'recv_draft')


@app.route("/acceptance/sklad/accept", methods=["POST"])
@login_required
def sklad_accept():
    """Склад принимает всё вписанное сразу по всем передачам. Невписанное остаётся ждать приёмки
    отдельной передачей «Остаток передачи №X»."""
    if session["role"] != "sklad":
        abort(403)
    back = redirect(url_for("acceptance"))
    docs = _sklad_incoming()
    if _receipt_conflict([l for d in docs for l in _load_lines(d['id'])]):
        flash("Данные изменились в другой вкладке. Обновите страницу.")
        return back
    vals, any_entered = {}, False
    for d in docs:
        for l in _load_lines(d["id"]):
            raw = (request.form.get(f"recv_{l['id']}") or "").strip()
            v = _int_pairs(raw) if raw else None
            if raw and v is None:
                flash(f"{l['customer']} — {l['model']}: впишите целое число пар.")
                return back
            if v is not None and v > l["pairs_sent"]:
                flash(f"Нельзя принять больше, чем отправили: {l['customer']} — {l['model']}, отправлено {l['pairs_sent']} пар.")
                return back
            vals[l["id"]] = v
            any_entered = any_entered or v is not None
    if not any_entered:
        flash("Впишите, сколько пришло.")
        return back
    now = db.now_str()
    left_total = 0
    for d in docs:
        lines = _load_lines(d["id"])
        got = [l for l in lines if vals.get(l["id"]) is not None]
        rest = [l for l in lines if vals.get(l["id"]) is None]
        if not got:
            continue
        if rest:
            cur = g.db.execute(
                "INSERT INTO documents (kind, status, created_role, created_at, sent_at, note) "
                "VALUES ('RETURN', 'sent', ?, ?, ?, ?)",
                (d["created_role"], d["created_at"], d["sent_at"],
                 f"Остаток передачи №{d['id']}" + (f" · {d['note']}" if d["note"] else "")))
            ids = [l["id"] for l in rest]
            g.db.execute("UPDATE lines SET document_id=? WHERE id IN (%s)" % ",".join("?" * len(ids)),
                         [cur.lastrowid] + ids)
            left_total += len(rest)
        for l in got:
            g.db.execute("UPDATE lines SET pairs_recv=?, recv_draft=NULL WHERE id=?", (vals[l["id"]], l["id"]))
        cur = g.db.execute("UPDATE documents SET status='accepted', accepted_at=? WHERE id=? AND status='sent'", (now, d["id"]))
        if cur.rowcount != 1:
            g.db.rollback()
            flash("Эта передача уже принята.")
            return back
        wh_from_doc(g.db, d["id"])   # с производства пары списаны ещё при отправке
    g.db.commit()
    msg = "Принято."
    if left_total:
        msg += f" Ещё {left_total} " + ("строка ждёт" if left_total == 1 else "строк ждут") + " приёмки."
    flash(msg)
    return back


# ---------- Приёмка на производстве: всё, что едет, одним списком ----------
def _incoming_transfers():
    return g.db.execute("SELECT * FROM requests WHERE status='shipped' AND transfer_id IS NULL ORDER BY id").fetchall()


def _prod_acceptance():
    tab = "done" if request.args.get("tab") == "done" else "wait"
    mats, pairs = [], []
    trs = _incoming_transfers()
    for r in trs:
        its = _placed_items(r["id"])
        ops = _ops_map("req", [i["id"] for i in its if i["line_kind"] == "pair"])
        for i in its:
            row = dict(i=i, ops=[o["name"] for o in ops.get(i["id"], [])], tr=r["id"])
            (pairs if i["line_kind"] == "pair" else mats).append(row)
    done = []
    if tab == "done":
        for r in g.db.execute("SELECT * FROM requests WHERE status='accepted' AND transfer_id IS NULL "
                              "AND (created_role='sklad' OR EXISTS (SELECT 1 FROM request_items x WHERE x.request_id=requests.id "
                              "AND x.line_kind IN ('pair','material'))) ORDER BY id DESC LIMIT 50").fetchall():
            its = _placed_items(r["id"])
            done.append(dict(r=r, pairs=sum((i["recv"] or 0) for i in its if i["line_kind"] == "pair"),
                             mats=sum(1 for i in its if i["line_kind"] == "material"), diff=_req_diff(r["id"])))
    legacy = [dict(d=d, sent=_doc_totals(d["id"])[0]) for d in
              g.db.execute("SELECT * FROM documents WHERE kind='OUT' AND status='sent' ORDER BY id").fetchall()]
    return render_template("prod_acceptance.html", tab=tab, mats=mats, pairs=pairs, n_wait=len(trs),
                           done=done, legacy=legacy)


MAX_QTY = 1_000_000   # больше миллиона за раз не бывает — защита от опечаток и «сломанных» чисел


def _int_pairs(raw):
    """Целое число пар 0…MAX_QTY или None (дроби, буквы, минус — ошибка)."""
    raw = (raw or "").strip().replace(" ", "")
    if not raw.isdigit():
        return None
    v = int(raw)
    return v if v <= MAX_QTY else None


def _recv_value(raw):
    raw = (raw or "").strip().replace(",", ".").replace(" ", "")
    if raw == "":
        return None
    try:
        v = float(raw)
    except ValueError:
        return None
    if not math.isfinite(v) or v > MAX_QTY:
        return None
    if v < 0:
        return None
    v = round(v, 3)
    return int(v) if v == int(v) else v


def _need_got(i):
    """Сколько реально пришло по строке заказа: сумма ПРИНЯТОГО производством (recv) во всех принятых
    передачах, куда склад клал эту строку. Старые заказы без такой связи — по-старому."""
    rows = g.db.execute("SELECT ri.recv FROM request_items ri JOIN requests t ON t.id=ri.request_id "
                        "WHERE ri.from_item=? AND t.status='accepted'", (i["id"],)).fetchall()
    if rows:
        return sum((r["recv"] or 0) for r in rows), True
    return None, False


def _settle_request(rid):
    """Заказ производства после приёмки: сколько пришло по каждой строке (по факту приёмки, а не по тому,
    что склад собрал). Если пришло меньше, чем заказали, заказ снова ждёт склад на остаток."""
    left = False
    for i in g.db.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='need'", (rid,)).fetchall():
        got, linked = _need_got(i)
        if not linked:   # старая схема: заявка сама ехала
            got = (i["delivered"] or 0) + (i["recv"] if i["recv"] is not None else (i["collected"] or 0))
        g.db.execute("UPDATE request_items SET delivered=?, collected=NULL, placed=0 WHERE id=?", (got, i["id"]))
        if i["qty"] and got < i["qty"] - 1e-9:
            left = True
    if left:
        g.db.execute("UPDATE requests SET status='open', transfer_id=NULL, taken_at=NULL, accepted_at=NULL WHERE id=?", (rid,))


def _refresh_need(need_id):
    """После разбора расхождения («Нашлось») пересчитать, сколько пришло по строке заказа;
    если заказ теперь получен полностью и склад ничего не собирает — закрыть его."""
    i = g.db.execute("SELECT * FROM request_items WHERE id=? AND line_kind='need'", (need_id,)).fetchone()
    if not i:
        return
    got, linked = _need_got(i)
    if not linked:
        return
    g.db.execute("UPDATE request_items SET delivered=? WHERE id=?", (got, need_id))
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (i["request_id"],)).fetchone()
    if not r or r["status"] != "open":
        return
    items = g.db.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='need'", (r["id"],)).fetchall()
    if all((x["delivered"] or 0) >= (x["qty"] or 0) - 1e-9 and not x["collected"] for x in items):
        g.db.execute("UPDATE requests SET status='accepted', accepted_at=? WHERE id=?", (db.now_str(), r["id"]))


def _is_partial(rid):
    return bool(g.db.execute("SELECT 1 FROM request_items WHERE request_id=? AND line_kind='need' AND delivered>0",
                             (rid,)).fetchone())


@app.route("/requests/<int:req_id>/close_rest", methods=["POST"])
@login_required
def request_close_rest(req_id):
    """«Остатка не будет»: заказ, пришедший частично, закрывается без остатка (склад или производство)."""
    role = session["role"]
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["created_role"] != "proizv" or r["status"] != "open" or not _is_partial(req_id):
        abort(404)
    if role not in ("sklad", "proizv"):
        abort(403)
    who = "склад" if role == "sklad" else "производство"
    note = ((r["note"] or "") + f" · Остатка не будет ({who})").strip(" ·")
    g.db.execute("UPDATE request_items SET collected=NULL, placed=0 WHERE request_id=?", (req_id,))
    g.db.execute("UPDATE requests SET status='accepted', accepted_at=?, note=? WHERE id=?", (db.now_str(), note, req_id))
    g.db.commit()
    flash(f"Заказ №{req_id} закрыт без остатка.")
    return redirect(url_for("documents"))


@app.route("/acceptance/save", methods=["POST"])
@login_required
def prod_accept_save():
    """Автосохранение вписанного (чтобы не потерять при обновлении страницы)."""
    if session["role"] != "proizv":
        abort(403)
    rows=[i for r in _incoming_transfers() for i in _placed_items(r['id'])]
    return _save_receipts('request_items',rows,'recv')


@app.route("/acceptance/accept", methods=["POST"])
@login_required
def prod_accept():
    """Принять всё вписанное сразу по всем передачам. Невписанное остаётся ждать приёмки
    (отдельной передачей «Остаток передачи №X»)."""
    if session["role"] != "proizv":
        abort(403)
    back = redirect(url_for("acceptance"))
    trs = _incoming_transfers()
    if _receipt_conflict([i for r in trs for i in _placed_items(r['id'])]):
        flash("Данные изменились в другой вкладке. Обновите страницу.")
        return back
    vals, any_entered, over = {}, False, False
    for r in trs:
        for i in _placed_items(r["id"]):
            raw = (request.form.get(f"recv_{i['id']}") or "").strip()
            if i["line_kind"] == "pair":
                v = _int_pairs(raw) if raw else None
                bad = bool(raw) and v is None
            else:
                v = _recv_value(raw)
                bad = bool(raw) and v is None
            if bad:
                title = f"{i['customer']} — {i['model']}" if i["line_kind"] == "pair" else i["item"]
                flash(f"{title}: " + ("впишите целое число пар." if i["line_kind"] == "pair" else "впишите число."))
                return back
            vals[i["id"]] = v
            if v is not None:
                any_entered = True
                if v - (i["collected"] or 0) > 1e-9:
                    over = True
    if not any_entered:
        flash("Впишите, сколько пришло.")
        return back
    if over:
        flash("Нельзя принять больше, чем отправили — проверьте строки.")
        return back
    now = db.now_str()
    left_total = 0
    accepted = []
    for r in trs:
        items = _placed_items(r["id"])
        got = [i for i in items if vals.get(i["id"]) is not None]
        rest = [i for i in items if vals.get(i["id"]) is None]
        if not got:
            continue
        for i in got:
            g.db.execute("UPDATE request_items SET recv=? WHERE id=?", (vals[i["id"]], i["id"]))
        if rest:
            cur = g.db.execute(
                "INSERT INTO requests (status, urgent, created_role, created_at, sent_at, taken_at, done_at, note, ship_note) "
                "VALUES ('shipped', ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["urgent"], r["created_role"], r["created_at"], r["sent_at"], r["taken_at"], r["done_at"], r["note"],
                 f"Остаток передачи №{r['id']}" + (f" · {r['ship_note']}" if r["ship_note"] else "")))
            new_id = cur.lastrowid
            ids = [i["id"] for i in rest]
            g.db.execute("UPDATE request_items SET request_id=?, recv=NULL WHERE id IN (%s)" % ",".join("?" * len(ids)),
                         [new_id] + ids)
            # заказ производства, часть которого ещё едет, считается выполненным только с остатком
            from_ids = [i["from_item"] for i in rest if i["from_item"]]
            if from_ids:
                g.db.execute("UPDATE requests SET transfer_id=? WHERE transfer_id=? AND id IN "
                             "(SELECT request_id FROM request_items WHERE id IN (%s))" % ",".join("?" * len(from_ids)),
                             [new_id, r["id"]] + from_ids)
            left_total += len(rest)
        mism = any(abs(vals[i["id"]] - (i["collected"] or 0)) > 1e-9 for i in got)
        g.db.execute("UPDATE requests SET status='accepted', accepted_at=?, discr=? WHERE id=?",
                     (now, 1 if mism else 0, r["id"]))
        stock_from_req(g.db, r["id"])
        linked = [x["id"] for x in g.db.execute("SELECT id FROM requests WHERE transfer_id=?", (r["id"],))]
        g.db.execute("UPDATE requests SET status='accepted', accepted_at=?, discr=? WHERE transfer_id=?",
                     (now, 1 if mism else 0, r["id"]))
        for rid in linked:
            _settle_request(rid)
        if r["created_role"] == "proizv":   # отправленная по старой схеме — сама заявка
            _settle_request(r["id"])
        accepted.append(r["id"])
    g.db.commit()
    msg = "Принято."
    if left_total:
        msg += f" Ещё {left_total} " + ("строка ждёт" if left_total == 1 else "строк ждут") + " приёмки."
    flash(msg)
    return back


# ---------- Обзор директора ----------
@app.route("/overview")
@login_required
def overview():
    """Обзор директора: всё по складу производства и передачам в пути."""
    if session["role"] != "director":
        abort(403)
    agg = {}

    def row(cid, mid, cust, model):
        return agg.setdefault((cid, mid), dict(customer=cust, model=model, key=f"p-{cid}-{mid}",
                                               at_prod=0, to_prod=0, to_sklad=0, work=0, fready=0, fbrak=0))

    for e in stock_balances():
        if e["type"] == "pair":
            _, c, m = e["key"].split("-")
            row(int(c), int(m), e["customer"], e["model"])["at_prod"] += e["qty"]
    work, fr, fbk = _prod_view()   # в работе / готово ждёт машину / брак ждёт машину
    for lst, fld in ((work, "work"), (fr, "fready"), (fbk, "fbrak")):
        for e in lst:
            row(e["cid"], e["mid"], e["customer"], e["model"])[fld] += e["qty"]
    # едет на производство: отправленные передачи склада (пары)
    for r in g.db.execute(
            """SELECT ri.customer_id ci, ri.model_id mi, c.name cu, m.name mo, SUM(ri.collected) p FROM request_items ri
               JOIN requests rq ON rq.id=ri.request_id
               JOIN customers c ON c.id=ri.customer_id JOIN models m ON m.id=ri.model_id
               WHERE ri.line_kind='pair' AND rq.status='shipped' GROUP BY ri.customer_id, ri.model_id"""):
        row(r["ci"], r["mi"], r["cu"], r["mo"])["to_prod"] += r["p"] or 0
    for r in g.db.execute(
            """SELECT l.customer_id ci, l.model_id mi, c.name cu, m.name mo, d.kind, SUM(l.pairs_sent) p FROM lines l
               JOIN documents d ON d.id=l.document_id
               JOIN customers c ON c.id=l.customer_id JOIN models m ON m.id=l.model_id
               WHERE d.status='sent' GROUP BY d.kind, l.customer_id, l.model_id"""):
        row(r["ci"], r["mi"], r["cu"], r["mo"])["to_prod" if r["kind"] == "OUT" else "to_sklad"] += r["p"] or 0
    by_model = [v for v in agg.values() if v["at_prod"] or v["to_prod"] or v["to_sklad"]]
    by_model.sort(key=lambda v: ((v["customer"] or "").lower(), (v["model"] or "").lower()))

    # последние передачи в обе стороны
    recent = []
    for rq in g.db.execute("SELECT * FROM requests WHERE status IN ('shipped','accepted') "
                           "AND transfer_id IS NULL ORDER BY id DESC LIMIT 10"):
        n = g.db.execute("SELECT COALESCE(SUM(collected),0) s FROM request_items WHERE request_id=? "
                         "AND line_kind='pair'", (rq["id"],)).fetchone()["s"]
        recent.append(dict(t=_sort_key(rq["done_at"]), when=rq["done_at"], dir="На производство", pairs=n,
                           status=REQ_STATUS[rq["status"]], cls="rq-" + rq["status"],
                           discr=rq["discr"], url=url_for("request_view", req_id=rq["id"]), id=rq["id"]))
    for d in g.db.execute("SELECT * FROM documents WHERE status!='draft' ORDER BY id DESC LIMIT 10"):
        sent, recv, diff, has_recv = _doc_totals(d["id"])
        recent.append(dict(t=_sort_key(d["sent_at"]), when=d["sent_at"], dir=KIND_SHORT[d["kind"]], pairs=sent,
                           status=STATUS_LABEL[d["status"]], cls=d["status"], discr=bool(has_recv and diff),
                           url=url_for("doc_view", doc_id=d["id"]), id=d["id"]))
    recent.sort(key=lambda x: x["t"], reverse=True)

    ready = [e for e in wh_balances() if e["ready"] > 0]
    return render_template(
        "overview.html", ready_models=len(ready), ready_pairs=sum(e["ready"] for e in ready),
        at_prod_total=sum(v["at_prod"] for v in agg.values()),
        fready_total=sum(v["fready"] for v in agg.values()),
        to_prod=sum(v["to_prod"] for v in agg.values()),
        to_sklad=sum(v["to_sklad"] for v in agg.values()),
        by_model=by_model, recent=recent[:10])


# ---------- Заявки «чего не хватает» ----------
def _load_req_items(req_id):
    return g.db.execute(
        """SELECT ri.*, c.name AS customer, m.name AS model
           FROM request_items ri
           LEFT JOIN customers c ON c.id = ri.customer_id
           LEFT JOIN models    m ON m.id = ri.model_id
           WHERE ri.request_id=?
           ORDER BY (ri.source='sklad'), ri.id""", (req_id,)
    ).fetchall()


@app.route("/requests")
@login_required
def requests_list():
    # Заявки и передачи объединены в одном разделе «Документы».
    return redirect(url_for("documents", tab="requests"))


@app.route("/requests/new", methods=["POST"])
@login_required
def request_new():
    if session["role"] != "proizv":
        abort(403)
    old = g.db.execute(
        "SELECT id FROM requests WHERE status='draft' AND created_role='proizv' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    if old:
        return redirect(url_for("request_view", req_id=old["id"]))
    return redirect(url_for("request_view", req_id=0))


def _new_request(role):
    return dict(id=0, status="progress" if role=="sklad" else "draft", created_role=role,
                created_at=db.now_str(), note=None, ship_note=None, transfer_id=None, urgent=0,
                sent_at=None, taken_at=None, done_at=None, accepted_at=None, discr=0)


def _ensure_request(req_id, role):
    if req_id:
        return req_id
    if role=="sklad":
        return _open_transfer(create=True)
    old=g.db.execute("SELECT id FROM requests WHERE status='draft' AND created_role='proizv' ORDER BY id DESC LIMIT 1").fetchone()
    if old:return old["id"]
    return g.db.execute("INSERT INTO requests(status,urgent,created_role,created_at) VALUES('draft',0,'proizv',?)",(db.now_str(),)).lastrowid


@app.route("/requests/<int:req_id>")
@login_required
def request_view(req_id):
    role = session["role"]
    r = _new_request(role) if req_id==0 and role in ('sklad','proizv') else g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        abort(404)
    if r["status"] == "draft" and role != "proizv":
        abort(404)
    # Сборка идёт только внутри создания передачи (переход с шагом или своя передача).
    # Открытая из списка заявка — просто список того, чего не хватает.
    wizard = (role == "sklad" and r["status"] == "progress" and r["created_role"] == "sklad")
    if wizard and r["status"] in ("open", "done"):
        g.db.execute(
            "UPDATE requests SET status='progress', taken_at=COALESCE(taken_at, ?) WHERE id=?",
            (db.now_str(), req_id))
        g.db.commit()
        r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    items = _load_req_items(req_id)
    can_edit = (r["status"] == "draft" and role == "proizv")
    can_submit = (r["status"] == "draft" and role == "proizv" and len(items) > 0)
    can_collect = wizard
    # склад может отправить на производство, когда собирает или уже отметил «собрано»
    can_ship = (role == "sklad" and r["status"] in ("progress", "done"))
    can_delete = req_id!=0 and _can_delete_req(r, role)
    # показывать колонку «собрано», если склад собирает или уже что-то отмечено
    has_collected = any(i["collected"] is not None for i in items)
    customers = g.db.execute(
        "SELECT * FROM customers WHERE archived=0 ORDER BY name").fetchall()
    models = g.db.execute(
        "SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall()
    # шаг мастера сборки (только пока склад собирает)
    step = 2   # шаг «Сборка» — общий список заявок (docs_collect), здесь только «что положил»
    has_reqs = bool(g.db.execute(
        "SELECT 1 FROM requests r JOIN request_items ri ON ri.request_id=r.id AND ri.line_kind='need' "
        "WHERE r.created_role='proizv' AND r.status IN ('open','progress','done') LIMIT 1").fetchone())
    ops_by_item = _ops_map("req", [i["id"] for i in items if i["line_kind"] == "pair"])
    linked = g.db.execute("SELECT id FROM requests WHERE transfer_id=? ORDER BY id", (req_id,)).fetchall()
    last_pair = None
    pairs_total = sum((i["collected"] or 0) for i in items if i["line_kind"] == "pair")
    partial = r["created_role"] == "proizv" and r["status"] == "open" and _is_partial(req_id)
    if can_edit:   # производство заполняет заявку — отдельная страница в стиле передачи склада
        return render_template("req_draft.html", r=r, items=items, materials=_materials_catalog(),
                               units=MAT_UNITS, can_delete=can_delete)
    return render_template("request_view.html", r=r, items=items,
                           operations=_operations(), ops_by_item=ops_by_item, linked=linked,
                           materials=_materials_catalog(),
                           units=MAT_UNITS,
                           can_edit=can_edit, can_submit=can_submit,
                           can_collect=can_collect,
                           can_ship=can_ship, can_delete=can_delete,
                           has_collected=has_collected, step=step, has_reqs=has_reqs, last_pair=last_pair, pairs_total=pairs_total, partial=partial,
                           customers=customers, models=models)


def _req_draft_owner(req_id):
    if req_id==0 and session.get('role')=='proizv':
        return _new_request('proizv')
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        abort(404)
    if r["status"] != "draft" or session.get("role") != "proizv":
        abort(403)
    return r


@app.route("/requests/<int:req_id>/item/add", methods=["POST"])
@login_required
def request_item_add(req_id):
    """Производство добавляет в заявку материал: из справочника, кол-во и единица обязательны, «Срочно» — у строки."""
    _req_draft_owner(req_id)

    def fail(msg, fields):
        if _is_ajax():
            return {"ok": False, "error": msg, "fields": fields}, 400
        flash(msg)
        return redirect(url_for("request_view", req_id=req_id))
    bad, msgs = [], []
    name = " ".join((request.form.get("item") or "").split())
    row = _find_ref("materials", name) if name else None
    if not name:
        bad.append("item"); msgs.append("материал")
    elif not row:
        return fail(f"Материала «{name}» нет в справочнике — выберите из списка или нажмите «+ Создать».", ["item"])
    qty = _num(request.form.get("qty"))
    if not qty:
        bad.append("qty"); msgs.append("количество")
    if bad:
        return fail("Укажите " + ", ".join(msgs) + ".", bad)
    m = g.db.execute("SELECT name, unit FROM materials WHERE id=?", (row["id"],)).fetchone()
    note = (request.form.get("note") or "").strip() or None
    urgent = 1 if request.form.get("urgent") else 0
    virtual = req_id==0
    req_id = _ensure_request(req_id, 'proizv')
    cur = g.db.execute(
        "INSERT INTO request_items (request_id, item, qty, note, unit, urgent) VALUES (?, ?, ?, ?, ?, ?)",
        (req_id, m["name"], qty, note, m["unit"], urgent))
    g.db.commit()
    if _is_ajax():
        it = g.db.execute("SELECT * FROM request_items WHERE id=?", (cur.lastrowid,)).fetchone()
        return {"ok": True, "redirect": url_for('request_view',req_id=req_id) if virtual else None, "html": render_template("_need_row.html", i=it, r={"id": req_id}, editable=True),
                "count": g.db.execute("SELECT COUNT(*) c FROM request_items WHERE request_id=?", (req_id,)).fetchone()["c"]}
    return redirect(url_for("request_view", req_id=req_id))


def _req_progress_sklad(req_id):
    if session.get("role") != "sklad":
        abort(403)
    if req_id==0:
        return _new_request('sklad')
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "progress" or r["created_role"] != "sklad":
        abort(404)
    return r


def _is_ajax():
    return request.headers.get("X-Requested-With") == "fetch"


def _placed_reply(req_id, item_id=None, error=None, fields=None):
    """Ответ на добавление/удаление в «что положил»: JSON без перезагрузки или обычный переход.
    fields — какие поля подсветить красным."""
    if _is_ajax():
        if error:
            return {"ok": False, "error": error, "fields": fields or []}, 400
        res = {"ok": True, "redirect": url_for('request_view',req_id=req_id) if request.view_args.get('req_id')==0 else None, "count": g.db.execute(
            "SELECT COUNT(*) c FROM request_items WHERE request_id=? AND line_kind IN ('pair','material')",
            (req_id,)).fetchone()["c"],
            "pairs": _fmt(g.db.execute(
                "SELECT COALESCE(SUM(collected),0) s FROM request_items WHERE request_id=? AND line_kind='pair'",
                (req_id,)).fetchone()["s"])}
        if item_id:
            it = [i for i in _load_req_items(req_id) if i["id"] == item_id][0]
            res["html"] = render_template(
                "_placed_pair.html" if it["line_kind"] == "pair" else "_placed_mat.html",
                i=it, r={"id": req_id, "status": "progress"}, editable=True,
                ops_by_item=_ops_map("req", [item_id]))
        return res
    if error:
        flash(error)
    return redirect(url_for("request_view", req_id=req_id, step=2))


def _num(raw, cap=None):
    """Число из поля: 2,5 → 2.5; целое остаётся целым; пусто/≤0/больше предела → None."""
    try:
        v = float((raw or "").replace(",", ".").replace(" ", ""))
    except ValueError:
        return None
    if not math.isfinite(v) or v <= 0 or v > (cap or MAX_QTY):
        return None
    v = round(v, 3)
    return int(v) if v == int(v) else v


@app.route("/requests/<int:req_id>/pair/add", methods=["POST"])
@login_required
def request_pair_add(req_id):
    """Кладовщик добавляет в «что положил» обувь: заказчик + модель + операции + пары + примечание.
    Заказчик и модель — только из справочника (новые создаются кнопкой «+ Создать»)."""
    _req_progress_sklad(req_id)
    bad, msgs = [], []
    cust = " ".join((request.form.get("customer_name") or "").split())
    model = " ".join((request.form.get("model_name") or "").split())
    crow = _find_ref("customers", cust) if cust else None
    mrow = _find_ref("models", model) if model else None
    if not cust:
        bad.append("customer_name"); msgs.append("заказчика")
    elif not crow:
        return _placed_reply(req_id, error=f"Заказчика «{cust}» нет в справочнике — выберите из списка или нажмите «+ Создать».",
                             fields=["customer_name"])
    if not model:
        bad.append("model_name"); msgs.append("модель")
    elif not mrow:
        return _placed_reply(req_id, error=f"Модели «{model}» нет в справочнике — выберите из списка или нажмите «+ Создать».",
                             fields=["model_name"])
    if not _selected_ops_ok():
        bad.append("ops"); msgs.append("операции")
    pairs = _int_pairs(request.form.get("pairs")) or 0
    if pairs <= 0:
        bad.append("pairs"); msgs.append("число пар")
    if bad:
        return _placed_reply(req_id, error="Укажите " + ", ".join(msgs) + ".", fields=bad)
    note = (request.form.get("note") or "").strip() or None
    req_id = _ensure_request(req_id, 'sklad')
    cur = g.db.execute(
        "INSERT INTO request_items (request_id, item, collected, placed, source, line_kind, "
        "customer_id, model_id, status, note) VALUES (?, '', ?, 1, 'sklad', 'pair', ?, ?, NULL, ?)",
        (req_id, pairs, crow["id"], mrow["id"], note))
    _save_item_ops("req", cur.lastrowid)
    g.db.commit()
    return _placed_reply(req_id, cur.lastrowid)


@app.route("/requests/<int:req_id>/material/add", methods=["POST"])
@login_required
def request_material_add(req_id):
    """Кладовщик добавляет в «что положил» материал: название (из справочника) + кол-во + единица — всё обязательно."""
    _req_progress_sklad(req_id)
    bad, msgs = [], []
    name = " ".join((request.form.get("item") or "").split())
    row = _find_ref("materials", name) if name else None
    if not name:
        bad.append("item"); msgs.append("материал")
    elif not row:
        return _placed_reply(req_id, error=f"Материала «{name}» нет в справочнике — выберите из списка или нажмите «+ Создать».",
                             fields=["item"])
    qty = _num(request.form.get("qty"))
    if not qty:
        bad.append("qty"); msgs.append("количество")
    unit = None
    if row:
        unit = g.db.execute("SELECT unit FROM materials WHERE id=?", (row["id"],)).fetchone()["unit"]
    elif request.form.get("unit") in MAT_UNITS:
        unit = request.form.get("unit")
    if not unit:
        bad.append("unit"); msgs.append("единицу измерения")
    if bad:
        return _placed_reply(req_id, error="Укажите " + ", ".join(msgs) + ".", fields=bad)
    item = g.db.execute("SELECT name FROM materials WHERE id=?", (row["id"],)).fetchone()["name"]
    note = (request.form.get("note") or "").strip() or None
    # сколько этого материала уже лежит в передаче + новое — хватает ли по учёту склада
    already = sum((x["collected"] or 0) for x in g.db.execute(
        "SELECT * FROM request_items WHERE request_id=? AND line_kind='material'", (req_id,)) if _mat_key(x["item"]) == _mat_key(item))
    have = mat_balances().get(_mat_key(item), {}).get("qty", 0)
    req_id = _ensure_request(req_id, 'sklad')
    cur = g.db.execute(
        "INSERT INTO request_items (request_id, item, qty, collected, placed, item_type, source, "
        "line_kind, unit, note) VALUES (?, ?, ?, ?, 1, 'material', 'sklad', 'material', ?, ?)",
        (req_id, item, qty, qty, unit, note))
    g.db.commit()
    res = _placed_reply(req_id, cur.lastrowid)
    # предупреждение «по учёту на складе только N» включим, когда склад внесёт начальные остатки
    return res


@app.route("/requests/<int:req_id>/placed/<int:item_id>/del", methods=["POST"])
@login_required
def request_placed_del(req_id, item_id):
    _req_progress_sklad(req_id)
    if not g.db.execute("SELECT 1 FROM request_items WHERE id=? AND request_id=? AND source='sklad'",
                        (item_id, req_id)).fetchone():
        abort(404)
    _del_item_ops("req", [item_id])
    g.db.execute("DELETE FROM request_items WHERE id=? AND request_id=? AND source='sklad'",
                 (item_id, req_id))
    if not g.db.execute('SELECT 1 FROM request_items WHERE request_id=?',(req_id,)).fetchone():
        g.db.execute('DELETE FROM requests WHERE id=?',(req_id,))
        g.db.commit()
        return {"ok":True,"redirect":url_for('request_view',req_id=0,step=2)} if _is_ajax() else redirect(url_for('request_view',req_id=0,step=2))
    g.db.commit()
    return _placed_reply(req_id)


@app.route("/requests/<int:req_id>/item/<int:item_id>/del", methods=["POST"])
@login_required
def request_item_del(req_id, item_id):
    _req_draft_owner(req_id)
    g.db.execute("DELETE FROM request_items WHERE id=? AND request_id=?", (item_id, req_id))
    if not g.db.execute('SELECT 1 FROM request_items WHERE request_id=?',(req_id,)).fetchone():
        g.db.execute('DELETE FROM requests WHERE id=?',(req_id,))
        g.db.commit()
        return {"ok":True,"redirect":url_for('request_view',req_id=0)} if _is_ajax() else redirect(url_for('request_view',req_id=0))
    g.db.commit()
    if _is_ajax():
        return {"ok": True, "count": g.db.execute("SELECT COUNT(*) c FROM request_items WHERE request_id=?",
                                                  (req_id,)).fetchone()["c"]}
    return redirect(url_for("request_view", req_id=req_id))


@app.route("/requests/<int:req_id>/submit", methods=["POST"])
@login_required
def request_submit(req_id):
    _req_draft_owner(req_id)
    if not _load_req_items(req_id):
        flash("Нельзя отправить пустую заявку.")
        return redirect(url_for("request_view", req_id=req_id))
    urgent = 1 if g.db.execute("SELECT 1 FROM request_items WHERE request_id=? AND urgent=1", (req_id,)).fetchone() else 0
    note = (request.form.get("note") or "").strip()
    g.db.execute("UPDATE requests SET status='open', sent_at=?, urgent=?, note=? WHERE id=?",
                 (db.now_str(), urgent, note, req_id))
    g.db.commit()
    flash(f"Заявка №{req_id} отправлена складу.")
    return redirect(url_for("documents", tab="mat"))


@app.route("/requests/<int:req_id>/ship", methods=["POST"])
@login_required
def request_ship(req_id):
    """Склад отправляет собранную заявку на производство."""
    if session["role"] != "sklad":
        abort(403)
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] not in ("progress", "done"):
        abort(404)
    placed = g.db.execute(
        "SELECT COUNT(*) c FROM request_items WHERE request_id=? AND line_kind IN ('pair','material')",
        (req_id,)).fetchone()["c"]
    if not placed:
        flash("Добавьте пары или материалы, затем отправляйте.")
        return redirect(url_for("request_view", req_id=req_id, step=2))
    ship_note = (request.form.get("ship_note") or "").strip() or None
    now = db.now_str()
    repair = g.db.execute("SELECT * FROM request_items WHERE request_id=? AND status='repair'", (req_id,)).fetchall()
    need = {}
    for it in repair:
        need[(it["customer_id"], it["model_id"])] = need.get((it["customer_id"], it["model_id"]), 0) + it["collected"]
    for (c, m), n in need.items():
        bal = g.db.execute("SELECT COALESCE(SUM(qty),0) s FROM wh_moves WHERE customer_id=? AND model_id=? "
                           "AND quality='brak'", (c, m)).fetchone()["s"]
        if n - bal > 1e-9:
            flash("Брака на складе меньше, чем положено в ремонт — проверьте строки «Ремонт».")
            return redirect(url_for("request_view", req_id=req_id, step=2))
    for (c, m), n in need.items():
        g.db.execute("INSERT INTO wh_moves (created_at, kind, customer_id, model_id, quality, qty, reason, ref_id, role) "
                     "VALUES (?, 'repair', ?, ?, 'brak', ?, 'Отправлено в ремонт', ?, 'sklad')", (now, c, m, -n, req_id))
    for it in g.db.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='material'", (req_id,)).fetchall():
        if it["collected"]:
            _mat_move("out", it["item"], it["unit"], -it["collected"], f"Передача №{req_id} на производство", ref_id=req_id)
    g.db.execute("UPDATE requests SET status='shipped', done_at=?, ship_note=? WHERE id=?",
                 (now, ship_note, req_id))
    # заявки производства, собранные в эту передачу, едут вместе с ней
    g.db.execute("UPDATE requests SET status='shipped', done_at=? WHERE transfer_id=?", (now, req_id))
    g.db.commit()
    flash("Передача отправлена на производство.")
    return redirect(url_for("request_view", req_id=req_id))


def _req_in_transit(r):
    """Заявка производства уже уехала (сама или в передаче склада)."""
    if r["status"] == "shipped":
        return True
    if r["transfer_id"]:
        t = g.db.execute("SELECT status FROM requests WHERE id=?", (r["transfer_id"],)).fetchone()
        return bool(t and t["status"] in ("shipped", "accepted"))
    return False


def _can_delete_req(r, role):
    """Кто может удалить: директор — всё, кроме принятого; производство — свою, пока склад не начал;
    склад — только свою неотправленную передачу."""
    if r["status"] in ("accepted", "shipped"):
        return False
    if r["created_role"] == "proizv" and _req_in_transit(r):
        return False   # заявка уже едет в передаче — удалять можно только передачу целиком
    if role == "director":
        return True
    if role == "proizv":
        return r["created_role"] == "proizv" and r["status"] in ("draft", "open")
    if role == "sklad":
        return r["created_role"] == "sklad" and r["status"] == "progress"
    return False


@app.route("/requests/<int:req_id>/delete", methods=["POST"])
@login_required
def request_delete(req_id):
    role = session["role"]
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        abort(404)
    if role == "sklad" and r["created_role"] == "proizv":
        abort(403)
    if r["status"] in ("accepted", "shipped"):
        flash("Отправленную или принятую передачу нельзя удалить. Исправление — через приёмку и расхождение.")
        return redirect(url_for("request_view", req_id=req_id))
    if r["created_role"] == "proizv" and _req_in_transit(r):
        flash(f"Заявка №{req_id} уже едет в передаче — удалить её нельзя. Можно удалить передачу целиком.")
        return redirect(url_for("request_view", req_id=req_id))
    if not _can_delete_req(r, role):
        abort(403)
    # заказ производства уже лежал в передаче склада — убираем оттуда то, что было положено по нему
    need_ids = [x["id"] for x in g.db.execute("SELECT id FROM request_items WHERE request_id=?", (req_id,))]
    if need_ids:
        q = ",".join("?" * len(need_ids))
        moved = [x["id"] for x in g.db.execute(f"SELECT id FROM request_items WHERE from_item IN ({q})", need_ids)]
        if moved:
            _del_item_ops("req", moved)
            g.db.execute("DELETE FROM request_items WHERE id IN (%s)" % ",".join("?" * len(moved)), moved)
    # удаляется передача склада — заказы, собранные в неё, снова ждут сборки с нуля
    g.db.execute("UPDATE request_items SET placed=0, collected=NULL WHERE request_id IN "
                 "(SELECT id FROM requests WHERE transfer_id=?)", (req_id,))
    g.db.execute("DELETE FROM stock_moves WHERE ref_type='req' AND ref_id=?", (req_id,))
    g.db.execute("DELETE FROM wh_moves WHERE kind='repair' AND ref_id=?", (req_id,))
    g.db.execute("DELETE FROM mat_moves WHERE kind='out' AND ref_id=?", (req_id,))
    g.db.execute("UPDATE requests SET transfer_id=NULL, status=CASE WHEN status IN ('progress','shipped') "
                 "THEN 'open' ELSE status END WHERE transfer_id=?", (req_id,))
    _del_item_ops("req", [x["id"] for x in g.db.execute(
        "SELECT id FROM request_items WHERE request_id=?", (req_id,))])
    g.db.execute("DELETE FROM request_items WHERE request_id=?", (req_id,))
    g.db.execute("DELETE FROM requests WHERE id=?", (req_id,))
    g.db.commit()
    flash(("Передача" if r["created_role"] == "sklad" else "Заявка") + f" №{req_id} удалена.")
    return redirect(url_for("requests_list"))


# ---------- Обновление системы ----------
UPDATE_LOG = os.path.join(BASE_DIR, "updates.log")
SKIP_FILES = {"jail.db", "jail.db-wal", "jail.db-shm", "jail.db-journal", "updates.log", "errors.log", "_upload.zip"}


BACKUP_DIR = os.path.join(BASE_DIR, "backups")
CODE_KEEP = 5       # сколько прошлых версий кода хранить для отката
DB_KEEP = 14        # сколько копий базы хранить
CODE_ITEMS = ["app.py", "db.py", "schema_hardening.py", "migrate.py", "__init__.py", "VERSION", "static", "templates"]


def _backup_db(tag):
    """Копия базы (через SQLite backup — безопасно даже во время работы)."""
    import sqlite3
    d = os.path.join(BACKUP_DIR, "db")
    os.makedirs(d, exist_ok=True)
    stamp = db.datetime.now(db.TZ).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(d, f"jail_{stamp}_{tag}.db")
    src = sqlite3.connect(db.DB_PATH)
    out = sqlite3.connect(dest)
    with out:
        src.backup(out)
    out.close()
    src.close()
    files = sorted(f for f in os.listdir(d) if f.endswith(".db"))
    for f in files[:-DB_KEEP]:
        os.remove(os.path.join(d, f))
    return dest


def _code_backups():
    """Сохранённые прошлые версии кода, новые первыми: [(папка, версия)]."""
    d = os.path.join(BACKUP_DIR, "code")
    if not os.path.isdir(d):
        return []
    res = []
    for name in sorted(os.listdir(d), reverse=True):
        p = os.path.join(d, name)
        try:
            with open(os.path.join(p, "VERSION"), encoding="utf-8") as f:
                res.append((p, f.read().strip()))
        except OSError:
            continue
    return res


def _backup_code():
    """Копия текущего кода перед обновлением — чтобы можно было вернуть прошлую версию."""
    import shutil
    stamp = db.datetime.now(db.TZ).strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(BACKUP_DIR, "code", f"{stamp}_v{_current_version()}")
    os.makedirs(dest, exist_ok=True)
    for item in CODE_ITEMS:
        src = os.path.join(BASE_DIR, item)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(dest, item), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.part"))
        elif os.path.exists(src):
            shutil.copy2(src, os.path.join(dest, item))
    for p, _ in _code_backups()[CODE_KEEP:]:
        shutil.rmtree(p, ignore_errors=True)
    return dest


def _restore_code(src_dir):
    """Вернуть код из копии (база не трогается): копия целиком собирается во временной папке,
    потом становится _next и применяется при перезапуске."""
    import shutil
    tmp_dir = STAGE_DIR + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    applied = []
    try:
        for root, _, files in os.walk(src_dir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, src_dir)
                dest = os.path.join(tmp_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(full, dest)   # новое время изменения — Python точно перечитает код
                applied.append(rel.replace(os.sep, "/"))
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    os.rename(tmp_dir, STAGE_DIR)
    # удаляем копию, чтобы следующий откат шёл дальше назад
    shutil.rmtree(src_dir, ignore_errors=True)
    return applied


STAGE_DIR = os.path.join(BASE_DIR, "_next")


def _staged_version():
    try:
        with open(os.path.join(STAGE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _apply_update(zip_path):
    """Разложить файлы обновления в папку _next. Поверх рабочих файлов их кладёт __init__.py
    при перезапуске сайта — так старый код и новые файлы никогда не работают вперемешку. БД не трогаем."""
    import zipfile
    import shutil
    # Сначала раскладываем во временную папку и только целиком готовую переименовываем в _next.
    # Если архив битый — во временной папке остаётся мусор, который удаляется; _next не трогается,
    # и при перезапуске не применится «половина» обновления.
    tmp_dir = STAGE_DIR + ".tmp"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    try:
        applied = _unpack_update(zip_path, tmp_dir)
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise
    shutil.rmtree(STAGE_DIR, ignore_errors=True)
    os.rename(tmp_dir, STAGE_DIR)
    return applied


def _unpack_update(zip_path, target):
    import zipfile
    import shutil
    base = os.path.abspath(target)
    with zipfile.ZipFile(zip_path) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("архив пустой")
        # защита от выхода за пределы папки
        for n in names:
            if n.startswith("/") or ".." in n.replace("\\", "/").split("/"):
                raise ValueError("недопустимый путь в архиве: " + n)
        # снять обёртку-папку, если весь архив в одной директории (не templates)
        tops = {n.replace("\\", "/").split("/")[0] for n in names}
        strip = ""
        if len(tops) == 1:
            only = tops.pop()
            if only != "templates" and all("/" in n.replace("\\", "/") for n in names):
                strip = only + "/"
        applied = []
        names.sort(key=lambda n: os.path.basename(n) == "VERSION")
        for n in names:
            rel = n.replace("\\", "/")
            if strip and rel.startswith(strip):
                rel = rel[len(strip):]
            if (not rel or os.path.basename(rel) in SKIP_FILES or os.path.basename(rel).startswith("jail.db")
                    or rel.startswith(("backups/", "_next/", "_next.tmp/", "_applying/"))):
                continue
            dest = os.path.abspath(os.path.join(base, rel))
            if not (dest == base or dest.startswith(base + os.sep)):
                raise ValueError("выход за пределы папки: " + rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with z.open(n) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            applied.append(rel)
    if not applied:
        raise ValueError("в архиве нет файлов для применения")
    if not any(os.path.basename(a) == "app.py" for a in applied) and not any(a.startswith("templates/") for a in applied):
        raise ValueError("это не архив обновления jail (нет app.py и шаблонов)")
    return applied


def _api_reload():
    """Перезагрузка через API PythonAnywhere (если в WSGI задан PA_API_TOKEN).
    Короткий таймаут: PythonAnywhere перезапускает сайт, дожидаясь конца текущих запросов, —
    если ждать ответа API долго, этот же запрос держит перезапуск, и сайт «бесконечно грузится»."""
    token = os.environ.get("PA_API_TOKEN")
    if not token:
        return None
    import urllib.request
    import socket
    user = os.environ.get("USER") or os.path.basename(os.path.expanduser("~"))
    host = request.host.split(":")[0]
    for api in ("www.pythonanywhere.com", "eu.pythonanywhere.com"):
        try:
            req = urllib.request.Request(
                f"https://{api}/api/v0/user/{user}/webapps/{host}/reload/",
                method="POST", headers={"Authorization": f"Token {token}"})
            with urllib.request.urlopen(req, timeout=4):
                return True
        except (socket.timeout, TimeoutError):
            return True          # запрос ушёл, перезапуск идёт — не держим его своим ожиданием
        except Exception as e:
            if "timed out" in str(e):
                return True
            continue
    return False


_RELOAD_AT = [0.0]


@app.route("/update/reload", methods=["POST"])
def update_reload():
    if os.environ.get("JAIL_ALLOW_WEB_UPDATES") != "1":
        abort(403)
    """Один перезапуск сайта (страница результата вызывает сама). Один способ за раз:
    API, если есть токен, иначе — «тронуть» WSGI-файл. Повторные вызовы чаще раза в 30 с игнорируются,
    чтобы перезапуски не накладывались друг на друга."""
    import time
    if time.time() - _RELOAD_AT[0] < 30:
        return "skip", 200, {"Cache-Control": "no-store"}
    _RELOAD_AT[0] = time.time()
    ok = _api_reload()
    if ok is None or ok is False:
        _try_reload()
        return "touch", 200, {"Cache-Control": "no-store"}
    return "ok", 200, {"Cache-Control": "no-store"}


def _try_reload():
    """Тронуть WSGI-файл, чтобы PythonAnywhere перезагрузил приложение."""
    import glob
    import time
    touched = None
    for wsgi in glob.glob("/var/www/*_wsgi.py"):
        try:
            now = time.time()
            os.utime(wsgi, (now, now))
            touched = touched or wsgi
        except OSError:
            continue
    return touched


def _current_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return VERSION


@app.route("/update", methods=["GET", "POST"])
def update():
    if os.environ.get("JAIL_ALLOW_WEB_UPDATES") != "1":
        abort(403)
    err = None
    applied = None
    reloaded = None
    if request.method == "POST":
        f = request.files.get("pkg")
        if not f or not f.filename.lower().endswith(".zip"):
            err = "Загрузите zip-файл обновления."
        else:
            tmp = os.path.join(BASE_DIR, "_upload.zip")
            f.save(tmp)
            try:
                applied = _apply_update(tmp)   # сначала проверяем и раскладываем архив целиком
                try:
                    _backup_db("before_update")    # база — на случай неудачного обновления
                    _backup_code()                 # код — для кнопки «Вернуть прошлую версию»
                except Exception:
                    import shutil
                    shutil.rmtree(STAGE_DIR, ignore_errors=True)   # без копий не ставим
                    raise
                with open(UPDATE_LOG, "a", encoding="utf-8") as lg:
                    lg.write(f"{db.datetime.now(db.TZ).strftime(db.SHOW_FMT)} · {f.filename} · {len(applied)} файл(ов)\n")
                reloaded = True    # сам перезапуск — один раз, со страницы результата (/update/reload)
            except Exception as e:
                err = "Ошибка обновления: " + str(e)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        # Страница-результат — самостоятельная, не наследует base.html:
        # после обновления шаблоны на диске новые, а код в памяти ещё старый
        # (до Reload), поэтому base.html может ссылаться на ещё не загруженные
        # маршруты. Отдельная страница застрахована от этого.
        return render_template("update_result.html", cur=(_staged_version() or _current_version()),
                               applied=applied, reloaded=reloaded, err=err)

    history = []
    if os.path.exists(UPDATE_LOG):
        with open(UPDATE_LOG, encoding="utf-8") as lg:
            history = [l.strip() for l in lg if l.strip()][-10:][::-1]

    backups = _code_backups()
    errors = ""
    if os.path.exists(ERROR_LOG):
        with open(ERROR_LOG, encoding="utf-8") as f:
            errors = f.read()[-6000:]
    return render_template("update.html", cur=_current_version(), errors=errors,
                           applied=applied, reloaded=reloaded, err=err,
                           history=history, prev=(backups[0][1] if backups else None))


@app.route("/update/rollback", methods=["POST"])
def update_rollback():
    if os.environ.get("JAIL_ALLOW_WEB_UPDATES") != "1":
        abort(403)
    """Вернуть прошлую версию кода (из копии, сделанной перед последним обновлением)."""
    backups = _code_backups()
    if not backups:
        return render_template("update_result.html", cur=_current_version(), applied=None,
                               reloaded=None, err="Нет сохранённой прошлой версии.")
    path, ver = backups[0]
    try:
        _backup_db("before_rollback")
        applied = _restore_code(path)
        with open(UPDATE_LOG, "a", encoding="utf-8") as lg:
            lg.write(f"{db.datetime.now(db.TZ).strftime(db.SHOW_FMT)} · откат на {ver}\n")
        reloaded = True
        err = None
    except Exception as e:
        applied, reloaded, err = None, None, "Ошибка отката: " + str(e)
    return render_template("update_result.html", cur=(_staged_version() or _current_version()),
                           applied=applied, reloaded=reloaded, err=err)


# Старые адреса из 0.68 (/sklad/..., /director/... и т. п.) — на главную.
# Остальное — понятная страница «не найдено», а не молчаливый переход (так видно, что пошло не так).
OLD_PREFIXES = ("/sklad", "/director", "/proizv", "/production", "/warehouse", "/orders", "/batches", "/supplies")


@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith(OLD_PREFIXES):
        return redirect(url_for("index"))
    if request.path.startswith("/static/"):
        return "Файл не найден", 404
    return render_template("error.html", code=404,
                           text="Такой страницы нет — возможно, запись удалили или ссылка устарела."), 404


ERROR_LOG = os.path.join(BASE_DIR, "errors.log")


@app.errorhandler(500)
def _server_error(e):
    """Ошибка сервера: понятная страница + запись подробностей в errors.log (видно на странице обновления)."""
    import traceback
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"\n==== {db.datetime.now(db.TZ).strftime(db.SHOW_FMT)} · {request.method} {request.path} · "
                    f"{session.get('role')}\n{traceback.format_exc()}")
    except Exception:
        pass
    try:
        return render_template("error.html", code=500,
                               text="Что-то пошло не так. Ошибка записана — передайте её разработчику."), 500
    except Exception:
        return "Ошибка сервера", 500


@app.errorhandler(403)
def _forbidden(e):
    return render_template("error.html", code=403, text="Этот раздел недоступен для вашей роли."), 403


def _materials_backfill():
    """Однократно собирает справочник материалов из уже введённых названий."""
    conn = db.get_db()
    if not _once(conn, "materials_backfill_v1"):
        conn.close()
        return
    seen = {" ".join((r["name"] or "").split()).lower() for r in conn.execute("SELECT name FROM materials")}
    rows = list(conn.execute("SELECT name, unit FROM stock_moves WHERE item_type='material'")) + \
        list(conn.execute("SELECT item AS name, unit FROM request_items WHERE line_kind='material'"))
    for r in rows:
        name = " ".join((r["name"] or "").split())
        if name and name.lower() not in seen and (r["unit"] or "") in MAT_UNITS:
            conn.execute("INSERT INTO materials (name, unit) VALUES (?, ?)", (name, r["unit"]))
            seen.add(name.lower())
    conn.commit()
    conn.close()


if os.environ.get("JAIL_SKIP_MIGRATIONS") != "1":
    _materials_backfill()


def _legacy_shipped_fix():
    """Однократно: заявки, отправленные по старой схеме (без передачи), получают строки «что едет» —
    иначе их нельзя принять в новой приёмке."""
    conn = db.get_db()
    if not _once(conn, "legacy_shipped_v1"):
        conn.close()
        return
    for r in conn.execute("SELECT * FROM requests WHERE status='shipped' AND transfer_id IS NULL "
                          "AND created_role='proizv'").fetchall():
        if conn.execute("SELECT 1 FROM request_items WHERE request_id=? AND line_kind IN ('pair','material')",
                        (r["id"],)).fetchone():
            continue
        for i in conn.execute("SELECT * FROM request_items WHERE request_id=? AND line_kind='need'", (r["id"],)).fetchall():
            q = i["collected"] if i["collected"] is not None else i["qty"]
            if not q:
                continue
            conn.execute("INSERT INTO request_items (request_id, item, qty, collected, placed, item_type, source, line_kind, "
                         "unit, note, from_item) VALUES (?, ?, ?, ?, 1, 'material', 'sklad', 'material', ?, ?, ?)",
                         (r["id"], i["item"], q, q, i["unit"] or "шт", i["note"], i["id"]))
    conn.commit()
    conn.close()


if os.environ.get("JAIL_SKIP_MIGRATIONS") != "1":
    _legacy_shipped_fix()


if __name__ == "__main__":
    app.run(debug=True, port=5001)
