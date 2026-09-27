"""Persistent orders, batches, inventory and payroll; additive migrations."""

OPERATIONS = (
    'Штробель сапожники', 'Штробель шить', 'Прошивка', 'Покраска + натирка',
    'Вставка в колодку', 'Вклейка простилок в колодку', 'Упаковка',
)

SCHEMA = '''
CREATE TABLE IF NOT EXISTS catalog_submissions (token TEXT PRIMARY KEY, kind TEXT NOT NULL, entity_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS model_sale_price_history (id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL REFERENCES models(id), price_cents INTEGER CHECK(price_cents>=0), at TEXT NOT NULL, actor TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS business_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id INTEGER NOT NULL REFERENCES customers(id),
 created_at TEXT NOT NULL, due_date TEXT, note TEXT, status TEXT NOT NULL DEFAULT 'confirmed'
 CHECK(status IN ('confirmed','working','completed','canceled')), token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS order_items (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
 model_id INTEGER NOT NULL REFERENCES models(id), qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0),
 original_qty INTEGER NOT NULL CHECK(original_qty>0), quantity_unit TEXT NOT NULL CHECK(quantity_unit IN ('pair','shoe')),
 price_kind TEXT NOT NULL CHECK(price_kind IN ('unit','total')), price_unit TEXT NOT NULL CHECK(price_unit IN ('pair','shoe')),
 unit_price_cents INTEGER CHECK(unit_price_cents>=0), total_cents INTEGER NOT NULL CHECK(total_cents>=0),
 settlement TEXT NOT NULL DEFAULT 'proportional' CHECK(settlement IN ('proportional','complete')),
 specification TEXT, terms_version INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS order_changes (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
 at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, snapshot TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_item_id INTEGER REFERENCES order_items(id),
 customer_id INTEGER NOT NULL REFERENCES customers(id), model_id INTEGER NOT NULL REFERENCES models(id),
 qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0), due_date TEXT, specification TEXT, note TEXT,
 status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','working','completed','closed','canceled')),
 created_at TEXT NOT NULL, token TEXT NOT NULL UNIQUE, manager_note TEXT
);
CREATE TABLE IF NOT EXISTS model_operation_templates (
 model_id INTEGER NOT NULL REFERENCES models(id), operation_id INTEGER NOT NULL REFERENCES operations(id),
 rate_cents INTEGER CHECK(rate_cents>=0), minutes_milli INTEGER CHECK(minutes_milli>=0),
 mode TEXT NOT NULL DEFAULT 'internal' CHECK(mode IN ('internal','external','ready','skip')),
 PRIMARY KEY(model_id,operation_id)
);
CREATE TABLE IF NOT EXISTS batch_operations (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES production_batches(id),
 operation_id INTEGER NOT NULL REFERENCES operations(id), qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0),
 rate_cents INTEGER CHECK(rate_cents>=0), rate_version INTEGER NOT NULL DEFAULT 1,
 mode TEXT NOT NULL DEFAULT 'internal' CHECK(mode IN ('internal','external','ready','skip')),
 minutes_milli INTEGER CHECK(minutes_milli>=0), position INTEGER NOT NULL DEFAULT 0,
 predecessor_id INTEGER REFERENCES batch_operations(id), parent_id INTEGER REFERENCES batch_operations(id),
 reason TEXT, external_done INTEGER NOT NULL DEFAULT 0 CHECK(external_done>=0),
 external_cost_cents INTEGER NOT NULL DEFAULT 0 CHECK(external_cost_cents>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_operation_normal ON batch_operations(batch_id,operation_id) WHERE parent_id IS NULL;
CREATE TABLE IF NOT EXISTS operation_rate_versions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_operation_id INTEGER NOT NULL REFERENCES batch_operations(id),
 version INTEGER NOT NULL, rate_cents INTEGER CHECK(rate_cents>=0), effective_date TEXT NOT NULL,
 at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, UNIQUE(batch_operation_id,version)
);
CREATE TABLE IF NOT EXISTS worker_skills (
 worker_id INTEGER NOT NULL REFERENCES workers(id), operation_id INTEGER NOT NULL REFERENCES operations(id),
 PRIMARY KEY(worker_id,operation_id)
);
CREATE TABLE IF NOT EXISTS batch_assignments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_operation_id INTEGER NOT NULL REFERENCES batch_operations(id),
 worker_id INTEGER NOT NULL REFERENCES workers(id), planned_pairs INTEGER CHECK(planned_pairs>0),
 share_bp INTEGER CHECK(share_bp>0 AND share_bp<=10000), hours_milli INTEGER CHECK(hours_milli>0),
 UNIQUE(batch_operation_id,worker_id)
);
CREATE TABLE IF NOT EXISTS materials (
 id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, unit TEXT NOT NULL,
 archived INTEGER NOT NULL DEFAULT 0, UNIQUE(name,unit)
);
CREATE TABLE IF NOT EXISTS model_material_templates (
 id INTEGER PRIMARY KEY AUTOINCREMENT, model_id INTEGER NOT NULL REFERENCES models(id),
 material_id INTEGER NOT NULL REFERENCES materials(id), norm_milli INTEGER NOT NULL CHECK(norm_milli>0),
 owner_customer_id INTEGER REFERENCES customers(id), estimated_unit_cents INTEGER CHECK(estimated_unit_cents>=0),
 UNIQUE(model_id,material_id,owner_customer_id)
);
CREATE TABLE IF NOT EXISTS batch_material_plan (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES production_batches(id),
 material_id INTEGER NOT NULL REFERENCES materials(id), owner_customer_id INTEGER REFERENCES customers(id),
 qty_milli INTEGER NOT NULL CHECK(qty_milli>0), estimated_unit_cents INTEGER CHECK(estimated_unit_cents>=0),
 reserved_milli INTEGER NOT NULL DEFAULT 0 CHECK(reserved_milli>=0), note TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_batch_material_owner ON batch_material_plan(batch_id,material_id,COALESCE(owner_customer_id,0));
CREATE TABLE IF NOT EXISTS stock_balances (
 id INTEGER PRIMARY KEY AUTOINCREMENT, material_id INTEGER NOT NULL REFERENCES materials(id),
 owner_customer_id INTEGER REFERENCES customers(id), qty_milli INTEGER NOT NULL DEFAULT 0 CHECK(qty_milli>=0),
 value_cents INTEGER NOT NULL DEFAULT 0 CHECK(value_cents>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_stock_owner ON stock_balances(material_id,COALESCE(owner_customer_id,0));
CREATE TABLE IF NOT EXISTS production_pool (
 id INTEGER PRIMARY KEY AUTOINCREMENT, material_id INTEGER NOT NULL REFERENCES materials(id),
 owner_customer_id INTEGER REFERENCES customers(id), qty_milli INTEGER NOT NULL DEFAULT 0 CHECK(qty_milli>=0),
 value_cents INTEGER NOT NULL DEFAULT 0 CHECK(value_cents>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_production_pool_owner ON production_pool(material_id,COALESCE(owner_customer_id,0));
CREATE TABLE IF NOT EXISTS production_stock (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES production_batches(id),
 material_id INTEGER NOT NULL REFERENCES materials(id), owner_customer_id INTEGER REFERENCES customers(id),
 qty_milli INTEGER NOT NULL DEFAULT 0 CHECK(qty_milli>=0), value_cents INTEGER NOT NULL DEFAULT 0 CHECK(value_cents>=0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_production_stock_owner ON production_stock(batch_id,material_id,COALESCE(owner_customer_id,0));
CREATE TABLE IF NOT EXISTS inventory_movements (
 id INTEGER PRIMARY KEY AUTOINCREMENT, material_id INTEGER NOT NULL REFERENCES materials(id),
 owner_customer_id INTEGER REFERENCES customers(id), batch_id INTEGER REFERENCES production_batches(id),
 kind TEXT NOT NULL CHECK(kind IN ('receipt','issue','return','consume','loss','allocate')),
 qty_milli INTEGER NOT NULL CHECK(qty_milli>0), cost_cents INTEGER NOT NULL CHECK(cost_cents>=0),
 occurred_on TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT, supplier TEXT,
 shipment_item_id INTEGER REFERENCES shipment_items(id), token TEXT UNIQUE
);
CREATE TABLE IF NOT EXISTS work_acceptances (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_operation_id INTEGER NOT NULL REFERENCES batch_operations(id),
 qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0), worked_on TEXT NOT NULL, posted_on TEXT NOT NULL,
 rate_cents INTEGER NOT NULL CHECK(rate_cents>=0), rate_version INTEGER NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents>=0), mode TEXT NOT NULL CHECK(mode IN ('individual','team')),
 actor TEXT NOT NULL, created_at TEXT NOT NULL, note TEXT, canceled_at TEXT, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS work_shares (
 id INTEGER PRIMARY KEY AUTOINCREMENT, acceptance_id INTEGER NOT NULL REFERENCES work_acceptances(id),
 worker_id INTEGER NOT NULL REFERENCES workers(id), share_bp INTEGER NOT NULL CHECK(share_bp>0 AND share_bp<=10000),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>=0), UNIQUE(acceptance_id,worker_id)
);
CREATE TABLE IF NOT EXISTS payroll_accruals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers(id),
 acceptance_id INTEGER REFERENCES work_acceptances(id), legacy_work_id INTEGER UNIQUE REFERENCES work_records(id) ON DELETE SET NULL,
 original_id INTEGER UNIQUE REFERENCES payroll_accruals(id), batch_id INTEGER REFERENCES production_batches(id),
 kind TEXT NOT NULL CHECK(kind IN ('work','opening','adjustment','reversal')),
 amount_cents INTEGER NOT NULL, posted_on TEXT NOT NULL, worked_on TEXT, created_at TEXT NOT NULL,
 note TEXT, actor TEXT NOT NULL, token TEXT UNIQUE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_accrual_acceptance_worker ON payroll_accruals(acceptance_id,worker_id) WHERE acceptance_id IS NOT NULL AND kind='work';
CREATE TABLE IF NOT EXISTS payroll_payments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, worker_id INTEGER NOT NULL REFERENCES workers(id),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), paid_on TEXT NOT NULL, created_at TEXT NOT NULL,
 kind TEXT NOT NULL DEFAULT 'payment' CHECK(kind IN ('payment','advance')), method TEXT, note TEXT,
 actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS payroll_cash_reversals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id INTEGER NOT NULL UNIQUE REFERENCES payroll_payments(id),
 worker_id INTEGER NOT NULL REFERENCES workers(id), amount_cents INTEGER NOT NULL CHECK(amount_cents>0),
 reversed_on TEXT NOT NULL, created_at TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payroll_payment_allocations (
 payment_id INTEGER NOT NULL REFERENCES payroll_payments(id), accrual_id INTEGER NOT NULL REFERENCES payroll_accruals(id),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), PRIMARY KEY(payment_id,accrual_id)
);
CREATE TABLE IF NOT EXISTS payroll_periods (
 id INTEGER PRIMARY KEY AUTOINCREMENT, start_on TEXT NOT NULL, end_on TEXT NOT NULL,
 closed_at TEXT NOT NULL, actor TEXT NOT NULL, CHECK(start_on<=end_on), UNIQUE(start_on,end_on)
);
CREATE TABLE IF NOT EXISTS payroll_period_totals (
 period_id INTEGER NOT NULL REFERENCES payroll_periods(id), worker_id INTEGER NOT NULL REFERENCES workers(id),
 opening_cents INTEGER NOT NULL, accrued_cents INTEGER NOT NULL, paid_cents INTEGER NOT NULL, closing_cents INTEGER NOT NULL,
 PRIMARY KEY(period_id,worker_id)
);
CREATE TABLE IF NOT EXISTS batch_costs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES production_batches(id),
 label TEXT NOT NULL, planned_cents INTEGER NOT NULL DEFAULT 0 CHECK(planned_cents>=0),
 actual_cents INTEGER NOT NULL DEFAULT 0 CHECK(actual_cents>=0), occurred_on TEXT NOT NULL,
 actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS production_outputs (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES production_batches(id),
 qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0), kind TEXT NOT NULL CHECK(kind IN ('good','reject')),
 occurred_on TEXT NOT NULL, note TEXT, actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER NOT NULL REFERENCES production_batches(id),
 qty_pairs INTEGER NOT NULL CHECK(qty_pairs>0), delivered_on TEXT NOT NULL, accepted_on TEXT,
 note TEXT, actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS customer_payments (
 id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER NOT NULL REFERENCES orders(id),
 amount_cents INTEGER NOT NULL CHECK(amount_cents>0), paid_on TEXT NOT NULL,
 note TEXT, actor TEXT NOT NULL, token TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_batches_order_item ON production_batches(order_item_id);
CREATE INDEX IF NOT EXISTS idx_tasks_batch ON batch_operations(batch_id);
CREATE INDEX IF NOT EXISTS idx_work_task ON work_acceptances(batch_operation_id,canceled_at);
CREATE INDEX IF NOT EXISTS idx_accrual_worker_date ON payroll_accruals(worker_id,posted_on);
CREATE INDEX IF NOT EXISTS idx_payment_worker_date ON payroll_payments(worker_id,paid_on);
CREATE INDEX IF NOT EXISTS idx_material_movements_batch ON inventory_movements(batch_id);
'''


