"""Orders, production planning, stock, accepted work and separate payroll cash ledger."""
from functools import wraps
from datetime import date
from decimal import Decimal
from urllib.parse import urlencode, urlsplit, urlunsplit, parse_qsl
import json
import secrets
import sqlite3
from werkzeug.datastructures import MultiDict
from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
if __package__:
    from . import business_core as core
    from . import experience as ux
    from . import director_flow as flow
    from . import catalog,sections
else:
    import business_core as core
    import experience as ux
    import director_flow as flow
    import catalog,sections

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


def cleanup_catalog_files():
    for path in getattr(g,'catalog_files',[]):path.unlink(missing_ok=True)
    g.catalog_files=[]


def mutate(action, target, success='Сохранено.'):
    """Rollback before flashing: app's audit hook commits all pending writes."""
    try:
        g.db.execute('BEGIN IMMEDIATE')
        result=action()
        sections.finish_ready(g.db,session['role'])
        g.db.commit()
        if isinstance(result,str): target=result
        flash(success)
        ctx=request.args.get('ctx','')
        if ctx and len(ctx)<=80:
            parts=urlsplit(target);query=dict(parse_qsl(parts.query));query['ctx']=ctx
            if getattr(g,'created_entity',None):query['created']=':'.join(map(str,g.created_entity))
            target=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),parts.fragment))
    except core.RuleError as exc:
        g.db.rollback(); cleanup_catalog_files(); flash(str(exc), 'error')
        if not ux.remember_form():
            g.render_failed_form=True
            return current_app.view_functions[request.endpoint](**request.view_args)
    except sqlite3.IntegrityError:
        g.db.rollback(); cleanup_catalog_files(); flash('Такая запись уже существует или связана с другими данными.', 'error')
        if not ux.remember_form():
            g.render_failed_form=True
            return current_app.view_functions[request.endpoint](**request.view_args)
    if '#' not in target and target.split('?',1)[0]==request.script_root+request.path:target+='#biz-page-start'
    return redirect(target)


def rows(sql,args=()): return g.db.execute(sql,args).fetchall()
def choices(table):
    if table=='models':return rows('SELECT m.* FROM models m JOIN customers c ON c.id=m.customer_id WHERE m.archived=0 AND c.archived=0 ORDER BY m.name')
    return rows('SELECT * FROM '+table+' WHERE archived=0 ORDER BY name')
def start_action(kind='order',customer=None,order=None,batch=None):
    """Resolve prerequisites before presenting an action, never through empty lists."""
    def action(label,endpoint,**params):return dict(label=label,url=url_for(endpoint,**params))
    if kind=='order':
        customers=choices('customers');models=choices('models')
        if not customers:return action('Добавить заказчика','business.customer_new')
        relevant=[m for m in models if not customer or m['customer_id']==int(customer)]
        if not relevant:return action('Добавить модель','business.model_new',customer=customer or (customers[0]['id'] if len(customers)==1 else None))
        return action('Создать заказ','business.order_new',customer=customer)
    if kind=='production':
        entries=rows("SELECT b.id FROM production_batches b LEFT JOIN order_items i ON i.id=b.order_item_id WHERE b.status NOT IN ('closed','canceled')"+(' AND i.order_id=?' if order else '')+(' AND b.id=?' if batch else ''),tuple(x for x in (order,batch) if x))
        if len(entries)==1:return action('Открыть партию','business.batch_detail',bid=entries[0]['id'])
        if entries:return action('Открыть производство','business.batches',order=order)
        if session.get('role') not in MANAGERS:return action('Запросить материалы','request_new')
        return start_action('order')
    if kind=='delivery':
        available=rows("""SELECT b.id FROM production_batches b JOIN order_items i ON i.id=b.order_item_id
            WHERE b.status<>'closed' AND
            (SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=b.id AND kind='good')>
            (SELECT COALESCE(SUM(qty_pairs),0) FROM deliveries WHERE batch_id=b.id)"""+(' AND i.order_id=?' if order else '')+(' AND b.id=?' if batch else ''),tuple(x for x in (order,batch) if x))
        if available:return action('Создать отгрузку','business.delivery_new',order=order,batch=batch)
        return start_action('production',order=order,batch=batch)
    if kind=='payment':
        if rows('SELECT 1 FROM orders LIMIT 1'):return action('Записать получение денег','business.customer_payment_new',order=order)
        return start_action('order')
    if kind=='return':
        available=rows("""SELECT b.id FROM production_batches b WHERE
            (SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=b.id AND kind='good')>
            (SELECT COALESCE(SUM(l.pairs_sent),0) FROM lines l JOIN documents d ON d.id=l.document_id WHERE l.batch_id=b.id AND d.kind='RETURN')+
            (SELECT COALESCE(SUM(factory_pairs),0) FROM deliveries WHERE batch_id=b.id)"""+(' AND b.id=?' if batch else ''),(batch,) if batch else ())
        if available:return action('Передать готовую обувь на склад','supply_return_new',batch=batch)
        return start_action('production',batch=batch)
    if kind=='supply':
        if not choices('materials') and not rows("SELECT 1 FROM production_batches WHERE status NOT IN ('closed','canceled') LIMIT 1") and not current_app.jail_has_incoming():
            return action('Добавить материал','business.material_new')
        return action('Создать поставку','supply_new',batch=batch)
    raise ValueError(kind)


