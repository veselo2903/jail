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
        response=client.get('/'+role+'/refs')
        assert response.status_code==200
        assert 'refs-list card' not in response.text and 'Показать архив' not in response.text
        if role=='proizv':assert 'refs-section' not in response.text
        else: assert response.text.count('refs-add card')==3
    client.get('/login/sklad')
    assert client.get('/sklad/orders/new').status_code==200
    with client.session_transaction() as session:csrf=session['csrf_token']
    for table in ('customers','models','workers'):
        assert client.post('/sklad/refs/'+table+'/add',data={'csrf_token':csrf,'name':'   ','number':' '}).status_code==302
    with db.get_db() as conn:
        assert all(conn.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]==0 for t in ('customers','models','workers'))
    response=client.post('/sklad/orders/new',data=dict(csrf_token=csrf,token='first-real-order',customer_id='new',new_customer='Фирма Север',model_id='new',new_model='Ботинки 714',qty='100',price='900',quantity_unit='pair',price_unit='pair',price_kind='unit',settlement='proportional'))
    assert response.status_code==302 and '/batches/' in response.location
    db.init_db()
    with db.get_db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM customers').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM models').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM workers').fetchone()[0]==0
    print('PASS: empty installation and repeated startup, no blank reference cards, no dummy records, first real order from zero')
