"""Business rules. All quantities and money use integers, never binary floats."""
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import json
import math

class RuleError(ValueError):
    pass


BUSINESS_TZ = timezone(timedelta(hours=3))


def today():
    return datetime.now(BUSINESS_TZ).date()


def now():
    return datetime.now(BUSINESS_TZ).isoformat(timespec='seconds')


def scaled(raw, scale=100, label='Сумма', positive=False):
    try:
        value = Decimal(str(raw or '').strip().replace(',', '.')) * scale
        if not value.is_finite() or value != value.to_integral_value() or value < (1 if positive else 0) or value > 10**12:
            raise ValueError
        return int(value)
    except (ValueError, InvalidOperation):
        raise RuleError(label + ': укажите корректное число' + (' больше нуля.' if positive else '.'))


def integer(raw, label='Количество', positive=True):
    return scaled(raw, 1, label, positive)


def day(raw=None):
    try:
        value = date.fromisoformat(raw or today().isoformat())
        if value > today():
            raise ValueError
        return value.isoformat()
    except ValueError:
        raise RuleError('Укажите дату, которая уже наступила.')


def require(conn, table, rid, active=False):
    allowed = {'customers','models','workers','orders','order_items','production_batches','batch_operations','materials','work_acceptances','payroll_accruals','deliveries'}
    if table not in allowed:
        raise RuntimeError('Unknown table')
    row = conn.execute('SELECT * FROM '+table+' WHERE id=?'+(' AND archived=0' if active else ''), (rid,)).fetchone()
    if not row:
        raise RuleError('Выбранная запись не найдена или находится в архиве.')
    return row


def editable_batch(conn, bid):
    b = require(conn, 'production_batches', bid)
    if b['status'] in ('closed','canceled'):
        raise RuleError('Партия закрыта. Изменения недоступны.')
    return b


def is_closed(conn, dated):
    return bool(conn.execute('SELECT 1 FROM payroll_periods WHERE ? BETWEEN start_on AND end_on', (dated,)).fetchone())


def posting_day(conn, worked):
    # Late work retains its factual date; accounting goes to the current open day.
    posted = today().isoformat() if is_closed(conn, worked) else worked
    if is_closed(conn, posted):
        raise RuleError('Текущий расчётный период закрыт.')
    return posted


def audit(conn, actor, action, target, details):
    conn.execute('INSERT INTO audit_events(at,actor_role,action,target,details) VALUES (?,?,?,?,?)',
                 (now(), actor, action, str(target), json.dumps(details, ensure_ascii=False)))


def create_order(conn, form, actor):
    token = form.get('token','')
    if not token or len(token)>128:
        raise RuleError('Обновите форму заказа.')
    old = conn.execute('SELECT id FROM orders WHERE token=?',(token,)).fetchone()
    if old:
        return old['id']
    if form.get('customer_id')=='new':
        name=form.get('new_customer','').strip()
        if not name: raise RuleError('Введите название нового заказчика.')
        cid=conn.execute('INSERT INTO customers(name) VALUES (?)',(name,)).lastrowid
    else:
        cid = integer(form.get('customer_id'), 'Заказчик')
        require(conn, 'customers', cid, True)
    due = form.get('due_date') or None
    if due:
        try: date.fromisoformat(due)
        except ValueError: raise RuleError('Проверьте срок заказа.')
    fields = {key:form.getlist(key) for key in ('model_id','new_model','qty','quantity_unit','price_kind','price_unit','price','specification','settlement')}
    rows = []
    for n in range(len(fields['qty'])):
        val = lambda key, default='': fields[key][n] if n<len(fields[key]) else default
        if not any(val(k).strip() for k in ('qty','price','new_model','model_id')):
            continue
        qty = integer(val('qty'), 'Количество')
        qunit, punit, pkind = val('quantity_unit','pair'), val('price_unit','pair'), val('price_kind','unit')
        if qunit not in ('pair','shoe') or punit not in ('pair','shoe') or pkind not in ('unit','total'):
            raise RuleError('Проверьте единицу количества и способ цены.')
        if qunit=='shoe' and qty%2:
            raise RuleError('Количество ботинок должно быть чётным: производство ведётся в парах.')
        pairs = qty if qunit=='pair' else qty//2
        if pairs>1_000_000:
            raise RuleError('В одной позиции допускается до 1 000 000 пар.')
        if val('new_model').strip():
            mid = conn.execute('INSERT INTO models(name,customer_id) VALUES (?,?)', (val('new_model').strip(),cid)).lastrowid
        else:
            mid = integer(val('model_id'),'Модель')
            model = require(conn,'models',mid,True)
            if model['customer_id'] is None and form.get('claim_model_'+str(n))=='1':
                conn.execute('UPDATE models SET customer_id=? WHERE id=?',(cid,mid))
                audit(conn,actor,'model_customer',mid,{'customer_id':cid,'reason':'Подтверждено при создании заказа'})
            elif model['customer_id'] != cid:
                raise RuleError('Модель должна принадлежать выбранному заказчику. Укажите владельца в справочнике или создайте новую модель.')
        price = scaled(val('price'),label='Цена')
        total = price if pkind=='total' else price*pairs*(2 if punit=='shoe' else 1)
        settlement = val('settlement','proportional')
        if settlement not in ('proportional','complete'): raise RuleError('Проверьте условие приёмки.')
        rows.append((mid,pairs,qty,qunit,pkind,punit,price if pkind=='unit' else None,total,settlement,val('specification').strip()))
    if not rows: raise RuleError('Добавьте модель и количество для заказа.')
    oid = conn.execute('INSERT INTO orders(customer_id,created_at,due_date,note,token) VALUES (?,?,?,?,?)',
                      (cid,now(),due,form.get('note','').strip(),token)).lastrowid
    for r in rows:
        iid = conn.execute('''INSERT INTO order_items(order_id,model_id,qty_pairs,original_qty,quantity_unit,price_kind,price_unit,unit_price_cents,total_cents,settlement,specification)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''', (oid,*r)).lastrowid
        bid = conn.execute('''INSERT INTO production_batches(order_item_id,customer_id,model_id,qty_pairs,due_date,specification,created_at,token)
            VALUES (?,?,?,?,?,?,?,?)''', (iid,cid,r[0],r[1],due,r[-1],now(),token+':'+str(iid))).lastrowid
        conn.execute('UPDATE production_batches SET contract_cents=? WHERE id=?',(r[7],bid))
        apply_template(conn,bid)
    audit(conn,actor,'order_create',oid,{'positions':len(rows)})
    return oid