def prerequisite_redirect(action):
    """Keep a form's return context when an old bookmarked URL needs prerequisites."""
    target=action['url'];parts=urlsplit(target);query=dict(parse_qsl(parts.query))
    for key in ('ctx','resume','created'):
        if request.args.get(key):query[key]=request.args[key]
    return redirect(urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),parts.fragment)))


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
    return dict(start_action=start_action, manager=session.get('role') in MANAGERS, business_token=lambda:secrets.token_urlsafe(24),
                business_today=core.today().isoformat(), retry_fields=ux.retry_form(), operation_modes=MODES, batch_statuses=STATUSES, movement_names=MOVES)


@bp.route(PREFIX+'/orders')
@access(True)
def orders():
    archive=request.args.get('archive')=='1'
    condition=" WHERE o.status IN ('completed','canceled')" if archive else " WHERE o.status NOT IN ('completed','canceled')"
    orders=rows('''SELECT o.*,c.name customer_name,COALESCE(SUM(i.total_cents),0) total,
       COUNT(i.id) positions,(SELECT COALESCE(SUM(amount_cents),0) FROM customer_payments WHERE order_id=o.id) paid
       FROM orders o JOIN customers c ON c.id=o.customer_id LEFT JOIN order_items i ON i.order_id=o.id
       '''+condition+' GROUP BY o.id ORDER BY o.id DESC')
    cid=request.args.get('customer',type=int);mid=request.args.get('model',type=int)
    if cid:orders=[o for o in orders if o['customer_id']==cid]
    if mid:
        matching={r[0] for r in rows('SELECT order_id FROM order_items WHERE model_id=?',(mid,))};orders=[o for o in orders if o['id'] in matching]
    has_orders=bool(rows('SELECT 1 FROM orders LIMIT 1'))
    has_archive=bool(rows("SELECT 1 FROM orders WHERE status IN ('completed','canceled') LIMIT 1"))
    customers=choices('customers');models=choices('models')
    setup=None
    if not customers:setup=dict(title='Сначала добавьте заказчика',text='Укажите, кто заказывает у вас обувь. Затем добавьте его модели и согласованные цены.',label='Добавить заказчика',url=url_for('business.customer_new'),step=1)
    elif not any(m['customer_id'] in [c['id'] for c in customers] for m in models):
        target=url_for('business.model_new',customer=customers[0]['id'] if len(customers)==1 else None)
        setup=dict(title='Теперь добавьте модели заказчика',text='Название или артикул модели и цена за пару. После этого можно принять первый заказ.',label='Добавить модель',url=target,step=2)
    elif not orders:setup=dict(title='Примите следующий заказ' if has_orders else 'Можно принять первый заказ',text='Выберите заказчика и его модели, укажите количество пар и срок. Согласованные цены подставятся сами.',label='Создать заказ',url=url_for('business.order_new'),step=3)
    if session['role'] in MANAGERS:
        overview=[]
        for row in orders:
            entry=dict(row);parts=rows(batch_title_query()+' WHERE oi.order_id=? ORDER BY b.id',(row['id'],))
            entry['total']=sections.order_total(g.db,row);entry['flow']=flow.order_state(g.db,row,parts);entry['paid']=sections.payment_total(g.db,row['id']);overview.append(entry)
        orders=overview
    return render_template('business/orders.html',orders=orders,setup=None if archive else setup,archive=archive,has_archive=has_archive)



