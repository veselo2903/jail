"""Checks the warehouse supply flow and existing safeguards."""
import json
import os
import pathlib
import secrets
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

with tempfile.TemporaryDirectory(prefix="jail-tests-") as temporary:
    os.environ["JAIL_DB_PATH"] = str(pathlib.Path(temporary) / "jail.db")
    os.environ["JAIL_SECRET_KEY"] = "local-tests-only-secret"
    from jail.app import app, db

    class RoleClient:
        ROLES = ("sklad", "proizv", "director")

        def __init__(self, inner):
            self.inner, self.role = inner, None

        def __getattr__(self, name):
            return getattr(self.inner, name)

        def _path(self, path):
            if self.role and path != "/" and not path.startswith(("/login", "/logout", "/docs")) \
                    and path.split("/")[1] not in self.ROLES:
                return "/%s%s" % (self.role, path)
            return path

        def get(self, path, **kw):
            response = self.inner.get(self._path(path), **kw)
            parts = path.split("?")[0].split("/")
            if len(parts) == 3 and parts[1] == "login" and parts[2] in self.ROLES:
                self.role = parts[2]
            return response

        def post(self, path, **kw):
            return self.inner.post(self._path(path), **kw)

    client = RoleClient(app.test_client())
    assert "тестовый режим" not in client.get("/login").text
    with client.session_transaction() as state:
        token = state["csrf_token"]

    def post(path, data=None):
        fields = dict(data or {})
        if path.endswith("/ship") or path.endswith("/supply/new"):
            fields.setdefault("send_token", secrets.token_urlsafe(24))
        return client.post(path, data={"csrf_token": token, **fields})

    def one(sql, args=()):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute(sql, args).fetchone()

    def count(table):
        return one("SELECT COUNT(*) FROM " + table)[0]

    assert client.get("/overview").status_code == 302
    assert client.get("/login/sklad").status_code == 302
    assert client.inner.get("/proizv/requests").status_code == 403
    empty = client.get("/requests")
    assert empty.status_code == 200
    assert "Создать поставку" in empty.text
    assert 'class="tag sent"' not in empty.text
    assert client.get("/requests/supply/new").status_code == 200
    assert "Начали собирать" not in empty.text
    assert "Сохранить сборку" not in empty.text
    assert client.get("/requests/collect").status_code == 302
    assert client.get("/requests/collect/blank").status_code == 302
    assert client.get("/requests/transfer/new").status_code == 302
    assert client.inner.get("/docs").location.endswith("/sklad/requests")
    assert client.post("/requests/supply/new").status_code == 400  # CSRF
    assert post("/requests/supply/new").status_code == 302
    assert count("shipments") == 0
    assert post("/requests/supply/new", {"material_name": ["Кожа"],
                                   "material_qty": ["0"], "material_unit": ["m2"]}).status_code == 302
    assert count("shipments") == 0
    assert client.get("/login/proizv").status_code == 302
    assert post("/requests/supply/new", {"material_name": ["Кожа"],
                                   "material_qty": ["1"], "material_unit": ["m2"]}).status_code == 403

    # Производство создаёт заявку; склад видит её после окна правки.
    assert post("/requests/new", {"note": "пусто"}).status_code == 200
    assert count("requests") == 0
    created = post("/requests/new", {
        "item": ["Кожа", "Нитки"], "qty": ["10", "5"],
        "unit": ["sht", "sht"], "unit_custom": ["", ""],
        "item_note": ["чёрная", ""], "urgent_1": "1", "note": "на партию"})
    assert created.status_code == 302
    req_id = one("SELECT id FROM requests")[0]
    item_ids = [x[0] for x in sqlite3.connect(db.DB_PATH).execute(
        "SELECT id FROM request_items WHERE request_id=? ORDER BY id", (req_id,))]
    assert client.get(f"/requests/{req_id}/edit").status_code == 200
    assert client.get("/login/sklad").status_code == 302
    assert "Кожа" not in client.get("/requests/supply/new").text
    assert post("/requests/supply/new", {f"ship_{item_ids[0]}": "1",
                                            f"qty_{item_ids[0]}": "4"}).status_code == 302
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE requests SET created_ts=created_ts-100 WHERE id=?", (req_id,))
    page = client.get("/requests")
    assert page.status_code == 200 and "Кожа" in page.text and "Создать поставку" in page.text
    assert "ship_%s" % item_ids[0] not in page.text
    supply_page = client.get("/requests/supply/new").text
    assert "Запрошено 10" in supply_page
    assert '<details class="sklad-incoming-spoiler">' in supply_page
    assert 'class="btn grey sm sklad-back-to-requests"' in supply_page

    # Первая отправка частичная. Остаток остаётся виден; повторная отправка закрывает заявку.
    partial = post("/requests/supply/new", {
        f"ship_{item_ids[0]}": "1", f"qty_{item_ids[0]}": "4"})
    assert partial.status_code == 302 and "sh-" in partial.location
    assert one("SELECT status FROM requests WHERE id=?", (req_id,))[0] == "progress"
    assert count("shipments") == 1
    assert one("SELECT qty FROM shipment_items WHERE request_item_id=?", (item_ids[0],))[0] == 4
    assert "осталось 6" in client.get("/requests/supply/new").text
    assert client.get("/login/proizv").status_code == 302
    assert post(f"/requests/{req_id}/delete").status_code == 403
    assert "Отправлена частично" in client.get("/requests").text
    assert "отправлено 4" in client.get("/requests").text
    assert post("/requests/new", {"item": ["Подкладка"], "qty": ["2"],
                                  "unit": ["sht"], "unit_custom": [""],
                                  "item_note": [""]}).status_code == 302
    second_id = one("SELECT MAX(id) FROM requests")[0]
    second_item = one("SELECT id FROM request_items WHERE request_id=?", (second_id,))[0]
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("UPDATE requests SET created_ts=created_ts-100 WHERE id=?", (second_id,))
    assert client.get("/login/sklad").status_code == 302
    complete = post("/requests/supply/new", {
        f"ship_{item_ids[0]}": "1", f"qty_{item_ids[0]}": "6",
        f"ship_{item_ids[1]}": "1", f"qty_{item_ids[1]}": "5",
        f"ship_{second_item}": "1", f"qty_{second_item}": "2",
        "material_name": ["Клей"], "material_qty": ["2"], "material_unit": ["sht"],
        "ship_note": "добавили клей"})
    assert complete.status_code == 302
    assert one("SELECT status FROM requests WHERE id=?", (req_id,))[0] == "shipped"
    assert one("SELECT status FROM requests WHERE id=?", (second_id,))[0] == "shipped"
    assert count("shipments") == 2
    assert one("SELECT COUNT(DISTINCT request_number) FROM shipment_items WHERE shipment_id=2")[0] == 2
    assert one("SELECT SUM(qty) FROM shipment_items WHERE request_item_id=?", (item_ids[0],))[0] == 10
    assert post("/requests/supply/new", {
        "send_token": one("SELECT client_token FROM shipments ORDER BY id DESC")[0],
        f"ship_{item_ids[1]}": "1", f"qty_{item_ids[1]}": "5"}).status_code == 302
    assert count("shipments") == 2  # повторный запрос не дублирует отгрузку
    assert "Кожа" in client.get("/requests").text

    # Самостоятельная поставка: материалы каждой партии связаны с её моделью и операциями.
    sent = post("/requests/supply/new", {
        "material_name": ["Подошва"], "material_qty": ["7"], "material_unit": ["pary"],
        "party_id": ["0", "1"],
        "party_customer_0": "1", "party_model_0": "1", "party_qty_0": "3",
        "party_operations_0": ["op1", "op7"],
        "party_material_name_0": ["Кожа", "Нитка"],
        "party_material_qty_0": ["4", "2"],
        "party_material_unit_0": ["m2", "sht"],
        "party_customer_1": "1", "party_model_1": "2", "party_qty_1": "5",
        "party_operations_1": ["op3"],
        "party_material_name_1": ["Клей"],
        "party_material_qty_1": ["1"], "party_material_unit_1": ["kg"],
        "ship_note": "вне заявки"})
    assert sent.status_code == 302
    ship_id = one("SELECT MAX(id) FROM shipments")[0]
    assert one("SELECT request_id, note FROM shipments WHERE id=?", (ship_id,)) == (None, "вне заявки")
    assert one("SELECT COUNT(*) FROM shipment_items WHERE shipment_id=?", (ship_id,))[0] == 6
    assert one("SELECT COUNT(*) FROM shipment_items WHERE shipment_id=? AND party_item_id IS NOT NULL", (ship_id,))[0] == 3
    assert one("SELECT COUNT(DISTINCT party_item_id) FROM shipment_items WHERE shipment_id=?", (ship_id,))[0] == 2
    assert one("SELECT party_item_id FROM shipment_items WHERE shipment_id=? AND item='Подошва'", (ship_id,))[0] is None
    pair_ops = [json.loads(x[0]) for x in sqlite3.connect(db.DB_PATH).execute(
        "SELECT operation FROM shipment_items WHERE shipment_id=? AND line_kind='pair' ORDER BY id",
        (ship_id,))]
    assert pair_ops == [["op1", "op7"], ["op3"]]
    warehouse = client.get("/requests")
    assert warehouse.status_code == 200 and "Поставка №" in warehouse.text
    assert "вне заявки" in warehouse.text
    assert client.get("/login/proizv").status_code == 302
    production = client.get("/requests")
    assert production.status_code == 200
    assert "Поставка без заявки" in production.text and "Подошва" in production.text
    assert "Отправки по заявке" in production.text
    assert "Штробель сапожники" in production.text

    # Старую отправленную заявку переносим в журнал один раз.
    with sqlite3.connect(db.DB_PATH) as conn:
        old = conn.execute(
            "INSERT INTO requests(status,urgent,created_role,created_at,done_at) "
            "VALUES ('shipped',0,'proizv','25.09.2026 09:00','25.09.2026 09:15')").lastrowid
        old_item = conn.execute(
            "INSERT INTO request_items(request_id,item,qty,collected,placed) VALUES (?,?,?,?,1)",
            (old, "Нить", 2, 2)).lastrowid
        conn.row_factory = sqlite3.Row
        db._migrate_shipments(conn)
        db._migrate_shipments(conn)
        conn.commit()
    assert one("SELECT COUNT(*) FROM shipments WHERE legacy_request_id=?", (old,))[0] == 1
    assert one("SELECT qty FROM shipment_items WHERE request_item_id=?", (old_item,))[0] == 2

    # Возврат, приёмка, аудит и зарплата продолжают работать.
    ret = post("/requests/transfer/new", {
        "customer_id": ["1"], "model_id": ["1"],
        "status": ["gotovoe"], "pairs": ["2"]})
    assert ret.status_code == 302
    doc_id = one("SELECT MAX(id) FROM documents")[0]
    line_id = one("SELECT id FROM lines WHERE document_id=?", (doc_id,))[0]
    assert client.get("/login/sklad").status_code == 302
    assert post(f"/requests/transfer/{doc_id}/accept", {f"recv_{line_id}": "2"}).status_code == 302
    assert one("SELECT status FROM documents WHERE id=?", (doc_id,))[0] == "accepted"
    assert client.get("/login/director").status_code == 302
    assert post("/payroll/rates", {"model_id": "1", "operation_id": "1",
                                   "rate": "1.25"}).status_code == 302
    assert client.get("/login/proizv").status_code == 302
    assert post("/payroll/records", {"worker_id": "1", "model_id": "1",
                                     "operation_id": "1", "pairs": "10",
                                     "work_date": "2026-09-25"}).status_code == 302
    assert one("SELECT amount_kopeks FROM work_records")[0] == 1250
    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute(
                "INSERT INTO shipment_items(shipment_id,line_kind,item,qty) VALUES (9999,'material','X',1)")
            raise AssertionError("missing shipment foreign key")
        except sqlite3.IntegrityError:
            pass
    assert one("PRAGMA integrity_check")[0] == "ok"
    print("PASS: warehouse send, partial remainder, independent send, migration, roles, CSRF, acceptance, payroll, SQLite")
