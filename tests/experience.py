"""Form retry, entity ownership and production information boundaries."""
import json,os,re,secrets,sys,tempfile
from pathlib import Path
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='jail-experience-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db');os.environ['JAIL_SECRET_KEY']='experience-isolated'
    from jail.app import app,db
    from jail import business_core as core
    app.testing=True;client=app.test_client()
    assert client.get('/login/proizv').location.endswith('/proizv/batches')
    client.get('/login/sklad',follow_redirects=True)
    with client.session_transaction() as s:csrf=s['csrf_token']
    def post(path,data,follow=False):
        f=MultiDict(data);f['csrf_token']=csrf;f.setdefault('token',secrets.token_hex(10))
        return client.post('/sklad'+path,data=f,follow_redirects=follow)
    def one(sql,args=()):
        with db.get_db() as c:return c.execute(sql,args).fetchone()
    def count(t):return one('SELECT COUNT(*) FROM '+t)[0]
    # Catalog creation in a foreign section fails without creating any record.
    response=post('/orders/new',dict(customer_id='new',new_customer='Север',model_id='new',new_model='714',qty='80',price='900'))
    assert response.status_code==200 and count('customers')==count('models')==0
    post('/customers/new',dict(name='Север',token='customer'))
    post('/models/new',dict(customer_id='1',name='714',sale_price='900',token='model'))
    r=post('/orders/new',dict(customer_id='1',model_id='1',qty='80',price='900',token='order'))
    assert r.status_code==302 and '/orders/' in r.location
    bid=one('SELECT id FROM production_batches')[0];path='/batches/'+str(bid)
    post(path,dict(action='operations_bulk',operation_id='1',rate_1='25'))
    tid=one('SELECT id FROM batch_operations')[0]
    post('/inventory/new',dict(name='Подошвы',unit='pary',token='material'))
    mid=one('SELECT id FROM materials')[0]
    # Small retries go through redirect; retries are consumed exactly once.
    response=post('/inventory',dict(action='material_move',kind='receipt',material_id=mid,qty='no',cost='500',note='Сохранить примечание'),True)
    assert response.status_code==200 and 'biz-retry-fields' in response.text and count('inventory_movements')==0
    data=json.loads(re.search(r'id="biz-retry-fields">(.*?)</script>',response.text,re.S).group(1))
    assert data['note']==['Сохранить примечание'] and data['material_id']==[str(mid)]
    assert 'biz-retry-fields' not in client.get('/sklad/inventory').text
    post('/inventory',dict(action='material_move',kind='receipt',material_id=mid,qty='80',cost='8800',token='receipt'))
    post('/inventory',dict(action='material_move',kind='receipt',material_id=mid,qty='80',cost='8800',token='receipt'))
    assert one('SELECT qty_milli FROM stock_balances')[0]==80000
    post('/inventory/new',dict(name='Клей',unit='kg',token='second-material'))
    before=count('inventory_movements')
    invalid=dict(kind='receipt',material_id=['1','2'],qty=['1','bad'],cost=['10','20'],owner_customer_id=['',''],token='multi-invalid')
    post('/inventory',invalid)
    assert count('inventory_movements')==before and one('SELECT qty_milli FROM stock_balances WHERE material_id=1')[0]==80000
    multiple=dict(kind='receipt',material_id=['2','2',''],qty=['1','2',''],cost=['0','5',''],owner_customer_id=['','',''],token='multi-once')
    post('/inventory',multiple);post('/inventory',multiple)
    assert one('SELECT qty_milli,value_cents FROM stock_balances WHERE material_id=2')[0]==3000
    assert one('SELECT value_cents FROM stock_balances WHERE material_id=2')[0]==500
    post('/inventory',{**multiple,'material_id':['2'],'qty':['20'],'cost':['10']})
    assert one('SELECT qty_milli FROM stock_balances WHERE material_id=2')[0]==3000
    post('/staff',dict(name='Иван',token='worker'))
    wid=one('SELECT id FROM workers')[0]
    bad=dict(action='work',task_id=tid,worker_id=wid,qty='81',note='Не терять')
    assert 'biz-retry-fields' in post(path,bad,True).text and count('work_acceptances')==0
    work={**bad,'qty':'4','token':'work-once'};post(path,work);post(path,work)
    assert count('workers')==count('work_acceptances')==1 and one('SELECT amount_cents FROM payroll_accruals')[0]==10000
    # Large retries render directly rather than overflowing the session cookie.
    r=post(path,{**bad,'note':secrets.token_hex(9000)})
    assert r.status_code==200 and 'biz-retry-fields' in r.text and count('work_acceptances')==1
    supply=dict(send_token='supply-retry',inventory_tracking='1',party_id=['2','5'],party_batch_2=str(bid),party_batch_5=str(bid),party_material_id_2=str(mid),party_material_qty_2='999',party_material_owner_2='',party_material_id_5=str(mid),party_material_qty_5='1',party_material_owner_5='',ship_note='Сохранить после ошибки')
    r=post('/supplies/new',supply,True)
    assert r.status_code==200 and 'biz-retry-fields' in r.text and count('shipments')==0
    retry=json.loads(re.search(r'id="biz-retry-fields">(.*?)</script>',r.text,re.S).group(1))
    assert retry['party_id']==['2','5'] and retry['ship_note']==['Сохранить после ошибки']
    # Navigation selects the owning page and carries the return context.
    r=post('/customers/new?ctx=return-example',dict(name='Юг',token='ctx-customer'))
    assert 'ctx=return-example' in r.location and 'created=customer%3A' in r.location
    client.get('/login/proizv')
    r=client.get('/proizv'+path)
    stock=json.loads(re.search(r'id="biz-stock-data">(.*?)</script>',r.text,re.S).group(1))
    assert stock['warehouse']==[] and all('value_cents' not in x and 'cost_cents' not in x for x in stock['production'])
    assert 'Экономика партии' not in r.text and 'Завершить партию' not in r.text
    for p in ('payroll/ledger','orders','customers','models','staff','inventory','customer-payments','deliveries'):
        assert client.get('/proizv/'+p).status_code==403,p
    assert client.get('/proizv/models/1/photo').status_code==404
    print('PASS: section ownership, atomic rollback, small/large retries, dynamic supply retry, idempotence, navigation context and factory financial access')
