"""Isolated integration tests for the manufacturing, material and wage ledgers."""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

with tempfile.TemporaryDirectory(prefix='jail-business-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db')
    os.environ['JAIL_SECRET_KEY']='test-only-business'
    from jail.app import app,db
    from jail import business_core as core
    app.testing=True
    client=app.test_client()
    client.get('/login'); client.get('/login/sklad')
    with client.session_transaction() as state: csrf=state['csrf_token']
    role='sklad'
    today=core.today().isoformat(); past=(core.today()-timedelta(days=1)).isoformat()
    serial=0
    def post(path,data=None):
        global serial
        serial+=1
        fields=MultiDict(data or {}); fields['csrf_token']=csrf; fields.setdefault('token','event-'+str(serial))
        return client.post('/'+role+path,data=fields)
    def one(sql,args=()):
        with db.get_db() as conn: return conn.execute(sql,args).fetchone()
    def switch(new):
        global role
        role=new; client.get('/login/'+new)
    def get(path):
        response=client.get('/'+role+path)
        assert response.status_code==200,(path,response.status_code)
        return response.text
    for path in ('/orders','/orders/new','/batches','/inventory','/payroll/ledger','/payroll/workers/1','/refs','/requests/supply/new'):
        get(path)
    assert client.post('/sklad/orders/new').status_code==400
    # Customer mismatch rollback, including model creation in an earlier row.
    payload={'customer_id':'1','model_id':['','2'],'new_model':['X',''],'qty':['3','2'], 'quantity_unit':['pair','pair'],
      'price_kind':['total','unit'],'price_unit':['pair','pair'],'price':['1000','5'],'settlement':['proportional','proportional'], 'specification':['','']}
    before=one('SELECT COUNT(*) c FROM models')['c']
    assert post('/orders/new',payload).status_code==200
    assert one('SELECT COUNT(*) c FROM models')['c']==before
    assert one('SELECT COUNT(*) c FROM orders')['c']==0
    payload={'customer_id':'1','model_id':[''],'new_model':['Рабочая модель'],'qty':['300'],'quantity_unit':['pair'],
      'price_kind':['total'],'price_unit':['pair'],'price':['1000'],'settlement':['proportional'],'specification':['Чёрная']}
    response=post('/orders/new',payload)
    assert response.status_code==302,response.text
    bid=one('SELECT id FROM production_batches')['id']; path='/batches/'+str(bid)
    assert one('SELECT total_cents FROM order_items')['total_cents']==100000
    get('/orders/1');get(path)
    # Per-shoe unit price converts explicitly; no odd shoes silently rounded.
    shoe={**payload,'new_model':['По ботинкам'],'qty':['5'],'quantity_unit':['shoe'],'price_kind':['unit'],'price_unit':['shoe'],'price':['12.50']}
    assert post('/orders/new',shoe).status_code==200
    shoe['qty']=['6'];assert post('/orders/new',shoe).status_code==302
    assert one('SELECT total_cents,qty_pairs FROM order_items ORDER BY id DESC')['total_cents']==7500
    # Operation versions, work volume, individual and team accounting.
    post(path,{'action':'operation','operation_id':'1','qty':'300','rate':'12.50','minutes':'2'})
    tid=one('SELECT id FROM batch_operations WHERE batch_id=?',(bid,))['id']
    post(path,{'action':'operation','operation_id':'1','qty':'300','rate':'50'})
    assert one('SELECT COUNT(*) c FROM batch_operations WHERE batch_id=?',(bid,))['c']==1
    switch('proizv');get(path)
    assert client.get('/proizv/orders').status_code==403
    assert client.get('/proizv/payroll/ledger').status_code==403
    assert 'Цена заказа' not in get(path) and 'Экономика партии' not in get(path)
    assert post(path,{'action':'operation','operation_id':'2','rate':'12'}).status_code==403
    work={'action':'work','task_id':str(tid),'qty':'100','worked_on':past,'worker_id':['1','2'],'share':['60','40'],'token':'team-one'}
    post(path,work);post(path,work)
    assert one('SELECT COUNT(*) c FROM work_acceptances')['c']==1
    assert one('SELECT SUM(amount_cents) total FROM payroll_accruals')['total']==125000
    assert one('SELECT qty_pairs FROM work_acceptances')['qty_pairs']==100
    assert post(path,{**work,'token':'overflow','qty':'201'}).status_code==302
    assert one('SELECT COUNT(*) c FROM work_acceptances')['c']==1
    post(path,{**work,'token':'bad-shares','share':['50','40']})
    assert one('SELECT COUNT(*) c FROM work_acceptances')['c']==1
    switch('sklad')
    post(path,{'action':'operation_edit','task_id':tid,'qty':'300','mode':'internal','rate':'20','minutes':'2','effective_date':today,'reason':'Новая расценка'})
    assert one('SELECT rate_version FROM batch_operations WHERE id=?',(tid,))['rate_version']==2
    assert one('SELECT amount_cents FROM work_acceptances')['amount_cents']==125000
    switch('proizv')
    post(path,{'action':'work','task_id':tid,'qty':'1','worked_on':past,'worker_id':'1'})
    assert one('SELECT rate_cents FROM work_acceptances ORDER BY id DESC')['rate_cents']==1250
    post(path,{'action':'work','task_id':tid,'qty':'1','worked_on':today,'worker_id':'1'})
    assert one('SELECT rate_cents FROM work_acceptances ORDER BY id DESC')['rate_cents']==2000
    switch('sklad')
    # Payment and advance are separate cash events; close period snapshots immutable.
    post('/payroll/workers/1',{'action':'payment','amount':'100','kind':'payment','paid_on':today,'token':'pay-one'})
    post('/payroll/workers/1',{'action':'payment','amount':'100','kind':'payment','paid_on':today,'token':'pay-one'})
    assert one('SELECT COUNT(*) c FROM payroll_payments')['c']==1
    assert core.worker_balance(db.get_db(),1)==68250
    post('/payroll/workers/2',{'action':'payment','amount':'700','kind':'payment','paid_on':today})
    assert one('SELECT COUNT(*) c FROM payroll_payments')['c']==1
    post('/payroll/workers/2',{'action':'payment','amount':'700','kind':'advance','paid_on':today})
    assert core.worker_balance(db.get_db(),2)==-20000
    post('/payroll/ledger',{'start':past,'end':past})
    snapshot=one('SELECT accrued_cents FROM payroll_period_totals WHERE worker_id=1')['accrued_cents']
    switch('proizv');post(path,{'action':'work','task_id':tid,'qty':'1','worked_on':past,'worker_id':'1'})
    late=one('SELECT worked_on,posted_on FROM work_acceptances ORDER BY id DESC')
    assert late['worked_on']==past and late['posted_on']==today
    assert one('SELECT accrued_cents FROM payroll_period_totals WHERE worker_id=1')['accrued_cents']==snapshot
    switch('sklad')
    aid=one('SELECT id FROM work_acceptances ORDER BY id DESC')['id']
    post(path,{'action':'reverse','acceptance_id':aid,'reason':'Ошибка количества'})
    post(path,{'action':'reverse','acceptance_id':aid,'reason':'Повтор'})
    assert one("SELECT COUNT(*) c FROM payroll_accruals WHERE kind='reversal'")['c']==1
    # Fractional materials, ownership, stock valuation and production consumption.
    post('/inventory',{'action':'material','name':'Кожа','unit':'m2'})
    mid=one('SELECT id FROM materials')['id']
    post('/inventory',{'kind':'receipt','material_id':mid,'qty':'10.125','cost':'1000','occurred_on':today,'token':'receipt-one'})
    post('/inventory',{'kind':'receipt','material_id':mid,'qty':'10.125','cost':'1000','occurred_on':today,'token':'receipt-one'})
    post('/inventory',{'kind':'receipt','material_id':mid,'qty':'4','cost':'5000','owner_customer_id':'1','occurred_on':today})
    assert one('SELECT qty_milli FROM stock_balances WHERE owner_customer_id IS NULL')['qty_milli']==10125
    assert one('SELECT value_cents FROM stock_balances WHERE owner_customer_id=1')['value_cents']==0
    post(path,{'action':'material','material_id':mid,'qty':'5','price':'100'})
    pid=one('SELECT id FROM batch_material_plan WHERE batch_id=?',(bid,))['id']
    post(path,{'action':'reserve','plan_id':pid})
    assert one('SELECT reserved_milli FROM batch_material_plan')['reserved_milli']==5000
    post(path,{'action':'material_move','kind':'issue','material_id':mid,'qty':'2.125','occurred_on':today})
    assert one('SELECT qty_milli FROM production_stock WHERE owner_customer_id IS NULL')['qty_milli']==2125
    assert one('SELECT reserved_milli FROM batch_material_plan')['reserved_milli']==2875
    conn=db.get_db();fin=core.batch_finance(conn,bid);conn.close()
    assert fin['material_actual']==0 and fin['wip']>0
    post(path,{'action':'material_move','kind':'consume','material_id':mid,'qty':'1.125','occurred_on':today})
    consumed=one("SELECT cost_cents FROM inventory_movements WHERE kind='consume'")['cost_cents']
    assert consumed>0
    # Linked shipments issue material once and never add operations/budgets.
    tasks_before=one('SELECT COUNT(*) c FROM batch_operations')['c']
    send={'party_id':['0'],'party_batch_0':str(bid),'party_material_id_0':[str(mid)],'party_material_qty_0':['1.5'],
       'party_material_owner_0':[''],'send_token':'linked-once'}
    post('/requests/supply/new',send);post('/requests/supply/new',send)
    assert one('SELECT COUNT(*) c FROM shipments')['c']==1
    assert one('SELECT COUNT(*) c FROM batch_operations')['c']==tasks_before
    assert one('SELECT COUNT(*) c FROM inventory_movements WHERE shipment_item_id IS NOT NULL')['c']==1
    post('/requests/supply/new',{**send,'send_token':'oversend','party_material_qty_0':['1000']})
    assert one('SELECT COUNT(*) c FROM shipments')['c']==1
    assert one('SELECT qty_milli FROM production_stock WHERE owner_customer_id IS NULL')['qty_milli']==2500
    post('/requests/supply/new',{'party_id':['0'],'party_batch_0':str(bid),'send_token':'empty-party'})
    assert one('SELECT COUNT(*) c FROM shipments')['c']==1
    # Full completion, recognised fixed total exact to the cent.
    done=one('SELECT COALESCE(SUM(qty_pairs),0) done FROM work_acceptances WHERE batch_operation_id=? AND canceled_at IS NULL',(tid,))['done']
    post(path,{'action':'work','task_id':tid,'qty':300-done,'worked_on':today,'worker_id':'1'})
    post(path,{'action':'output','kind':'good','qty':'300','occurred_on':today})
    post(path,{'action':'delivery','qty':'100','delivered_on':today})
    did=one('SELECT id FROM deliveries')['id']
    post(path,{'action':'delivery_accept','delivery_id':did,'accepted_on':today})
    conn=db.get_db();assert core.batch_finance(conn,bid)['revenue']==33333;conn.close()
    post(path,{'action':'delivery','qty':'200','delivered_on':today})
    did=one('SELECT id FROM deliveries ORDER BY id DESC')['id']
    post(path,{'action':'delivery_accept','delivery_id':did,'accepted_on':today})
    conn=db.get_db();assert core.batch_finance(conn,bid)['revenue']==100000;conn.close()
    post(path,{'action':'close'})
    assert one('SELECT status FROM production_batches WHERE id=?',(bid,))['status']!='closed' # production stock must be resolved
    post(path,{'action':'material_move','kind':'return','material_id':mid,'qty':'2.5','occurred_on':today})
    post(path,{'action':'close'})
    assert one('SELECT status FROM production_batches WHERE id=?',(bid,))['status']=='closed'
    for view in ('/orders','/orders/1',path,'/batches','/batches?archive=1','/inventory','/payroll/ledger','/payroll/workers/1','/requests','/requests/supply/new','/refs'):
        get(view)
    # Split fixed-price positions without rounding the order total; amend with history.
    payload['new_model']=['Разделяемая'];payload['price']=['1000'];payload['qty']=['300']
    post('/orders/new',payload)
    split_source=one('SELECT id FROM production_batches ORDER BY id DESC')['id']
    split_path='/batches/'+str(split_source)
    post(split_path,{'action':'operations_bulk','operation_id':['1','7'],'rate_1':'5','rate_7':'2'})
    assert one('SELECT COUNT(*) c FROM batch_operations WHERE batch_id=?',(split_source,))['c']==2
    post(split_path,{'action':'split','qty':'100','token':'split-once'})
    post(split_path,{'action':'split','qty':'100','token':'split-once'})
    order_item=one('SELECT order_item_id FROM production_batches WHERE id=?',(split_source,))['order_item_id']
    assert one('SELECT SUM(contract_cents) amount,SUM(qty_pairs) qty,COUNT(*) c FROM production_batches WHERE order_item_id=?',(order_item,))['amount']==100000
    assert one('SELECT COUNT(*) c FROM production_batches WHERE order_item_id=?',(order_item,))['c']==2
    split_oid=one('SELECT order_id FROM order_items WHERE id=?',(order_item,))['order_id']
    amended={'action':'terms','item_id':order_item,'price':'1000.01','price_kind':'total','price_unit':'pair','reason':'Уточнение договора'}
    post('/orders/'+str(split_oid),amended);post('/orders/'+str(split_oid),amended)
    assert one('SELECT SUM(contract_cents) amount FROM production_batches WHERE order_item_id=?',(order_item,))['amount']==100001
    assert one('SELECT COUNT(*) c FROM order_changes WHERE order_id=?',(split_oid,))['c']==1
    # General supplies retain stock value until allocated to a particular batch.
    post('/requests/supply/new',{'extra_material_id':[str(mid)],'extra_material_qty':['1.5'],'extra_material_owner':[''],'send_token':'pool-once','inventory_tracking':'1'})
    assert one('SELECT qty_milli FROM production_pool')['qty_milli']==1500
    post(split_path,{'action':'material_move','kind':'allocate','material_id':mid,'qty':'0.5','occurred_on':today})
    assert one('SELECT qty_milli FROM production_pool')['qty_milli']==1000
    assert one('SELECT qty_milli FROM production_stock WHERE batch_id=?',(split_source,))['qty_milli']==500
    assert get('/inventory').count('Общие материалы в производстве')==1
    # Two gunicorn-like writers cannot over-accept the same operation.
    from concurrent.futures import ThreadPoolExecutor
    task=one('SELECT id,qty_pairs FROM batch_operations WHERE batch_id=? AND operation_id=7',(split_source,))
    def concurrent_accept(n):
        conn=db.get_db()
        try:
            conn.execute('BEGIN IMMEDIATE')
            result=core.accept_work(conn,split_source,MultiDict({'task_id':task['id'],'qty':'150','worked_on':today,'worker_id':'1','token':'concurrent-'+str(n)}),'sklad')
            conn.commit();return True
        except core.RuleError:
            conn.rollback();return False
        finally: conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(concurrent_accept,(1,2)))
    assert sum(results)==1
    assert one('SELECT SUM(qty_pairs) qty FROM work_acceptances WHERE batch_operation_id=?',(task['id'],))['qty']==150
    # Reversing a cash typo changes debt, not wages; repeat is idempotent.
    conn=db.get_db();before_balance=core.worker_balance(conn,1);conn.close()
    pid=one("SELECT id FROM payroll_payments WHERE token='pay-one'")['id']
    post('/payroll/workers/1',{'action':'payment_reverse','payment_id':pid,'reason':'Ошибка суммы'})
    post('/payroll/workers/1',{'action':'payment_reverse','payment_id':pid,'reason':'Повтор'})
    conn=db.get_db();assert core.worker_balance(conn,1)==before_balance+10000;conn.close()
    assert one('SELECT COUNT(*) c FROM payroll_cash_reversals')['c']==1
    get('/payroll/workers/1');get('/payroll/ledger')
    from jail.business import quantity
    assert quantity(80000)=='80' and quantity(100000)=='100' and quantity(1500)=='1.5'
    assert client.get('/sklad/batches/999999').status_code==404
    db.init_db() # idempotent additive migration does not duplicate legacy or current ledger entries
    assert one('PRAGMA integrity_check')[0]=='ok'
    assert one('PRAGMA foreign_key_check') is None
    print('PASS: orders, exact pricing, rollback, roles, rate versions, team volume, late work, reversals, cash, closing, fractional stock, ownership, supplies, delivery, migration')