@bp.route(PREFIX+'/orders/new',methods=['GET','POST'])
@access(True)
def order_new():
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
        try:
            g.db.execute('BEGIN IMMEDIATE')
            oid=core.create_order(g.db,request.form,session['role'])
            g.db.commit(); flash('Заказ создан. Для каждой модели подготовлена партия.')
            return redirect(url_for('business.order_detail',oid=oid))
        except (core.RuleError,sqlite3.IntegrityError) as exc:
            g.db.rollback(); cleanup_catalog_files(); flash(str(exc) if isinstance(exc,core.RuleError) else 'Проверьте модель и заказчика.', 'error')
    form=MultiDict(request.form)
    if request.method=='GET':
        cid=request.args.get('customer',type=int)
        if cid:core.require(g.db,'customers',cid,True);form['customer_id']=str(cid)
        mid=request.args.get('model',type=int)
        if mid:
            model=core.require(g.db,'models',mid,True)
            if not model['customer_id'] or (cid and model['customer_id']!=cid):abort(400)
            form['customer_id']=str(model['customer_id']);form.setlist('model_id',[str(mid)]);form.setlist('qty',['']);form.setlist('price',[str(Decimal(model['sale_price_cents'])/100) if model['sale_price_cents'] is not None else ''])
    if request.method=='GET':
        next_step=start_action('order',customer=request.args.get('customer',type=int))
        if urlsplit(next_step['url']).path!=request.script_root+request.path:return prerequisite_redirect(next_step)
    fields=('qty','model_id','new_model','price','quantity_unit','price_kind','price_unit','settlement','specification')
    position_rows=[{key:form.getlist(key)[n] if n<len(form.getlist(key)) else '' for key in fields} for n in range(len(form.getlist('qty')))]
    return render_template('business/director_order_new.html',customers=choices('customers'),models=choices('models'),form=form,position_rows=position_rows)



@bp.route(PREFIX+'/orders/<int:oid>',methods=['GET','POST'])
@access(True)
def order_detail(oid):
    order=core.require(g.db,'orders',oid)
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
        def change():
            action=request.form.get('action')
            token=request.form.get('token','')
            if action=='terms':core.amend_terms(g.db,oid,request.form,session['role'])
            elif action=='cancel':sections.cancel_remaining(g.db,oid,request.form,session['role'])
            elif action=='delete':
                sections.delete_order(g.db,oid,session['role']);return url_for('business.orders')
            elif action=='settlement':
                if order['status']!='canceled':raise core.RuleError('Окончательная сумма относится к отменённому заказу.')
                value=core.scaled(request.form.get('settlement_amount'))
                g.db.execute('UPDATE orders SET cancellation_settlement_cents=? WHERE id=?',(value,oid));core.audit(g.db,session['role'],'order_final_settlement',oid,{'amount_cents':value})
            else: raise core.RuleError('Неизвестное действие.')
        return mutate(change,url_for('business.order_detail',oid=oid))
    customer=core.require(g.db,'customers',order['customer_id'])
    items=rows('SELECT i.*,m.name FROM order_items i JOIN models m ON m.id=i.model_id WHERE order_id=?',(oid,))
    batches=rows(batch_title_query()+' WHERE oi.order_id=? ORDER BY b.id',(oid,))
    payments=rows('SELECT * FROM customer_payments WHERE order_id=? ORDER BY paid_on DESC,id DESC',(oid,))
    finance=[core.batch_finance(g.db,b['id']) for b in batches]
    workflow=flow.order_state(g.db,order,batches)
    changes=[]
    for change in rows('SELECT * FROM order_changes WHERE order_id=? ORDER BY id DESC',(oid,)):
        entry=dict(change); entry['before']=json.loads(entry['snapshot']);changes.append(entry)
    return render_template('business/director_order_detail.html',economics=dict(planned=None if any(f['unknown'] for f in finance) else sum(f['planned'] for f in finance),actual=sum(f['actual'] for f in finance),margin=(None if order['cancellation_settlement_cents'] is None else order['cancellation_settlement_cents']-sum(f['actual'] for f in finance)) if order['status']=='canceled' else None if any(f['margin'] is None for f in finance) else sum(f['margin'] for f in finance)),order=order,customer=customer,items=items,batches=batches,payments=payments,
         total=sections.order_total(g.db,order),paid=sections.payment_total(g.db,oid),earned=sum(f['revenue'] for f in finance),
         changes=changes,workflow=workflow,unused=sections.order_unused(g.db,oid))


