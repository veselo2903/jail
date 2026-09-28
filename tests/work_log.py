"""A factory day sheet records quantities atomically and accrues director-approved wages."""
import os,secrets,sys,tempfile
from datetime import timedelta
from pathlib import Path
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
with tempfile.TemporaryDirectory(prefix='jail-work-log-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db');os.environ['JAIL_SECRET_KEY']='isolated-work-log'
    from jail.app import app,db
    from jail import business_core as core
    app.testing=True;client=app.test_client();role='director'
    def switch(value):
        global role,csrf
        role=value;client.get('/login/'+value,follow_redirects=True)
        with client.session_transaction() as state:csrf=state['csrf_token']
    switch('director')
    def post(path,values):
        data=MultiDict()
        for name,value in values.items():
            if isinstance(value,list):data.setlist(name,[str(x) for x in value])
            else:data[name]=str(value)
        data['csrf_token']=csrf;data.setdefault('token',secrets.token_hex(12))
        return client.post('/'+role+path,data=data)
    def scalar(sql,args=()):
        with db.get_db() as conn:return conn.execute(sql,args).fetchone()[0]
    def created(response):return int(response.location.rsplit('/',1)[1])
    cid=created(post('/customers/new',dict(name='Север')))
    assert client.get('/director/models/new').status_code==302
    page=client.get('/director/models/new?customer='+str(cid))
    assert 'name="customer_id" value="1"' in page.text and '<select name="customer_id"' not in page.text
    assert post('/models/new',dict(name='Нельзя без заказчика',customer_id=cid)).status_code==400
    assert scalar('SELECT COUNT(*) FROM models')==0
    assert post('/models/new?customer=1',dict(name='Не та фирма',customer_id='2')).status_code==302
    assert scalar('SELECT COUNT(*) FROM models')==0
    mid=created(post('/models/new?customer=1',dict(name='Ботинки 1',customer_id=cid,sale_price='1000')))
    oid=created(post('/orders/new',dict(customer_id=cid,model_id=mid,qty='10',price='1000')))
    bid=scalar('SELECT id FROM production_batches WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?)',(oid,))
    post('/batches/'+str(bid),dict(action='operations_bulk',operation_id='1',rate_1='25'))
    tid=scalar('SELECT id FROM batch_operations WHERE batch_id=?',(bid,))
    w1=created(post('/staff/new',dict(name='Иван')));w2=created(post('/staff/new',dict(name='Мария')))
    switch('sklad')
    assert post('/batches/'+str(bid),dict(action='operation_edit',task_id=tid,rate='50',qty='10',mode='internal')).status_code==403
    assert post('/batches/'+str(bid),dict(action='operations_bulk',operation_id='2',rate_2='12')).status_code==403
    assert scalar('SELECT rate_cents FROM batch_operations WHERE id=?',(tid,))==2500
    assert post('/work-log',dict(action='daily_work',worked_on=core.today().isoformat(),worker_id=w1,batch_id=bid,task_id=tid,qty='1')).status_code==403
    switch('proizv')
    assert post('/batches/'+str(bid),dict(action='operation_edit',task_id=tid,rate='50',qty='10',mode='internal')).status_code==403
    day=core.today().isoformat()
    sheet=dict(action='daily_work',worked_on=day,token='day-1',worker_id=[w1,w2],batch_id=[bid,bid],task_id=[tid,tid],qty=['4','3'])
    assert post('/work-log',sheet).status_code==302
    assert scalar('SELECT COUNT(*) FROM work_acceptances')==2
    assert scalar('SELECT SUM(amount_cents) FROM payroll_accruals')==17500
    assert scalar('SELECT SUM(qty_pairs) FROM work_acceptances WHERE canceled_at IS NULL')==7
    post('/work-log',sheet)
    assert scalar('SELECT COUNT(*) FROM work_acceptances')==2
    bad={**sheet,'token':'over-volume','qty':['2','2']}
    assert post('/work-log',bad).status_code==302
    assert scalar('SELECT COUNT(*) FROM work_acceptances')==2
    assert scalar('SELECT SUM(amount_cents) FROM payroll_accruals')==17500
    factory_html=client.get('/proizv/work-log?date='+day).text
    assert 'Иван' in factory_html and 'Мария' in factory_html and 'Записать с бумажного листа' in factory_html
    assert '175,00' not in factory_html and '25,00' not in factory_html
    assert 'name="rate"' not in factory_html and 'name="rate_1"' not in client.get('/proizv/batches/'+str(bid)).text
    aid=scalar('SELECT id FROM work_acceptances WHERE token=?',('day-1:0',))
    assert post('/work-log',dict(action='reverse',worked_on=day,acceptance_id=aid)).status_code==302
    assert scalar('SELECT SUM(amount_cents) FROM payroll_accruals')==7500
    assert scalar('SELECT SUM(qty_pairs) FROM work_acceptances WHERE canceled_at IS NULL')==3
    switch('director')
    director_html=client.get('/director/work-log?date='+day).text
    assert '75,00' in director_html and 'Сохранённая выработка' in director_html
    assert 'work-log-form' not in director_html
    print('PASS daily factory sheet: scoped models, director-only rates, automatic wages, atomic save, duplicate safety, correction and role privacy')
