"""Order receipts and customer deliveries have their own sections and forms."""
from flask import g,request,session,url_for,render_template,redirect
if __package__:
    from . import business_core as core
else:
    import business_core as core


def payment_total(conn,oid):
    return conn.execute('SELECT COALESCE(SUM(p.amount_cents-COALESCE(r.amount_cents,0)),0) FROM customer_payments p LEFT JOIN customer_payment_reversals r ON r.payment_id=p.id WHERE p.order_id=?',(oid,)).fetchone()[0]


def accepted_pairs(conn,bid):
    return conn.execute('''SELECT COALESCE(SUM(CASE WHEN EXISTS(SELECT 1 FROM delivery_acceptances a WHERE a.delivery_id=d.id)
       THEN (SELECT SUM(a.qty_pairs) FROM delivery_acceptances a WHERE a.delivery_id=d.id)
       ELSE CASE WHEN d.accepted_on IS NOT NULL THEN d.qty_pairs ELSE 0 END END),0) FROM deliveries d WHERE d.batch_id=?''',(bid,)).fetchone()[0]


def order_total(conn,order):
    return order['cancellation_settlement_cents'] if order['status']=='canceled' else conn.execute('SELECT COALESCE(SUM(total_cents),0) FROM order_items WHERE order_id=?',(order['id'],)).fetchone()[0]


def accept_delivery(conn,did,qty,dated,actor,token):
    if conn.execute('SELECT 1 FROM delivery_acceptances WHERE token=?',(token,)).fetchone():return
    d=core.require(conn,'deliveries',did);dated=core.day(dated);qty=core.integer(qty)
    before=conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM delivery_acceptances WHERE delivery_id=?',(did,)).fetchone()[0]
    if before==0 and d['accepted_on']:before=d['qty_pairs']
    if before+qty>d['qty_pairs']:raise core.RuleError('Количество больше оставшегося в отгрузке.')
    if dated<d['delivered_on']:raise core.RuleError('Приёмка не может быть раньше отправки.')
    conn.execute('INSERT INTO delivery_acceptances(delivery_id,qty_pairs,accepted_on,actor,token,created_at) VALUES (?,?,?,?,?,?)',(did,qty,dated,actor,token,core.now()))
    if before+qty==d['qty_pairs']:conn.execute('UPDATE deliveries SET accepted_on=? WHERE id=?',(dated,did))
    core.audit(conn,actor,'delivery_accept',did,{'qty_pairs':qty,'accepted_on':dated})