@bp.route(PREFIX+'/batches')
@access()
def batches():
    show=request.args.get('archive')=='1'
    condition=" WHERE b.status IN ('closed','canceled')" if show else " WHERE b.status NOT IN ('closed','canceled')"
    data=rows(batch_title_query()+condition+(' AND b.order_item_id IN (SELECT id FROM order_items WHERE order_id='+str(request.args.get('order',type=int))+')' if request.args.get('order',type=int) else '')+(' AND b.status<>\'planned\'' if session['role']=='proizv' and request.args.get('preparation')!='1' and not show else '')+' ORDER BY b.due_date IS NULL,b.due_date,b.id DESC')
    overview=[]
    for row in data:
        entry=dict(row);tasks=core.task_rows(g.db,row['id'])
        entry['tasks_total']=len([t for t in tasks if t['mode']!='skip'])
        entry['tasks_done']=len([t for t in tasks if t['mode']!='skip' and t['done']>=t['qty_pairs']])
        entry['flow']=flow.batch_state(g.db,row) if session['role']=='director' else None
        entry['next_label']='Настроить задания' if not tasks and session['role'] in MANAGERS else 'Ожидает заданий склада' if not tasks else 'Задания выполнены' if entry['tasks_done']==entry['tasks_total'] else str(entry['tasks_done'])+' из '+str(entry['tasks_total'])+' операций выполнено'
        overview.append(entry)
    return render_template('business/batches.html',batches=overview,archive=show,has_archive=bool(rows("SELECT 1 FROM production_batches WHERE status IN ('closed','canceled') LIMIT 1")))


