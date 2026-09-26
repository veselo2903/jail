"""Orders, production planning, stock, accepted work and separate payroll cash ledger."""
from functools import wraps
from datetime import date
from decimal import Decimal
import json
import secrets
import sqlite3
from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
if __package__:
    from . import business_core as core
else:
    import business_core as core

bp=Blueprint('business',__name__)
PREFIX='/<any(sklad,proizv,director):role_url>'
MANAGERS=('sklad','director')
MODES={'internal':'Своими силами','external':'Подрядчик','ready':'Уже выполнено','skip':'Не требуется'}
STATUSES={'planned':'Планируется','working':'В работе','completed':'Изготовлена','closed':'Закрыта','canceled':'Отменена', 'confirmed':'Принят'}
MOVES={'receipt':'Поступление','issue':'Передача в производство','return':'Возврат на склад','consume':'Использовано','loss':'Потери','allocate':'Из общих материалов в партию'}


def access(manager=False):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args,**kw):
            role=session.get('role')
            if not role: return redirect(url_for('login'))
            if manager and role not in MANAGERS: abort(403)
            return fn(*args,**kw)
        return wrapped
    return decorate


def mutate(action, target, success='Сохранено.'):
    """Rollback before flashing: app's audit hook commits all pending writes."""
    try:
        g.db.execute('BEGIN IMMEDIATE')
        result=action()
        g.db.commit()
        if isinstance(result,str): target=result
        flash(success)
    except core.RuleError as exc:
        g.db.rollback(); flash(str(exc))
    except sqlite3.IntegrityError:
        g.db.rollback(); flash('Такая запись уже существует или связана с другими данными.')
    return redirect(target)


def rows(sql,args=()): return g.db.execute(sql,args).fetchall()
def choices(table): return rows('SELECT * FROM '+table+' WHERE archived=0 ORDER BY name')
def batch_title_query():
    return '''SELECT b.*,m.name model_name,c.name customer_name,oi.order_id,
        (SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs p WHERE p.batch_id=b.id AND p.kind='good') good
        FROM production_batches b JOIN models m ON m.id=b.model_id JOIN customers c ON c.id=b.customer_id
        LEFT JOIN order_items oi ON oi.id=b.order_item_id'''


@bp.errorhandler(core.RuleError)
def rule_error(exc):
    g.db.rollback()
    from werkzeug.exceptions import NotFound
    return NotFound(description=str(exc)).get_response()


@bp.app_template_filter('money')
def money(value):
    if value is None: return 'Не задано'
    cents=int(value); sign='−' if cents<0 else ''; cents=abs(cents)
    return sign+f'{cents//100:,}'.replace(',',' ')+f',{cents%100:02d} ₽'


