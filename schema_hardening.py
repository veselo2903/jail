"""Versioned storage guarantees. Runs once before starting web workers."""
import re
from decimal import Decimal, ROUND_HALF_UP

RELATIONS = {
    'requests': [('transfer_id','requests','id','SET NULL')],
    'request_items': [('request_id','requests','id','CASCADE'),('customer_id','customers','id','RESTRICT'),('model_id','models','id','RESTRICT')],
    'lines': [('document_id','documents','id','CASCADE'),('customer_id','customers','id','RESTRICT'),('model_id','models','id','RESTRICT')],
    'item_ops': [('operation_id','operations','id','RESTRICT')],
    'finish': [('customer_id','customers','id','RESTRICT'),('model_id','models','id','RESTRICT')],
    'stock_moves': [('customer_id','customers','id','RESTRICT'),('model_id','models','id','RESTRICT')],
    'wh_moves': [('customer_id','customers','id','RESTRICT'),('model_id','models','id','RESTRICT')],
    'work_records': [('worker_id','workers','id','RESTRICT'),('operation_id','operations','id','RESTRICT'),('model_id','models','id','RESTRICT')],
}
CHECKS = {
    'customers': ['archived IN (0,1)', "length(trim(name))>0"],
    'models': ['archived IN (0,1)', "length(trim(name))>0"],
    'workers': ['archived IN (0,1)', "length(trim(number))>0", "length(trim(name))>0"],
    'documents': ["kind IN ('OUT','RETURN')", "status IN ('draft','sent','accepted')", "created_role IN ('sklad','proizv','director')"],
    'requests': ["status IN ('draft','open','progress','done','shipped','accepted')", "created_role IN ('sklad','proizv','director')"],
    'lines': ['pairs_sent>=0 AND pairs_sent=CAST(pairs_sent AS INTEGER)', 'pairs_recv IS NULL OR (pairs_recv>=0 AND pairs_recv=CAST(pairs_recv AS INTEGER))'],
    'request_items': ["line_kind IN ('need','pair','material')", 'collected IS NULL OR collected>=0', 'recv IS NULL OR recv>=0', "line_kind<>'pair' OR recv IS NULL OR recv=CAST(recv AS INTEGER)", "line_kind<>'pair' OR collected IS NULL OR collected=CAST(collected AS INTEGER)"],
    'operations': ['price>=0 AND price<=1000000'],
    'item_ops': ["kind IN ('req','line')", 'price>=0 AND price<=1000000'],
    'payments': ['amount>0 AND amount<=10000000000'],
    'finish': ["kind IN ('ready','brak')", 'pairs=CAST(pairs AS INTEGER)'],
    'wh_moves': ["quality IN ('ready','brak')", 'qty=CAST(qty AS INTEGER)'],
}