def migrate(conn):
    conn.executescript(SCHEMA)
    additions = {
        'customers': [('contact','TEXT'),('note','TEXT')],
        'models': [('customer_id', 'INTEGER REFERENCES customers(id)'),('sale_price_cents','INTEGER CHECK(sale_price_cents>=0)'),('description','TEXT'),('photo_filename','TEXT')],
        'production_batches': [('contract_cents', 'INTEGER CHECK(contract_cents>=0)'),
                               ('no_materials', 'INTEGER NOT NULL DEFAULT 0 CHECK(no_materials IN (0,1))')],
        'requests': [('batch_id', 'INTEGER REFERENCES production_batches(id)')],
        'shipment_items': [('batch_id', 'INTEGER REFERENCES production_batches(id)'),
                           ('material_id', 'INTEGER REFERENCES materials(id)'),
                           ('owner_customer_id', 'INTEGER REFERENCES customers(id)'),
                           ('batch_reference', 'INTEGER NOT NULL DEFAULT 0')],
    }
    for table, columns in additions.items():
        present = {r['name'] for r in conn.execute('PRAGMA table_info('+table+')')}
        for name, declaration in columns:
            if name not in present:
                conn.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+declaration)
    for position, name in enumerate(OPERATIONS, 1):
        placeholder = conn.execute('SELECT id FROM operations WHERE name=?', ('Операция '+str(position),)).fetchone()
        existing = conn.execute('SELECT id FROM operations WHERE name=?', (name,)).fetchone()
        if placeholder and not existing:
            conn.execute('UPDATE operations SET name=? WHERE id=?', (name, placeholder['id']))
        elif not existing:
            conn.execute('INSERT INTO operations(ord,name) VALUES (?,?)', (position, name))
    conn.execute('''INSERT OR IGNORE INTO payroll_accruals
        (worker_id,legacy_work_id,kind,amount_cents,posted_on,worked_on,created_at,note,actor)
        SELECT worker_id,id,'work',amount_kopeks,work_date,work_date,created_at,
        'История выработки до связи с партиями','migration' FROM work_records''')
    if not conn.execute("SELECT 1 FROM business_migrations WHERE name='legacy_rate_templates'").fetchone():
        conn.execute('''INSERT OR IGNORE INTO model_operation_templates(model_id,operation_id,rate_cents)
            SELECT model_id,operation_id,rate_kopeks FROM prices''')
        conn.execute("INSERT INTO business_migrations(name) VALUES ('legacy_rate_templates')")