def register(bp,access,mutate,rows,choices,prefix,batch_query):
    def orders_data():
        result=[]
        for r in rows('SELECT o.*,c.name customer_name FROM orders o JOIN customers c ON c.id=o.customer_id ORDER BY o.id DESC'):
            entry=dict(r);entry['total']=order_total(g.db,r);entry['paid']=payment_total(g.db,r['id']);entry['remaining']=None if entry['total'] is None else entry['total']-entry['paid'];result.append(entry)
        return result

    @bp.route(prefix+'/customer-payments',methods=['GET','POST'])
    @access(True)
    def customer_payments():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                if request.form.get('action')!='reverse':raise core.RuleError('Неизвестное действие.')
                pid=core.integer(request.form.get('payment_id'),'Платёж');p=g.db.execute('SELECT * FROM customer_payments WHERE id=?',(pid,)).fetchone()
                if not p:raise core.RuleError('Платёж не найден.')
                if g.db.execute('SELECT 1 FROM customer_payment_reversals WHERE payment_id=?',(pid,)).fetchone():return
                reason=request.form.get('reason','').strip()
                if not reason:raise core.RuleError('Укажите причину отмены записи.')
                token=request.form.get('token')
                if not token:raise core.RuleError('Обновите форму.')
                g.db.execute('INSERT INTO customer_payment_reversals(payment_id,amount_cents,reversed_on,reason,actor,created_at,token) VALUES (?,?,?,?,?,?,?)',(pid,p['amount_cents'],core.day(),reason,session['role'],core.now(),token))
                core.audit(g.db,session['role'],'customer_payment_reverse',pid,{'reason':reason})
            return mutate(change,url_for('business.customer_payments',order=request.args.get('order')),'Запись оплаты отменена. История сохранена.')
        oid=request.args.get('order',type=int);orders=orders_data()
        if oid:orders=[o for o in orders if o['id']==oid]
        orders.sort(key=lambda o:(o['remaining'] is not None and o['remaining']<=0,-o['id']))
        history=rows('SELECT p.*,c.name customer_name,r.reason reversal_reason,r.reversed_on FROM customer_payments p JOIN orders o ON o.id=p.order_id JOIN customers c ON c.id=o.customer_id LEFT JOIN customer_payment_reversals r ON r.payment_id=p.id'+(' WHERE p.order_id=?' if oid else '')+' ORDER BY p.id DESC',(oid,) if oid else ())
        return render_template('business/customer_payments.html',orders=orders,history=history,order_id=oid)

    @bp.route(prefix+'/customer-payments/new',methods=['GET','POST'])
    @access(True)
    def customer_payment_new():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                oid=core.integer(request.form.get('order_id'),'Заказ');core.require(g.db,'orders',oid)
                token=request.form.get('token')
                if not token or len(token)>128:raise core.RuleError('Обновите форму оплаты.')
                if not g.db.execute('SELECT 1 FROM customer_payments WHERE token=?',(token,)).fetchone():
                    amount=core.scaled(request.form.get('amount'),positive=True)
                    pid=g.db.execute('INSERT INTO customer_payments(order_id,amount_cents,paid_on,note,actor,token) VALUES (?,?,?,?,?,?)',(oid,amount,core.day(request.form.get('paid_on')),request.form.get('note','').strip(),session['role'],token)).lastrowid
                    core.audit(g.db,session['role'],'customer_payment',pid,{'order_id':oid,'amount_cents':amount})
                return url_for('business.customer_payments',order=oid)
            return mutate(change,url_for('business.customer_payment_new',order=request.form.get('order_id')),'Получение денег записано.')
        data=orders_data()
        if request.method=='GET' and not data:
            if __package__:from .business import start_action,prerequisite_redirect
            else:from business import start_action,prerequisite_redirect
            return prerequisite_redirect(start_action('order'))
        return render_template('business/customer_payment_new.html',orders=data,order_id=request.args.get('order',type=int))

    @bp.route(prefix+'/deliveries')
    @access(True)
    def deliveries():
        oid=request.args.get('order',type=int);bid=request.args.get('batch',type=int)
        documents=rows('''SELECT h.*,c.name customer_name,COALESCE(SUM(d.qty_pairs),0) qty,
           COALESCE((SELECT SUM(a.qty_pairs) FROM delivery_acceptances a JOIN deliveries x ON x.id=a.delivery_id WHERE x.document_id=h.id),0) accepted
           FROM delivery_documents h JOIN orders o ON o.id=h.order_id JOIN customers c ON c.id=o.customer_id JOIN deliveries d ON d.document_id=h.id'''+(' WHERE h.order_id=?' if oid else ' WHERE d.batch_id=?' if bid else '')+' GROUP BY h.id ORDER BY (qty>accepted) DESC,h.id DESC',(oid,) if oid else (bid,) if bid else ())
        return render_template('business/deliveries.html',deliveries=documents,order_id=oid,batch_id=bid)

    @bp.route(prefix+'/deliveries/new',methods=['GET','POST'])
    @access(True)
    def delivery_new():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                f=request.form;token=f.get('token')
                if not token or len(token)>128:raise core.RuleError('Обновите форму отгрузки.')
                old=g.db.execute('SELECT id FROM delivery_documents WHERE token=?',(token,)).fetchone()
                if old:return url_for('business.delivery_detail',did=old['id'])
                oid=core.integer(f.get('order_id'),'Заказ');core.require(g.db,'orders',oid)
                selected=[]
                for b in rows(batch_query()+' WHERE oi.order_id=?',(oid,)):
                    raw=f.get('qty_'+str(b['id']),'').strip()
                    if raw and core.integer(raw,positive=False)>0:selected.append((b,raw))
                if not selected:raise core.RuleError('Укажите количество хотя бы для одной модели.')
                dated=core.day(f.get('delivered_on'))
                did=g.db.execute('INSERT INTO delivery_documents(order_id,delivered_on,note,actor,token) VALUES (?,?,?,?,?)',(oid,dated,f.get('note',''),session['role'],token)).lastrowid
                for b,qty in selected:
                    rowtoken=token+':'+str(b['id']);core.add_delivery(g.db,b['id'],dict(token=rowtoken,qty=qty,delivered_on=dated,note=f.get('note','')),session['role'])
                    g.db.execute('UPDATE deliveries SET document_id=? WHERE token=?',(did,rowtoken))
                core.audit(g.db,session['role'],'delivery_document',did,{'order_id':oid})
                return url_for('business.delivery_detail',did=did)
            return mutate(change,url_for('business.delivery_new',order=request.form.get('order_id')),'Отгрузка записана.')
        oid=request.args.get('order',type=int);bid=request.args.get('batch',type=int)
        if bid:
            batch=core.require(g.db,'production_batches',bid);item=core.require(g.db,'order_items',batch['order_item_id']);oid=item['order_id']
        data=rows(batch_query()+" WHERE oi.order_id IS NOT NULL AND b.status<>'closed' ORDER BY b.id")
        parts=[]
        for r in data:
            b=dict(r);b['available']=b['good']-rows('SELECT COALESCE(SUM(qty_pairs),0) n FROM deliveries WHERE batch_id=?',(b['id'],))[0]['n']
            if b['available']>0:parts.append(b)
        if request.method=='GET' and not any(not oid or b['order_id']==oid for b in parts):
            if __package__:from .business import start_action,prerequisite_redirect
            else:from business import start_action,prerequisite_redirect
            return prerequisite_redirect(start_action('production',order=oid,batch=bid))
        return render_template('business/delivery_new.html',orders=orders_data(),batches=parts,order_id=oid,batch_id=bid)

    @bp.route(prefix+'/deliveries/<int:did>',methods=['GET','POST'])
    @access(True)
    def delivery_detail(did):
        document=rows('SELECT h.*,c.name customer_name FROM delivery_documents h JOIN orders o ON o.id=h.order_id JOIN customers c ON c.id=o.customer_id WHERE h.id=?',(did,))
        if not document:raise core.RuleError('Отгрузка не найдена.')
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                token=request.form.get('token')
                if not token:raise core.RuleError('Обновите форму.')
                count=0
                for d in rows('SELECT * FROM deliveries WHERE document_id=?',(did,)):
                    raw=request.form.get('qty_'+str(d['id']),'').strip()
                    if raw and core.integer(raw,positive=False)>0:accept_delivery(g.db,d['id'],raw,request.form.get('accepted_on'),session['role'],token+':'+str(d['id']));count+=1
                if not count:raise core.RuleError('Укажите фактически принятые пары.')
            return mutate(change,url_for('business.delivery_detail',did=did),'Приёмка заказчиком записана.')
        items=rows('SELECT d.*,m.name model_name,(SELECT COALESCE(SUM(qty_pairs),0) FROM delivery_acceptances a WHERE a.delivery_id=d.id) accepted FROM deliveries d JOIN production_batches b ON b.id=d.batch_id JOIN models m ON m.id=b.model_id WHERE d.document_id=?',(did,))
        return render_template('business/delivery_detail.html',document=document[0],items=items,history=rows('SELECT a.*,m.name model_name FROM delivery_acceptances a JOIN deliveries d ON d.id=a.delivery_id JOIN production_batches b ON b.id=d.batch_id JOIN models m ON m.id=b.model_id WHERE d.document_id=? ORDER BY a.id DESC',(did,)))


