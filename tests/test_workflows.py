"""Meaningful regression checks against isolated databases, never the live database."""
import os, sys, tempfile, unittest, json, sqlite3, re
from pathlib import Path
TMP=tempfile.TemporaryDirectory(prefix='jail-tests-')
os.environ['JAIL_DB_PATH']=TMP.name+'/startup.db';os.environ['JAIL_COOKIE_PATH']='/';os.environ['JAIL_COOKIE_SECURE']='0'
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app as module, db
from flask import g

class Workflows(unittest.TestCase):
 def setUp(self):
  db.DB_PATH=TMP.name+'/'+self._testMethodName+'.db';module._CACHE.clear();db.init_db()
  self.app=module.app;self.app.testing=True;self.client=self.app.test_client()
  c=db.get_db();c.execute("INSERT INTO customers(id,name) VALUES(1,'Customer')");c.execute("INSERT INTO models(id,name) VALUES(1,'Model')");c.commit();c.close()
 def role(self,name):
  with self.client.session_transaction() as s:s['role']=name
 def sql(self,sql,args=()):
  c=db.get_db()
  try:
   rows=c.execute(sql,args).fetchall();c.commit();return rows
  finally:c.close()
 def seed(self,n=10):
  sig=json.dumps([['Operation',10.0]])
  self.sql("INSERT INTO requests(id,status,created_role,created_at,accepted_at) VALUES(1,'accepted','sklad',?,?)",(db.now_str(),db.now_str()))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,customer_id,model_id,collected,recv) VALUES(1,1,'','pair',1,1,?,?)",(n,n))
  self.sql("INSERT INTO item_ops(kind,item_id,operation_id,name,price) VALUES('req',1,1,'Operation',10)")
  self.sql("INSERT INTO stock_moves(created_at,kind,item_type,customer_id,model_id,qty) VALUES(?,'in','pair',1,1,?)",(db.now_str(),n));return sig
 def test_duplicate_intake(self):
  sig=self.seed();self.role('proizv');key=module._fin_key(dict(cid=1,mid=1,lot=sig),'ready')
  self.client.post('/stock/intake',data={'k_0':key,'q_0':'8','k_1':key,'q_1':'8'})
  self.assertEqual(self.sql('select count(*) from finish')[0][0],0)
 def test_duplicate_loading(self):
  sig=self.seed();self.sql("INSERT INTO finish(created_at,customer_id,model_id,lot,kind,pairs,role) VALUES(?,1,1,?,'ready',10,'proizv')",(db.now_str(),sig))
  self.role('proizv');key=module._fin_key(dict(cid=1,mid=1,lot=sig),'ready')
  self.client.post('/docs/load',data={'k_0':key,'q_0':'8','k_1':key,'q_1':'8'})
  self.assertEqual(self.sql('select count(*) from documents')[0][0],0);self.assertEqual(self.sql('select sum(qty) from stock_moves')[0][0],10)
 def test_happy_debt_price_and_repeat(self):
  sig=self.seed();key=module._fin_key(dict(cid=1,mid=1,lot=sig),'ready');self.role('proizv')
  self.client.post('/stock/intake',data={'k_0':key,'q_0':'10'});self.client.post('/docs/load',data={'k_0':key,'q_0':'10'})
  self.role('sklad');self.client.post('/acceptance/sklad/accept',data={'recv_1':'10'});self.client.post('/acceptance/sklad/accept',data={'recv_1':'10'})
  self.assertEqual(self.sql('select sum(qty) from wh_moves')[0][0],10)
  with self.app.test_request_context('/debt'):
   g.db=db.get_db();self.assertEqual(module.compute_debt()['total'],100);g.db.close()
  self.role('director');self.client.post('/refs/operations/save',data={'price_1':'999.12'})
  with self.app.test_request_context('/debt'):
   g.db=db.get_db();self.assertEqual(module.compute_debt()['total'],100);g.db.close()
 def test_pipe_defect_load(self):
  sig=self.seed();self.role('proizv');key=module._fin_key(dict(cid=1,mid=1,lot=sig),'ready')
  self.client.post('/stock/intake/brak',data={'k':key,'pairs':'2','note':'crack | sole'})
  key=module._fin_key(dict(cid=1,mid=1,lot=sig,note='crack | sole'),'brak');self.client.post('/docs/load',data={'k_0':key,'q_0':'2'})
  self.assertEqual(self.sql('select sum(pairs_sent) from lines')[0][0],2)
 def test_payment_repeat_cancel_history(self):
  self.role('director');data={'amount':'123.455','action_token':'unique-payment','payment_date':'2026-10-02'}
  self.client.post('/debt/pay',data=data);self.client.post('/debt/pay',data=data)
  row=self.sql('select * from payments')[0];self.assertEqual(row['amount_kopeks'],12346);self.assertEqual(len(self.sql('select * from payments')),1)
  self.client.post('/debt/pay/1/delete',data={'reason':'Mistake'});self.assertIsNotNone(self.sql('select cancelled_at from payments')[0][0])
  self.assertEqual(self.client.get('/debt').status_code,200)
  self.assertEqual(self.sql('select cancelled_by from payments')[0][0],'director')
 def test_negative_material(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(1,'shipped','sklad',?)",(db.now_str(),))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,collected,unit) VALUES(1,1,'Material','material',10,'шт')")
  self.role('proizv');self.client.post('/acceptance/accept',data={'recv_1':'-3'})
  self.assertEqual(self.sql('select status from requests')[0][0],'shipped')
 def test_partial_transfer_not_deleted(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(1,'shipped','sklad',?)",(db.now_str(),))
  for i in (1,2):self.sql("INSERT INTO request_items(id,request_id,item,line_kind,collected,unit) VALUES(?,1,'Material','material',10,'шт')",(i,))
  self.role('proizv');self.client.post('/acceptance/accept',data={'recv_1':'10'})
  self.role('director');self.client.post('/requests/2/delete')
  self.assertEqual(self.sql('select status from requests where id=2')[0][0],'shipped')
 def test_partial_return_not_deleted(self):
  self.sql("INSERT INTO documents(id,kind,status,created_role,created_at,sent_at) VALUES(1,'RETURN','sent','proizv',?,?)",(db.now_str(),db.now_str()))
  for i in (1,2):self.sql("INSERT INTO lines(id,document_id,customer_id,model_id,status,pairs_sent) VALUES(?,1,1,1,'gotovoe',5)",(i,))
  self.role('sklad');self.client.post('/acceptance/sklad/accept',data={'recv_1':'5'})
  self.role('director');self.client.post('/docs/2/delete');self.assertEqual(self.sql('select status from documents where id=2')[0][0],'sent')
 def test_receipt_conflict(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(1,'shipped','sklad',?)",(db.now_str(),))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,collected,unit) VALUES(1,1,'Material','material',10,'шт')")
  self.role('proizv');r=self.client.post('/acceptance/save',data={'recv_1':'9','rev_1':'0'});self.assertEqual(r.status_code,200)
  self.assertEqual(self.client.post('/acceptance/save',data={'recv_1':'8','rev_1':'0'}).status_code,409)
  self.assertEqual(self.sql('select recv from request_items')[0][0],9)
 def test_no_empty_draft(self):
  self.role('sklad');self.client.post('/docs/collect/blank');self.assertEqual(self.client.get('/requests/0').status_code,200)
  self.assertEqual(self.sql('select count(*) from requests')[0][0],0)
  self.role('proizv');self.client.post('/requests/new');self.assertEqual(self.client.get('/requests/0').status_code,200)
  self.assertEqual(self.sql('select count(*) from requests')[0][0],0)
 def test_first_virtual_item(self):
  self.sql("INSERT INTO materials(name,unit) VALUES('Material','шт')")
  for role,url,data in [('proizv','/requests/0/item/add',{'item':'Material','qty':'5'}),('sklad','/requests/0/material/add',{'item':'Material','qty':'5'})]:
   self.role(role);r=self.client.post(url,data=data,headers={'X-Requested-With':'fetch'});self.assertEqual(r.status_code,200);self.assertTrue(r.json['redirect']);self.assertEqual(self.client.get(r.json['redirect']).status_code,200)
 def test_fractional_pair_and_duplicate_ref(self):
  self.sql("INSERT INTO wh_moves(created_at,kind,customer_id,model_id,quality,qty) VALUES(?,'in',1,1,'ready',10)",(db.now_str(),))
  self.role('sklad');self.client.post('/wh/writeoff',data={'cid':'1','mid':'1','quality':'ready','qty':'0.5'});self.assertEqual(self.sql('select sum(qty) from wh_moves')[0][0],10)
  self.sql("INSERT INTO customers(id,name) VALUES(2,'Other')");self.client.post('/refs/customers/2/edit',data={'name':'Customer'});self.assertEqual(self.sql('select name from customers where id=2')[0][0],'Other')
 def test_ids_integrity_and_backup(self):
  self.sql("INSERT INTO documents(kind,status,created_role,created_at) VALUES('RETURN','draft','proizv',?)",(db.now_str(),));self.sql('delete from documents where id=1')
  self.sql("INSERT INTO documents(kind,status,created_role,created_at) VALUES('RETURN','draft','proizv',?)",(db.now_str(),));self.assertEqual(self.sql('select id from documents')[0][0],2)
  row=self.sql('select number,document_uuid from documents')[0];self.assertEqual(row['number'],2);self.assertEqual(len(row['document_uuid']),32)
  with self.assertRaises(sqlite3.IntegrityError):self.sql('update documents set number=1 where id=2')
  with self.assertRaises(sqlite3.IntegrityError):self.sql("INSERT INTO lines(document_id,customer_id,model_id,pairs_sent) VALUES(999,1,1,1)")
  c=sqlite3.connect(db.DB_PATH);target=sqlite3.connect(TMP.name+'/backup.db');c.backup(target);self.assertEqual(target.execute('pragma integrity_check').fetchone()[0],'ok');target.close();c.close()
 def test_page_and_roles(self):
  for role,paths in {'sklad':['/wh','/docs','/acceptance','/refs'],'proizv':['/stock','/docs','/acceptance','/stock/intake','/stock/intake/brak'],'director':['/overview','/stats','/debt','/refs']}.items():
   self.role(role)
   for path in paths:self.assertEqual(self.client.get(path).status_code,200,(role,path))
  self.role('proizv');self.assertEqual(self.client.post('/debt/pay',data={'amount':'100','action_token':'x'}).status_code,403)
 def test_discrepancy_counts_and_resolution(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at,accepted_at) VALUES(1,'accepted','sklad',?,?)",(db.now_str(),db.now_str()))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,collected,recv,unit) VALUES(1,1,'Material','material',10,8,'шт')")
  self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(2,'accepted','sklad',?)",(db.now_str(),))
  self.sql("INSERT INTO request_items(request_id,item,line_kind,collected,recv,unit) VALUES(2,'Material','material',10,NULL,'шт')")
  def check_counts():
   module._CACHE.clear()
   with self.app.test_request_context('/discrepancies'):
    g.db=db.get_db()
    for role in ('sklad','proizv','director'):
     expected=sum(module._disc_needs(x,role) for x in module._disc_cases())
     self.assertEqual(module._open_discr_count(role),expected)
    g.db.close()
  check_counts();self.role('sklad');self.client.post('/discrepancies/req/1/propose',data={'pick_1':'found'});check_counts()
  self.role('proizv');self.client.post('/discrepancies/req/1/answer',data={'action':'agree'});check_counts()
  self.assertEqual(self.sql('select status from discr_case')[0][0],'closed')
 def test_find_ref_transaction_rollback(self):
  self.sql('update customers set archived=1 where id=1')
  with self.app.test_request_context('/'):
   g.db=db.get_db();g.db.execute('BEGIN IMMEDIATE');module._find_ref('customers','Customer');g.db.rollback();g.db.close()
  self.assertEqual(self.sql('select archived from customers where id=1')[0][0],1)
  self.role('sklad');self.client.post('/refs/customers/add',data={'name':'Customer'})
  self.assertEqual(self.sql('select archived from customers where id=1')[0][0],0)
 def test_collect_invalid_without_empty_transfer(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(1,'open','proizv',?)",(db.now_str(),))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,qty) VALUES(1,1,'Material','need',NULL)")
  self.role('sklad');r=self.client.post('/docs/collect/save',data={'placed_1':'1','col_1':''});self.assertEqual(r.status_code,302)
  self.assertEqual(len(self.sql('select * from requests')),1)
  self.assertEqual(self.sql('select placed from request_items')[0][0],0)

 def test_sklad_cannot_delete_production_request(self):
  self.role('sklad')
  for rid,status in enumerate(('open','progress','done'),start=1):
   self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(?,?,'proizv',?)",(rid,status,db.now_str()))
   self.sql("INSERT INTO request_items(request_id,item,line_kind,qty) VALUES(?,'Material','need',5)",(rid,))
   self.assertNotIn(f'action="/requests/{rid}/delete"',self.client.get(f'/requests/{rid}').text)
   self.assertEqual(self.client.post(f'/requests/{rid}/delete').status_code,403)
   self.assertEqual(self.sql('select status from requests where id=?',(rid,))[0][0],status)
  self.assertTrue(module._can_delete_req({'created_role':'sklad','status':'progress'},'sklad'))
  self.assertTrue(module._can_delete_req({'created_role':'proizv','status':'open','transfer_id':None},'proizv'))

 def test_partial_production_order_keeps_remainder(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at,urgent,note) VALUES(1,'open','proizv',?,1,'Order comment')",(db.now_str(),))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,qty,unit,urgent,note) VALUES(1,1,'Leather','need',10,'м2',1,'Item comment')")
  self.role('sklad');view=self.client.get('/requests/1').text
  self.assertNotIn('Подробнее',view);self.assertIn('10 м2',view);self.assertIn('Item comment',view);self.assertIn('Order comment',view)
  self.client.post('/docs/collect/save',data={'placed_1':'1','col_1':'5'})
  transfer=self.sql("select id from requests where created_role='sklad'")[0][0]
  self.client.post(f'/requests/{transfer}/ship')
  view=self.client.get('/requests/1').text;self.assertIn('5 м2',view);self.assertIn('В пути',view)
  item=self.sql('select id from request_items where request_id=?',(transfer,))[0][0]
  self.role('proizv');self.client.post('/acceptance/accept',data={f'recv_{item}':'5'})
  self.assertEqual(self.sql('select status from requests where id=1')[0][0],'open')
  self.assertEqual(self.sql('select delivered from request_items where id=1')[0][0],5)
  self.role('sklad');view=self.client.get('/requests/1').text;self.assertIn('5 м2',view);self.assertIn('Получено 5 м2',view)
  self.client.post('/docs/collect/save',data={'placed_1':'1','col_1':'5'})
  transfer=self.sql("select id from requests where created_role='sklad' and status='progress'")[0][0]
  self.client.post(f'/requests/{transfer}/ship');item=self.sql('select id from request_items where request_id=?',(transfer,))[0][0]
  self.role('proizv');self.client.post('/acceptance/accept',data={f'recv_{item}':'5'})
  self.assertEqual(self.sql('select delivered from request_items where id=1')[0][0],10)
  self.assertEqual(self.sql('select status from requests where id=1')[0][0],'accepted')

 def test_collect_quantity_without_checkbox(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(1,'open','proizv',?)",(db.now_str(),))
  self.sql("INSERT INTO request_items(id,request_id,item,line_kind,qty,delivered,unit) VALUES(1,1,'Leather','need',10,5,'шт')")
  self.role('sklad');view=self.client.get('/docs/collect').text
  self.assertNotIn('type="checkbox"',view);self.assertIn('data-quantity="5"',view)
  self.client.post('/docs/collect/save',data={'quantity_selection':'1','col_1':'3'})
  self.assertEqual(self.sql('select collected,placed from request_items where id=1')[0]['collected'],3)
  self.assertEqual(self.sql("select collected from request_items where line_kind='material'")[0][0],3)
  self.client.post('/docs/collect/save',data={'quantity_selection':'1','col_1':''})
  self.assertEqual(self.sql('select placed from request_items where id=1')[0][0],0)
  self.assertEqual(len(self.sql("select * from request_items where line_kind='material'")),0)

 def test_warehouse_list_contains_expandable_order_items(self):
  self.sql("INSERT INTO requests(id,status,created_role,created_at,note) VALUES(1,'open','proizv',?,'General comment')",(db.now_str(),))
  self.sql("INSERT INTO request_items(request_id,item,line_kind,qty,unit,urgent,note) VALUES(1,'Leather','need',10,'м2',1,'Urgent item')")
  self.sql("INSERT INTO request_items(request_id,item,line_kind,qty,delivered,unit) VALUES(1,'Soles','need',20,5,'шт')")
  self.role('sklad');view=self.client.get('/docs').text
  self.assertIn('sklad-request-spoiler',view);self.assertIn('Развернуть все',view)
  for text in ['Leather','Soles','10 м2','15 шт','General comment','Urgent item']:
   self.assertIn(text,view)
  self.assertNotIn('onclick="location',view)
  self.assertEqual(self.sql('select status from requests where id=1')[0][0],'open')

 def test_warehouse_filters_partial_and_transit(self):
  for rid,status,name,got,sent in [(1,'open','NewItem',0,None),(2,'open','PartialItem',5,None),(3,'shipped','SentPartialItem',0,5),(4,'shipped','SentFullItem',0,10)]:
   self.sql("INSERT INTO requests(id,status,created_role,created_at) VALUES(?,?,'proizv',?)",(rid,status,db.now_str()))
   self.sql("INSERT INTO request_items(request_id,item,line_kind,qty,delivered,collected,unit) VALUES(?,?,'need',10,?,?,'шт')",(rid,name,got,sent))
  self.role('sklad')
  collect=self.client.get('/docs?sub=collect').text
  self.assertIn('NewItem',collect);self.assertNotIn('PartialItem',collect)
  remaining=self.client.get('/docs?sub=remaining').text
  self.assertIn('PartialItem',remaining);self.assertIn('SentPartialItem',remaining);self.assertNotIn('SentFullItem',remaining)
  self.assertIn('sklad-shortage-preview',remaining);self.assertIn('5 шт',remaining)
  transit=self.client.get('/docs?sub=transit').text
  self.assertIn('SentPartialItem',transit);self.assertIn('SentFullItem',transit);self.assertNotIn('NewItem',transit)

 def test_errors_have_plain_messages_for_forms(self):
  self.role('sklad')
  r=self.client.post('/wh/writeoff',data={},headers={'X-Requested-With':'fetch'})
  self.assertEqual(r.status_code,400);self.assertFalse(r.json['ok']);self.assertIn('Проверьте',r.json['error'])
  r=self.client.post('/debt/pay',headers={'X-Requested-With':'fetch'})
  self.assertEqual(r.status_code,403);self.assertIn('роли',r.json['error'])
  r=self.client.get('/missing-page');self.assertEqual(r.status_code,404);self.assertNotIn('class="big">404',r.text)

if __name__=='__main__':unittest.main(verbosity=2)
