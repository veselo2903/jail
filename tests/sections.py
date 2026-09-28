"""Canonical section actions, stock on receipt, partial delivery and safe history."""
import os,secrets,sys,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='jail-sections-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db');os.environ['JAIL_SECRET_KEY']='section-integration'
    from jail.app import app,db
    from jail import business_core as core,sections
    app.testing=True;client=app.test_client();role='director'
    def switch(value):
        global role,csrf
        role=value;client.get('/login/'+role,follow_redirects=True)
        with client.session_transaction() as s:csrf=s['csrf_token']
    switch(role)
    def post(path,fields=None):
        data=MultiDict(fields or {});data['csrf_token']=csrf;data.setdefault('token',secrets.token_hex(12))
        r=client.post('/'+role+path,data=data)
        assert r.status_code in (200,302),(path,r.status_code,r.text[:250])
        if r.status_code==302:
            page=client.get(r.location,follow_redirects=True);assert page.status_code==200,(r.location,page.status_code)
        return r
    def one(sql,args=()):
        with db.get_db() as c:return c.execute(sql,args).fetchone()
    def scalar(sql,args=()):return one(sql,args)[0]
    def created(r):return int(urlsplit(r.location).path.rsplit('/',1)[-1])
    def page(path):assert client.get('/'+role+path,follow_redirects=True).status_code==200,path
    for name in app.jinja_env.list_templates():app.jinja_env.get_template(name)
    for table in ('customers','models','workers','orders','materials'):assert scalar('SELECT COUNT(*) FROM '+table)==0
    # Unused customer's own models and price records can be deleted together.
    unused=created(post('/customers/new',dict(name='Удаляемый')))
    post('/models/new?customer='+str(unused),dict(customer_id=unused,name='Черновая модель',sale_price='10'))
    post('/customers/'+str(unused),dict(action='delete'))
    assert scalar('SELECT COUNT(*) FROM customers')==0 and scalar('SELECT COUNT(*) FROM models')==0
    cid=created(post('/customers/new',dict(name='Север',token='customer-once')))
    post('/customers/new',dict(name='Север',token='customer-once'));assert scalar('SELECT COUNT(*) FROM customers')==1
    a=created(post('/models/new?customer='+str(cid),dict(customer_id=cid,name='А',sale_price='300')))
    b=created(post('/models/new?customer='+str(cid),dict(customer_id=cid,name='Б',sale_price='1000')))
    oid=created(post('/orders/new',dict(customer_id=cid,model_id=[str(a),str(b)],qty=['10','5'],price=['',''],token='order-once')))
    post('/orders/new',dict(customer_id=cid,model_id=[str(a),str(b)],qty=['10','5'],price=['',''],token='order-once'))
    assert scalar('SELECT COUNT(*) FROM orders')==1
    with db.get_db() as c:bids=[r[0] for r in c.execute('SELECT id FROM production_batches ORDER BY id')]
    ba,bb=bids;pa='/batches/'+str(ba);pb='/batches/'+str(bb)
    post('/models/'+str(a),dict(action='model_edit',customer_id=cid,name='А',sale_price='400'))
    assert scalar('SELECT SUM(total_cents) FROM order_items WHERE order_id=?',(oid,))==800000
    # Customer/model creation cannot be smuggled through another section.
    post('/customers/'+str(cid),dict(action='model_add',name='Чужой путь'))
    assert scalar('SELECT COUNT(*) FROM models')==2
    post(pa,dict(action='worker_add',name='Не здесь'));assert scalar('SELECT COUNT(*) FROM workers')==0
    for path in (pa,pb):
        post(path,dict(action='operations_bulk',operation_id='1',rate_1='25'))
        post(path,dict(action='start'))
    assert scalar('SELECT COUNT(*) FROM production_batches WHERE status="working"')==2
    post(pb,dict(action='no_materials'))
    mid=created(post('/inventory/new',dict(name='Клей',unit='kg')))
    post('/inventory',dict(action='move',kind='receipt',material_id=mid,qty='100',cost='1000',token='purchase'))
    assert scalar('SELECT SUM(qty_milli) FROM stock_balances')==100000
    post(pa,dict(action='material',material_id=mid,qty='30',price='10'))
    def send(qty,token):
        return created(post('/supplies/new',dict(send_token=token,inventory_tracking='1',party_id='0',party_batch_0=ba,party_material_id_0=str(mid),party_material_qty_0=str(qty),party_material_owner_0='')))
    sid=send(20,'send-1');send(20,'send-1')
    assert scalar('SELECT COUNT(*) FROM shipments')==1
    assert scalar('SELECT SUM(qty_milli) FROM stock_balances')==80000
    assert scalar('SELECT COALESCE(SUM(qty_milli),0) FROM production_stock')==0
    assert scalar('SELECT SUM(qty_milli) FROM supply_pending_stock')==20000
    iid=scalar('SELECT id FROM shipment_items WHERE material_id=? AND shipment_id=?',(mid,sid))
    switch('proizv');post('/supplies/'+str(sid),dict(action='receive',**{'qty_'+str(iid):'18'},note='Две единицы не пришли',token='receive-once'))
    post('/supplies/'+str(sid),dict(action='receive',**{'qty_'+str(iid):'18'},note='Две единицы не пришли',token='receive-once'))
    assert scalar('SELECT SUM(qty_milli) FROM production_stock')==18000, client.get('/proizv/supplies/'+str(sid)).text
    post(pa,dict(action='material_move',kind='consume',material_id=mid,qty='19'))
    assert scalar('SELECT SUM(qty_milli) FROM production_stock')==18000
    switch('director')
    for kind in ('return','loss'):post('/supplies/'+str(sid),dict(action=kind,item_id=iid,qty='1',note='Уточнено'))
    assert scalar('SELECT SUM(qty_milli) FROM supply_pending_stock')==0
    assert scalar('SELECT SUM(qty_milli) FROM stock_balances')==81000
    sid2=send(5,'send-2');iid2=scalar('SELECT id FROM shipment_items WHERE material_id=? AND shipment_id=?',(mid,sid2))
    switch('proizv');post('/supplies/'+str(sid2),dict(action='receive',**{'qty_'+str(iid2):'6'},note='Получено больше'))
    assert scalar('SELECT SUM(qty_milli) FROM production_stock')==23000
    switch('director');post('/supplies/'+str(sid2),dict(action='extra',item_id=iid2,qty='1',note='Отправили дополнительную единицу'))
    assert scalar('SELECT SUM(qty_milli) FROM production_stock')==24000
    assert scalar('SELECT SUM(qty_milli) FROM stock_balances')==75000
    wid=created(post('/staff',dict(name='Иван',number='17')))
    post('/staff/'+str(wid),dict(action='skills',operation_id='1'))
    post('/payroll/workers/'+str(wid),dict(action='worker_edit',name='Иван',number='17'))
    post('/payroll/workers/'+str(wid),dict(action='skills',operation_id='1'))
    assert 'Сведения и навыки сотрудника' in client.get('/director/payroll/workers/'+str(wid)).text
    post('/customers/'+str(cid),dict(action='archive'))
    assert scalar('SELECT archived FROM customers WHERE id=?',(cid,))==1
    # Archived owner still allows finishing existing jobs.
    switch('proizv');post(pa,dict(action='material_move',kind='consume',material_id=mid,qty='24'))
    assert scalar('SELECT SUM(qty_milli) FROM production_stock')==0
    for bid,qty in ((ba,10),(bb,5)):
        task=scalar('SELECT id FROM batch_operations WHERE batch_id=?',(bid,))
        post('/batches/'+str(bid),dict(action='work',task_id=task,worker_id=wid,qty=str(qty)))
        post('/batches/'+str(bid),dict(action='output',kind='good',qty=str(qty)))
    assert scalar('SELECT SUM(amount_cents) FROM payroll_accruals')==37500
    switch('director')
    did=created(post('/deliveries/new',{'order_id':oid,'qty_'+str(ba):'10','qty_'+str(bb):'5'}))
    with db.get_db() as c:lines={r['batch_id']:r['id'] for r in c.execute('SELECT * FROM deliveries WHERE document_id=?',(did,))}
    post('/deliveries/'+str(did),{'qty_'+str(lines[ba]):'7','qty_'+str(lines[bb]):'5'})
    assert scalar('SELECT status FROM production_batches WHERE id=?',(bb,))=='closed'
    with db.get_db() as c:assert core.batch_finance(c,ba)['accepted']==7
    post('/deliveries/'+str(did),{'qty_'+str(lines[ba]):'3'})
    assert scalar('SELECT status FROM orders WHERE id=?',(oid,))=='completed'
    post('/customer-payments/new',dict(order_id=oid,amount='8000',token='payment-once'))
    post('/customer-payments/new',dict(order_id=oid,amount='8000',token='payment-once'))
    pid=scalar('SELECT id FROM customer_payments');assert scalar('SELECT COUNT(*) FROM customer_payments')==1
    post('/customer-payments',dict(action='reverse',payment_id=pid,reason='Неверная запись',token='reverse-once'))
    post('/customer-payments',dict(action='reverse',payment_id=pid,reason='Неверная запись',token='reverse-once'))
    with db.get_db() as c:assert sections.payment_total(c,oid)==0
    post('/customers/'+str(cid),dict(action='delete'));assert scalar('SELECT COUNT(*) FROM customers')==1
    post('/customers/'+str(cid),dict(action='restore'))
    post('/staff/'+str(wid),dict(action='archive'))
    post('/payroll/workers/'+str(wid),dict(action='payment',kind='payment',amount='375'))
    with db.get_db() as c:assert core.worker_balance(c,wid)==0
    # Delete unused order and owned preparation, but cancel worked order with immutable payroll.
    empty=created(post('/orders/new',dict(customer_id=cid,model_id=a,qty='2',price='400')))
    eb=scalar('SELECT b.id FROM production_batches b JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=?',(empty,))
    post('/batches/'+str(eb),dict(action='operation',operation_id='2',rate='10'))
    post('/orders/'+str(empty),dict(action='delete'));assert not one('SELECT id FROM orders WHERE id=?',(empty,))
    cancel=created(post('/orders/new',dict(customer_id=cid,model_id=a,qty='10',price='400')))
    cb=scalar('SELECT b.id FROM production_batches b JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=?',(cancel,))
    post('/batches/'+str(cb),dict(action='operation',operation_id='1',rate='10'))
    post('/batches/'+str(cb),dict(action='start'))
    post('/staff/'+str(wid),dict(action='restore'))
    ct=scalar('SELECT id FROM batch_operations WHERE batch_id=?',(cb,))
    switch('proizv');post('/batches/'+str(cb),dict(action='work',task_id=ct,worker_id=wid,qty='2'))
    post('/batches/'+str(cb),dict(action='output',kind='good',qty='2'))
    returned=created(post('/supplies/return/new',dict(batch_id=cb,qty='2')))
    switch('director');post('/deliveries/new',{'order_id':cancel,'qty_'+str(cb):'2'})
    assert scalar('SELECT COUNT(*) FROM deliveries WHERE batch_id=?',(cb,))==0
    line=scalar('SELECT id FROM lines WHERE document_id=?',(returned,))
    post('/requests/transfer/'+str(returned)+'/accept',{'recv_'+str(line):'1','note_'+str(line):'Одна пара ещё не пришла'})
    assert scalar('SELECT pairs_recv FROM lines WHERE id=?',(line,))==1
    post('/requests/transfer/'+str(returned)+'/accept',{'recv_'+str(line):'2','note_'+str(line):'Получена оставшаяся пара'})
    assert scalar('SELECT pairs_recv FROM lines WHERE id=?',(line,))==2
    post('/deliveries/new',{'order_id':cancel,'qty_'+str(cb):'2'})
    assert scalar('SELECT factory_pairs FROM deliveries WHERE batch_id=?',(cb,))==0
    post('/orders/'+str(cancel),dict(action='cancel',note='Заказчик отменил остаток',settlement_amount='500'))
    assert scalar('SELECT status FROM orders WHERE id=?',(cancel,))=='canceled'
    assert scalar('SELECT SUM(amount_cents) FROM payroll_accruals')==39500
    page('/orders/'+str(cancel))
    post('/orders/'+str(cancel),dict(action='settlement',settlement_amount='700'))
    assert scalar('SELECT cancellation_settlement_cents FROM orders WHERE id=?',(cancel,))==70000
    unset=created(post('/orders/new',dict(customer_id=cid,model_id=a,qty='1',price='400')))
    post('/orders/'+str(unset),dict(action='cancel',note='Окончательную сумму уточним'))
    page('/orders/'+str(unset))
    assert scalar('SELECT cancellation_settlement_cents FROM orders WHERE id=?',(unset,)) is None
    # Requests are informational; rejects own a reason and processing leaves the list.
    switch('proizv');r=post('/requests/new',dict(item='Подошвы',qty='5',unit='sht',urgent_1='1',note='Срочно'))
    rid=scalar('SELECT MAX(id) FROM requests');request_page='/requests'
    before=scalar('SELECT COUNT(*) FROM shipments');switch('sklad');page(request_page)
    assert scalar('SELECT COUNT(*) FROM shipments')==before
    post('/requests/'+str(rid)+'/reject',dict(reason='Не требуется'))
    assert scalar('SELECT status FROM requests WHERE id=?',(rid,))=='shipped'
    assert scalar('SELECT rejection_note FROM request_items WHERE request_id=?',(rid,))=='Не требуется'
    for role_value in ('director','sklad','proizv'):
        switch(role_value)
        for path in ('/batches','/batches/'+str(ba),'/requests','/supplies','/supplies/'+str(sid)):page(path)
        if role_value!='proizv':
            for path in ('/customers','/customers/'+str(cid),'/models','/models/'+str(a),'/orders','/orders/'+str(oid),'/inventory','/inventory/materials/'+str(mid),'/staff','/staff/'+str(wid),'/payroll/ledger','/deliveries','/deliveries/'+str(did),'/customer-payments'):page(path)
    db.init_db();db.init_db()
    with db.get_db() as c:
        assert not c.execute('PRAGMA foreign_key_check').fetchone()
        assert c.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert sections.accepted_pairs(c,ba)==10
    print('PASS: section ownership, deletion/archive, immutable prices and wages, receipt/pending/shortage/excess, grouped partial delivery, payments/reversal, safe cancellation, internal shoe return, requests and all role pages')