@bp.app_template_filter('quantity')
def quantity(value):
    text=format(Decimal(value or 0)/1000,'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


@bp.app_context_processor
def context():
    return dict(manager=session.get('role') in MANAGERS, business_token=lambda:secrets.token_urlsafe(24),
                business_today=core.today().isoformat(), operation_modes=MODES, batch_statuses=STATUSES, movement_names=MOVES)


@bp.route(PREFIX+'/orders')
@access(True)
def orders():
    orders=rows('''SELECT o.*,c.name customer_name,COALESCE(SUM(i.total_cents),0) total,
       COUNT(i.id) positions,(SELECT COALESCE(SUM(amount_cents),0) FROM customer_payments WHERE order_id=o.id) paid
       FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN order_items i ON i.order_id=o.id
       GROUP BY o.id ORDER BY o.id DESC''')
    return render_template('business/orders.html',orders=orders)


@bp.route(PREFIX+'/orders/new',methods=['GET','POST'])
@access(True)
def order_new():
    if request.method=='POST':
        try:
            g.db.execute('BEGIN IMMEDIATE')
            oid=core.create_order(g.db,request.form,session['role'])
            g.db.commit(); flash('Заказ создан. Для каждой модели подготовлена партия.')
            return redirect(url_for('business.order_detail',oid=oid))
        except (core.RuleError,sqlite3.IntegrityError) as exc:
            g.db.rollback(); flash(str(exc) if isinstance(exc,core.RuleError) else 'Проверьте модель и заказчика.')
    return render_template('business/order_new.html',customers=choices('customers'),models=choices('models'),form=request.form)


@bp.route(PREFIX+'/orders/<int:oid>',methods=['GET','POST'])
@access(True)
def order_detail(oid):
    order=core.require(g.db,'orders',oid)
    if request.method=='POST':
        def change():
            action=request.form.get('action')
            token=request.form.get('token','')
            if action=='payment':
                if not token: raise core.RuleError('Обновите форму оплаты.')
                if g.db.execute('SELECT 1 FROM customer_payments WHERE token=?',(token,)).fetchone(): return
                amount=core.scaled(request.form.get('amount'),positive=True)
                g.db.execute('INSERT INTO customer_payments(order_id,amount_cents,paid_on,note,actor,token) VALUES (?,?,?,?,?,?)',
                    (oid,amount,core.day(request.form.get('paid_on')),request.form.get('note',''),session['role'],token))
            elif action=='terms': core.amend_terms(g.db,oid,request.form,session['role'])
            elif action=='cancel':
                if g.db.execute('SELECT 1 FROM work_acceptances a JOIN batch_operations t ON t.id=a.batch_operation_id JOIN production_batches b ON b.id=t.batch_id JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=? AND a.canceled_at IS NULL',(oid,)).fetchone():
                    raise core.RuleError('В заказе уже есть выполненная работа. Сначала завершите расчёты по партиям.')
                if g.db.execute('SELECT 1 FROM production_stock p JOIN production_batches b ON b.id=p.batch_id JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=? AND p.qty_milli>0',(oid,)).fetchone():
                    raise core.RuleError('Верните оставшиеся материалы из производства перед отменой заказа.')
                reason=request.form.get('note','').strip()
                if not reason: raise core.RuleError('Укажите причину отмены заказа.')
                core.audit(g.db,session['role'],'order_cancel',oid,{'reason':reason})
                g.db.execute("UPDATE orders SET status='canceled' WHERE id=?",(oid,))
                g.db.execute("UPDATE production_batches SET status='canceled' WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?)",(oid,))
                g.db.execute('UPDATE batch_material_plan SET reserved_milli=0 WHERE batch_id IN (SELECT id FROM production_batches WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=?))',(oid,))
            else: raise core.RuleError('Неизвестное действие.')
        return mutate(change,url_for('business.order_detail',oid=oid))
    customer=core.require(g.db,'customers',order['customer_id'])
    items=rows('SELECT i.*,m.name FROM order_items i JOIN models m ON m.id=i.model_id WHERE order_id=?',(oid,))
    batches=rows(batch_title_query()+' WHERE oi.order_id=? ORDER BY b.id',(oid,))
    payments=rows('SELECT * FROM customer_payments WHERE order_id=? ORDER BY paid_on DESC,id DESC',(oid,))
    finance=[core.batch_finance(g.db,b['id']) for b in batches]
    changes=[]
    for change in rows('SELECT * FROM order_changes WHERE order_id=? ORDER BY id DESC',(oid,)):
        entry=dict(change); entry['before']=json.loads(entry['snapshot']);changes.append(entry)
    return render_template('business/order_detail.html',order=order,customer=customer,items=items,batches=batches,payments=payments,
         total=sum(i['total_cents'] for i in items),paid=sum(p['amount_cents'] for p in payments),earned=sum(f['revenue'] for f in finance),
         changes=changes)


@bp.route(PREFIX+'/batches')
@access()
def batches():
    show=request.args.get('archive')=='1'
    condition=" WHERE b.status IN ('closed','canceled')" if show else " WHERE b.status NOT IN ('closed','canceled')"
    data=rows(batch_title_query()+condition+' ORDER BY b.due_date IS NULL,b.due_date,b.id DESC')
    return render_template('business/batches.html',batches=data,archive=show)


@bp.route(PREFIX+'/batches/<int:bid>',methods=['GET','POST'])
@access()
def batch_detail(bid):
    b=core.require(g.db,'production_batches',bid)
    if request.method=='POST':
        action=request.form.get('action')
        if action not in ('work','output','material_move') and session['role'] not in MANAGERS: abort(403)
        if action=='material_move' and session['role'] not in MANAGERS and request.form.get('kind') not in ('consume','return','loss'): abort(403)
        def change():
            actor=session['role']; f=request.form
            core.editable_batch(g.db,bid)
            if action=='operation': core.add_operation(g.db,bid,f,actor)
            elif action=='operations_bulk':
                selected=f.getlist('operation_id')
                if not selected: raise core.RuleError('Выберите хотя бы одну операцию.')
                for opid in selected:
                    core.add_operation(g.db,bid,dict(operation_id=opid,rate=f.get('rate_'+opid,''),minutes=f.get('minutes_'+opid,''),mode=f.get('mode_'+opid,'internal')),actor)
            elif action=='operation_edit': core.update_operation(g.db,bid,core.integer(f.get('task_id')),f,actor)
            elif action=='template': core.save_template(g.db,bid)
            elif action=='split':
                newbid=core.split_batch(g.db,bid,f,actor)
                flash('Создана партия №'+str(newbid)+'. Назначения для обеих партий задайте заново.')
                return url_for('business.batch_detail',bid=newbid)
            elif action=='material': core.plan_material(g.db,bid,f)
            elif action=='reserve': core.reserve_material(g.db,bid,core.integer(f.get('plan_id')))
            elif action=='material_remove':
                g.db.execute('DELETE FROM batch_material_plan WHERE id=? AND batch_id=?',(core.integer(f.get('plan_id')),bid))
            elif action=='unreserve': g.db.execute('UPDATE batch_material_plan SET reserved_milli=0 WHERE id=? AND batch_id=?',(core.integer(f.get('plan_id')),bid))
            elif action=='material_move':
                form=f.to_dict(); form['batch_id']=str(bid)
                core.inventory_move(g.db,form,actor)
            elif action=='work': core.accept_work(g.db,bid,f,actor)
            elif action=='output': core.record_output(g.db,bid,f,actor)
            elif action=='reverse':
                a=core.require(g.db,'work_acceptances',core.integer(f.get('acceptance_id')))
                t=core.require(g.db,'batch_operations',a['batch_operation_id'])
                if t['batch_id']!=bid: raise core.RuleError('Выработка другой партии.')
                core.reverse_work(g.db,a['id'],f.get('reason',''),actor)
            elif action=='assignment':
                tid=core.integer(f.get('task_id')); t=core.require(g.db,'batch_operations',tid)
                if t['batch_id']!=bid: raise core.RuleError('Операция другой партии.')
                wid=core.integer(f.get('worker_id')); core.require(g.db,'workers',wid,True)
                hours=core.scaled(f.get('hours'),1000,'Часы',True) if f.get('hours') else None
                planned=core.integer(f.get('qty')) if f.get('qty') else None
                if planned and planned>t['qty_pairs']: raise core.RuleError('Назначение превышает объём операции.')
                if not f.get('override') and not g.db.execute('SELECT 1 FROM worker_skills WHERE worker_id=? AND operation_id=?',(wid,t['operation_id'])).fetchone():
                    raise core.RuleError('Для сотрудника не отмечен навык. Укажите навык в «Расчётах» или отметьте назначение без навыка.')
                g.db.execute('''INSERT INTO batch_assignments(batch_operation_id,worker_id,hours_milli,planned_pairs) VALUES (?,?,?,?)
                    ON CONFLICT(batch_operation_id,worker_id) DO UPDATE SET hours_milli=excluded.hours_milli,planned_pairs=excluded.planned_pairs''',(tid,wid,hours,planned))
            elif action=='unassign':
                g.db.execute('DELETE FROM batch_assignments WHERE id=? AND batch_operation_id IN (SELECT id FROM batch_operations WHERE batch_id=?)',(core.integer(f.get('assignment_id')),bid))
            elif action=='external':
                tid=core.integer(f.get('task_id')); t=core.require(g.db,'batch_operations',tid)
                if t['batch_id']!=bid or t['mode']!='external': raise core.RuleError('Выберите операцию подрядчика.')
                qty=core.integer(f.get('qty'),positive=False); cost=core.scaled(f.get('cost'))
                if qty<t['external_done'] or qty>t['qty_pairs'] or cost<t['external_cost_cents']: raise core.RuleError('Итог подрядчика должен быть не меньше предыдущего и не больше объёма операции.')
                g.db.execute('UPDATE batch_operations SET external_done=?,external_cost_cents=? WHERE id=?',(qty,cost,tid))
                core.audit(g.db,actor,'external_work',tid,{'qty':qty,'cost_cents':cost})
            elif action=='cost':
                if not f.get('label','').strip(): raise core.RuleError('Назовите расход.')
                if not f.get('token'): raise core.RuleError('Обновите форму.')
                if g.db.execute('SELECT 1 FROM batch_costs WHERE token=?',(f['token'],)).fetchone(): return
                g.db.execute('INSERT INTO batch_costs(batch_id,label,planned_cents,actual_cents,occurred_on,actor,token) VALUES (?,?,?,?,?,?,?)',
                   (bid,f['label'].strip(),core.scaled(f.get('planned') or '0'),core.scaled(f.get('actual') or '0'),core.day(f.get('occurred_on')),actor,f['token']))
            elif action=='delivery': core.add_delivery(g.db,bid,f,actor)
            elif action=='delivery_accept':
                d=core.require(g.db,'deliveries',core.integer(f.get('delivery_id')))
                if d['batch_id']!=bid: raise core.RuleError('Отгрузка другой партии.')
                dated=core.day(f.get('accepted_on'))
                if dated<d['delivered_on']: raise core.RuleError('Приёмка не может быть раньше отгрузки.')
                g.db.execute('UPDATE deliveries SET accepted_on=COALESCE(accepted_on,?) WHERE id=?',(dated,d['id']))
            elif action=='close':
                fin=core.batch_finance(g.db,bid)
                if fin['accepted']<b['qty_pairs']: raise core.RuleError('Заказчик ещё не принял весь объём партии.')
                if g.db.execute('SELECT 1 FROM production_stock WHERE batch_id=? AND qty_milli>0',(bid,)).fetchone(): raise core.RuleError('Отметьте расход или возврат оставшихся материалов партии.')
                if any(t['done']<t['qty_pairs'] for t in core.task_rows(g.db,bid) if t['mode']!='skip'):
                    raise core.RuleError('Завершите задания партии, включая переделки, перед закрытием.')
                if fin['unknown']: raise core.RuleError('Заполните расценки и оценки материалов перед закрытием партии.')
                g.db.execute("UPDATE production_batches SET status='closed' WHERE id=?",(bid,))
                g.db.execute('UPDATE batch_material_plan SET reserved_milli=0 WHERE batch_id=?',(bid,))
                oid=g.db.execute('SELECT order_id FROM order_items WHERE id=?',(b['order_item_id'],)).fetchone()
                if oid and not g.db.execute("SELECT 1 FROM production_batches b JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=? AND b.status NOT IN ('closed','canceled')",(oid[0],)).fetchone():
                    g.db.execute("UPDATE orders SET status='completed' WHERE id=?",(oid[0],))
                core.audit(g.db,actor,'batch_close',bid,fin)
            else: raise core.RuleError('Неизвестное действие.')
        return mutate(change,url_for('business.batch_detail',bid=bid), 'Изменения сохранены.')
    batch=rows(batch_title_query()+' WHERE b.id=?',(bid,))[0]
    tasks=core.task_rows(g.db,bid)
    material_plans=rows('''SELECT p.*,m.name,m.unit,c.name owner_name FROM batch_material_plan p JOIN materials m ON m.id=p.material_id
        LEFT JOIN customers c ON c.id=p.owner_customer_id WHERE p.batch_id=? ORDER BY p.id''',(bid,))
    production_stock=rows('''SELECT p.*,m.name,m.unit,c.name owner_name FROM production_stock p JOIN materials m ON m.id=p.material_id
        LEFT JOIN customers c ON c.id=p.owner_customer_id WHERE p.batch_id=? AND p.qty_milli>0 ORDER BY m.name''',(bid,))
    history=rows('''SELECT a.*,o.name operation_name, GROUP_CONCAT(w.name, ', ') workers
        FROM work_acceptances a JOIN batch_operations t ON t.id=a.batch_operation_id JOIN operations o ON o.id=t.operation_id
        JOIN work_shares s ON s.acceptance_id=a.id JOIN workers w ON w.id=s.worker_id WHERE t.batch_id=? GROUP BY a.id ORDER BY a.id DESC''',(bid,))
    movements=rows('''SELECT v.*,m.name,m.unit,c.name owner_name FROM inventory_movements v JOIN materials m ON m.id=v.material_id
        LEFT JOIN customers c ON c.id=v.owner_customer_id WHERE v.batch_id=? ORDER BY v.id DESC''',(bid,))
    return render_template('business/batch.html',batch=batch,tasks=tasks,materials=choices('materials'),plans=material_plans,
        stock=production_stock,operations=rows('SELECT * FROM operations ORDER BY ord,id'),workers=choices('workers'),history=history,
        finance=core.batch_finance(g.db,bid) if session['role'] in MANAGERS else None,movements=movements,
        deliveries=rows('SELECT * FROM deliveries WHERE batch_id=? ORDER BY id DESC',(bid,)),
        outputs=rows('SELECT * FROM production_outputs WHERE batch_id=? ORDER BY id DESC',(bid,)),
        costs=rows('SELECT * FROM batch_costs WHERE batch_id=? ORDER BY id DESC',(bid,)),
        editable=b['status'] not in ('closed','canceled'))


@bp.route(PREFIX+'/inventory',methods=['GET','POST'])
@access(True)
def inventory():
    if request.method=='POST':
        def change():
            if request.form.get('action')=='material':
                name=request.form.get('name','').strip(); unit=request.form.get('unit','').strip()
                if not name or unit not in ('sht','pary','m2','kg','l','m'): raise core.RuleError('Укажите название и единицу материала.')
                g.db.execute('INSERT INTO materials(name,unit) VALUES (?,?)',(name,unit))
            else: core.inventory_move(g.db,request.form,session['role'])
        return mutate(change,url_for('business.inventory'))
    stock=rows('''SELECT s.*,m.name,m.unit,c.name owner_name,
       COALESCE((SELECT SUM(reserved_milli) FROM batch_material_plan WHERE material_id=s.material_id AND owner_customer_id IS s.owner_customer_id),0) reserved
       FROM stock_balances s JOIN materials m ON m.id=s.material_id LEFT JOIN customers c ON c.id=s.owner_customer_id ORDER BY m.name,c.name''')
    movements=rows('''SELECT v.*,m.name,m.unit,c.name owner_name FROM inventory_movements v JOIN materials m ON m.id=v.material_id
        LEFT JOIN customers c ON c.id=v.owner_customer_id ORDER BY v.id DESC LIMIT 100''')
    return render_template('business/inventory.html',stock=stock,pool=rows('SELECT p.*,m.name,m.unit,c.name owner_name FROM production_pool p JOIN materials m ON m.id=p.material_id LEFT JOIN customers c ON c.id=p.owner_customer_id WHERE p.qty_milli>0 ORDER BY m.name'),movements=movements,materials=choices('materials'),customers=choices('customers'),
        batches=rows(batch_title_query()+" WHERE b.status NOT IN ('closed','canceled') ORDER BY b.id DESC"))


@bp.route(PREFIX+'/payroll/ledger',methods=['GET','POST'])
@access(True)
def payroll():
    start=request.args.get('start',core.today().replace(day=1).isoformat()); end=request.args.get('end',core.today().isoformat())
    try:
        date.fromisoformat(start); date.fromisoformat(end)
        if start>end: raise ValueError
    except ValueError: abort(400)
    if request.method=='POST':
        return mutate(lambda:core.close_period(g.db,request.form.get('start'),request.form.get('end'),session['role']),url_for('business.payroll',start=start,end=end),'Период закрыт. Итоги сохранены.')
    workers=[]
    for w in rows('SELECT * FROM workers ORDER BY archived,number+0,number'):
        d=dict(w); wid=w['id']; d['opening']=core.worker_balance(g.db,wid,start)
        d['accrued']=g.db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_accruals WHERE worker_id=? AND posted_on BETWEEN ? AND ?',(wid,start,end)).fetchone()[0]
        d['paid']=g.db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_payments WHERE worker_id=? AND paid_on BETWEEN ? AND ?',(wid,start,end)).fetchone()[0]
        d['paid']-=g.db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM payroll_cash_reversals WHERE worker_id=? AND reversed_on BETWEEN ? AND ?',(wid,start,end)).fetchone()[0]
        d['closing']=d['opening']+d['accrued']-d['paid']; d['balance']=core.worker_balance(g.db,wid)
        workers.append(d)
    periods=rows('SELECT * FROM payroll_periods ORDER BY start_on DESC')
    snapshots={p['id']:rows('SELECT t.*,w.name,w.number FROM payroll_period_totals t JOIN workers w ON w.id=t.worker_id WHERE t.period_id=?',(p['id'],)) for p in periods}
    return render_template('business/payroll.html',workers=workers,start=start,end=end,periods=periods,snapshots=snapshots)


@bp.route(PREFIX+'/payroll/workers/<int:wid>',methods=['GET','POST'])
@access(True)
def worker(wid):
    w=core.require(g.db,'workers',wid)
    if request.method=='POST':
        def change():
            f=request.form; actor=session['role']; action=f.get('action')
            if action=='payment': core.pay_worker(g.db,wid,f,actor)
            elif action=='payment_reverse': core.reverse_payment(g.db,wid,core.integer(f.get('payment_id')),f.get('reason',''),actor)
            elif action=='adjustment': core.payroll_adjustment(g.db,wid,f,actor)
            elif action=='skills':
                selected=f.getlist('operation_id')
                for op in selected:
                    if not g.db.execute('SELECT 1 FROM operations WHERE id=?',(core.integer(op),)).fetchone(): raise core.RuleError('Операция не найдена.')
                g.db.execute('DELETE FROM worker_skills WHERE worker_id=?',(wid,))
                g.db.executemany('INSERT INTO worker_skills(worker_id,operation_id) VALUES (?,?)',[(wid,int(op)) for op in selected])
            else: raise core.RuleError('Неизвестное действие.')
        return mutate(change,url_for('business.worker',wid=wid))
    accruals=rows('''SELECT a.*,wa.qty_pairs,wa.rate_cents,wa.rate_version,o.name operation_name,m.name model_name,s.share_bp
       FROM payroll_accruals a LEFT JOIN work_acceptances wa ON wa.id=a.acceptance_id LEFT JOIN batch_operations t ON t.id=wa.batch_operation_id
       LEFT JOIN operations o ON o.id=t.operation_id LEFT JOIN production_batches b ON b.id=a.batch_id LEFT JOIN models m ON m.id=b.model_id
       LEFT JOIN work_shares s ON s.acceptance_id=wa.id AND s.worker_id=a.worker_id WHERE a.worker_id=? ORDER BY a.posted_on DESC,a.id DESC''',(wid,))
    return render_template('business/worker.html',worker=w,balance=core.worker_balance(g.db,wid),accruals=accruals,
        payments=rows('SELECT p.*,r.reversed_on,r.reason reversal_reason FROM payroll_payments p LEFT JOIN payroll_cash_reversals r ON r.payment_id=p.id WHERE p.worker_id=? ORDER BY p.paid_on DESC,p.id DESC',(wid,)),
        assignments=rows('''SELECT a.*,t.batch_id,t.qty_pairs,t.minutes_milli,o.name FROM batch_assignments a JOIN batch_operations t ON t.id=a.batch_operation_id
            JOIN operations o ON o.id=t.operation_id WHERE a.worker_id=?''',(wid,)),
        operations=rows('SELECT * FROM operations ORDER BY ord,id'),skills={r['operation_id'] for r in rows('SELECT operation_id FROM worker_skills WHERE worker_id=?',(wid,))})


def register(app):
    app.register_blueprint(bp)
