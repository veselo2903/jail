"""The director's path from empty books to production and finished orders."""
import base64,io,json,os,re,secrets,sys,tempfile
from pathlib import Path
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='jail-director-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db');os.environ['JAIL_SECRET_KEY']='director-test'
    from jail.app import app,db
    from jail import director_flow as flow
    app.testing=True;client=app.test_client();role='director'
    client.get('/login/director',follow_redirects=True)
    with client.session_transaction() as state:csrf=state['csrf_token']
    def post(path,fields,follow=False):
        data=MultiDict()
        for key,value in fields.items():
            if isinstance(value,list):data.setlist(key,value)
            else:data.add(key,value)
        data['csrf_token']=csrf;data.setdefault('token',secrets.token_hex(12))
        return client.post('/'+role+path,data=data,follow_redirects=follow)
    def one(sql,args=()):
        with db.get_db() as conn:return conn.execute(sql,args).fetchone()
    def state(bid):
        with db.get_db() as conn:return flow.batch_state(conn,conn.execute('SELECT * FROM production_batches WHERE id=?',(bid,)).fetchone())
    assert 'Добавить заказчика' in client.get('/director/orders').text
    assert 'refs' not in client.get('/director/orders').text.split('<nav')[1].split('</nav>')[0]
    assert one('SELECT COUNT(*) n FROM customers')['n']==0
    assert post('/customers/new',{'name':'  '},True).status_code==200
    assert one('SELECT COUNT(*) n FROM customers')['n']==0
    response=post('/customers/new',{'name':'Фирма Север','token':'customer-once'})
    cid=int(response.location.rsplit('/',1)[1])
    post('/customers/new',{'name':'Фирма Север','token':'customer-once'})
    assert one('SELECT COUNT(*) n FROM customers')['n']==1
    assert 'Добавить модель' in client.get('/director/orders').text
    path='/customers/'+str(cid)
    post('/models/new?customer='+str(cid),dict(customer_id=cid,name='714',sale_price='900,25',token='model-once'))
    post('/models/new?customer='+str(cid),dict(customer_id=cid,name='714',sale_price='900,25',token='model-once'))
    mid=one('SELECT id FROM models')['id'];assert one('SELECT COUNT(*) n FROM models')['n']==1
    assert one('SELECT sale_price_cents FROM models')['sale_price_cents']==90025
    page=client.get('/director/orders/new?customer='+str(cid)+'&model='+str(mid))
    assert page.status_code==200 and 'data-director-order' in page.text and '900.25' in page.text
    order=dict(customer_id=str(cid),model_id=str(mid),qty='10',price='',quantity_unit='pair',price_unit='pair',price_kind='unit',settlement='proportional')
    response=post('/orders/new',order);assert '/orders/' in response.location
    oid=int(response.location.rsplit('/',1)[1]);bid=one('SELECT id FROM production_batches')['id'];bp='/batches/'+str(bid)
    assert one('SELECT total_cents FROM order_items')['total_cents']==900250
    assert 'Выберите операции' in client.get(response.location).text
    post('/models/'+str(mid),dict(action='model_edit',customer_id=cid,name='714',sale_price='1100'))
    assert one('SELECT sale_price_cents FROM models')['sale_price_cents']==110000
    assert one('SELECT total_cents FROM order_items')['total_cents']==900250
    response=post('/orders/new',order);assert one('SELECT total_cents FROM order_items ORDER BY id DESC')['total_cents']==1100000
    assert state(bid)['stage']==0 and not state(bid)['prepared']
    post(bp,dict(action='start'))
    assert one('SELECT status FROM production_batches WHERE id=?',(bid,))['status']=='planned'
    post(bp,dict(action='operations_bulk',operation_id='1',rate_1='25'))
    assert state(bid)['prepared'] and state(bid)['target']=='biz-director-start'
    post(bp,dict(action='no_materials'))
    assert state(bid)['prepared']
    post('/staff',dict(name='Иван',token='staff-once'));post('/staff',dict(name='Иван',token='staff-once'))
    assert one('SELECT COUNT(*) n FROM workers')['n']==1
    assert state(bid)['prepared'] and state(bid)['target']=='biz-director-start'
    assert client.get('/director'+bp).text.count('name="action" value="start"')==1
    wid=one('SELECT id FROM workers')['id']
    post('/staff/'+str(wid),dict(action='worker_edit',name='Иван Петров',number='17'))
    assert one('SELECT name,number FROM workers WHERE id=?',(wid,))['name']=='Иван Петров'
    post(bp,dict(action='start'));assert state(bid)['stage']==1
    tid=one('SELECT id FROM batch_operations WHERE batch_id=?',(bid,))['id'];wid=one('SELECT id FROM workers')['id']
    role='proizv';client.get('/login/proizv')
    assert client.get('/proizv/customers').status_code==403
    assert client.get('/proizv/staff').status_code==403
    assert post(bp,dict(action='start')).status_code==403
    assert '900.25' not in client.get('/proizv'+bp).text
    post(bp,dict(action='work',task_id=str(tid),qty='10',worker_id=str(wid),share='100'))
    assert state(bid)['stage']==2 and state(bid)['target']=='biz-output'
    post(bp,dict(action='output',kind='good',qty='10'))
    role='director';client.get('/login/director')
    assert state(bid)['stage']==2 and state(bid)['target']=='biz-delivery'
    post('/deliveries/new',{'order_id':oid,'qty_'+str(bid):'10'});assert state(bid)['stage']==3
    did=one('SELECT id FROM deliveries')['id']
    from jail import business_core as core
    document=one('SELECT document_id FROM deliveries WHERE id=?',(did,))[0]
    post('/deliveries/'+str(document),{'qty_'+str(did):'10','accepted_on':core.today().isoformat()})
    assert state(bid)['stage']==4
    assert 'Заказ завершён' in client.get('/director/orders/'+str(oid)).text
    # Prices and different models are scoped to the selected customer.
    response=post('/customers/new',dict(name='Фирма Юг'));other=int(response.location.rsplit('/',1)[1])
    post('/models/'+str(mid),dict(action='model_edit',customer_id=other,name='Чужая',sale_price='1'))
    assert one('SELECT name FROM models WHERE id=?',(mid,))['name']=='714'
    assert client.get('/director/orders/new?customer='+str(other)+'&model='+str(mid)).status_code==400
    # Photo upload is optional, authenticated, typed and never stores user filenames.
    png=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aB1sAAAAASUVORK5CYII=')
    post('/models/'+str(mid),dict(action='model_edit',customer_id=cid,name='714',sale_price='1100',photo=(io.BytesIO(png),'../../shoe.png')))
    filename=one('SELECT photo_filename FROM models WHERE id=?',(mid,))['photo_filename'];assert re.fullmatch(r'[a-f0-9]{32}\.png',filename)
    photo=client.get('/director/models/'+str(mid)+'/photo');assert photo.status_code==200 and photo.mimetype=='image/png' and photo.headers['X-Content-Type-Options']=='nosniff'
    files=list((Path(folder)/'model-photos').iterdir())
    post('/models/new?customer='+str(cid),dict(customer_id=cid,name='Плохое фото',sale_price='1',photo=(io.BytesIO(b'<svg></svg>'),'shoe.png')))
    assert one("SELECT COUNT(*) n FROM models WHERE name='Плохое фото'")['n']==0 and list((Path(folder)/'model-photos').iterdir())==files
    db.init_db();assert not one('PRAGMA foreign_key_check')
    print('PASS: director first setup, scoped catalog, price defaults/history/snapshots, idempotence, preparation and worker, handoff, factory work, delivery/close, photo and access')