@bp.route(PREFIX+'/batches/<int:bid>',methods=['GET','POST'])
@access()
def batch_detail(bid):
    b=core.require(g.db,'production_batches',bid)
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
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
            elif action=='operation_edit':
                task=core.require(g.db,'batch_operations',core.integer(f.get('task_id')))
                fields=f.to_dict()
                if task['rate_cents'] is None and fields.get('rate') and not fields.get('reason'): fields['reason']='Первичная расценка'
                core.update_operation(g.db,bid,task['id'],fields,actor)
            elif action=='worker_add':raise core.RuleError('Добавьте сотрудника в разделе «Сотрудники».')
            elif action=='start':
                state=flow.batch_state(g.db,b)
                if not state['prepared']:raise core.RuleError('Выберите операции и укажите оплату за пару.')
                if b['status']=='planned':
                    g.db.execute("UPDATE production_batches SET status='working' WHERE id=?",(bid,))
                    if b['order_item_id']:g.db.execute("UPDATE orders SET status='working' WHERE id=(SELECT order_id FROM order_items WHERE id=?) AND status='confirmed'",(b['order_item_id'],))
                    core.audit(g.db,actor,'batch_start',bid,{'prepared':True})
            elif action=='template':raise core.RuleError('Настройки следующих заказов сохраняются в разделе «Модели».')
            elif action=='split':
                newbid=core.split_batch(g.db,bid,f,actor)
                flash('Создана партия №'+str(newbid)+'. Назначения для обеих партий задайте заново.')
                return url_for('business.batch_detail',bid=newbid)
            elif action=='material': ux.plan(g.db,bid,f)
            elif action=='no_materials':
                g.db.execute('UPDATE production_batches SET no_materials=1 WHERE id=?',(bid,))
                core.audit(g.db,actor,'no_materials',bid,{'reason':'Материалы для партии не требуются'})
            elif action=='reserve': core.reserve_material(g.db,bid,core.integer(f.get('plan_id')))
            elif action=='material_remove':
                g.db.execute('DELETE FROM batch_material_plan WHERE id=? AND batch_id=?',(core.integer(f.get('plan_id')),bid))
            elif action=='unreserve': g.db.execute('UPDATE batch_material_plan SET reserved_milli=0 WHERE id=? AND batch_id=?',(core.integer(f.get('plan_id')),bid))
            elif action=='material_move':
                if f.get('kind') not in ('consume','return','loss','allocate'):raise core.RuleError('Поступление записывается в «Материалах», отправка — в «Поставках».')
                ux.receipt(g.db,f,actor,bid)
            elif action=='work': ux.work(g.db,bid,f,actor)
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
                    raise core.RuleError('Для сотрудника не отмечен навык. Укажите навык в разделе «Сотрудники» или отметьте назначение без навыка.')
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
            elif action in ('delivery','delivery_accept'):raise core.RuleError('Отгрузки и приёмка заказчиком записываются в разделе «Отгрузки».')
            elif action=='close':
                fin=core.batch_finance(g.db,bid)
                if fin['accepted']<b['qty_pairs']: raise core.RuleError('Заказчик ещё не принял весь объём партии.')
                if g.db.execute('SELECT 1 FROM production_stock WHERE batch_id=? AND qty_milli>0',(bid,)).fetchone(): raise core.RuleError('Отметьте расход или возврат оставшихся материалов партии.')
                if sections.supply_unresolved(g.db,bid): raise core.RuleError('Сначала уточните расхождения поставки этой партии в разделе «Поставки».')
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
        return mutate(change,url_for('business.batch_detail',bid=bid),{'worker_add':'Сотрудник добавлен. Можно продолжить подготовку модели.','start':'Задание передано в производство. Склад и производство видят его в своих партиях.','work':'Работа записана. Оплата начислена сотрудникам.', 'operations_bulk':'Операции добавлены в задания партии.', 'material':'Материал сохранён в плане партии.', 'material_move':'Движение записано. Остатки обновлены.', 'output':'Выпуск готовой обуви записан.', 'delivery':'Отгрузка записана. Отметьте приёмку после получения обуви заказчиком.', 'delivery_accept':'Приёмка заказчиком записана.', 'close':'Партия закрыта. Итоги сохранены.', 'template':'Настройки сохранены для следующих заказов этой модели.'}.get(action,'Изменения сохранены.'))
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
    outputs=rows('SELECT * FROM production_outputs WHERE batch_id=? ORDER BY id DESC',(bid,))
    deliveries=rows('SELECT d.*,(SELECT COALESCE(SUM(a.qty_pairs),0) FROM delivery_acceptances a WHERE a.delivery_id=d.id) accepted_qty FROM deliveries d WHERE batch_id=? ORDER BY id DESC',(bid,))
    workers=choices('workers')
    guidance=ux.batch_guidance(batch,tasks,material_plans,production_stock,outputs,deliveries,session['role'] in MANAGERS,len(workers),any(v['kind'] in ('issue','allocate','consume','loss') for v in movements))
    director_state=flow.batch_state(g.db,batch) if session['role'] in MANAGERS else None
    if director_state:
        target=director_state['target'];pane='materials' if target in ('biz-plan-material','biz-material-move') else 'finish' if target in ('biz-delivery','biz-output','biz-close') or director_state['stage']==4 else 'tasks'
        guidance.update(title=director_state['title'],text=director_state['text'],target=target,label=director_state['label'],pane=pane)
    if guidance.get('target')=='biz-work' and not workers and session['role'] in MANAGERS:guidance['href']=url_for('business.staff_new');guidance['label']='Добавить сотрудника'
    if guidance.get('target')=='biz-supplies':guidance['href']=url_for('supplies',batch=bid)
    if guidance.get('target')=='biz-delivery':guidance['href']=url_for('business.deliveries',batch=bid) if guidance['delivered']>=guidance['good'] else url_for('business.delivery_new',batch=bid)
    warehouse_stock=rows('SELECT material_id,owner_customer_id,qty_milli FROM stock_balances WHERE qty_milli>0') if session['role'] in MANAGERS else []
    stock_data=dict(batch_id=bid,warehouse=[dict(r) for r in warehouse_stock],production=[{key:r[key] for key in ('material_id','owner_customer_id','qty_milli')} for r in production_stock],pool=[dict(r) for r in rows('SELECT material_id,owner_customer_id,qty_milli FROM production_pool WHERE qty_milli>0')])
    return render_template('business/batch.html',guidance=guidance,director_state=director_state,stock_data=stock_data,batch=batch,tasks=tasks,materials=rows('SELECT m.* FROM materials m WHERE m.archived=0 OR EXISTS(SELECT 1 FROM production_stock s WHERE s.material_id=m.id AND s.batch_id=?) ORDER BY m.name',(bid,)),plans=material_plans,
        stock=production_stock,operations=rows('SELECT * FROM operations ORDER BY ord,id'),workers=workers,history=history,
        finance=core.batch_finance(g.db,bid) if session['role'] in MANAGERS else None,movements=movements,
        deliveries=deliveries,outputs=outputs,
        costs=rows('SELECT * FROM batch_costs WHERE batch_id=? ORDER BY id DESC',(bid,)),
        editable=b['status'] not in ('closed','canceled'))


