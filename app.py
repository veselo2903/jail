import os
import hmac
import json
import secrets
import time
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import wraps
from flask import (
    Flask, g, session, request, redirect, url_for,
    render_template, abort, flash
)
from werkzeug.middleware.proxy_fix import ProxyFix

# Работает и как отдельное приложение (python app.py),
# и как раздел /jail внутри основной системы (from jail.app import app).
if __package__:
    from . import db
else:
    import db

app = Flask(__name__)
app.secret_key = os.environ.get("JAIL_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError("JAIL_SECRET_KEY is required")
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
# Отдельное имя cookie, чтобы сессии jail и основной системы не пересекались.
app.config["SESSION_COOKIE_NAME"] = "jail_session"
app.config["SESSION_COOKIE_PATH"] = os.environ.get("JAIL_COOKIE_PATH", "/")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("JAIL_COOKIE_SECURE") == "1"
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
ENABLE_WEB_UPDATES = False

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
    VERSION = f.read().strip()

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
    "RETURN": ("Возврат на склад (производство → склад)", "proizv", "sklad"),
}
KIND_SHORT = {"OUT": "На производство", "RETURN": "Возврат"}

# Статус товара в строке (что именно передаём)
LINE_STATUSES = {
    "zagotovka": "Заготовки",
    "upakovka":  "На упаковку",
    "gotovoe":   "Готовое",
}
# Какие статусы доступны для выбора в документе данного типа
KIND_LINE_STATUSES = {
    "OUT":    ["zagotovka", "upakovka"],
    "RETURN": ["gotovoe"],
}

# Заявку производство может править, а склад её видит только после этого срока.
EDIT_WINDOW = 20  # ВРЕМЕННО 20 секунд для проверки; рабочее значение: 15 * 60

STATUS_LABEL = {"sent": "Отправлен", "accepted": "Принят"}

# Заявки «чего не хватает» (производство → склад)
REQ_STATUS = {"open": "Новая", "progress": "Собирается",
              "done": "Собрано", "shipped": "Отправлено"}

# Тип позиции при сборке (кладовщик отмечает, что это)
ITEM_TYPES = {"material": "Материал", "zagotovka": "Заготовка", "gotovoe": "Готовая обувь"}

# Операции, которые склад отмечает отдельно для каждой модели.
OPERATIONS = {
    "op1": "Штробель сапожники",
    "op2": "Штробель шить",
    "op3": "Прошивка",
    "op4": "Покраска + натирка",
    "op5": "Вставка в колодку",
    "op6": "Вклейка простилок в колодку",
    "op7": "Упаковка",
}
UNITS = {"sht": "шт", "pary": "пары", "m2": "м²"}


def _selected_operations():
    """Проверить набор операций из формы, сохранив порядок списка."""
    raw = request.form.getlist("operations")
    if not raw and request.form.get("operation"):
        raw = [request.form["operation"]]  # совместимость со старой формой
    if not raw or any(value not in OPERATIONS for value in raw):
        return []
    return [key for key in OPERATIONS if key in raw]


def _operation_names(value):
    """Показать новые наборы и ранее сохранённое одиночное значение."""
    if not value:
        return []
    try:
        keys = json.loads(value)
    except (TypeError, ValueError):
        keys = value
    if isinstance(keys, str):
        keys = [keys]
    if not isinstance(keys, list):
        return []
    return [OPERATIONS[key] for key in keys if isinstance(key, str) and key in OPERATIONS]


# ---------- Соединение с БД ----------
@app.before_request
def _csrf_guard():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        expected = session.get("csrf_token", "")
        supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            abort(400, description="Недействительный токен формы. Обновите страницу и повторите действие.")


@app.url_value_preprocessor
def _pull_role_url(endpoint, values):
    """Роль из адреса (/sklad/..., /proizv/..., /director/...) в g, не в аргументы view."""
    g.url_role = values.pop("role_url", None) if values else None


@app.url_defaults
def _add_role_url(endpoint, values):
    """url_for() сам подставляет роль текущей сессии в адрес страницы."""
    if "role_url" not in values and session.get("role") in ROLES \
            and app.url_map.is_endpoint_expecting(endpoint, "role_url"):
        values["role_url"] = session["role"]


@app.before_request
def _role_url_guard():
    role = session.get("role")
    if g.get("url_role") and role in ROLES and g.url_role != role:
        abort(403)  # адрес другой роли: /sklad/... открывает только склад


@app.before_request
def _open_db():
    g.db = db.get_db()


@app.teardown_request
def _close_db(exc):
    d = getattr(g, "db", None)
    if d is not None:
        d.close()


@app.after_request
def _audit_change(response):
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and response.status_code < 400:
        d = getattr(g, "db", None)
        if d is not None:
            d.execute(
                "INSERT INTO audit_events (at, actor_role, action, target, ip) VALUES (?, ?, ?, ?, ?)",
                (db.now_str(), session.get("role"), request.endpoint or request.method,
                 request.path, request.remote_addr),
            )
            d.commit()
    return response


def _audit_snapshot(action, target, record):
    g.db.execute(
        "INSERT INTO audit_events (at, actor_role, action, target, details, ip) VALUES (?, ?, ?, ?, ?, ?)",
        (db.now_str(), session.get("role"), action, target,
         json.dumps(record, ensure_ascii=False), request.remote_addr),
    )


def _pending_accept_count(role):
    """Сколько документов ждёт приёмки этой ролью."""
    if role not in ("sklad", "proizv"):
        return 0
    kinds = [k for k, v in KINDS.items() if v[2] == role]
    if not kinds:
        return 0
    q = ("SELECT COUNT(*) c FROM documents WHERE status='sent' AND kind IN (%s)"
         % ",".join("?" * len(kinds)))
    return g.db.execute(q, kinds).fetchone()["c"]


def _sklad_visible_sql():
    """Условие и параметры: склад видит новую заявку не раньше, чем через EDIT_WINDOW после создания."""
    return ("(status IN ('progress','done') OR "
            "(status='open' AND (created_ts IS NULL OR created_ts <= ?)))",
            (int(time.time()) - EDIT_WINDOW,))


def _hidden_from_sklad(r):
    return (r["status"] == "open" and r["created_ts"] is not None
            and r["created_ts"] > time.time() - EDIT_WINDOW)


def _req_editable(r):
    """Производство может править свою заявку в течение EDIT_WINDOW после создания."""
    return (r["created_role"] == "proizv" and r["status"] == "open"
            and r["created_ts"] is not None and time.time() < r["created_ts"] + EDIT_WINDOW)


def _clock(ts):
    return datetime.fromtimestamp(ts, db.TZ).strftime("%H:%M:%S")