def order_unused(conn,oid):
    if __package__:
        from . import catalog
    else:
        import catalog
    if catalog.references(conn,'orders',oid,('order_items','order_changes')):return False
    for b in conn.execute('SELECT b.id FROM production_batches b JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=?',(oid,)):
        if catalog.references(conn,'production_batches',b[0],('batch_operations','batch_material_plan')):return False
        if conn.execute('SELECT 1 FROM batch_operations WHERE batch_id=? AND (external_done>0 OR external_cost_cents>0)',(b[0],)).fetchone():return False
        for t in conn.execute('SELECT id FROM batch_operations WHERE batch_id=?',(b[0],)):
            if catalog.references(conn,'batch_operations',t[0],('batch_assignments','operation_rate_versions','batch_operations')):return False
    return True


def delete_order(conn,oid,actor):
    if not order_unused(conn,oid):raise core.RuleError('У заказа уже есть работа, движения или деньги. Сохраните историю и отмените оставшуюся работу.')
    snapshot=dict(core.require(conn,'orders',oid))
    bids=[r[0] for r in conn.execute('SELECT b.id FROM production_batches b JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=?',(oid,))]
    for bid in bids:
        for table in ('batch_assignments','operation_rate_versions'):
            key='batch_operation_id';conn.execute('DELETE FROM '+table+' WHERE '+key+' IN (SELECT id FROM batch_operations WHERE batch_id=?)',(bid,))
        conn.execute('DELETE FROM batch_operations WHERE batch_id=?',(bid,));conn.execute('DELETE FROM batch_material_plan WHERE batch_id=?',(bid,));conn.execute('DELETE FROM production_batches WHERE id=?',(bid,))
    conn.execute('DELETE FROM order_changes WHERE order_id=?',(oid,));conn.execute('DELETE FROM order_items WHERE order_id=?',(oid,));conn.execute('DELETE FROM orders WHERE id=?',(oid,))
    core.audit(conn,actor,'order_delete',oid,snapshot)


