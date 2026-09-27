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
        paths=['batches','requests','supplies','supplies/return/new'] if role=='proizv' else ['customers','models','orders','batches','inventory','staff','payroll/ledger','requests','supplies','deliveries','customer-payments','models/new','orders/new','supplies/new','deliveries/new','customer-payments/new']
        for path in paths:
            response=client.get('/'+role+'/'+path);assert response.status_code==200,(role,path)
            content=response.text.split('id="biz-page-start">',1)[1].split('</div>\n</div>\n<script>',1)[0]
            assert 'class="biz-empty"' in content,(role,path)
            assert content.count('biz-empty-action')==1,(role,path)
            assert not any(value in content for value in ('<h2','<table','<form','class="card','biz-list-help','biz-next-step')),(role,path)
    client.get('/login/sklad')

    assert client.get('/sklad/orders/new').status_code==200
    with client.session_transaction() as session:csrf=session['csrf_token']
    for table in ('customers','models','workers'):
        assert client.post('/sklad/refs/'+table+'/add',data={'csrf_token':csrf,'name':'   ','number':' '}).status_code==302
    with db.get_db() as conn:
        assert all(conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]==0 for t in ('customers','models','workers'))
    client.post('/sklad/customers/new',data=dict(csrf_token=csrf,token='first-customer',name='Фирма Север'))
    client.post('/sklad/models/new',data=dict(csrf_token=csrf,token='first-model',customer_id='1',name='Ботинки 714',sale_price='900'))
    response=client.post('/sklad/orders/new',data=dict(csrf_token=csrf,token='first-real-order',customer_id='1',model_id='1',qty='100',price='',quantity_unit='pair',price_unit='pair',price_kind='unit',settlement='proportional'))
    assert response.status_code==302 and '/orders/' in response.location
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM models').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0]==0
    print('PASS: empty installation and repeated startup, no blank reference cards, no dummy records, first real order from zero')
