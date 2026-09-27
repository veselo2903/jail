"""Additive section records. Existing ids and accounting facts are preserved."""
SCHEMA='''
CREATE TABLE IF NOT EXISTS customer_payment_reversals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id INTEGER NOT NULL UNIQUE REFERENCES customer_payments(id),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), reversed_on TEXT NOT NULL, reason TEXT NOT NULL,
 actor TEXT NOT NULL, created_at TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS delivery_documents (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER REFERENCES orders(id), delivered_on TEXT NOT NULL,
 note TEXT, actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS delivery_acceptances (
 id INTEGER PRIMARY KEY AUTOINCREMENT, delivery_id INTEGER NOT NULL REFERENCES deliveries(id),
 qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0), accepted_on TEXT NOT NULL,
 actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supply_receipts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_id INTEGER NOT NULL UNIQUE REFERENCES shipments(id),
 received_on TEXT NOT NULL, note TEXT, actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS supply_receipt_items (
 shipment_item_id INTEGER PRIMARY KEY REFERENCES shipment_items(id), receipt_id INTEGER NOT NULL REFERENCES supply_receipts(id),
 qty_milli INTEGER NOT NULL CHECK(qty_milli>=0), credited_milli INTEGER NOT NULL DEFAULT 0 CHECK(credited_milli>=0)
);
CREATE TABLE IF NOT EXISTS supply_pending_stock (
 shipment_item_id INTEGER PRIMARY KEY REFERENCES shipment_items(id), material_id INTEGER NOT NULL REFERENCES materials(id),
 owner_customer_id INTEGER REFERENCES customers(id), batch_id INTEGER REFERENCES production_batches(id),
 qty_milli INTEGER NOT NULL CHECK(qty_milli>=0), value_cents INTEGER NOT NULL CHECK(value_cents>=0)
);
CREATE TABLE IF NOT EXISTS supply_resolutions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, shipment_item_id INTEGER NOT NULL REFERENCES shipment_items(id),
 kind TEXT NOT NULL CHECK(kind IN ('receive','return','loss','extra')), qty_milli INTEGER NOT NULL CHECK(qty_milli>0),
 note TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
'''

def migrate(conn):
    conn.executescript(SCHEMA)
    if 'document_id' not in {r[1] for r in conn.execute('PRAGMA table_info(deliveries)')}:
        conn.execute('ALTER TABLE deliveries ADD COLUMN document_id INTEGER REFERENCES delivery_documents(id)')
    if 'cancellation_settlement_cents' not in {r[1] for r in conn.execute('PRAGMA table_info(orders)')}:
        conn.execute('ALTER TABLE orders ADD COLUMN cancellation_settlement_cents INTEGER CHECK(cancellation_settlement_cents>=0)')
    conn.execute("INSERT INTO delivery_acceptances(delivery_id,qty_pairs,accepted_on,actor,token,created_at) SELECT d.id,d.qty_pairs,d.accepted_on,d.actor,'legacy:delivery:'||d.id,d.accepted_on FROM deliveries d WHERE d.accepted_on IS NOT NULL AND NOT EXISTS (SELECT 1 FROM delivery_acceptances a WHERE a.delivery_id=d.id)")

    conn.execute("INSERT OR IGNORE INTO delivery_documents(order_id,delivered_on,note,actor,token) SELECT i.order_id,d.delivered_on,d.note,d.actor,'legacy:delivery:'||d.id FROM deliveries d JOIN production_batches b ON b.id=d.batch_id JOIN order_items i ON i.id=b.order_item_id WHERE d.document_id IS NULL")
    conn.execute("UPDATE deliveries SET document_id=(SELECT h.id FROM delivery_documents h WHERE h.token='legacy:delivery:'||deliveries.id) WHERE document_id IS NULL")

    if 'receipt_required' not in {r[1] for r in conn.execute('PRAGMA table_info(shipments)')}:
        conn.execute('ALTER TABLE shipments ADD COLUMN receipt_required INTEGER NOT NULL DEFAULT 0')
    if 'batch_id' not in {r[1] for r in conn.execute('PRAGMA table_info(lines)')}:
        conn.execute('ALTER TABLE lines ADD COLUMN batch_id INTEGER REFERENCES production_batches(id)')

    if 'rejection_note' not in {r[1] for r in conn.execute('PRAGMA table_info(request_items)')}:
        conn.execute('ALTER TABLE request_items ADD COLUMN rejection_note TEXT')
    if 'factory_pairs' not in {r[1] for r in conn.execute('PRAGMA table_info(deliveries)')}:
        conn.execute('ALTER TABLE deliveries ADD COLUMN factory_pairs INTEGER NOT NULL DEFAULT 0 CHECK(factory_pairs>=0)')
        conn.execute('UPDATE deliveries SET factory_pairs=qty_pairs')