def _window_label():
    return "%d мин" % (EDIT_WINDOW // 60) if EDIT_WINDOW % 60 == 0 else "%d с" % EDIT_WINDOW


def _open_requests_count():
    """Сколько заявок ждёт склад (новые + собираются)."""
    if getattr(g, "db", None) is None:
        return 0
    cond, args = _sklad_visible_sql()
    return g.db.execute("SELECT COUNT(*) c FROM requests WHERE " + cond, args).fetchone()["c"]


@app.context_processor
def _inject():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    role = session.get("role")
    pending = 0
    req_open = 0
    if role in ("sklad", "proizv") and getattr(g, "db", None) is not None:
        pending = _pending_accept_count(role)
    if role in ("sklad", "proizv", "director") and getattr(g, "db", None) is not None:
        req_open = _open_requests_count()
    return dict(
        VERSION=VERSION, ROLES=ROLES, KINDS=KINDS,
        KIND_SHORT=KIND_SHORT, STATUS_LABEL=STATUS_LABEL,
        LINE_STATUSES=LINE_STATUSES, KIND_LINE_STATUSES=KIND_LINE_STATUSES,
        REQ_STATUS=REQ_STATUS, ITEM_TYPES=ITEM_TYPES,
        OPERATIONS=OPERATIONS, UNITS=UNITS, operation_names=_operation_names,
        role=role, role_name=ROLES.get(role), pending_accept=pending,
        req_open=req_open, enable_web_updates=ENABLE_WEB_UPDATES,
        csrf_token=session["csrf_token"],
    )


# ---------- Доступ ----------
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if session.get("role") not in ROLES:
            return redirect(url_for("login"))
        return f(*a, **k)
    return w


def can_edit_refs():
    return session.get("role") in ("sklad", "director")


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
    return redirect(url_for("documents"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    if session["role"] == "director":
        return redirect(url_for("overview"))
    return redirect(url_for("documents"))


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
    try:
        return db.datetime.strptime(created_at, "%d.%m.%Y %H:%M")
    except Exception:
        return db.datetime.min


def _req_row(req_id):
    """Адрес списка заявок с раскрытой строкой этой заявки (отдельной страницы заявки нет)."""
    return url_for("documents", _anchor="dq-%d" % req_id)


def _request_detail(r, role):
    """Содержимое раскрывающейся строки заявки; одно и то же для всех ролей."""
    items = _load_req_items(r["id"])
    shipped = r["status"] == "shipped"
    # что склад положил и сколько — видно складу и директору сразу, производству после отправки
    show_result = shipped or role in ("sklad", "director")
    sklad_work = role == "sklad" and r["status"] in ("progress", "done")
    return dict(
        note=r["note"], ship_note=r["ship_note"] if (shipped or sklad_work) else None, shipped=shipped,
        visible_at=_clock(r["created_ts"] + EDIT_WINDOW) if (role == "proizv" and _req_editable(r)) else None,
        need=[i for i in items if i["source"] == "req"],
        added=[i for i in items if i["source"] == "sklad"] if show_result else [],
        show_result=show_result,
        can_take=(role == "sklad" and r["status"] == "open"),
        can_collect=(role == "sklad" and r["status"] == "progress"),
        can_ship=(role == "sklad" and r["status"] in ("progress", "done")),
    )


@app.route("/<any(sklad,proizv,director):role_url>/requests")
@login_required
def documents():
    role = session["role"]
    rows = []

    # --- Передачи ---
    kinds = _visible_kinds(role)
    docs = g.db.execute(
        "SELECT * FROM documents WHERE kind IN (%s) ORDER BY id DESC"
        % ",".join("?" * len(kinds)), kinds).fetchall()
    for d in docs:
        sent, recv, diff, has_recv = _doc_totals(d["id"])
        if sent == 0:
            continue
        creator, acceptor = _doc_dir(d["kind"])
        need_accept = (d["status"] == "sent" and role == acceptor)
        rows.append(dict(
            group="doc", id=d["id"], created_at=d["created_at"],
            title="Передача", direction=("Склад → Производство" if d["kind"] == "OUT" else "Производство → Склад"),
            amount=("%s → %s" % (sent, recv) if has_recv else str(sent)), unit="пар",
            status=d["status"], status_label=STATUS_LABEL[d["status"]], status_cls=d["status"],
            diff=(diff if has_recv else 0), has_recv=has_recv, urgent=0,
            need_action=need_accept, can_del=(role == "director"),
            del_url=url_for("doc_delete", doc_id=d["id"]),
            url=url_for("doc_view", doc_id=d["id"]),
        ))

    # --- Заявки ---
    reqs = g.db.execute("SELECT * FROM requests ORDER BY id DESC").fetchall()
    for r in reqs:
        if role == "sklad" and _hidden_from_sklad(r):
            continue
        n = g.db.execute("SELECT COUNT(*) c FROM request_items WHERE request_id=?",
                         (r["id"],)).fetchone()["c"]
        if n == 0:
            continue
        need = (role == "sklad" and r["status"] in ("open", "progress", "done"))
        rows.append(dict(
            group="req", id=r["id"], created_at=r["created_at"],
            title="Заявка", direction=("Склад → Производство" if r["created_role"] == "sklad" else "Производство → Склад"),
            amount=str(n), unit="поз.",
            status=r["status"], status_label=REQ_STATUS[r["status"]], status_cls="rq-" + r["status"],
            diff=0, has_recv=False, urgent=r["urgent"],
            need_action=need,
            can_del=((role == "proizv" and r["status"] in ("open", "progress")) or role == "director"),
            can_edit=(role == "proizv" and _req_editable(r)),
            edit_url=url_for("request_edit", req_id=r["id"]),
            del_url=url_for("request_delete", req_id=r["id"]),
            url=None,
            detail=_request_detail(r, role),
        ))

    rows.sort(key=lambda x: (x["need_action"], _sort_key(x["created_at"])), reverse=True)

    to_action = sum(1 for x in rows if x["need_action"])
    # что может создавать роль
    can_transfer = any(v[1] == role for v in KINDS.values())   # склад: OUT, производство: RETURN
    can_request = (role == "proizv")

    customers = models = []
    if role == "sklad":
        customers = g.db.execute("SELECT * FROM customers WHERE archived=0 ORDER BY name").fetchall()
        models = g.db.execute("SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall()
    return render_template("documents.html", rows=rows, n_all=len(rows), customers=customers, models=models,
                           to_action=to_action,
                           can_transfer=can_transfer, can_request=can_request)


@app.route("/<any(sklad,proizv,director):role_url>/requests/collect")
@login_required
def docs_collect():
    """Склад: единый список позиций по открытым заявкам."""
    if session["role"] != "sklad":
        abort(403)
    cond, args = _sklad_visible_sql()
    reqs = g.db.execute(
        "SELECT * FROM requests WHERE " + cond + " ORDER BY urgent DESC, id DESC", args).fetchall()
    groups = []
    for r in reqs:
        items = g.db.execute(
            "SELECT * FROM request_items WHERE request_id=? AND line_kind='need' ORDER BY id",
            (r["id"],)).fetchall()
        groups.append(dict(r=r, its=items))
    return render_template("docs_collect.html", groups=groups)


@app.route("/<any(sklad,proizv,director):role_url>/requests/collect/save", methods=["POST"])
@login_required
def docs_collect_save():
    """Сохранить общий чек-лист без изменения не показанных заявок."""
    if session["role"] != "sklad":
        abort(403)
    cond, args = _sklad_visible_sql()
    reqs = g.db.execute(
        "SELECT * FROM requests WHERE " + cond + " ORDER BY urgent DESC, id DESC", args).fetchall()
    target = None
    changes = []
    starts = []
    for r in reqs:
        items = g.db.execute(
            "SELECT * FROM request_items WHERE request_id=? AND line_kind='need' ORDER BY id",
            (r["id"],)).fetchall()
        updated = False
        any_placed = False
        for item in items:
            iid = item["id"]
            key = f"col_{iid}"
            if key not in request.form:
                continue
            updated = True
            placed = bool(request.form.get(f"placed_{iid}"))
            raw = (request.form.get(key) or "").strip()
            try:
                count = item["qty"] if placed and not raw else item["collected"] if not raw else int(raw)
            except ValueError:
                flash("Количество должно быть целым числом.")
                return redirect(url_for("docs_collect"))
            if count is not None and (count < 0 or count > 1_000_000):
                flash("Количество должно быть от 0 до 1 000 000.")
                return redirect(url_for("docs_collect"))
            if placed and (count is None or count <= 0):
                flash("Для отмеченной позиции укажите положительное количество.")
                return redirect(url_for("docs_collect"))
            changes.append((count, int(placed), iid, r["id"]))
            any_placed = any_placed or placed
        if updated and any_placed:
            if r["status"] == "open":
                starts.append(r["id"])
            if target is None and r["status"] in ("open", "progress"):
                target = r["id"]
    for count, placed, iid, req_id in changes:
        g.db.execute(
            "UPDATE request_items SET collected=?, placed=? WHERE id=? AND request_id=?",
            (count, placed, iid, req_id),
        )
    for req_id in starts:
        g.db.execute(
            "UPDATE requests SET status='progress', taken_at=COALESCE(taken_at, ?) WHERE id=?",
            (db.now_str(), req_id),
        )
    g.db.commit()
    if target is not None:
        return redirect(_req_row(target))
    flash("Отметьте позицию с положительным количеством.")
    return redirect(url_for("docs_collect"))


@app.route("/<any(sklad,proizv,director):role_url>/requests/collect/<int:req_id>/start", methods=["POST"])
@login_required
def docs_collect_start(req_id):
    """Взять заявку в сборку и открыть мастер (шаг 1)."""
    if session["role"] != "sklad":
        abort(403)
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] not in ("open", "progress", "done") or _hidden_from_sklad(r):
        abort(404)
    if r["status"] == "open":
        g.db.execute("UPDATE requests SET status='progress', taken_at=? WHERE id=?",
                     (db.now_str(), req_id))
        g.db.commit()
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/collect/blank", methods=["GET", "POST"])
@login_required
def docs_collect_blank():
    """Создать передачу только вместе с первой позицией."""
    if session["role"] != "sklad":
        abort(403)
    if request.method == "GET":
        customers = g.db.execute("SELECT * FROM customers WHERE archived=0 ORDER BY name").fetchall()
        models = g.db.execute("SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall()
        return render_template("blank_transfer.html", customers=customers, models=models)
    line_kind = request.form.get("line_kind")
    if line_kind == "pair":
        try:
            cid = int(request.form["customer_id"])
            mid = int(request.form["model_id"])
            qty = int(request.form["pairs"])
        except (KeyError, ValueError):
            qty = 0
        operations = _selected_operations()
        valid = (qty > 0 and bool(operations) and
                 g.db.execute("SELECT 1 FROM customers WHERE id=? AND archived=0", (cid,)).fetchone() and
                 g.db.execute("SELECT 1 FROM models WHERE id=? AND archived=0", (mid,)).fetchone()) if qty else False
    elif line_kind == "material":
        item = (request.form.get("item") or "").strip()
        try:
            qty = int(request.form.get("qty", ""))
        except ValueError:
            qty = 0
        unit = request.form.get("unit")
        valid = bool(item and qty > 0 and unit in UNITS)
    else:
        valid = False
    if not valid:
        flash("Заполните первую позицию и укажите количество больше нуля.")
        return redirect(url_for("docs_collect_blank"))
    cur = g.db.execute(
        "INSERT INTO requests (status, urgent, created_role, created_at, taken_at) "
        "VALUES ('progress', 0, 'sklad', ?, ?)", (db.now_str(), db.now_str()))
    if line_kind == "pair":
        g.db.execute(
            "INSERT INTO request_items (request_id, item, collected, placed, source, line_kind, customer_id, model_id, operation) "
            "VALUES (?, '', ?, 1, 'sklad', 'pair', ?, ?, ?)",
            (cur.lastrowid, qty, cid, mid, json.dumps(operations)))
    else:
        g.db.execute(
            "INSERT INTO request_items (request_id, item, qty, collected, placed, item_type, source, line_kind, unit) "
            "VALUES (?, ?, ?, ?, 1, 'material', 'sklad', 'material', ?)",
            (cur.lastrowid, item, qty, qty, unit))
    g.db.commit()
    return redirect(_req_row(cur.lastrowid))


def _parse_doc_rows():
    """Собрать строки документа из параллельных полей формы."""
    f = request.form
    cols = {k: f.getlist(k) for k in ("customer_id", "model_id", "status", "pairs")}
    rows = []
    for n in range(max(len(v) for v in cols.values())):
        rows.append({k: (v[n] if n < len(v) else "").strip() for k, v in cols.items()})
    return rows


@app.route("/docs")
@app.route("/docs/<path:rest>")
def legacy_docs(rest=""):
    """Старые ссылки /docs/... ведут на новые адреса раздела «Заявки»."""
    if rest.startswith("collect"):
        path = "requests/" + rest
    elif rest == "new" or rest.split("/")[0].isdigit():
        path = "requests/transfer/" + rest
    else:
        path = "requests"
    return _redirect_with_role(path)


def _redirect_with_role(path):
    role = session.get("role")
    if role not in ROLES:
        return redirect(url_for("login"))
    query = ("?" + request.query_string.decode()) if request.query_string else ""
    return redirect("%s/%s/%s%s" % (request.script_root, role, path, query))


@app.route("/<any(sklad,proizv,director):role_url>/requests/transfer/new", methods=["GET", "POST"])
@login_required
def doc_new():
    role = session["role"]
    # какие типы может создавать эта роль
    creatable = [k for k, v in KINDS.items() if v[1] == role]
    if not creatable:
        abort(403)

    # У роли ровно один тип: склад -> OUT, производство -> RETURN.
    kind = creatable[0]
    allowed = KIND_LINE_STATUSES[kind]
    customers = g.db.execute("SELECT * FROM customers WHERE archived=0 ORDER BY name").fetchall()
    models = g.db.execute("SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall()
    blank = {"customer_id": "", "model_id": "", "status": allowed[0], "pairs": ""}

    def page(rows, note):
        return render_template("document_new.html", kind=kind, customers=customers, models=models,
                               rows=rows or [blank], blank=blank, note=note)

    if request.method == "GET":
        return page([blank], "")
    rows = _parse_doc_rows()
    note = (request.form.get("note") or "").strip()

    def fail(message):
        flash(message)
        return page(rows, note)

    parsed = []
    for n, row in enumerate(rows, 1):
        if not row["pairs"]:
            continue  # пустая карточка — пропускаем
        try:
            cid, mid, pairs = int(row["customer_id"]), int(row["model_id"]), int(row["pairs"])
        except ValueError:
            return fail("Строка %d: заполните заказчика, модель и число пар." % n)
        if pairs <= 0:
            return fail("Строка %d: число пар должно быть больше нуля." % n)
        if (not g.db.execute("SELECT 1 FROM customers WHERE id=? AND archived=0", (cid,)).fetchone() or
                not g.db.execute("SELECT 1 FROM models WHERE id=? AND archived=0", (mid,)).fetchone()):
            return fail("Строка %d: выберите действующих заказчика и модель." % n)
        status = row["status"] if row["status"] in allowed else allowed[0]
        parsed.append((cid, mid, status, pairs))
    if not parsed:
        return fail("Добавьте хотя бы одну строку с числом пар.")
    now = db.now_str()
    cur = g.db.execute(
        "INSERT INTO documents (kind, status, created_role, created_at, sent_at, note) "
        "VALUES (?, 'sent', ?, ?, ?, ?)", (kind, role, now, now, note))
    for cid, mid, status, pairs in parsed:
        g.db.execute(
            "INSERT INTO lines (document_id, customer_id, model_id, status, pairs_sent) VALUES (?, ?, ?, ?, ?)",
            (cur.lastrowid, cid, mid, status, pairs))
    g.db.commit()
    flash("Заявка №%d отправлена на приёмку." % cur.lastrowid)
    return redirect(url_for("documents"))


@app.route("/<any(sklad,proizv,director):role_url>/requests/transfer/<int:doc_id>")
@login_required
def doc_view(doc_id):
    role = session["role"]
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d or d["kind"] not in _visible_kinds(role):
        abort(404)
    lines = _load_lines(doc_id)
    sent, recv, diff, has_recv = _doc_totals(doc_id)
    creator, acceptor = _doc_dir(d["kind"])

    can_accept = (d["status"] == "sent" and role == acceptor)
    can_delete = (role == "director")

    return render_template(
        "document_view.html", d=d, lines=lines, sent=sent, recv=recv,
        diff=diff, has_recv=has_recv, creator=creator, acceptor=acceptor,
        can_accept=can_accept, can_delete=can_delete,
    )


@app.route("/<any(sklad,proizv,director):role_url>/requests/transfer/<int:doc_id>/accept", methods=["POST"])
@login_required
def doc_accept(doc_id):
    role = session["role"]
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        abort(404)
    _, acceptor = _doc_dir(d["kind"])
    if d["status"] != "sent" or role != acceptor:
        abort(403)

    received = []
    for l in _load_lines(doc_id):
        raw = request.form.get(f"recv_{l['id']}", "")
        try:
            recv = int(raw)
        except (TypeError, ValueError):
            flash("Укажите принятое количество для каждой строки.")
            return redirect(url_for("doc_view", doc_id=doc_id))
        if recv < 0:
            flash("Принятое количество не может быть отрицательным.")
            return redirect(url_for("doc_view", doc_id=doc_id))
        note = (request.form.get(f"note_{l['id']}", "") or "").strip()
        received.append((recv, note, l["id"]))
    for recv, note, line_id in received:
        g.db.execute(
            "UPDATE lines SET pairs_recv=?, discrepancy_note=? WHERE id=?",
            (recv, note, line_id))

    g.db.execute("UPDATE documents SET status='accepted', accepted_at=? WHERE id=?",
                 (db.now_str(), doc_id))
    g.db.commit()
    return redirect(url_for("doc_view", doc_id=doc_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/transfer/<int:doc_id>/delete", methods=["POST"])
@login_required
def doc_delete(doc_id):
    role = session["role"]
    d = g.db.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not d:
        abort(404)
    creator, _ = _doc_dir(d["kind"])
    # Отправленный или принятый документ удаляет только директор
    if role != "director":
        abort(403)
    _audit_snapshot("doc_delete_snapshot", str(doc_id),
                    {"document": dict(d), "lines": [dict(x) for x in _load_lines(doc_id)]})
    g.db.execute("DELETE FROM lines WHERE document_id=?", (doc_id,))
    g.db.execute("DELETE FROM documents WHERE id=?", (doc_id,))
    g.db.commit()
    flash(f"Заявка №{doc_id} удалена.")
    return redirect(url_for("documents"))


# ---------- Справочники ----------
@app.route("/<any(sklad,proizv,director):role_url>/refs")
@login_required
def refs():
    show_arch = request.args.get("arch") == "1"

    def load(table):
        q = f"SELECT * FROM {table}"
        if not show_arch:
            q += " WHERE archived=0"
        q += " ORDER BY " + ("number+0, number" if table == "workers" else "name")
        return g.db.execute(q).fetchall()

    return render_template(
        "refs.html", show_arch=show_arch,
        customers=load("customers"), models=load("models"),
        workers=load("workers"), can_edit=can_edit_refs(),
    )


def _ref_guard(table):
    if not can_edit_refs():
        abort(403)
    if table not in ("customers", "models", "workers"):
        abort(404)


@app.route("/<any(sklad,proizv,director):role_url>/refs/<table>/add", methods=["POST"])
@login_required
def ref_add(table):
    _ref_guard(table)
    if table == "workers":
        number = (request.form.get("number") or "").strip()
        name = (request.form.get("name") or "").strip()
        if number and name:
            g.db.execute("INSERT INTO workers (number, name) VALUES (?, ?)",
                         (number, name))
    else:
        name = (request.form.get("name") or "").strip()
        if name:
            g.db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    g.db.commit()
    return redirect(url_for("refs", arch=request.args.get("arch"), _anchor="refs-" + table))


@app.route("/<any(sklad,proizv,director):role_url>/refs/<table>/quick_add", methods=["POST"])
@login_required
def ref_quick_add(table):
    """Быстрое добавление заказчика/модели прямо из документа (склад/директор)."""
    if session.get("role") not in ("sklad", "director"):
        return {"ok": False, "error": "нет прав"}, 403
    if table not in ("customers", "models"):
        return {"ok": False, "error": "неизвестный справочник"}, 404
    name = (request.form.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "Введите название"}, 400
    row = g.db.execute(
        f"SELECT id FROM {table} WHERE lower(name)=lower(?) AND archived=0",
        (name,)).fetchone()
    if row:
        return {"ok": True, "id": row["id"], "name": name, "existed": True}
    cur = g.db.execute(f"INSERT INTO {table} (name) VALUES (?)", (name,))
    g.db.commit()
    return {"ok": True, "id": cur.lastrowid, "name": name, "existed": False}


@app.route("/<any(sklad,proizv,director):role_url>/refs/<table>/<int:rid>/edit", methods=["POST"])
@login_required
def ref_edit(table, rid):
    _ref_guard(table)
    if table == "workers":
        number = (request.form.get("number") or "").strip()
        name = (request.form.get("name") or "").strip()
        if number and name:
            g.db.execute("UPDATE workers SET number=?, name=? WHERE id=?",
                         (number, name, rid))
    else:
        name = (request.form.get("name") or "").strip()
        if name:
            g.db.execute(f"UPDATE {table} SET name=? WHERE id=?", (name, rid))
    g.db.commit()
    return redirect(url_for("refs", arch=request.args.get("arch"), _anchor="refs-" + table))


@app.route("/<any(sklad,proizv,director):role_url>/refs/<table>/<int:rid>/arch", methods=["POST"])
@login_required
def ref_arch(table, rid):
    _ref_guard(table)
    val = 0 if request.form.get("restore") else 1
    g.db.execute(f"UPDATE {table} SET archived=? WHERE id=?", (val, rid))
    g.db.commit()
    return redirect(url_for("refs", arch=request.args.get("arch"), _anchor="refs-" + table))


# ---------- Расхождения ----------
@app.route("/<any(sklad,proizv,director):role_url>/discrepancies")
@login_required
def discrepancies():
    if not can_see_discrepancies():
        abort(403)

    # Отдано в производство (A+B, принятые) и вернулось (RETURN, принятые), по моделям
    out = g.db.execute(
        """SELECT m.name AS model, SUM(l.pairs_recv) AS pairs
           FROM lines l
           JOIN documents d ON d.id = l.document_id
           JOIN models m ON m.id = l.model_id
           WHERE d.kind='OUT' AND d.status='accepted'
             AND l.pairs_recv IS NOT NULL
           GROUP BY m.id""").fetchall()
    back = g.db.execute(
        """SELECT m.name AS model, SUM(l.pairs_recv) AS pairs
           FROM lines l
           JOIN documents d ON d.id = l.document_id
           JOIN models m ON m.id = l.model_id
           WHERE d.kind='RETURN' AND d.status='accepted'
             AND l.pairs_recv IS NOT NULL
           GROUP BY m.id""").fetchall()

    agg = {}
    for r in out:
        agg.setdefault(r["model"], {"out": 0, "back": 0})["out"] += r["pairs"] or 0
    for r in back:
        agg.setdefault(r["model"], {"out": 0, "back": 0})["back"] += r["pairs"] or 0

    report = []
    total_diff = 0
    for model, v in agg.items():
        diff = v["out"] - v["back"]
        total_diff += diff
        report.append(dict(model=model, out=v["out"], back=v["back"], diff=diff))
    report.sort(key=lambda x: (-x["diff"], x["model"]))

    # Документы с расхождениями при приёмке (факт != отдано)
    docs = g.db.execute(
        "SELECT * FROM documents WHERE status='accepted' ORDER BY id DESC").fetchall()
    doc_discr = []
    for d in docs:
        bad = []
        for l in _load_lines(d["id"]):
            if l["pairs_recv"] is not None and l["pairs_recv"] != l["pairs_sent"]:
                bad.append(l)
        if bad:
            doc_discr.append(dict(d=d, bad=bad))

    return render_template("discrepancies.html", report=report,
                           total_diff=total_diff, doc_discr=doc_discr)


# ---------- Приёмка ----------
@app.route("/<any(sklad,proizv,director):role_url>/acceptance")
@login_required
def acceptance():
    role = session["role"]
    if role not in ("sklad", "proizv"):
        abort(403)
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


# ---------- Обзор директора ----------
@app.route("/<any(sklad,proizv,director):role_url>/overview")
@login_required
def overview():
    if session["role"] != "director":
        abort(403)

    def sum_lines(where, params, field):
        q = (f"SELECT COALESCE(SUM(l.{field}),0) s FROM lines l "
             f"JOIN documents d ON d.id=l.document_id WHERE {where}")
        return g.db.execute(q, params).fetchone()["s"]

    # В пути
    to_prod = sum_lines("d.kind='OUT' AND d.status='sent'", [], "pairs_sent")
    to_sklad = sum_lines("d.kind='RETURN' AND d.status='sent'", [], "pairs_sent")

    # По моделям: принято производством (A/B accepted recv) и возвращено (RETURN accepted recv)
    prod_in = g.db.execute(
        """SELECT m.id mid, m.name model, COALESCE(SUM(l.pairs_recv),0) p
           FROM lines l JOIN documents d ON d.id=l.document_id
           JOIN models m ON m.id=l.model_id
           WHERE d.kind='OUT' AND d.status='accepted' AND l.pairs_recv IS NOT NULL
           GROUP BY m.id""").fetchall()
    prod_out = g.db.execute(
        """SELECT m.id mid, m.name model, COALESCE(SUM(l.pairs_recv),0) p
           FROM lines l JOIN documents d ON d.id=l.document_id
           JOIN models m ON m.id=l.model_id
           WHERE d.kind='RETURN' AND d.status='accepted' AND l.pairs_recv IS NOT NULL
           GROUP BY m.id""").fetchall()
    # В пути по моделям
    sent_prod = g.db.execute(
        """SELECT m.id mid, m.name model, COALESCE(SUM(l.pairs_sent),0) p
           FROM lines l JOIN documents d ON d.id=l.document_id
           JOIN models m ON m.id=l.model_id
           WHERE d.kind='OUT' AND d.status='sent'
           GROUP BY m.id""").fetchall()
    sent_back = g.db.execute(
        """SELECT m.id mid, m.name model, COALESCE(SUM(l.pairs_sent),0) p
           FROM lines l JOIN documents d ON d.id=l.document_id
           JOIN models m ON m.id=l.model_id
           WHERE d.kind='RETURN' AND d.status='sent'
           GROUP BY m.id""").fetchall()

    agg = {}
    def acc(rows, key):
        for r in rows:
            e = agg.setdefault(r["model"], {"at_prod": 0, "to_prod": 0, "to_sklad": 0})
            if key == "in":
                e["at_prod"] += r["p"]
            elif key == "out":
                e["at_prod"] -= r["p"]
            elif key == "sent_prod":
                e["to_prod"] += r["p"]
            elif key == "sent_back":
                e["to_sklad"] += r["p"]
    acc(prod_in, "in"); acc(prod_out, "out")
    acc(sent_prod, "sent_prod"); acc(sent_back, "sent_back")

    by_model = []
    at_prod_total = 0
    for model, v in agg.items():
        at_prod_total += v["at_prod"]
        if v["at_prod"] or v["to_prod"] or v["to_sklad"]:
            by_model.append(dict(model=model, **v))
    by_model.sort(key=lambda x: (-x["at_prod"], x["model"]))

    # Последние документы
    recent = []
    for d in g.db.execute(
            "SELECT * FROM documents WHERE EXISTS "
            "(SELECT 1 FROM lines WHERE document_id=documents.id) "
            "ORDER BY id DESC LIMIT 8").fetchall():
        sent, recv, diff, has_recv = _doc_totals(d["id"])
        recent.append(dict(d=d, sent=sent, recv=recv, diff=diff, has_recv=has_recv))

    # Счётчики документов
    cnt = {"sent": 0, "accepted": 0}
    for r in g.db.execute(
            "SELECT status, COUNT(*) c FROM documents WHERE EXISTS "
            "(SELECT 1 FROM lines WHERE document_id=documents.id) GROUP BY status"):
        cnt[r["status"]] = r["c"]

    return render_template("overview.html",
                           to_prod=to_prod, to_sklad=to_sklad,
                           at_prod_total=at_prod_total, by_model=by_model,
                           recent=recent, cnt=cnt)


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


def _parse_request_rows():
    """Собрать позиции новой заявки из параллельных полей формы."""
    f = request.form
    names, qtys, units = f.getlist("item"), f.getlist("qty"), f.getlist("unit")
    customs, notes = f.getlist("unit_custom"), f.getlist("item_note")
    rows = []
    for n in range(len(names)):
        def at(lst):
            return (lst[n] if n < len(lst) else "").strip()
        rows.append({"item": at(names), "qty": at(qtys), "unit": at(units) or "sht",
                     "unit_custom": at(customs)[:20], "item_note": at(notes),
                     "urgent": bool(f.get("urgent_%d" % (n + 1)))})
    return rows


REQUEST_BLANK_ROW = {"item": "", "qty": "", "unit": "sht", "unit_custom": "", "item_note": "", "urgent": False}


def _request_page(rows, note, edit=None):
    """Одна и та же страница для создания и для правки заявки."""
    return render_template(
        "request_new.html", rows=rows or [REQUEST_BLANK_ROW], blank=REQUEST_BLANK_ROW, note=note,
        edit_id=edit["id"] if edit else None,
        edit_until=_clock(edit["created_ts"] + EDIT_WINDOW) if edit else None)


def _read_request_form():
    """Разобрать и проверить форму заявки: (строки формы, примечание, позиции, ошибка)."""
    rows = _parse_request_rows()
    note = (request.form.get("note") or "").strip()
    parsed = []
    for n, row in enumerate(rows, 1):
        if not (row["item"] or row["qty"] or row["item_note"]):
            continue  # полностью пустая карточка — пропускаем
        if not row["item"]:
            return rows, note, None, "Позиция %d: укажите, что нужно." % n
        qty = None
        if row["qty"]:
            try:
                qty = int(row["qty"])
            except ValueError:
                qty = 0
            if qty <= 0:
                return rows, note, None, "Позиция %d: количество должно быть больше нуля." % n
        unit = None
        if qty is not None:
            if row["unit"] == "other":
                unit = row["unit_custom"] or None
            elif row["unit"] in UNITS:
                unit = row["unit"]
            else:
                return rows, note, None, "Позиция %d: неизвестная единица измерения." % n
        parsed.append((row["item"], qty, unit, row["item_note"] or None, 1 if row["urgent"] else 0))
    if not parsed:
        return rows, note, None, "Добавьте хотя бы одну позицию заявки."
    return rows, note, parsed, None


def _insert_request_items(req_id, parsed):
    for item, qty, unit, item_note, item_urgent in parsed:
        g.db.execute(
            "INSERT INTO request_items (request_id, item, qty, note, unit, urgent) VALUES (?, ?, ?, ?, ?, ?)",
            (req_id, item, qty, item_note, unit, item_urgent))


@app.route("/<any(sklad,proizv,director):role_url>/requests/new", methods=["GET", "POST"])
@login_required
def request_new():
    if session["role"] != "proizv":
        abort(403)
    if request.method == "GET":
        return _request_page([REQUEST_BLANK_ROW], "")
    rows, note, parsed, error = _read_request_form()
    if error:
        flash(error)
        return _request_page(rows, note)
    now = db.now_str()
    cur = g.db.execute(
        "INSERT INTO requests (status, urgent, created_role, created_at, created_ts, sent_at, note) "
        "VALUES ('open', ?, ?, ?, ?, ?, ?)",
        (1 if any(p[4] for p in parsed) else 0, "proizv", now, int(time.time()), now, note))  # срочность заявки = есть срочная позиция
    _insert_request_items(cur.lastrowid, parsed)
    g.db.commit()
    flash("Заявка №%d создана. Склад увидит её через %s, до этого её можно изменить."
          % (cur.lastrowid, _window_label()))
    return redirect(url_for("documents"))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/edit", methods=["GET", "POST"])
@login_required
def request_edit(req_id):
    """Правка заявки производством: та же страница, что и при создании, но только первые 15 минут."""
    if session["role"] != "proizv":
        abort(403)
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        abort(404)
    if not _req_editable(r):
        flash("Заявку №%d уже нельзя изменить: прошло больше %s." % (req_id, _window_label()))
        return redirect(url_for("documents"))
    if request.method == "GET":
        rows = []
        for i in _load_req_items(req_id):
            unit = i["unit"] or "sht"
            known = unit in UNITS
            rows.append({"item": i["item"], "qty": i["qty"] if i["qty"] is not None else "",
                         "unit": unit if known else "other", "unit_custom": "" if known else unit,
                         "item_note": i["note"] or "", "urgent": bool(i["urgent"])})
        return _request_page(rows, r["note"] or "", edit=r)
    rows, note, parsed, error = _read_request_form()
    if error:
        flash(error)
        return _request_page(rows, note, edit=r)
    _audit_snapshot("request_edit_snapshot", str(req_id),
                    {"request": dict(r), "items": [dict(x) for x in _load_req_items(req_id)]})
    g.db.execute("DELETE FROM request_items WHERE request_id=?", (req_id,))
    _insert_request_items(req_id, parsed)
    g.db.execute("UPDATE requests SET urgent=?, note=? WHERE id=?",
                 (1 if any(p[4] for p in parsed) else 0, note, req_id))
    g.db.commit()
    flash("Заявка №%d изменена." % req_id)
    return redirect(url_for("documents"))


def _req_progress_sklad(req_id):
    if session.get("role") != "sklad":
        abort(403)
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "progress":
        abort(404)
    return r


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/collect", methods=["POST"])
@login_required
def request_collect(req_id):
    """Кладовщик сохраняет сборку: галочка «положил», кол-во, тип по каждой позиции."""
    _req_progress_sklad(req_id)
    for it in _load_req_items(req_id):
        if it["source"] != "req":
            continue
        iid = it["id"]
        placed = 1 if request.form.get(f"placed_{iid}") else 0
        raw = (request.form.get(f"col_{iid}", "") or "").strip()
        if raw == "":
            # положил, но кол-во не вписал — берём заявленное
            val = it["qty"] if placed else it["collected"]
        else:
            try:
                val = int(raw)
                if val < 0:
                    val = 0
            except ValueError:
                val = it["collected"]
        itype = request.form.get(f"type_{iid}") or None
        if itype not in ITEM_TYPES:
            itype = it["item_type"]
        g.db.execute(
            "UPDATE request_items SET collected=?, placed=?, item_type=? WHERE id=?",
            (val, placed, itype, iid))
    g.db.commit()
    # шаг 1 → шаг 2 (что положил)
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/pair/add", methods=["POST"])
@login_required
def request_pair_add(req_id):
    """Кладовщик добавляет пары с операцией на модель."""
    _req_progress_sklad(req_id)
    try:
        cid = int(request.form["customer_id"])
        mid = int(request.form["model_id"])
        pairs = int(request.form["pairs"])
    except (KeyError, ValueError):
        flash("Заполните заказчика, модель и число пар.")
        return redirect(_req_row(req_id))
    if pairs <= 0:
        flash("Число пар должно быть больше нуля.")
        return redirect(_req_row(req_id))
    if not g.db.execute("SELECT 1 FROM customers WHERE id=? AND archived=0", (cid,)).fetchone() or \
       not g.db.execute("SELECT 1 FROM models WHERE id=? AND archived=0", (mid,)).fetchone():
        flash("Выберите действующих заказчика и модель.")
        return redirect(_req_row(req_id))
    operations = _selected_operations()
    if not operations:
        flash("Выберите хотя бы одну операцию.")
        return redirect(_req_row(req_id))
    g.db.execute(
        "INSERT INTO request_items (request_id, item, collected, placed, source, line_kind, "
        "customer_id, model_id, operation) VALUES (?, '', ?, 1, 'sklad', 'pair', ?, ?, ?)",
        (req_id, pairs, cid, mid, json.dumps(operations)))
    g.db.commit()
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/material/add", methods=["POST"])
@login_required
def request_material_add(req_id):
    """Кладовщик добавляет в «что положил» материал: название + кол-во."""
    _req_progress_sklad(req_id)
    item = (request.form.get("item") or "").strip()
    if not item:
        flash("Укажите материал.")
        return redirect(_req_row(req_id))
    try:
        qty = int(request.form.get("qty"))
        if qty <= 0:
            qty = None
    except (TypeError, ValueError):
        qty = None
    unit = request.form.get("unit")
    if unit not in UNITS:
        flash("Выберите единицу измерения.")
        return redirect(_req_row(req_id))
    g.db.execute(
        "INSERT INTO request_items (request_id, item, qty, collected, placed, item_type, source, line_kind, unit) "
        "VALUES (?, ?, ?, ?, 1, 'material', 'sklad', 'material', ?)",
        (req_id, item, qty, qty, unit))
    g.db.commit()
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/placed/<int:item_id>/del", methods=["POST"])
@login_required
def request_placed_del(req_id, item_id):
    r = _req_progress_sklad(req_id)
    g.db.execute("DELETE FROM request_items WHERE id=? AND request_id=? AND source='sklad'",
                 (item_id, req_id))
    if r["created_role"] == "sklad" and not g.db.execute(
            "SELECT 1 FROM request_items WHERE request_id=?", (req_id,)).fetchone():
        g.db.execute("DELETE FROM requests WHERE id=?", (req_id,))
        g.db.commit()
        return redirect(url_for("documents"))
    g.db.commit()
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/take", methods=["POST"])
@login_required
def request_take(req_id):
    if session["role"] != "sklad":
        abort(403)
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] != "open" or _hidden_from_sklad(r):
        abort(404)
    g.db.execute("UPDATE requests SET status='progress', taken_at=? WHERE id=?",
                 (db.now_str(), req_id))
    g.db.commit()
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/ship", methods=["POST"])
@login_required
def request_ship(req_id):
    """Склад отправляет собранную заявку на производство."""
    if session["role"] != "sklad":
        abort(403)
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r or r["status"] not in ("progress", "done"):
        abort(404)
    n = g.db.execute(
        "SELECT COUNT(*) FROM request_items WHERE request_id=? AND "
        "((source='sklad' AND placed=1 AND COALESCE(collected, 0)>0) OR "
        "(source='req' AND placed=1 AND COALESCE(collected, 0)>0))", (req_id,),
    ).fetchone()[0]
    if not n:
        flash("Добавьте хотя бы одну позицию с положительным количеством.")
        return redirect(_req_row(req_id))
    ship_note = (request.form.get("ship_note") or "").strip() or None
    g.db.execute("UPDATE requests SET status='shipped', done_at=?, ship_note=? WHERE id=?",
                 (db.now_str(), ship_note, req_id))
    g.db.commit()
    flash("Заявка отправлена на производство.")
    return redirect(_req_row(req_id))


@app.route("/<any(sklad,proizv,director):role_url>/requests/<int:req_id>/delete", methods=["POST"])
@login_required
def request_delete(req_id):
    role = session["role"]
    r = g.db.execute("SELECT * FROM requests WHERE id=?", (req_id,)).fetchone()
    if not r:
        abort(404)
    allowed = (role == "director") or (role == "proizv" and r["status"] in ("open", "progress"))
    if not allowed:
        abort(403)
    _audit_snapshot("request_delete_snapshot", str(req_id),
                    {"request": dict(r), "items": [dict(x) for x in _load_req_items(req_id)]})
    g.db.execute("DELETE FROM request_items WHERE request_id=?", (req_id,))
    g.db.execute("DELETE FROM requests WHERE id=?", (req_id,))
    g.db.commit()
    flash(f"Заявка №{req_id} удалена.")
    return redirect(url_for("documents"))


# ---------- Сдельная зарплата ----------
def _parse_kopeks(raw):
    try:
        value = Decimal((raw or "").strip().replace(",", "."))
        kopeks = value * 100
        if not value.is_finite() or value < 0 or value > 10_000_000 or kopeks != kopeks.to_integral_value():
            raise ValueError
        return int(kopeks)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Ставка должна быть неотрицательной суммой с точностью до копейки.")


@app.template_filter("rubles")
def _rubles(kopeks):
    kopeks = int(kopeks or 0)
    return f"{kopeks // 100}.{kopeks % 100:02d}"


@app.route("/<any(sklad,proizv,director):role_url>/payroll")
@login_required
def payroll():
    role = session["role"]
    if role not in ("proizv", "director"):
        abort(403)
    today = db.datetime.now(db.TZ).date()
    start = request.args.get("start", today.replace(day=1).isoformat())
    end = request.args.get("end", today.isoformat())
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
        if start > end:
            raise ValueError
    except ValueError:
        abort(400, description="Неверный период отчёта.")

    workers = g.db.execute("SELECT * FROM workers WHERE archived=0 ORDER BY number+0, number").fetchall()
    models = g.db.execute("SELECT * FROM models WHERE archived=0 ORDER BY name").fetchall()
    operations = g.db.execute("SELECT * FROM operations ORDER BY ord, id").fetchall()
    rates = g.db.execute("SELECT * FROM prices").fetchall()
    rate_map = {(r["model_id"], r["operation_id"]): r["rate_kopeks"] for r in rates}
    records = g.db.execute(
        """SELECT wr.*, w.number AS worker_number, w.name AS worker_name,
                  m.name AS model_name, o.name AS operation_name
           FROM work_records wr
           JOIN workers w ON w.id=wr.worker_id
           JOIN models m ON m.id=wr.model_id
           JOIN operations o ON o.id=wr.operation_id
           WHERE wr.work_date BETWEEN ? AND ?
           ORDER BY wr.work_date DESC, wr.id DESC""", (start, end),
    ).fetchall()
    totals = {}
    for r in records:
        item = totals.setdefault(r["worker_id"], dict(number=r["worker_number"],
                              name=r["worker_name"], pairs=0, amount_kopeks=0))
        item["pairs"] += r["pairs"]
        item["amount_kopeks"] += r["amount_kopeks"]
    return render_template("payroll.html", workers=workers, models=models,
                           operations=operations, rate_map=rate_map,
                           records=records, totals=sorted(totals.values(), key=lambda x: x["number"]),
                           total_kopeks=sum(x["amount_kopeks"] for x in records),
                           start=start, end=end, today=today.isoformat())


@app.route("/<any(sklad,proizv,director):role_url>/payroll/rates", methods=["POST"])
@login_required
def payroll_rate_set():
    if session["role"] != "director":
        abort(403)
    try:
        model_id = int(request.form["model_id"])
        operation_id = int(request.form["operation_id"])
        kopeks = _parse_kopeks(request.form.get("rate"))
    except (KeyError, ValueError) as exc:
        flash(str(exc) or "Выберите модель, операцию и ставку.")
        return redirect(url_for("payroll"))
    if not g.db.execute("SELECT 1 FROM models WHERE id=? AND archived=0", (model_id,)).fetchone() or \
       not g.db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone():
        abort(400)
    g.db.execute(
        """INSERT INTO prices(model_id,operation_id,rate,rate_kopeks) VALUES (?,?,?,?)
           ON CONFLICT(model_id,operation_id) DO UPDATE SET
             rate=excluded.rate, rate_kopeks=excluded.rate_kopeks""",
        (model_id, operation_id, kopeks / 100, kopeks),
    )
    g.db.commit()
    flash("Ставка сохранена. Уже внесённые начисления не изменились.")
    return redirect(url_for("payroll"))


@app.route("/<any(sklad,proizv,director):role_url>/payroll/records", methods=["POST"])
@login_required
def payroll_record_add():
    if session["role"] not in ("proizv", "director"):
        abort(403)
    try:
        worker_id = int(request.form["worker_id"])
        model_id = int(request.form["model_id"])
        operation_id = int(request.form["operation_id"])
        pairs = int(request.form["pairs"])
        work_date = date.fromisoformat(request.form["work_date"]).isoformat()
        if pairs <= 0 or pairs > 1_000_000:
            raise ValueError
    except (KeyError, ValueError):
        flash("Укажите работника, модель, операцию, дату и положительное число пар.")
        return redirect(url_for("payroll"))
    if not g.db.execute("SELECT 1 FROM workers WHERE id=? AND archived=0", (worker_id,)).fetchone() or \
       not g.db.execute("SELECT 1 FROM models WHERE id=? AND archived=0", (model_id,)).fetchone() or \
       not g.db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone():
        abort(400)
    rate = g.db.execute("SELECT rate_kopeks FROM prices WHERE model_id=? AND operation_id=?",
                        (model_id, operation_id)).fetchone()
    if not rate or rate["rate_kopeks"] <= 0:
        flash("Для модели и операции сначала нужно задать положительную ставку.")
        return redirect(url_for("payroll"))
    kopeks = rate["rate_kopeks"]
    g.db.execute(
        """INSERT INTO work_records(worker_id,operation_id,model_id,pairs,created_at,
                                    work_date,rate_kopeks,amount_kopeks)
           VALUES (?,?,?,?,?,?,?,?)""",
        (worker_id, operation_id, model_id, pairs, db.now_str(), work_date,
         kopeks, pairs * kopeks),
    )
    g.db.commit()
    flash("Выработка внесена.")
    return redirect(url_for("payroll", start=work_date[:7] + "-01", end=work_date))


@app.route("/<any(sklad,proizv,director):role_url>/payroll/records/<int:record_id>/delete", methods=["POST"])
@login_required
def payroll_record_delete(record_id):
    if session["role"] != "director":
        abort(403)
    record = g.db.execute("SELECT * FROM work_records WHERE id=?", (record_id,)).fetchone()
    if not record:
        abort(404)
    _audit_snapshot("payroll_record_delete_snapshot", str(record_id), dict(record))
    g.db.execute("DELETE FROM work_records WHERE id=?", (record_id,))
    g.db.commit()
    flash("Запись выработки удалена; копия сохранена в журнале изменений.")
    return redirect(url_for("payroll"))


# ---------- Обновление системы ----------
UPDATE_LOG = os.path.join(BASE_DIR, "updates.log")
SKIP_FILES = {"jail.db", "jail.db-wal", "jail.db-shm", "updates.log", "_upload.zip"}


def _apply_update(zip_path):
    """Распаковать zip с изменёнными файлами поверх приложения. БД не трогаем."""
    import zipfile
    import shutil
    base = os.path.abspath(BASE_DIR)
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
        for n in names:
            rel = n.replace("\\", "/")
            if strip and rel.startswith(strip):
                rel = rel[len(strip):]
            if not rel or os.path.basename(rel) in SKIP_FILES:
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
    return applied


def _try_reload():
    """Тронуть WSGI-файл, чтобы PythonAnywhere перезагрузил приложение."""
    import glob
    for wsgi in glob.glob("/var/www/*_wsgi.py"):
        try:
            os.utime(wsgi, None)
            return wsgi
        except OSError:
            continue
    return None


def _current_version():
    try:
        with open(os.path.join(BASE_DIR, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return VERSION


@app.route("/<any(sklad,proizv,director):role_url>/update", methods=["GET", "POST"])
def update():
    if not ENABLE_WEB_UPDATES:
        abort(404)
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
                applied = _apply_update(tmp)
                with open(UPDATE_LOG, "a", encoding="utf-8") as lg:
                    lg.write(f"{db.now_str()} · {f.filename} · {len(applied)} файл(ов)\n")
                reloaded = _try_reload()
            except Exception as e:
                err = "Ошибка обновления: " + str(e)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        # Страница-результат — самостоятельная, не наследует base.html:
        # после обновления шаблоны на диске новые, а код в памяти ещё старый
        # (до Reload), поэтому base.html может ссылаться на ещё не загруженные
        # маршруты. Отдельная страница застрахована от этого.
        return render_template("update_result.html", cur=_current_version(),
                               applied=applied, reloaded=reloaded, err=err)

    history = []
    if os.path.exists(UPDATE_LOG):
        with open(UPDATE_LOG, encoding="utf-8") as lg:
            history = [l.strip() for l in lg if l.strip()][-10:][::-1]

    return render_template("update.html", cur=_current_version(),
                           applied=applied, reloaded=reloaded, err=err,
                           history=history)


@app.route("/<path:rest>", methods=["GET", "POST"])
def legacy_without_role(rest):
    """Адрес без роли (старые ссылки) -> тот же адрес с ролью из сессии."""
    if request.method != "GET" or rest.split("/")[0] in ROLES:
        abort(404)
    return _redirect_with_role(rest)


if __name__ == "__main__":
    app.run(debug=True, port=5001)