@bp.route(PREFIX+'/inventory',methods=['GET','POST'])
@access(True)
def inventory():
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
        def change():
            if request.form.get('action')=='material':raise core.RuleError('Откройте «Добавить материал» в этом разделе.')
            else:
                if request.form.get('kind')!='receipt':raise core.RuleError('Отправьте материалы в разделе «Поставки».')
                token,old=catalog.once(g.db,request.form,'inventory_receipt')
                if old is not None:return
                ids=request.form.getlist('material_id')
                if len(ids)<=1:ux.receipt(g.db,request.form,session['role'])
                else:
                    if len(ids)>100:raise core.RuleError('В поступлении допускается до 100 строк.')
                    count=0
                    for n,mid in enumerate(ids):
                        fields=request.form.to_dict()
                        for key in ('material_id','qty','cost','owner_customer_id'):
                            values=request.form.getlist(key);fields[key]=values[n] if n<len(values) else ''
                        if not mid and not fields['qty'].strip() and not fields['cost'].strip():continue
                        fields['token']=request.form.get('token','')+':'+str(n) if request.form.get('token') else ''
                        core.inventory_move(g.db,fields,session['role']);count+=1
                    if not count:raise core.RuleError('Добавьте хотя бы один материал и количество.')
                catalog.remember(g.db,token,'inventory_receipt',g.db.execute('SELECT MAX(id) FROM inventory_movements').fetchone()[0])
        return mutate(change,url_for('business.inventory'),'Материал добавлен в справочник.' if request.form.get('action')=='material' else 'Движение записано. Остатки материалов обновлены.')
    stock=rows('''SELECT s.*,m.name,m.unit,c.name owner_name,
       COALESCE((SELECT SUM(reserved_milli) FROM batch_material_plan WHERE material_id=s.material_id AND owner_customer_id IS s.owner_customer_id),0) reserved
       FROM stock_balances s JOIN materials m ON m.id=s.material_id LEFT JOIN customers c ON c.id=s.owner_customer_id ORDER BY m.name,c.name''')
    movements=rows('''SELECT v.*,m.name,m.unit,c.name owner_name FROM inventory_movements v JOIN materials m ON m.id=v.material_id
        LEFT JOIN customers c ON c.id=v.owner_customer_id ORDER BY v.id DESC LIMIT 100''')
    return render_template('business/inventory.html',stock_data=dict(warehouse=[dict(r) for r in stock],production=[dict(r) for r in rows('SELECT batch_id,material_id,owner_customer_id,qty_milli FROM production_stock WHERE qty_milli>0')],pool=[dict(r) for r in rows('SELECT material_id,owner_customer_id,qty_milli FROM production_pool WHERE qty_milli>0')]),stock=stock,pool=rows('SELECT p.*,m.name,m.unit,c.name owner_name FROM production_pool p JOIN materials m ON m.id=p.material_id LEFT JOIN customers c ON c.id=p.owner_customer_id WHERE p.qty_milli>0 ORDER BY m.name'),movements=movements,materials=choices('materials'),archived_materials=rows('SELECT * FROM materials WHERE archived=1 ORDER BY name'),customers=choices('customers'),
        batches=rows(batch_title_query()+" WHERE b.status NOT IN ('closed','canceled') ORDER BY b.id DESC"))


@bp.route(PREFIX+'/inventory/new',methods=['GET','POST'])
@access(True)
def material_new():
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
        def change():
            f=request.form;token,old=catalog.once(g.db,f,'material_create')
            if old:g.created_entity=('material',old);return url_for('business.material_detail',mid=old)
            name=catalog.text(f,'name','название материала',required=True);unit=f.get('unit')
            if unit not in ('sht','pary','m2','kg','l','m'):raise core.RuleError('Выберите единицу материала.')
            mid=g.db.execute('INSERT INTO materials(name,unit) VALUES (?,?)',(name,unit)).lastrowid
            catalog.remember(g.db,token,'material_create',mid);g.created_entity=('material',mid)
            core.audit(g.db,session['role'],'material_create',mid,{'name':name,'unit':unit})
            return url_for('business.material_detail',mid=mid)
        return mutate(change,url_for('business.material_new'),'Материал сохранён.')
    return render_template('business/material_new.html')


