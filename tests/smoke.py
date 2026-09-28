"""Request viewing, partial supply, urgent positions, rejection and migration."""
import os,secrets,sqlite3,sys,tempfile
from pathlib import Path
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='jail-smoke-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db');os.environ['JAIL_SECRET_KEY']='smoke-isolated'
    from jail.app import app,db
    app.testing=True;client=app.test_client();role='sklad'
    client.get('/login/sklad',follow_redirects=True)
    with client.session_transaction() as s:csrf=s['csrf_token']
    def switch(value):
        global role
        role=value;client.get('/login/'+value,follow_redirects=True)
    def post(path,data=None):
        f=MultiDict(data or {});f['csrf_token']=csrf;f.setdefault('token',secrets.token_hex(8));f.setdefault('send_token',secrets.token_hex(8))
        return client.post('/'+role+path,data=f)
    def one(sql,args=()):
        with db.get_db() as c:return c.execute(sql,args).fetchone()
    def count(t):return one('SELECT COUNT(*) FROM '+t)[0]
    def page(path):return client.get('/'+role+path,follow_redirects=True).text
    assert client.get('/proizv/requests').status_code==403
    assert 'тестовый режим' not in page('/requests') and 'class="tag sent"' not in page('/requests')
    assert client.post('/sklad/supplies/new').status_code==400
    assert client.get('/sklad/requests/supply/new').location.endswith('/sklad/supplies/new')
    assert client.get('/sklad/refs').location.endswith('/sklad/customers')
    assert post('/supplies/new').status_code==302 and count('shipments')==0
    post('/inventory/new',dict(name='Кожа',unit='m2'))
    post('/inventory',dict(kind='receipt',material_id='1',qty='100',cost='1000'))
    switch('proizv')
    assert post('/requests/new',dict(note='Пусто')).status_code==200 and count('requests')==0
    r=post('/requests/new',dict(item=['Кожа','Нитки'],qty=['10','5'],unit=['m2','sht'],urgent_1='1',item_note=['Чёрная',''],note='На партию'))
    assert r.status_code==302
    rid=one('SELECT id FROM requests')[0]
    with db.get_db() as c:items=[r[0] for r in c.execute('SELECT id FROM request_items ORDER BY id')]
    assert client.get('/proizv/requests/'+str(rid)+'/edit').status_code==200
    switch('sklad');assert 'Заявка №'+str(rid) not in page('/supplies/new')
    assert post('/supplies/new',{'qty_'+str(items[0]):'4','inventory_material_'+str(items[0]):'1'}).status_code==302
    assert count('shipments')==0 # pending producer edit window
    with db.get_db() as c:c.execute('UPDATE requests SET created_ts=created_ts-100 WHERE id=?',(rid,))
    html=page('/requests');assert 'Кожа' in html and 'ship_'+str(items[0]) not in html
    assert count('shipments')==0
    html=page('/supplies/new');assert 'Заявки производства' in html and 'Запрошено 10' in html
    assert '<details class="sklad-incoming-spoiler" >' in html and 'Дополнительные материалы вне партии' in html
    r=post('/supplies/new',{'qty_'+str(items[0]):'4','inventory_material_'+str(items[0]):'1','inventory_tracking':'1','send_token':'partial'})
    assert r.status_code==302 and '/supplies/' in r.location
    assert one('SELECT status FROM requests')[0]=='progress' and count('shipments')==1
    assert 'Осталось 6' in page('/supplies/new')
    post('/supplies/new',{'qty_'+str(items[0]):'4','inventory_material_'+str(items[0]):'1','send_token':'partial'})
    assert count('shipments')==1
    # Position urgency remains visible when reading the expanded request.
    assert page('/requests').count('Срочно')>=1
    path='/requests/'+str(rid)+'/items/'+str(items[1])+'/reject'
    assert client.post('/sklad'+path).status_code==400
    r=post(path,dict(reason='Нитки не требуются'));assert r.status_code==200 and not r.json['complete']
    post('/supplies/new',{'qty_'+str(items[0]):'6','inventory_material_'+str(items[0]):'1','inventory_tracking':'1','send_token':'rest'})
    assert count('shipments')==2 and one('SELECT status FROM requests')[0]=='shipped'
    assert 'Обработанные заявки' in page('/requests') and 'Кожа' in page('/requests')
    assert post(path,dict(reason='Повтор')).json['complete'] and count('shipments')==2
    assert post('/requests/'+str(rid)+'/items/'+str(items[0])+'/reject').status_code==409
    switch('proizv')
    assert post('/requests/'+str(rid)+'/delete').status_code==403
    assert post('/supplies/new').status_code==403
    assert 'Нитки не требуются' in page('/requests?archive=1')
    # Migration of old shipped requests is once-only; rejected requests never create fake shipments.
    with db.get_db() as c:
        old=c.execute("INSERT INTO requests(status,created_role,created_at,done_at) VALUES ('shipped','proizv','25.09.2026 09:00','25.09.2026 09:15')").lastrowid
        c.execute('INSERT INTO request_items(request_id,item,qty,collected,placed) VALUES (?,?,2,2,1)',(old,'Нить'))
        rejected=c.execute("INSERT INTO requests(status,created_role,created_at) VALUES ('shipped','proizv','25.09.2026 09:00')").lastrowid
        c.execute("INSERT INTO request_items(request_id,item,qty,status) VALUES (?,? ,3,'rejected')",(rejected,'Отказ'))
        db._migrate_shipments(c);db._migrate_shipments(c)
    assert one('SELECT COUNT(*) FROM shipments WHERE legacy_request_id=?',(old,))[0]==1
    assert one('SELECT COUNT(*) FROM shipments WHERE legacy_request_id=?',(rejected,))[0]==0
    assert post('/payroll/records',dict(worker_id='1',model_id='1',operation_id='1',pairs='10',work_date='2026-09-25')).status_code==302
    assert count('work_records')==0
    with db.get_db() as c:
        try:c.execute("INSERT INTO shipment_items(shipment_id,line_kind,item,qty) VALUES (9999,'material','X',1)");raise AssertionError('Missing foreign key')
        except sqlite3.IntegrityError:c.rollback()
        assert not c.execute('PRAGMA foreign_key_check').fetchone()
    print('PASS: request window/read-only view, urgency, partial fulfillment, stock, rejection, history, migration, CSRF and roles')
