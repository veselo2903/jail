"""First-use workflows, atomic shortcuts, retries and production visibility."""
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from werkzeug.datastructures import MultiDict
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))

with tempfile.TemporaryDirectory(prefix='jail-experience-') as folder:
    os.environ['JAIL_DB_PATH']=str(Path(folder)/'test.db')
    os.environ['JAIL_SECRET_KEY']='experience-isolated'
    from jail.app import app,db
    app.testing=True
    client=app.test_client()
    assert client.get('/login/proizv').location.endswith('/proizv/batches')
    client.get('/login/sklad',follow_redirects=True)
    with client.session_transaction() as session: csrf=session['csrf_token']
    def post(path,data,follow=False):
        fields=MultiDict(data);fields['csrf_token']=csrf;fields.setdefault('token',secrets.token_hex(8))
        return client.post('/sklad'+path,data=fields,follow_redirects=follow)
    def one(sql,args=()):
        with db.get_db() as conn: return conn.execute(sql,args).fetchone()
    def count(table): return one('SELECT COUNT(*) c FROM '+table)['c']
    new_order=dict(customer_id='new',new_customer='Фирма Север',model_id='new',new_model='Ботинки 80',qty='80',price='950',quantity_unit='pair',price_unit='pair',price_kind='unit',settlement='proportional')
    before=(count('customers'),count('models'))
    assert post('/orders/new',{**new_order,'qty':'oops'}).status_code==200
    assert before==(count('customers'),count('models'))
    result=post('/orders/new',{**new_order,'token':'first-order'})
    assert result.status_code==302 and '/batches/' in result.location
    bid=int(result.location.rsplit('/',1)[1]);path='/batches/'+str(bid)
    assert post('/orders/new',{**new_order,'token':'first-order'}).location==result.location
    assert count('customers')==before[0]+1 and count('models')==before[1]+1
    assert 'Выбрать операции' in client.get(result.location).text
    cid=one('SELECT customer_id FROM production_batches WHERE id=?',(bid,))['customer_id']
    legacy=dict(customer_id=str(cid),model_id='1',new_model='',qty='2',price='20',quantity_unit='pair',price_unit='pair',price_kind='unit',settlement='proportional')
    assert post('/orders/new',legacy).status_code==200
    assert one('SELECT customer_id FROM models WHERE id=1')['customer_id'] is None
    assert post('/orders/new',{**legacy,'claim_model_0':'1'}).status_code==302
    assert one('SELECT customer_id FROM models WHERE id=1')['customer_id']==cid
    post(path,dict(action='operations_bulk',operation_id='1'))
    tid=one('SELECT id FROM batch_operations WHERE batch_id=?',(bid,))['id']
    assert 'Указать цену' in client.get('/sklad'+path).text
    post(path,dict(action='operation_edit',task_id=str(tid),qty='80',rate='25',mode='internal',effective_date='2026-09-27'))
    assert one('SELECT rate_cents FROM batch_operations WHERE id=?',(tid,))['rate_cents']==2500
    material=dict(action='material',material_id='new',new_material='Подошвы 80',new_unit='pary',qty='80',price='110')
    post(path,material)
    mid=one("SELECT id FROM materials WHERE name='Подошвы 80'")['id']
    assert 'Передать материалы' in client.get('/sklad'+path).text
    before=count('materials')
    bad=dict(action='material_move',kind='receipt',material_id='new',new_material='Без остатка',new_unit='kg',qty='no',cost='500')
    response=post('/inventory',bad,True)
    assert response.status_code==200 and 'biz-retry-fields' in response.text and count('materials')==before
    assert json.loads(re.search(r'id="biz-retry-fields">(.*?)</script>',response.text,re.S).group(1))['new_material']==['Без остатка']
    assert 'biz-retry-fields' not in client.get('/sklad/inventory').text
    receipt=dict(action='material_move',kind='receipt',material_id=str(mid),qty='80',cost='8800',token='receipt-one')
    post('/inventory',receipt);post('/inventory',receipt)
    assert one('SELECT qty_milli FROM stock_balances WHERE material_id=?',(mid,))['qty_milli']==80000
    post(path,dict(action='material_move',kind='issue',material_id=str(mid),qty='80'))
    assert 'Записать работу' in client.get('/sklad'+path).text
    before=count('workers')
    work=dict(action='work',task_id=str(tid),worker_id='new',new_worker_name='Иван',share='100',qty='81',worked_on='2026-09-27')
    assert 'biz-retry-fields' in post(path,work,True).text
    assert count('workers')==before and count('work_acceptances')==0
    work.update(qty='4',token='work-one');post(path,work);post(path,work)
    assert count('workers')==before+1 and count('work_acceptances')==1
    assert one('SELECT amount_cents FROM payroll_accruals')['amount_cents']==10000
    # Large failed forms render directly, without cookie truncation or orphan records.
    response=post(path,{**work,'token':'large-error','qty':'999','note':secrets.token_hex(9000)})
    assert response.status_code==200 and 'biz-retry-fields' in response.text
    assert count('work_acceptances')==1 and count('workers')==before+1
    supply=dict(send_token='supply-retry',inventory_tracking='1',party_id=['2','5'],party_batch_2=str(bid),party_batch_5=str(bid),party_material_id_2=str(mid),party_material_qty_2='999',party_material_owner_2='',party_material_id_5=str(mid),party_material_qty_5='1',party_material_owner_5='',ship_note='Сохранить после ошибки')
    response=post('/requests/supply/new',supply,True)
    assert response.status_code==200 and 'biz-retry-fields' in response.text and count('shipments')==0
    client.get('/login/proizv')
    response=client.get('/proizv'+path)
    stock=json.loads(re.search(r'id="biz-stock-data">(.*?)</script>',response.text,re.S).group(1))
    assert stock['warehouse']==[]
    assert all('value_cents' not in r and 'cost_cents' not in r for r in stock['production'])
    assert 'Экономика партии' not in response.text and 'Завершить партию' not in response.text
    assert client.get('/proizv/payroll/ledger').status_code==403
    print('PASS: role entry, inline order and ownership, atomic catalog/work shortcuts, idempotence, guidance, small/large retries, supply retry, production visibility')
