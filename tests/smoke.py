"""Isolated checks for access, state transitions, and data integrity."""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

with tempfile.TemporaryDirectory(prefix="jail-tests-") as temporary:
    os.environ["JAIL_DB_PATH"] = str(pathlib.Path(temporary) / "jail.db")
    os.environ["JAIL_SECRET_KEY"] = "local-tests-only-secret"
    from jail.app import app, db

    client = app.test_client()
    assert client.get("/login").status_code == 200
    with client.session_transaction() as state:
        token = state["csrf_token"]

    def post(path, data=None):
        return client.post(path, data={"csrf_token": token, **(data or {})})

    def query(sql, args=()):
        with sqlite3.connect(db.DB_PATH) as conn:
            return conn.execute(sql, args).fetchone()

    assert client.get("/overview").status_code == 302
    assert client.get("/login/director").status_code == 302
    assert "пар сейчас на производстве" not in client.get("/overview").text
    assert "Расхождения при приёмке (0)" not in client.get("/discrepancies").text
    assert client.get("/login/sklad").status_code == 302
    assert client.get("/docs/new").status_code == 200
    assert "Здесь пока пусто" not in client.get("/docs").text
    assert "Новых заявок к сборке нет" not in client.get("/docs/collect").text
    assert "Новых документов на приёмку нет" not in client.get("/acceptance").text
    assert query("SELECT COUNT(*) FROM documents")[0] == 0
    assert client.post("/docs/new").status_code == 400
    assert post("/docs/new").status_code == 302
    assert query("SELECT COUNT(*) FROM documents")[0] == 0
    created = post("/docs/new", {"customer_id": "1", "model_id": "1", "pairs": "10"})
    assert created.status_code == 302
    doc_id = int(created.location.rsplit("/", 1)[-1])
    assert query("SELECT COUNT(*) FROM lines WHERE document_id=?", (doc_id,))[0] == 1
    first_line_id = query("SELECT id FROM lines WHERE document_id=?", (doc_id,))[0]

    assert post(f"/docs/{doc_id}/line/add", {"customer_id": "9999", "model_id": "9999", "pairs": "10"}).status_code == 302
    assert query("SELECT COUNT(*) FROM lines")[0] == 1
    assert post(f"/docs/{doc_id}/line/add", {"customer_id": "1", "model_id": "1", "pairs": "10"}).status_code == 302
    line_id = query("SELECT MAX(id) FROM lines WHERE document_id=?", (doc_id,))[0]
    assert post(f"/docs/{doc_id}/send").status_code == 302
    assert client.get("/login/proizv").status_code == 302
    assert post(f"/docs/{doc_id}/accept").status_code == 302
    assert query("SELECT status FROM documents WHERE id=?", (doc_id,))[0] == "sent"
    assert post(f"/docs/{doc_id}/accept", {f"recv_{first_line_id}": "10",
                                          f"recv_{line_id}": "9"}).status_code == 302
    assert query("SELECT status FROM documents WHERE id=?", (doc_id,))[0] == "accepted"

    with sqlite3.connect(db.DB_PATH) as conn:
        for status in ("open", "done", "shipped"):
            conn.execute("INSERT INTO requests(status,urgent,created_role,created_at) VALUES (?,0,'proizv','25.09.2026 09:00')", (status,))
    assert post("/requests/3/delete").status_code == 403
    assert query("SELECT status FROM requests WHERE id=3")[0] == "shipped"
    assert client.get("/login/sklad").status_code == 302
    assert client.get("/requests/1").status_code == 200
    assert client.get("/requests/2").status_code == 200
    assert query("SELECT status FROM requests WHERE id=1")[0] == "open"
    assert query("SELECT status FROM requests WHERE id=2")[0] == "done"
    assert client.get("/docs/collect/blank").status_code == 200
    assert post("/docs/collect/blank").status_code == 302
    assert query("SELECT COUNT(*) FROM requests")[0] == 3
    blank = post("/docs/collect/blank", {"line_kind": "material", "item": "Ткань",
                                         "qty": "2", "unit": "m2"})
    req_id = int(blank.location.split("/requests/")[1].split("?")[0])
    assert query("SELECT COUNT(*) FROM request_items WHERE request_id=?", (req_id,))[0] == 1
    assert "В заявке нет позиций." not in client.get(f"/requests/{req_id}").text
    assert post(f"/requests/{req_id}/ship").status_code == 302
    assert query("SELECT status FROM requests WHERE id=?", (req_id,))[0] == "shipped"

    with sqlite3.connect(db.DB_PATH) as conn:
        req_ids = []
        item_ids = []
        for item_name in ("Подошва", "Нитки"):
            cur = conn.execute(
                "INSERT INTO requests(status,urgent,created_role,created_at) "
                "VALUES ('open',0,'proizv','25.09.2026 09:00')")
            req_ids.append(cur.lastrowid)
            cur = conn.execute(
                "INSERT INTO request_items(request_id,item,qty) VALUES (?,?,?)",
                (req_ids[-1], item_name, 3),
            )
            item_ids.append(cur.lastrowid)
    collect_page = client.get("/docs/collect")
    assert collect_page.status_code == 200
    assert "Подошва" in collect_page.text and "Нитки" in collect_page.text
    assert query("SELECT status FROM requests WHERE id=?", (req_ids[0],))[0] == "open"
    saved = post("/docs/collect/save", {f"col_{item_ids[0]}": "3",
                                        f"placed_{item_ids[0]}": "1"})
    assert saved.status_code == 302 and f"/requests/{req_ids[0]}?step=2" in saved.location
    assert query("SELECT status FROM requests WHERE id=?", (req_ids[0],))[0] == "progress"
    assert query("SELECT status FROM requests WHERE id=?", (req_ids[1],))[0] == "open"
    assert query("SELECT placed FROM request_items WHERE id=?", (item_ids[1],))[0] == 0
    invalid = post("/docs/collect/save", {f"col_{item_ids[0]}": "3",
                                          f"placed_{item_ids[0]}": "1",
                                          f"col_{item_ids[1]}": "-1",
                                          f"placed_{item_ids[1]}": "1"})
    assert invalid.status_code == 302
    assert query("SELECT status FROM requests WHERE id=?", (req_ids[1],))[0] == "open"
    assert query("SELECT placed FROM request_items WHERE id=?", (item_ids[1],))[0] == 0
    form = client.get(f"/requests/{req_ids[0]}?step=2")
    for label in ("Штробель сапожники", "Штробель шить", "Прошивка",
                  "Покраска + натирка", "Вставка в колодку",
                  "Вклейка простилок в колодку", "Упаковка"):
        assert label in form.text
    assert post(f"/requests/{req_ids[0]}/pair/add", {"customer_id": "1", "model_id": "1",
                                                    "pairs": "5"}).status_code == 302
    assert query("SELECT COUNT(*) FROM request_items WHERE request_id=? AND line_kind='pair'", (req_ids[0],))[0] == 0
    assert post(f"/requests/{req_ids[0]}/pair/add", {"customer_id": "1", "model_id": "1",
                                                    "pairs": "5", "operations": ["op2", "op4", "op7"]}).status_code == 302
    assert post(f"/requests/{req_ids[0]}/pair/add", {"customer_id": "1", "model_id": "2",
                                                    "pairs": "8", "operations": ["op3"]}).status_code == 302
    with sqlite3.connect(db.DB_PATH) as conn:
        pairs = conn.execute(
            "SELECT model_id,collected,operation FROM request_items "
            "WHERE request_id=? AND line_kind='pair' ORDER BY id", (req_ids[0],)).fetchall()
    assert [(r[0], r[1], json.loads(r[2])) for r in pairs] == [
        (1, 5, ["op2", "op4", "op7"]), (2, 8, ["op3"])]
    assert post(f"/requests/{req_ids[0]}/material/add", {"item": "Кожа", "qty": "7",
                                                        "unit": "m2"}).status_code == 302
    assert query("SELECT unit FROM request_items WHERE request_id=? AND line_kind='material'", (req_ids[0],))[0] == "m2"
    assert post(f"/requests/{req_ids[0]}/collect", {f"col_{item_ids[0]}": "3",
                                                      f"placed_{item_ids[0]}": "1"}).status_code == 302
    assert query("SELECT placed FROM request_items WHERE request_id=? AND line_kind='pair'", (req_ids[0],))[0] == 1
    page = client.get(f"/requests/{req_ids[0]}?step=2")
    assert page.status_code == 200 and "Штробель шить" in page.text and "Упаковка" in page.text and "м²" in page.text

    with sqlite3.connect(db.DB_PATH) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("INSERT INTO lines(document_id,customer_id,model_id,pairs_sent) VALUES (?,?,?,?)", (doc_id, 9999, 9999, 1))
            raise AssertionError("missing foreign-key check")
        except sqlite3.IntegrityError:
            pass

    assert client.get("/login/director").status_code == 302
    assert post(f"/docs/{doc_id}/delete").status_code == 302
    row = query("SELECT details FROM audit_events WHERE action='doc_delete_snapshot' ORDER BY id DESC LIMIT 1")
    assert json.loads(row[0])["document"]["id"] == doc_id
    assert client.get("/payroll").status_code == 200
    assert post("/payroll/rates", {"model_id": "1", "operation_id": "1", "rate": "1.25"}).status_code == 302
    assert client.get("/login/proizv").status_code == 302
    assert post("/payroll/records", {"worker_id": "1", "model_id": "1", "operation_id": "1",
                                     "pairs": "10", "work_date": "2026-09-25"}).status_code == 302
    assert query("SELECT amount_kopeks FROM work_records")[0] == 1250
    assert post("/payroll/rates", {"model_id": "1", "operation_id": "1", "rate": "2.00"}).status_code == 403
    assert client.get("/login/director").status_code == 302
    assert post("/payroll/rates", {"model_id": "1", "operation_id": "1", "rate": "2.00"}).status_code == 302
    assert query("SELECT amount_kopeks FROM work_records")[0] == 1250
    assert client.get("/payroll?start=2026-09-01&end=2026-09-30").status_code == 200
    record_id = query("SELECT id FROM work_records")[0]
    assert post(f"/payroll/records/{record_id}/delete").status_code == 302
    assert query("SELECT COUNT(*) FROM work_records")[0] == 0
    assert query("SELECT COUNT(*) FROM audit_events WHERE action='payroll_record_delete_snapshot'")[0] == 1

    # Ни одна форма не создаёт запись без первой позиции.
    assert client.get("/requests/new").status_code == 403
    assert client.get("/login/proizv").status_code == 302
    before = query("SELECT COUNT(*) FROM requests")[0]
    assert client.get("/requests/new").status_code == 200
    assert post("/requests/new", {"note": "пусто"}).status_code == 200
    assert post("/requests/new", {"item": ["Клей", "Нитки"], "qty": ["0", "1"],
                                  "unit": ["sht", "sht"], "unit_custom": ["", ""],
                                  "item_note": ["", ""]}).status_code == 200
    assert query("SELECT COUNT(*) FROM requests")[0] == before
    multi = post("/requests/new", {"item": ["Клей", "Кожа", "", "Нитки"], "qty": ["2", "3", "", ""],
                                   "unit": ["m2", "other", "sht", "sht"],
                                   "unit_custom": ["", "кг", "", ""],
                                   "item_note": ["", "чёрная", "", ""], "note": "общее", "urgent_2": "1"})
    assert multi.status_code == 302
    assert "/docs" in multi.location and "requests" in multi.location
    multi_id = query("SELECT MAX(id) FROM requests")[0]
    assert query("SELECT status, sent_at IS NOT NULL FROM requests WHERE id=?", (multi_id,)) == ("open", 1)
    assert query("SELECT note, urgent FROM requests WHERE id=?", (multi_id,)) == ("общее", 1)
    assert query("SELECT COUNT(*) FROM request_items WHERE request_id=?", (multi_id,))[0] == 3
    assert query("SELECT item FROM request_items WHERE request_id=? AND urgent=1", (multi_id,)) == ("Кожа",)
    assert query("SELECT COUNT(*) FROM request_items WHERE request_id=? AND urgent=0", (multi_id,))[0] == 2
    assert query("SELECT unit FROM request_items WHERE request_id=? AND item='Клей'", (multi_id,))[0] == "m2"
    assert query("SELECT unit, note FROM request_items WHERE request_id=? AND item='Кожа'", (multi_id,)) == ("кг", "чёрная")
    assert query("SELECT unit FROM request_items WHERE request_id=? AND item='Нитки'", (multi_id,))[0] is None
    assert client.get(f"/requests/{multi_id}").status_code == 404  # у производства нет страницы заявки
    docs_page = client.get("/docs?tab=requests").text
    assert "кг" in docs_page and "dq-item-urgent" in docs_page and "onclick=\"location='/requests/" not in docs_page
    new_req = post("/requests/new", {"item": "Клей", "qty": "2"})
    assert new_req.status_code == 302
    new_req_id = query("SELECT MAX(id) FROM requests")[0]
    assert post(f"/requests/{new_req_id}/item/1/del").status_code == 404      # редактирования черновика больше нет
    assert post(f"/requests/{new_req_id}/submit").status_code == 404
    assert post(f"/requests/{new_req_id}/delete").status_code == 302
    assert query("SELECT COUNT(*) FROM requests WHERE id=?", (new_req_id,))[0] == 0

    new_doc = post("/docs/new", {"customer_id": "1", "model_id": "1", "pairs": "1"})
    new_doc_id = int(new_doc.location.rsplit("/", 1)[-1])
    new_line_id = query("SELECT id FROM lines WHERE document_id=?", (new_doc_id,))[0]
    assert post(f"/docs/{new_doc_id}/line/{new_line_id}/del").status_code == 302
    assert query("SELECT COUNT(*) FROM documents WHERE id=?", (new_doc_id,))[0] == 0

    assert client.get("/login/sklad").status_code == 302
    before = query("SELECT COUNT(*) FROM requests")[0]
    assert post("/docs/collect/blank", {"line_kind": "pair", "customer_id": "1",
                                        "model_id": "1", "pairs": "0", "operation": "op1"}).status_code == 302
    assert query("SELECT COUNT(*) FROM requests")[0] == before
    new_blank = post("/docs/collect/blank", {"line_kind": "pair", "customer_id": "1",
                                             "model_id": "1", "pairs": "1",
                                             "operations": ["op1", "op6"]})
    new_blank_id = int(new_blank.location.split("/requests/")[1].split("?")[0])
    new_blank_item = query("SELECT id FROM request_items WHERE request_id=?", (new_blank_id,))[0]
    assert json.loads(query("SELECT operation FROM request_items WHERE id=?", (new_blank_item,))[0]) == ["op1", "op6"]
    assert post(f"/requests/{new_blank_id}/placed/{new_blank_item}/del").status_code == 302
    assert query("SELECT COUNT(*) FROM requests WHERE id=?", (new_blank_id,))[0] == 0
    assert query("PRAGMA integrity_check")[0] == "ok"
    print("PASS: CSRF, safe GET, roles, transitions, global collection, operation and units, foreign keys, audit, payroll, SQLite")
