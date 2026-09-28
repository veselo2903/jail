"""An empty installation stays empty across startup; references are user-created."""
import os
from pathlib import Path
import sys
import tempfile
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='jail-empty-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db')
    os.environ['JAIL_SECRET_KEY']='empty-start-test'
    from jail.app import app,db
    app.testing=True
    for _ in range(2):
        db.init_db()
        with db.get_db() as conn:
            for table in ('customers','models','workers','materials','orders','production_batches'):
                assert conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]==0,table
            assert conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]==7
    client=app.test_client()
    for role in ('sklad','director','proizv'):
        client.get('/login/'+role)
        response=client.get('/'+role+'/refs',follow_redirects=True)
        assert response.status_code==200
        assert 'refs-list card' not in response.text and 'Показать архив' not in response.text
        if role=='proizv':assert 'refs-section' not in response.text
        else: assert 'refs-add card' not in response.text
    for role in ('director','sklad','proizv'):
        client.get('/login/'+role)
        paths=['batches','requests','supplies'] if role=='proizv' else ['customers','models','orders','batches','inventory','staff','payroll/ledger','requests','supplies','deliveries','customer-payments']
        for path in paths:
            response=client.get('/'+role+'/'+path);assert response.status_code==200,(role,path)
            content=response.text.split('id="biz-page-start">',1)[1].split('</div>\n</div>\n<script>',1)[0]
            assert 'class="biz-empty"' in content,(role,path)
            assert content.count('biz-empty-action')==1,(role,path)
            assert not any(value in content for value in ('<h2','<table','<form','class="card','biz-list-help','biz-next-step')),(role,path)
    client.get('/login/sklad')

    with client.session_transaction() as state:
        state['_flashes']=[('message','Не терять сообщение')]
        state['retry_form']={'path':'/sklad/customers/new','fields':{'name':['Сохранённое поле']}}
    response=client.get('/sklad/customers/new',headers={'X-Jail-Prefetch':'1'})
    assert 'Не терять сообщение' not in response.text and 'biz-retry-fields' not in response.text
    with client.session_transaction() as state:
        assert state['_flashes'] and state['retry_form']
    response=client.get('/sklad/customers/new')
    assert 'Не терять сообщение' in response.text and 'biz-retry-fields' in response.text

    for path in ('orders/new','models/new','supplies/new','deliveries/new','customer-payments/new'):
        response=client.get('/sklad/'+path,follow_redirects=True)
        assert response.status_code==200 and '<form' in response.text and 'biz-empty-action' not in response.text,path
    assert client.get('/sklad/orders/new').status_code==302
    with client.session_transaction() as session:csrf=session['csrf_token']
    for table in ('customers','models','workers'):
        assert client.post('/sklad/refs/'+table+'/add',data={'csrf_token':csrf,'name':'   ','number':' '}).status_code==302
    with db.get_db() as conn:
        assert all(conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]==0 for t in ('customers','models','workers'))
    client.post('/sklad/customers/new',data=dict(csrf_token=csrf,token='first-customer',name='Фирма Север'))
    response=client.get('/sklad/batches')
    assert '>Добавить модель</a>' in response.text and '/sklad/models/new?customer=1' in response.text
    with client.session_transaction() as state:csrf=state['csrf_token']
    client.post('/sklad/customers/new',data=dict(csrf_token=csrf,token='second-customer',name='Фирма Юг'))
    response=client.get('/sklad/batches')
    assert 'href="/sklad/customers?for=model"' in response.text and '>Выбрать заказчика</a>' in response.text
    response=client.get('/sklad/models/new')
    assert response.status_code==302 and '/sklad/customers?for=model' in response.location
    response=client.get('/sklad/models/new?customer=1')
    assert 'name="customer_id" value="1"' in response.text and '<select name="customer_id"' not in response.text
    client.post('/sklad/models/new?customer=1',data=dict(csrf_token=csrf,token='first-model',customer_id='1',name='Ботинки 714',sale_price='900'))
    response=client.get('/sklad/orders/new?customer=2')
    assert response.status_code==302 and response.location=='/sklad/models/new?customer=2'
    response=client.get('/sklad/batches')
    assert '>Создать заказ</a>' in response.text and '/sklad/orders/new' in response.text
    response=client.post('/sklad/orders/new',data=dict(csrf_token=csrf,token='first-real-order',customer_id='1',model_id='1',qty='100',price='',quantity_unit='pair',price_unit='pair',price_kind='unit',settlement='proportional'))
    assert response.status_code==302 and '/orders/' in response.location
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==2
        assert conn.execute('SELECT COUNT(*) FROM models').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0]==0
    print('PASS: empty installation and repeated startup, no blank reference cards, no dummy records, first real order from zero')