def add_operation(conn, bid, form, actor):
    b = editable_batch(conn,bid)
    opid = integer(form.get('operation_id'),'Операция')
    if not conn.execute('SELECT 1 FROM operations WHERE id=?',(opid,)).fetchone(): raise RuleError('Выберите операцию.')
    qty = integer(form.get('qty') or b['qty_pairs'])
    if qty>b['qty_pairs']: raise RuleError('Объём операции превышает размер партии.')
    mode = form.get('mode','internal')
    if mode not in ('internal','external','ready','skip'): raise RuleError('Проверьте способ выполнения.')
    rate = scaled(form['rate']) if form.get('rate','').strip() else None
    minutes = scaled(form['minutes'],1000,'Норма времени') if form.get('minutes','').strip() else None
    parent = integer(form.get('parent_id')) if form.get('parent_id') else None
    reason = form.get('reason','').strip()
    if parent:
        task = require(conn,'batch_operations',parent)
        if task['batch_id']!=bid or not reason: raise RuleError('Для переделки выберите операцию этой партии и укажите причину.')
        if conn.execute('SELECT 1 FROM batch_operations WHERE parent_id=? AND reason=?',(parent,reason)).fetchone():
            raise RuleError('Такая переделка уже добавлена.')
    elif conn.execute('SELECT 1 FROM batch_operations WHERE batch_id=? AND operation_id=? AND parent_id IS NULL',(bid,opid)).fetchone():
        raise RuleError('Эта операция уже есть в плане. Измените существующую строку.')
    taskid = conn.execute('''INSERT INTO batch_operations(batch_id,operation_id,qty_pairs,rate_cents,mode,minutes_milli,position,parent_id,reason)
                  VALUES (?,?,?,?,?,?,?,?,?)''',(bid,opid,qty,rate,mode,minutes,opid,parent,reason)).lastrowid
    conn.execute('INSERT INTO operation_rate_versions(batch_operation_id,version,rate_cents,effective_date,at,actor,reason) VALUES (?,1,?,?,?,?,?)',
                 (taskid,rate,'0001-01-01',now(),actor,'Начальные условия'))
    return taskid


def apply_template(conn,bid):
    b = require(conn,'production_batches',bid)
    for t in conn.execute('SELECT * FROM model_operation_templates WHERE model_id=?',(b['model_id'],)).fetchall():
        form = dict(operation_id=t['operation_id'],rate=str(Decimal(t['rate_cents'])/100) if t['rate_cents'] is not None else '',
                    minutes=str(Decimal(t['minutes_milli'])/1000) if t['minutes_milli'] is not None else '',mode=t['mode'])
        add_operation(conn,bid,form,'template')
    for t in conn.execute('SELECT * FROM model_material_templates WHERE model_id=?',(b['model_id'],)).fetchall():
        conn.execute('''INSERT INTO batch_material_plan(batch_id,material_id,owner_customer_id,qty_milli,estimated_unit_cents)
            VALUES (?,?,?,?,?)''',(bid,t['material_id'],t['owner_customer_id'],t['norm_milli']*b['qty_pairs'],t['estimated_unit_cents']))


def save_template(conn,bid):
    b = editable_batch(conn,bid)
    conn.execute('DELETE FROM model_operation_templates WHERE model_id=?',(b['model_id'],))
    conn.execute('DELETE FROM model_material_templates WHERE model_id=?',(b['model_id'],))
    conn.execute('''INSERT INTO model_operation_templates(model_id,operation_id,rate_cents,minutes_milli,mode)
       SELECT ?,operation_id,rate_cents,minutes_milli,mode FROM batch_operations WHERE batch_id=? AND parent_id IS NULL''',(b['model_id'],bid))
    for p in conn.execute('SELECT * FROM batch_material_plan WHERE batch_id=?',(bid,)):
        norm = (p['qty_milli']+b['qty_pairs']-1)//b['qty_pairs']
        conn.execute('''INSERT INTO model_material_templates(model_id,material_id,norm_milli,owner_customer_id,estimated_unit_cents)
            VALUES (?,?,?,?,?)''',(b['model_id'],p['material_id'],norm,p['owner_customer_id'],p['estimated_unit_cents']))