def cancel_remaining(conn,oid,form,actor):
    order=core.require(conn,'orders',oid)
    if order['status']=='canceled':return
    if order['status']=='completed':raise core.RuleError('Заказ уже выполнен.')
    reason=form.get('note','').strip()
    if not reason:raise core.RuleError('Укажите причину отмены оставшейся работы.')
    batches=conn.execute('SELECT b.* FROM production_batches b JOIN order_items i ON i.id=b.order_item_id WHERE i.order_id=?',(oid,)).fetchall()
    for b in batches:
        if conn.execute('SELECT 1 FROM production_stock WHERE batch_id=? AND qty_milli>0',(b['id'],)).fetchone():raise core.RuleError('Сначала запишите расход или возврат оставшихся материалов в производстве.')
        if supply_unresolved(conn,b['id']):raise core.RuleError('Сначала уточните неполученную поставку этой партии.')
    value=core.scaled(form['settlement_amount']) if form.get('settlement_amount','').strip() else None
    conn.execute("UPDATE orders SET status='canceled',cancellation_settlement_cents=? WHERE id=?",(value,oid))
    for b in batches:
        if b['status']!='closed':conn.execute("UPDATE production_batches SET status='canceled' WHERE id=?",(b['id'],))
        conn.execute('UPDATE batch_material_plan SET reserved_milli=0 WHERE batch_id=?',(b['id'],))
    core.audit(conn,actor,'order_cancel_remaining',oid,{'reason':reason,'final_settlement_cents':value,'preserved_batches':[b['id'] for b in batches]})


def finish_ready(conn,actor):
    """Completion follows actual accepted volume and cleared material obligations."""
    for b in conn.execute("SELECT * FROM production_batches WHERE order_item_id IS NOT NULL AND status IN ('working','completed')").fetchall():
        bid=b['id']
        if accepted_pairs(conn,bid)<b['qty_pairs']:continue
        if conn.execute('SELECT 1 FROM production_stock WHERE batch_id=? AND qty_milli>0',(bid,)).fetchone():continue
        if supply_unresolved(conn,bid):continue
        if any(t['done']<t['qty_pairs'] for t in core.task_rows(conn,bid) if t['mode']!='skip'):continue
        fin=core.batch_finance(conn,bid)
        if fin['unknown']:continue
        conn.execute("UPDATE production_batches SET status='closed' WHERE id=?",(bid,));conn.execute('UPDATE batch_material_plan SET reserved_milli=0 WHERE batch_id=?',(bid,))
        core.audit(conn,actor,'batch_complete_automatic',bid,{'accepted':fin['accepted']})
    conn.execute("UPDATE orders SET status='completed' WHERE status NOT IN ('completed','canceled') AND EXISTS(SELECT 1 FROM order_items i JOIN production_batches b ON b.order_item_id=i.id WHERE i.order_id=orders.id) AND NOT EXISTS(SELECT 1 FROM order_items i JOIN production_batches b ON b.order_item_id=i.id WHERE i.order_id=orders.id AND b.status NOT IN ('closed','canceled'))")


def supply_unresolved(conn,bid):
    return bool(conn.execute('SELECT 1 FROM supply_pending_stock WHERE batch_id=? AND qty_milli>0',(bid,)).fetchone() or conn.execute('SELECT 1 FROM supply_receipt_items r JOIN shipment_items i ON i.id=r.shipment_item_id WHERE i.batch_id=? AND r.qty_milli>r.credited_milli',(bid,)).fetchone())