def migrate(conn):
    key='hardening_v116'
    if conn.execute('SELECT 1 FROM meta WHERE key=?',(key,)).fetchone():
        return
    conn.commit()
    conn.execute('PRAGMA foreign_keys=OFF')
    conn.execute('BEGIN IMMEDIATE')
    try:
        tables=set(RELATIONS)|set(CHECKS)|{'mat_moves','stock_moves','discr_case','discr_close'}
        for table in sorted(tables):
            sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
            indexes=[r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,))]
            sql=re.sub(r'CREATE TABLE\s+(?:IF NOT EXISTS\s+)?["`]?'+re.escape(table)+r'["`]?', 'CREATE TABLE "_new_'+table+'"',sql,count=1,flags=re.I)
            sql=re.sub(r'INTEGER PRIMARY KEY(?! AUTOINCREMENT)', 'INTEGER PRIMARY KEY AUTOINCREMENT',sql,count=1,flags=re.I)
            extras=['FOREIGN KEY('+col+') REFERENCES '+target+'('+pk+') ON DELETE '+action for col,target,pk,action in RELATIONS.get(table,[])]
            extras+=['CHECK('+expr+')' for expr in CHECKS.get(table,[])]
            if extras:
                pos=sql.rfind(')');sql=sql[:pos]+',\n'+',\n'.join(extras)+sql[pos:]
            conn.execute(sql)
            cols=','.join('"'+r[1]+'"' for r in conn.execute('PRAGMA table_info('+table+')'))
            conn.execute('INSERT INTO "_new_'+table+'" ('+cols+') SELECT '+cols+' FROM "'+table+'"')
            conn.execute('DROP TABLE "'+table+'"');conn.execute('ALTER TABLE "_new_'+table+'" RENAME TO "'+table+'"')
            for index in indexes:conn.execute(index)
        for table, column in [('customers','name'),('models','name'),('materials','name'),('workers','number')]:
            conn.execute('ALTER TABLE '+table+' ADD COLUMN normalized_key TEXT')
            conn.execute('UPDATE '+table+' SET normalized_key=jail_norm('+column+')')
            conn.execute('CREATE UNIQUE INDEX uq_'+table+'_norm ON '+table+'(normalized_key)')
            for event in ('INSERT', 'UPDATE OF '+column):
                suffix='ins' if event=='INSERT' else 'upd'
                conn.execute(f"CREATE TRIGGER norm_{table}_{suffix} AFTER {event} ON {table} BEGIN UPDATE {table} SET normalized_key=jail_norm(NEW.{column}) WHERE id=NEW.id; END")
        conn.execute('CREATE TABLE document_sequences (kind TEXT PRIMARY KEY, last_number INTEGER NOT NULL)')
        for table in ('documents','requests'):
            conn.execute('ALTER TABLE '+table+' ADD COLUMN number INTEGER')
            conn.execute('ALTER TABLE '+table+' ADD COLUMN document_uuid TEXT')
            conn.execute('UPDATE '+table+' SET number=id, document_uuid=lower(hex(randomblob(16)))')
            last=conn.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0]
            conn.execute('INSERT INTO document_sequences(kind,last_number) VALUES(?,?)',(table,last))
            conn.execute('CREATE UNIQUE INDEX uq_'+table+'_number ON '+table+'(number)')
            conn.execute('CREATE UNIQUE INDEX uq_'+table+'_uuid ON '+table+'(document_uuid)')
            conn.execute(f"""CREATE TRIGGER number_{table}_insert AFTER INSERT ON {table}
                BEGIN UPDATE document_sequences SET last_number=MAX(last_number+1,NEW.id) WHERE kind='{table}';
                UPDATE {table} SET number=(SELECT last_number FROM document_sequences WHERE kind='{table}'),
                document_uuid=lower(hex(randomblob(16))) WHERE id=NEW.id; END""")
            conn.execute(f"""CREATE TRIGGER number_{table}_immutable BEFORE UPDATE OF number,document_uuid ON {table}
                WHEN OLD.number IS NOT NULL AND (NEW.number IS NOT OLD.number OR NEW.document_uuid IS NOT OLD.document_uuid)
                BEGIN SELECT RAISE(ABORT,'Document number is immutable'); END""")
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS uq_item_ops ON item_ops(kind,item_id,operation_id)')
        for table,column in [('payments','amount'),('operations','price'),('item_ops','price')]:
            cents=column+'_kopeks'
            conn.execute('ALTER TABLE '+table+' ADD COLUMN '+cents+' INTEGER NOT NULL DEFAULT 0')
            for row in conn.execute('SELECT id,'+column+' FROM '+table).fetchall():
                value=int((Decimal(str(row[1]))*100).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
                conn.execute('UPDATE '+table+' SET '+cents+'=?,'+column+'=? WHERE id=?',(value,value/100,row[0]))
            for event in ('INSERT','UPDATE OF '+column):
                suffix='ins' if event=='INSERT' else 'upd'
                conn.execute(f'''CREATE TRIGGER cents_{table}_{suffix} AFTER {event} ON {table}
                BEGIN UPDATE {table} SET {cents}=CAST(ROUND(NEW.{column}*100) AS INTEGER),
                {column}=ROUND(NEW.{column},2) WHERE id=NEW.id; END''')
        conn.execute('ALTER TABLE lines ADD COLUMN recv_version INTEGER NOT NULL DEFAULT 0')
        conn.execute('ALTER TABLE request_items ADD COLUMN recv_version INTEGER NOT NULL DEFAULT 0')
        for col,decl in [('action_token','TEXT'),('payment_date','TEXT'),('cancelled_at','TEXT'),('cancel_reason','TEXT'),('created_by',"TEXT NOT NULL DEFAULT 'director'"),('cancelled_by','TEXT')]:
            conn.execute('ALTER TABLE payments ADD COLUMN '+col+' '+decl)
        conn.execute("UPDATE payments SET payment_date=substr(created_at,1,10)")
        conn.execute('CREATE UNIQUE INDEX uq_payment_action ON payments(action_token) WHERE action_token IS NOT NULL')
        violations=conn.execute('PRAGMA foreign_key_check').fetchall()
        if violations:raise ValueError('Migration found broken relations: '+str([tuple(v) for v in violations[:5]]))
        conn.execute('INSERT INTO meta(key,value) VALUES(?,datetime(\'now\'))',(key,))
        conn.commit()
    except Exception:
        conn.rollback();raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