@bp.route(PREFIX+'/inventory/materials/<int:mid>',methods=['GET','POST'])
@access(True)
def material_detail(mid):
    material=core.require(g.db,'materials',mid)
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
        def change():
            f=request.form;action=f.get('action')
            if action in ('delete','archive','restore'):
                catalog.lifecycle(g.db,'materials',mid,action,session['role'])
                if action=='delete':return url_for('business.inventory')
            elif action=='material_edit':
                name=catalog.text(f,'name','название материала',required=True);unit=f.get('unit')
                if unit not in ('sht','pary','m2','kg','l','m'):raise core.RuleError('Выберите единицу.')
                if unit!=material['unit'] and catalog.references(g.db,'materials',mid):raise core.RuleError('Материал уже используется. Единицу менять нельзя.')
                g.db.execute('UPDATE materials SET name=?,unit=? WHERE id=?',(name,unit,mid))
            else:raise core.RuleError('Неизвестное действие.')
        return mutate(change,url_for('business.material_detail',mid=mid),'Материал удалён.' if request.form.get('action')=='delete' else 'Материал сохранён.')
    return render_template('business/material.html',material=material,used=catalog.references(g.db,'materials',mid))


@bp.route(PREFIX+'/payroll/ledger',methods=['GET','POST'])
@access(True)
def payroll():
    start=request.args.get('start',core.today().replace(day=1).isoformat()); end=request.args.get('end',core.today().isoformat())
    try:
        date.fromisoformat(start); date.fromisoformat(end)
        if start>end: raise ValueError
    except ValueError: abort(400)
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
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
    if request.method=='POST' and not getattr(g,'render_failed_form',False):
        def change():
            f=request.form; actor=session['role']; action=f.get('action')
            if action=='payment': core.pay_worker(g.db,wid,f,actor)
            elif action=='payment_reverse': core.reverse_payment(g.db,wid,core.integer(f.get('payment_id')),f.get('reason',''),actor)
            elif action=='adjustment': core.payroll_adjustment(g.db,wid,f,actor)
            elif action=='skills':raise core.RuleError('Навыки редактируются в разделе «Сотрудники».')
            else: raise core.RuleError('Неизвестное действие.')
        return mutate(change,url_for('business.worker',wid=wid),'Выплата записана. Остаток зарплаты обновлён.' if request.form.get('action')=='payment' else 'Изменения сохранены.')
    accruals=rows('''SELECT a.*,wa.qty_pairs,wa.rate_cents,wa.rate_version,o.name operation_name,m.name model_name,s.share_bp
       FROM payroll_accruals a LEFT JOIN work_acceptances wa ON wa.id=a.acceptance_id LEFT JOIN batch_operations t ON t.id=wa.batch_operation_id
       LEFT JOIN operations o ON o.id=t.operation_id LEFT JOIN production_batches b ON b.id=a.batch_id LEFT JOIN models m ON m.id=b.model_id
       LEFT JOIN work_shares s ON s.acceptance_id=wa.id AND s.worker_id=a.worker_id WHERE a.worker_id=? ORDER BY a.posted_on DESC,a.id DESC''',(wid,))
    return render_template('business/worker.html',worker=w,balance=core.worker_balance(g.db,wid),accruals=accruals,
        payments=rows('SELECT p.*,r.reversed_on,r.reason reversal_reason FROM payroll_payments p LEFT JOIN payroll_cash_reversals r ON r.payment_id=p.id WHERE p.worker_id=? ORDER BY p.paid_on DESC,p.id DESC',(wid,)),
        assignments=rows('''SELECT a.*,t.batch_id,t.qty_pairs,t.minutes_milli,o.name FROM batch_assignments a JOIN batch_operations t ON t.id=a.batch_operation_id
            JOIN operations o ON o.id=t.operation_id WHERE a.worker_id=?''',(wid,)),
        operations=rows('SELECT * FROM operations ORDER BY ord,id'),skills={r['operation_id'] for r in rows('SELECT operation_id FROM worker_skills WHERE worker_id=?',(wid,))})


@bp.app_template_filter('money_input')
def money_input(value):
    return format(Decimal(value)/100,'.2f') if value is not None else ''


def register(app):
    catalog.register(bp,access,mutate,rows,choices,PREFIX)
    sections.register(bp,access,mutate,rows,choices,PREFIX,batch_title_query)
    app.register_blueprint(bp)