def update_operation(conn,bid,tid,form,actor):
    editable_batch(conn,bid)
    t = require(conn,'batch_operations',tid)
    if t['batch_id']!=bid: raise RuleError('Операция другой партии.')
    rate = scaled(form['rate']) if form.get('rate','').strip() else None
    minutes = scaled(form['minutes'],1000,'Норма времени') if form.get('minutes','').strip() else None
    mode = form.get('mode','internal')
    qty = integer(form.get('qty') or t['qty_pairs'])
    b = require(conn,'production_batches',bid)
    done = conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM work_acceptances WHERE batch_operation_id=? AND canceled_at IS NULL',(tid,)).fetchone()[0]
    if mode not in ('internal','external','ready','skip') or qty>b['qty_pairs'] or qty<max(done,t['external_done']):
        raise RuleError('Проверьте объём и способ выполнения операции.')
    if done and mode != t['mode']: raise RuleError('У операции есть принятая выработка. Способ выполнения менять нельзя.')
    version = t['rate_version']
    if rate != t['rate_cents']:
        effective = day(form.get('effective_date'))
        reason = form.get('reason','').strip()
        if not reason: raise RuleError('Укажите причину изменения расценки.')
        if is_closed(conn,effective): raise RuleError('Нельзя изменить условия в закрытом периоде.')
        # Require chronological changes. Historical acceptance remains immutable.
        latest = conn.execute('SELECT MAX(effective_date) FROM operation_rate_versions WHERE batch_operation_id=?',(tid,)).fetchone()[0]
        if latest and effective<latest: raise RuleError('Дата расценки должна быть не раньше предыдущей версии.')
        version += 1
        conn.execute('INSERT INTO operation_rate_versions(batch_operation_id,version,rate_cents,effective_date,at,actor,reason) VALUES (?,?,?,?,?,?,?)',
                     (tid,version,rate,effective,now(),actor,reason))
        audit(conn,actor,'rate_change',tid,{'old':t['rate_cents'],'new':rate,'effective':effective,'reason':reason})
    conn.execute('UPDATE batch_operations SET qty_pairs=?,rate_cents=?,rate_version=?,minutes_milli=?,mode=? WHERE id=?', (qty,rate,version,minutes,mode,tid))


def owner_from(conn,bid,raw):
    owner = integer(raw,'Владелец') if raw else None
    if owner:
        require(conn,'customers',owner,True)
        if bid and owner!=require(conn,'production_batches',bid)['customer_id']:
            raise RuleError('Материал заказчика должен принадлежать заказчику этой партии.')
    return owner


def plan_material(conn,bid,form):
    editable_batch(conn,bid)
    mid = integer(form.get('material_id'),'Материал')
    require(conn,'materials',mid,True)
    owner = owner_from(conn,bid,form.get('owner_customer_id'))
    qty = scaled(form.get('qty'),1000,'Количество материала',True)
    price = scaled(form['price']) if form.get('price','').strip() and not owner else None
    old = conn.execute('SELECT * FROM batch_material_plan WHERE batch_id=? AND material_id=? AND owner_customer_id IS ?',(bid,mid,owner)).fetchone()
    if old:
        if qty<old['reserved_milli']: raise RuleError('План меньше зарезервированного количества.')
        conn.execute('UPDATE batch_material_plan SET qty_milli=?,estimated_unit_cents=?,note=? WHERE id=?',(qty,price,form.get('note',''),old['id']))
    else:
        conn.execute('INSERT INTO batch_material_plan(batch_id,material_id,owner_customer_id,qty_milli,estimated_unit_cents,note) VALUES (?,?,?,?,?,?)',
                     (bid,mid,owner,qty,price,form.get('note','')))


def reserve_material(conn,bid,pid):
    editable_batch(conn,bid)
    p=conn.execute('SELECT * FROM batch_material_plan WHERE id=? AND batch_id=?',(pid,bid)).fetchone()
    if not p: raise RuleError('Материал не найден в плане.')
    issued=conn.execute("SELECT COALESCE(SUM(CASE WHEN kind IN ('issue','allocate') THEN qty_milli WHEN kind='return' THEN -qty_milli ELSE 0 END),0) FROM inventory_movements WHERE batch_id=? AND material_id=? AND owner_customer_id IS ?",(bid,p['material_id'],p['owner_customer_id'])).fetchone()[0]
    need=max(0,p['qty_milli']-issued)
    s=conn.execute('SELECT qty_milli FROM stock_balances WHERE material_id=? AND owner_customer_id IS ?',(p['material_id'],p['owner_customer_id'])).fetchone()
    reserved=conn.execute('SELECT COALESCE(SUM(reserved_milli),0) FROM batch_material_plan WHERE material_id=? AND owner_customer_id IS ? AND id<>?',(p['material_id'],p['owner_customer_id'],pid)).fetchone()[0]
    if need>max(0,(s['qty_milli'] if s else 0)-reserved): raise RuleError('Свободного остатка не хватает для резерва.')
    conn.execute('UPDATE batch_material_plan SET reserved_milli=? WHERE id=?',(need,pid))


def stock_row(conn,table,mid,owner,bid=None):
    if table in ('stock_balances','production_pool'):
        conn.execute('INSERT OR IGNORE INTO '+table+'(material_id,owner_customer_id,qty_milli,value_cents) VALUES (?,?,0,0)',(mid,owner))
        return conn.execute('SELECT * FROM '+table+' WHERE material_id=? AND owner_customer_id IS ?',(mid,owner)).fetchone()
    if table!='production_stock': raise RuntimeError('Unknown stock table')
    conn.execute('INSERT OR IGNORE INTO production_stock(batch_id,material_id,owner_customer_id,qty_milli,value_cents) VALUES (?,?,?,0,0)',(bid,mid,owner))
    return conn.execute('SELECT * FROM production_stock WHERE batch_id=? AND material_id=? AND owner_customer_id IS ?',(bid,mid,owner)).fetchone()


def inventory_move(conn,form,actor,shipment_item_id=None):
    token=form.get('token','')
    if not token or len(token)>180: raise RuleError('Обновите форму движения материала.')
    old=conn.execute('SELECT id FROM inventory_movements WHERE token=?',(token,)).fetchone()
    if old: return old['id']
    kind=form.get('kind')
    if kind not in ('receipt','issue','return','consume','loss','allocate'): raise RuleError('Выберите движение материала.')
    mid=integer(form.get('material_id'),'Материал')
    material=require(conn,'materials',mid,True)
    qty=scaled(form.get('qty'),1000,'Количество материала',True)
    if material['unit'] in ('sht','pary') and qty%1000: raise RuleError('Штуки и пары должны быть целыми.')
    bid=integer(form.get('batch_id')) if form.get('batch_id') else None
    if kind=='allocate' and not bid: raise RuleError('Выберите партию для распределения общего материала.')
    if bid: editable_batch(conn,bid)
    owner=owner_from(conn,bid,form.get('owner_customer_id'))
    dated=day(form.get('occurred_on'))
    stock=stock_row(conn,'stock_balances',mid,owner)
    prodtable='production_stock' if bid else 'production_pool'
    prod=stock_row(conn,prodtable,mid,owner,bid) if kind!='receipt' else None
    if kind=='receipt':
        cost=scaled(form.get('cost') or '0') if not owner else 0
        if not owner and not form.get('cost','').strip(): raise RuleError('Укажите стоимость поступления, либо 0 для бесплатного материала.')
        conn.execute('UPDATE stock_balances SET qty_milli=qty_milli+?,value_cents=value_cents+? WHERE id=?',(qty,cost,stock['id']))
    else:
        source_table='stock_balances' if kind=='issue' else 'production_pool' if kind=='allocate' else prodtable
        source=stock if kind=='issue' else stock_row(conn,'production_pool',mid,owner) if kind=='allocate' else prod
        if qty>source['qty_milli']: raise RuleError('Количество превышает доступный остаток материала.')
        if kind=='issue':
            sql='SELECT COALESCE(SUM(reserved_milli),0) FROM batch_material_plan WHERE material_id=? AND owner_customer_id IS ?'
            args=(mid,owner)
            if bid: sql+=' AND batch_id<>?'; args+=(bid,)
            other=conn.execute(sql,args).fetchone()[0]
            if qty>stock['qty_milli']-other: raise RuleError('Материал зарезервирован для другой партии.')
        cost=source['value_cents'] if qty==source['qty_milli'] else source['value_cents']*qty//source['qty_milli']
        conn.execute('UPDATE '+source_table+' SET qty_milli=qty_milli-?,value_cents=value_cents-? WHERE id=?',(qty,cost,source['id']))
        if kind in ('issue','return','allocate'):
            dest,table=(stock,'stock_balances') if kind=='return' else (prod,prodtable)
            conn.execute('UPDATE '+table+' SET qty_milli=qty_milli+?,value_cents=value_cents+? WHERE id=?',(qty,cost,dest['id']))
        if kind in ('issue','allocate') and bid:
            conn.execute('UPDATE batch_material_plan SET reserved_milli=MAX(0,reserved_milli-?) WHERE batch_id=? AND material_id=? AND owner_customer_id IS ?',(qty,bid,mid,owner))
    iid=conn.execute('''INSERT INTO inventory_movements(material_id,owner_customer_id,batch_id,kind,qty_milli,cost_cents,occurred_on,created_at,note,supplier,shipment_item_id,token)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(mid,owner,bid,kind,qty,cost,dated,now(),form.get('note',''),form.get('supplier',''),shipment_item_id,token)).lastrowid
    audit(conn,actor,'inventory_'+kind,iid,{'batch':bid,'qty_milli':qty,'cost_cents':cost})
    return iid


def accept_work(conn,bid,form,actor):
    editable_batch(conn,bid)
    token=form.get('token','')
    if not token or len(token)>128: raise RuleError('Обновите форму выработки.')
    old=conn.execute('SELECT id FROM work_acceptances WHERE token=?',(token,)).fetchone()
    if old: return old['id']
    tid=integer(form.get('task_id'),'Операция')
    t=require(conn,'batch_operations',tid)
    if t['batch_id']!=bid or t['mode']!='internal': raise RuleError('Выберите собственную операцию этой партии.')
    qty=integer(form.get('qty'))
    worked=day(form.get('worked_on'))
    posted=posting_day(conn,worked)
    rate=conn.execute('SELECT * FROM operation_rate_versions WHERE batch_operation_id=? AND effective_date<=? ORDER BY effective_date DESC,version DESC LIMIT 1',(tid,worked)).fetchone()
    if not rate or rate['rate_cents'] is None: raise RuleError('Для даты работы не задана расценка операции.')
    done=conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM work_acceptances WHERE batch_operation_id=? AND canceled_at IS NULL',(tid,)).fetchone()[0]
    if done+qty>t['qty_pairs']: raise RuleError('Принятая выработка превышает объём операции. Для переделки добавьте отдельное задание.')
    if t['predecessor_id']:
        previous=conn.execute('SELECT mode,qty_pairs,external_done FROM batch_operations WHERE id=?',(t['predecessor_id'],)).fetchone()
        prevdone=conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM work_acceptances WHERE batch_operation_id=? AND canceled_at IS NULL',(t['predecessor_id'],)).fetchone()[0]
        if previous['mode'] in ('ready','skip'): prevdone=previous['qty_pairs']
        elif previous['mode']=='external': prevdone=previous['external_done']
        if done+qty>prevdone: raise RuleError('Предыдущая операция ещё не выполнена в этом объёме.')
    workers=form.getlist('worker_id')
    shares=form.getlist('share')
    if not workers or len(workers)!=len(set(workers)): raise RuleError('Выберите работников без повторений.')
    participants=[]
    for i,w in enumerate(workers):
        wid=integer(w,'Работник'); require(conn,'workers',wid,True)
        bp=10000 if len(workers)==1 else scaled(shares[i] if i<len(shares) else '',100,'Доля работника',True)
        participants.append((wid,bp))
    if sum(bp for _,bp in participants)!=10000: raise RuleError('Доли бригады должны составлять ровно 100%.')
    amount=qty*rate['rate_cents']
    aid=conn.execute('''INSERT INTO work_acceptances(batch_operation_id,qty_pairs,worked_on,posted_on,rate_cents,rate_version,amount_cents,mode,actor,created_at,note,token)
          VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',(tid,qty,worked,posted,rate['rate_cents'],rate['version'],amount,'individual' if len(workers)==1 else 'team',actor,now(),form.get('note',''),token)).lastrowid
    distributed=0
    for i,(wid,bp) in enumerate(participants):
        part=amount-distributed if i==len(participants)-1 else amount*bp//10000
        distributed+=part
        conn.execute('INSERT INTO work_shares(acceptance_id,worker_id,share_bp,amount_cents) VALUES (?,?,?,?)',(aid,wid,bp,part))
        conn.execute('''INSERT INTO payroll_accruals(worker_id,acceptance_id,batch_id,kind,amount_cents,posted_on,worked_on,created_at,note,actor)
            VALUES (?,?,?,'work',?,?,?,?,?,?)''',(wid,aid,bid,part,posted,worked,now(),form.get('note',''),actor))
    conn.execute("UPDATE production_batches SET status='working' WHERE id=? AND status='planned'",(bid,))
    if bitem := require(conn,'production_batches',bid)['order_item_id']:
        conn.execute("UPDATE orders SET status='working' WHERE id=(SELECT order_id FROM order_items WHERE id=?) AND status='confirmed'",(bitem,))
    audit(conn,actor,'work_accept',aid,{'pairs':qty,'amount_cents':amount,'version':rate['version']})
    return aid


def reverse_work(conn,aid,reason,actor):
    if not reason.strip(): raise RuleError('Укажите причину отмены выработки.')
    a=require(conn,'work_acceptances',aid)
    if a['canceled_at']: return
    t=require(conn,'batch_operations',a['batch_operation_id']); editable_batch(conn,t['batch_id'])
    posted=posting_day(conn,day())
    for accrual in conn.execute("SELECT * FROM payroll_accruals WHERE acceptance_id=? AND kind='work'",(aid,)).fetchall():
        conn.execute('''INSERT INTO payroll_accruals(worker_id,original_id,acceptance_id,batch_id,kind,amount_cents,posted_on,worked_on,created_at,note,actor)
             VALUES (?,?,?,?,'reversal',?,?,?,?,?,?)''',(accrual['worker_id'],accrual['id'],aid,t['batch_id'],-accrual['amount_cents'],posted,a['worked_on'],now(),reason,actor))
    conn.execute('UPDATE work_acceptances SET canceled_at=? WHERE id=?',(now(),aid))
    audit(conn,actor,'work_reverse',aid,{'reason':reason})


def worker_balance(conn,wid,before=None):
    where=' AND posted_on<?' if before else ''
    args=(wid,before) if before else (wid,)
    accrued=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_accruals WHERE worker_id=?'+where,args).fetchone()[0]
    where=' AND paid_on<?' if before else ''
    paid=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_payments WHERE worker_id=?'+where,args).fetchone()[0]
    clause=' AND reversed_on<?' if before else ''
    returned=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_cash_reversals WHERE worker_id=?'+clause,args).fetchone()[0]
    return accrued-paid+returned


def pay_worker(conn,wid,form,actor):
    require(conn,'workers',wid)
    token=form.get('token','')
    if not token or len(token)>128: raise RuleError('Обновите форму выплаты.')
    old=conn.execute('SELECT id FROM payroll_payments WHERE token=?',(token,)).fetchone()
    if old: return old['id']
    amount=scaled(form.get('amount'),positive=True)
    dated=day(form.get('paid_on'))
    if is_closed(conn,dated): raise RuleError('Выплату нельзя вносить в закрытый период.')
    kind=form.get('kind','payment')
    if kind not in ('payment','advance'): raise RuleError('Выберите вид выплаты.')
    if kind=='payment' and amount>max(0,worker_balance(conn,wid,(date.fromisoformat(dated)+timedelta(days=1)).isoformat())):
        raise RuleError('Выплата превышает долг. Выберите «Аванс», если хотите выплатить заранее.')
    pid=conn.execute('INSERT INTO payroll_payments(worker_id,amount_cents,paid_on,created_at,kind,method,note,actor,token) VALUES (?,?,?,?,?,?,?,?,?)',
                     (wid,amount,dated,now(),kind,form.get('method',''),form.get('note',''),actor,token)).lastrowid
    # Allocate cash to unallocated positive accruals without treating it as a second expense.
    remaining=amount
    for a in conn.execute('''SELECT a.id,a.amount_cents-COALESCE((SELECT SUM(x.amount_cents) FROM payroll_payment_allocations x WHERE x.accrual_id=a.id AND NOT EXISTS(SELECT 1 FROM payroll_cash_reversals r WHERE r.payment_id=x.payment_id)),0) AS due
          FROM payroll_accruals a WHERE worker_id=? AND posted_on<=? AND amount_cents>0 AND NOT EXISTS(SELECT 1 FROM payroll_accruals r WHERE r.original_id=a.id)
          ORDER BY posted_on,id''',(wid,dated)).fetchall():
        used=min(remaining,max(0,a['due']))
        if used: conn.execute('INSERT INTO payroll_payment_allocations(payment_id,accrual_id,amount_cents) VALUES (?,?,?)',(pid,a['id'],used)); remaining-=used
        if not remaining: break
    audit(conn,actor,'payroll_'+kind,pid,{'worker':wid,'amount_cents':amount})
    return pid


def payroll_adjustment(conn,wid,form,actor):
    require(conn,'workers',wid)
    token=form.get('token','')
    if not token: raise RuleError('Обновите форму.')
    if conn.execute('SELECT 1 FROM payroll_accruals WHERE token=?',(token,)).fetchone(): return
    kind=form.get('kind','adjustment')
    if kind not in ('opening','adjustment'): raise RuleError('Выберите начальный остаток или корректировку.')
    if kind=='opening' and conn.execute('SELECT 1 FROM payroll_accruals WHERE worker_id=?',(wid,)).fetchone():
        raise RuleError('Начальный остаток вводится до первого начисления. Используйте корректировку.')
    reason=form.get('note','').strip()
    if not reason: raise RuleError('Укажите основание суммы.')
    amount=scaled(form.get('amount'),positive=True)
    if form.get('direction')=='minus': amount=-amount
    dated=day(form.get('posted_on'))
    if is_closed(conn,dated): raise RuleError('Период закрыт.')
    conn.execute('''INSERT INTO payroll_accruals(worker_id,kind,amount_cents,posted_on,created_at,note,actor,token)
                VALUES (?,?,?,?,?,?,?,?)''',(wid,kind,amount,dated,now(),reason,actor,token))


def close_period(conn,start,end,actor):
    start=day(start); end=day(end)
    if start>end: raise RuleError('Начало периода должно быть не позже конца.')
    if conn.execute('SELECT 1 FROM payroll_periods WHERE start_on<=? AND end_on>=?',(end,start)).fetchone(): raise RuleError('Период пересекается с уже закрытым.')
    pid=conn.execute('INSERT INTO payroll_periods(start_on,end_on,closed_at,actor) VALUES (?,?,?,?)',(start,end,now(),actor)).lastrowid
    for w in conn.execute('SELECT id FROM workers'):
        opening=worker_balance(conn,w['id'],start)
        accrued=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_accruals WHERE worker_id=? AND posted_on BETWEEN ? AND ?',(w['id'],start,end)).fetchone()[0]
        paid=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_payments WHERE worker_id=? AND paid_on BETWEEN ? AND ?',(w['id'],start,end)).fetchone()[0]
        paid-=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_cash_reversals WHERE worker_id=? AND reversed_on BETWEEN ? AND ?',(w['id'],start,end)).fetchone()[0]
        conn.execute('INSERT INTO payroll_period_totals VALUES (?,?,?,?,?,?)',(pid,w['id'],opening,accrued,paid,opening+accrued-paid))
    audit(conn,actor,'payroll_close',pid,{'start':start,'end':end})


def record_output(conn,bid,form,actor):
    b=editable_batch(conn,bid)
    token=form.get('token','')
    if not token: raise RuleError('Обновите форму.')
    if conn.execute('SELECT 1 FROM production_outputs WHERE token=?',(token,)).fetchone(): return
    qty=integer(form.get('qty'))
    kind=form.get('kind','good')
    if kind not in ('good','reject'): raise RuleError('Выберите результат выпуска.')
    good=conn.execute("SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=? AND kind='good'",(bid,)).fetchone()[0]
    if kind=='good' and good+qty>b['qty_pairs']: raise RuleError('Годный выпуск превышает размер партии.')
    if kind=='good':
        for t in task_rows(conn,bid):
            if not t['parent_id'] and t['mode']!='skip' and t['done']<good+qty:
                raise RuleError('Сначала отметьте выполнение операций для этого количества пар.')
    conn.execute('INSERT INTO production_outputs(batch_id,qty_pairs,kind,occurred_on,note,actor,token) VALUES (?,?,?,?,?,?,?)',
                 (bid,qty,kind,day(form.get('occurred_on')),form.get('note',''),actor,token))
    if kind=='good' and good+qty==b['qty_pairs']: conn.execute("UPDATE production_batches SET status='completed' WHERE id=?",(bid,))


def add_delivery(conn,bid,form,actor):
    b=editable_batch(conn,bid)
    token=form.get('token','')
    if not token: raise RuleError('Обновите форму.')
    if conn.execute('SELECT 1 FROM deliveries WHERE token=?',(token,)).fetchone(): return
    qty=integer(form.get('qty'))
    good=conn.execute("SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=? AND kind='good'",(bid,)).fetchone()[0]
    sent=conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM deliveries WHERE batch_id=?',(bid,)).fetchone()[0]
    if sent+qty>good: raise RuleError('Недостаточно готовых пар для отгрузки.')
    dated=day(form.get('delivered_on'))
    conn.execute('INSERT INTO deliveries(batch_id,qty_pairs,delivered_on,note,actor,token) VALUES (?,?,?,?,?,?)',
                 (bid,qty,dated,form.get('note',''),actor,token))


def task_rows(conn,bid):
    rows=[]
    for r in conn.execute('''SELECT t.*,o.name,COALESCE((SELECT SUM(qty_pairs) FROM work_acceptances WHERE batch_operation_id=t.id AND canceled_at IS NULL),0) accepted
          FROM batch_operations t JOIN operations o ON o.id=t.operation_id WHERE t.batch_id=? ORDER BY position,t.id''',(bid,)):
        d=dict(r)
        d['done']=d['qty_pairs'] if d['mode'] in ('ready','skip') else d['external_done'] if d['mode']=='external' else d['accepted']
        d['assignments']=conn.execute('SELECT a.*,w.name,w.number FROM batch_assignments a JOIN workers w ON w.id=a.worker_id WHERE a.batch_operation_id=?',(d['id'],)).fetchall()
        mins=d['minutes_milli']
        hours=sum(a['hours_milli'] or 0 for a in d['assignments'])
        d['workload_milli_hours']=((d['qty_pairs']-d['done'])*mins+59)//60 if mins is not None else None
        d['headcount']=math.ceil(d['workload_milli_hours']*len(d['assignments'])/hours) if hours and d['workload_milli_hours'] is not None else None
        rows.append(d)
    return rows


def batch_finance(conn,bid):
    b=require(conn,'production_batches',bid)
    terms=require(conn,'order_items',b['order_item_id']) if b['order_item_id'] else None
    total=b['contract_cents'] if b['contract_cents'] is not None else terms['total_cents'] if terms else None
    tasks=task_rows(conn,bid)
    labor_unknown=any(t['rate_cents'] is None and t['mode'] in ('internal','external') for t in tasks)
    labor_plan=sum(t['qty_pairs']*(t['rate_cents'] or 0) for t in tasks if t['mode'] in ('internal','external'))
    labor_actual=conn.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_accruals WHERE batch_id=?',(bid,)).fetchone()[0]
    labor_actual+=sum(t['external_cost_cents'] for t in tasks if t['mode']=='external')
    # Immutable accepted sums plus forecast for remaining units, with current rates.
    labor_forecast=labor_actual+sum(max(0,t['qty_pairs']-t['done'])*(t['rate_cents'] or 0) for t in tasks if t['mode'] in ('internal','external'))
    plans=conn.execute('SELECT * FROM batch_material_plan WHERE batch_id=?',(bid,)).fetchall()
    mat_unknown=(not plans and not b['no_materials']) or any(p['estimated_unit_cents'] is None and p['owner_customer_id'] is None for p in plans)
    mat_plan=sum((p['qty_milli']*(p['estimated_unit_cents'] or 0)+500)//1000 for p in plans if not p['owner_customer_id'])
    mat_actual=conn.execute("SELECT COALESCE(SUM(cost_cents),0) FROM inventory_movements WHERE batch_id=? AND kind IN ('consume','loss') AND owner_customer_id IS NULL",(bid,)).fetchone()[0]
    costs=conn.execute('SELECT COALESCE(SUM(planned_cents),0),COALESCE(SUM(actual_cents),0) FROM batch_costs WHERE batch_id=?',(bid,)).fetchone()
    wip=conn.execute('SELECT COALESCE(SUM(value_cents),0) FROM production_stock WHERE batch_id=? AND owner_customer_id IS NULL',(bid,)).fetchone()[0]
    accepted=conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM deliveries WHERE batch_id=? AND accepted_on IS NOT NULL',(bid,)).fetchone()[0]
    whole_accepted=accepted
    if terms and terms['settlement']=='complete':
        whole_accepted=conn.execute('SELECT COALESCE(SUM(d.qty_pairs),0) FROM deliveries d JOIN production_batches p ON p.id=d.batch_id WHERE p.order_item_id=? AND d.accepted_on IS NOT NULL',(terms['id'],)).fetchone()[0]
    revenue=0 if total is None else (total if whole_accepted>=terms['qty_pairs'] else 0) if terms['settlement']=='complete' else total if accepted==b['qty_pairs'] else total*accepted//b['qty_pairs']
    mat_remaining=0
    for p in plans:
        used=conn.execute("SELECT COALESCE(SUM(qty_milli),0) FROM inventory_movements WHERE batch_id=? AND material_id=? AND owner_customer_id IS ? AND kind IN ('consume','loss')",(bid,p['material_id'],p['owner_customer_id'])).fetchone()[0]
        stock=conn.execute('SELECT qty_milli,value_cents FROM production_stock WHERE batch_id=? AND material_id=? AND owner_customer_id IS ?',(bid,p['material_id'],p['owner_customer_id'])).fetchone()
        stockqty=stock['qty_milli'] if stock else 0
        if not p['owner_customer_id']:
            mat_remaining+=max(0,p['qty_milli']-used-stockqty)*(p['estimated_unit_cents'] or 0)//1000
    forecast=labor_forecast+mat_actual+wip+mat_remaining+max(costs[0],costs[1])
    unknown=labor_unknown or mat_unknown or not tasks
    if b['status']=='closed': forecast=labor_actual+mat_actual+costs[1]
    return dict(total=total,revenue=revenue,accepted=accepted,labor_plan=labor_plan,labor_actual=labor_actual,
                material_plan=mat_plan,material_actual=mat_actual,wip=wip,other_plan=costs[0],other_actual=costs[1],
                planned=labor_plan+mat_plan+costs[0],actual=labor_actual+mat_actual+costs[1],forecast=forecast,
                unknown=unknown,margin=(total-forecast if total is not None and not unknown else None))


def split_batch(conn,bid,form,actor):
    b=editable_batch(conn,bid)
    token=form.get('token','')
    if not token: raise RuleError('Обновите форму.')
    prior=conn.execute('SELECT id FROM production_batches WHERE token=?',(token,)).fetchone()
    if prior: return prior['id']
    qty=integer(form.get('qty'))
    if not b['order_item_id'] or b['status']!='planned' or qty>=b['qty_pairs']:
        raise RuleError('Разделить можно ещё не запущенную партию из заказа. В исходной партии должна остаться хотя бы одна пара.')
    if conn.execute('SELECT 1 FROM inventory_movements WHERE batch_id=?',(bid,)).fetchone() or conn.execute('SELECT 1 FROM shipment_items WHERE batch_id=?',(bid,)).fetchone() or conn.execute('SELECT 1 FROM batch_operations WHERE batch_id=? AND parent_id IS NOT NULL',(bid,)).fetchone():
        raise RuleError('У партии уже есть поставки, движения или переделки. Разделение выполняется до начала работы.')
    if conn.execute('SELECT 1 FROM production_outputs WHERE batch_id=?',(bid,)).fetchone() or conn.execute('SELECT 1 FROM batch_operations WHERE batch_id=? AND external_done>0',(bid,)).fetchone() or conn.execute('SELECT 1 FROM batch_costs WHERE batch_id=?',(bid,)).fetchone() or conn.execute('SELECT 1 FROM work_acceptances a JOIN batch_operations t ON t.id=a.batch_operation_id WHERE t.batch_id=?',(bid,)).fetchone():
        raise RuleError('У партии уже есть выработка.')
    terms=require(conn,'order_items',b['order_item_id'])
    total=b['contract_cents'] if b['contract_cents'] is not None else terms['total_cents']
    newtotal=total*qty//b['qty_pairs']
    newbid=conn.execute('''INSERT INTO production_batches(order_item_id,customer_id,model_id,qty_pairs,due_date,specification,note,created_at,token,contract_cents)
             VALUES (?,?,?,?,?,?,?,?,?,?)''',(b['order_item_id'],b['customer_id'],b['model_id'],qty,b['due_date'],b['specification'],b['note'],now(),token,newtotal)).lastrowid
    for t in conn.execute('SELECT * FROM batch_operations WHERE batch_id=?',(bid,)).fetchall():
        newqty=t['qty_pairs']*qty//b['qty_pairs']
        if not newqty or newqty==t['qty_pairs']: raise RuleError('Объём одной из операций слишком мал для такого разделения.')
        task=add_operation(conn,newbid,dict(operation_id=t['operation_id'],qty=newqty,mode=t['mode'],
            rate=str(Decimal(t['rate_cents'])/100) if t['rate_cents'] is not None else '',
            minutes=str(Decimal(t['minutes_milli'])/1000) if t['minutes_milli'] is not None else ''),actor)
        conn.execute('UPDATE batch_operations SET qty_pairs=qty_pairs-? WHERE id=?',(newqty,t['id']))
    for p in conn.execute('SELECT * FROM batch_material_plan WHERE batch_id=?',(bid,)).fetchall():
        newqty=p['qty_milli']*qty//b['qty_pairs']
        if not newqty: raise RuleError('Количество одного из материалов слишком мало для такого разделения.')
        # Remove reservation for both parts; warehouse can reserve them again explicitly.
        conn.execute('UPDATE batch_material_plan SET qty_milli=qty_milli-?,reserved_milli=0 WHERE id=?',(newqty,p['id']))
        conn.execute('INSERT INTO batch_material_plan(batch_id,material_id,owner_customer_id,qty_milli,estimated_unit_cents,note) VALUES (?,?,?,?,?,?)',
            (newbid,p['material_id'],p['owner_customer_id'],newqty,p['estimated_unit_cents'],p['note']))
    conn.execute('UPDATE production_batches SET no_materials=? WHERE id=?',(b['no_materials'],newbid))
    conn.execute('DELETE FROM batch_assignments WHERE batch_operation_id IN (SELECT id FROM batch_operations WHERE batch_id=?)',(bid,))
    conn.execute('UPDATE production_batches SET qty_pairs=qty_pairs-?,contract_cents=? WHERE id=?',(qty,total-newtotal,bid))
    audit(conn,actor,'batch_split',bid,{'new_batch':newbid,'qty':qty,'new_contract_cents':newtotal,'note':form.get('note','')})
    return newbid


def amend_terms(conn,oid,form,actor):
    order=require(conn,'orders',oid)
    if order['status'] in ('completed','canceled'): raise RuleError('Заказ завершён. Условия менять нельзя.')
    iid=integer(form.get('item_id')); item=require(conn,'order_items',iid)
    if item['order_id']!=oid: raise RuleError('Позиция другого заказа.')
    if conn.execute('SELECT 1 FROM deliveries d JOIN production_batches b ON b.id=d.batch_id WHERE b.order_item_id=?',(iid,)).fetchone():
        raise RuleError('Позиция уже отгружалась. Для новых условий создайте отдельный заказ.')
    reason=form.get('reason','').strip()
    if not reason: raise RuleError('Укажите причину изменения цены.')
    price=scaled(form.get('price'))
    kind=form.get('price_kind'); unit=form.get('price_unit')
    if kind not in ('unit','total') or unit not in ('pair','shoe'): raise RuleError('Проверьте способ цены.')
    total=price if kind=='total' else price*item['qty_pairs']*(2 if unit=='shoe' else 1)
    if total==item['total_cents'] and kind==item['price_kind'] and unit==item['price_unit'] and (kind=='total' or price==item['unit_price_cents']): return
    conn.execute('INSERT INTO order_changes(order_id,at,actor,reason,snapshot) VALUES (?,?,?,?,?)',
        (oid,now(),actor,reason,json.dumps(dict(item),ensure_ascii=False)))
    conn.execute('UPDATE order_items SET price_kind=?,price_unit=?,unit_price_cents=?,total_cents=?,terms_version=terms_version+1 WHERE id=?',
        (kind,unit,price if kind=='unit' else None,total,iid))
    batches=conn.execute('SELECT * FROM production_batches WHERE order_item_id=? ORDER BY id',(iid,)).fetchall()
    distributed=0
    for n,b in enumerate(batches):
        part=total-distributed if n==len(batches)-1 else total*b['qty_pairs']//item['qty_pairs']
        distributed+=part
        conn.execute('UPDATE production_batches SET contract_cents=? WHERE id=?',(part,b['id']))
    audit(conn,actor,'order_terms',oid,{'item':iid,'new_total':total,'reason':reason})


def reverse_payment(conn,wid,pid,reason,actor):
    if not reason.strip(): raise RuleError('Укажите причину отмены выплаты.')
    payment=conn.execute('SELECT * FROM payroll_payments WHERE id=? AND worker_id=?',(pid,wid)).fetchone()
    if not payment: raise RuleError('Выплата не найдена.')
    if conn.execute('SELECT 1 FROM payroll_cash_reversals WHERE payment_id=?',(pid,)).fetchone(): return
    posted=posting_day(conn,day())
    conn.execute('INSERT INTO payroll_cash_reversals(payment_id,worker_id,amount_cents,reversed_on,created_at,reason,actor) VALUES (?,?,?,?,?,?,?)',
        (pid,wid,payment['amount_cents'],posted,now(),reason,actor))
    audit(conn,actor,'payroll_cash_reverse',pid,{'reason':reason,'amount_cents':payment['amount_cents']})
