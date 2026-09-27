"""Internal supplies, receiving and explicit resolution of discrepancies."""
from datetime import datetime
from flask import abort,g,request,session,url_for,render_template,redirect
if __package__:
    from . import business_core as core
else:
    import business_core as core


def move_pending(conn,item_id,qty,kind,actor,note,token):
    p=conn.execute('SELECT * FROM supply_pending_stock WHERE shipment_item_id=?',(item_id,)).fetchone()
    if not p or qty>p['qty_milli']:raise core.RuleError('Количество превышает остаток, который ещё не получен.')
    cost=p['value_cents'] if qty==p['qty_milli'] else p['value_cents']*qty//p['qty_milli']
    conn.execute('UPDATE supply_pending_stock SET qty_milli=qty_milli-?,value_cents=value_cents-? WHERE shipment_item_id=?',(qty,cost,item_id))
    if kind in ('receive','return'):
        table='stock_balances' if kind=='return' else 'production_stock' if p['batch_id'] else 'production_pool'
        row=core.stock_row(conn,table,p['material_id'],p['owner_customer_id'],p['batch_id'])
        conn.execute('UPDATE '+table+' SET qty_milli=qty_milli+?,value_cents=value_cents+? WHERE id=?',(qty,cost,row['id']))
    if kind in ('return','loss'):
        conn.execute('INSERT INTO inventory_movements(material_id,owner_customer_id,batch_id,kind,qty_milli,cost_cents,occurred_on,created_at,note,shipment_item_id,token) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(p['material_id'],p['owner_customer_id'],p['batch_id'],kind,qty,cost,core.day(),core.now(),note,item_id,token))
    core.audit(conn,actor,'supply_'+kind,item_id,{'qty_milli':qty,'cost_cents':cost,'note':note})


def receive(conn,sid,form,actor):
    token=form.get('token')
    if not token or len(token)>128:raise core.RuleError('Обновите форму получения.')
    old=conn.execute('SELECT id FROM supply_receipts WHERE shipment_id=?',(sid,)).fetchone()
    if old:return
    dated=core.day(form.get('received_on'));note=form.get('note','').strip()
    shipment=conn.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()
    if not shipment:raise core.RuleError('Поставка не найдена.')
    sent_on=datetime.strptime(shipment['created_at'],'%d.%m.%Y %H:%M').date().isoformat() if '.' in shipment['created_at'][:10] else shipment['created_at'][:10]
    if dated<sent_on:raise core.RuleError('Получение не может быть раньше отправки.')
    receipt=conn.execute('INSERT INTO supply_receipts(shipment_id,received_on,note,actor,token,created_at) VALUES (?,?,?,?,?,?)',(sid,dated,note,actor,token,core.now())).lastrowid
    for item in conn.execute('SELECT i.*,m.unit material_unit FROM shipment_items i LEFT JOIN materials m ON m.id=i.material_id WHERE shipment_id=?',(sid,)).fetchall():
        if item['batch_reference']:continue
        qty=core.scaled(form.get('qty_'+str(item['id'])),1000,'Полученное количество')
        if (item['line_kind']=='pair' or item['material_unit'] in ('sht','pary')) and qty%1000:raise core.RuleError('Пары и штуки должны быть целыми.')
        sent=core.scaled(str(item['qty']),1000,'Отправленное количество')
        if qty!=sent and not note:raise core.RuleError('При расхождении укажите, что получилось при получении.')
        pending=conn.execute('SELECT * FROM supply_pending_stock WHERE shipment_item_id=?',(item['id'],)).fetchone()
        credit=min(qty,sent) if pending else qty
        if pending and credit:move_pending(conn,item['id'],credit,'receive',actor,note,token+':'+str(item['id']))
        conn.execute('INSERT INTO supply_receipt_items(shipment_item_id,receipt_id,qty_milli,credited_milli) VALUES (?,?,?,?)',(item['id'],receipt,qty,credit))
    core.audit(conn,actor,'supply_receive',sid,{'received_on':dated,'note':note})


def register(app,access,mutate,prefix):
    app.register_error_handler(core.RuleError,lambda exc:(str(exc),404))
    @app.route(prefix+'/requests/supply/new',methods=['GET','POST'])
    @access(True)
    def legacy_supply_new():return redirect(url_for('supply_new',**request.args),code=307 if request.method=='POST' else 302)

    @app.route(prefix+'/supplies')
    @access()
    def supplies():
        bid=request.args.get('batch',type=int)
        shipments=g.db.execute('''SELECT s.*,r.id receipt_id,r.received_on,r.note receipt_note,
           (SELECT COUNT(*) FROM shipment_items WHERE shipment_id=s.id) positions,
           (SELECT COUNT(*) FROM shipment_items i JOIN supply_receipt_items ri ON ri.shipment_item_id=i.id WHERE i.shipment_id=s.id AND ri.qty_milli<>CAST(ROUND(i.qty*1000) AS INTEGER)) differences,
           (SELECT COALESCE(SUM(p.qty_milli),0) FROM supply_pending_stock p JOIN shipment_items i ON i.id=p.shipment_item_id WHERE i.shipment_id=s.id) pending
           FROM shipments s LEFT JOIN supply_receipts r ON r.shipment_id=s.id'''+(' WHERE EXISTS(SELECT 1 FROM shipment_items i WHERE i.shipment_id=s.id AND i.batch_id=?)' if bid else '')+' ORDER BY r.id IS NULL DESC,s.id DESC',(bid,) if bid else ()).fetchall()
        docs=g.db.execute("SELECT d.*,(SELECT COUNT(*) FROM lines WHERE document_id=d.id AND pairs_recv IS NOT NULL AND pairs_recv<>pairs_sent) differences,(SELECT SUM(pairs_sent) FROM lines WHERE document_id=d.id) qty FROM documents d ORDER BY status='sent' DESC,id DESC").fetchall()
        issues=request.args.get('issues')=='1';has_issues=any(s['pending'] or s['differences'] for s in shipments) or any(d['differences'] for d in docs)
        if issues:shipments=[s for s in shipments if s['pending'] or s['differences']];docs=[d for d in docs if d['differences']]
        return render_template('supplies.html',shipments=shipments,documents=docs,batch_id=bid,issues=issues,has_issues=has_issues)

    @app.route(prefix+'/supplies/<int:sid>',methods=['GET','POST'])
    @access()
    def supply_detail(sid):
        shipment=g.db.execute('SELECT * FROM shipments WHERE id=?',(sid,)).fetchone()
        if not shipment:abort(404)
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            action=request.form.get('action')
            if action=='receive' and session['role']!='proizv':abort(403)
            if action!='receive' and session['role'] not in ('sklad','director'):abort(403)
            def change():
                if action=='receive':receive(g.db,sid,request.form,session['role']);return
                token=request.form.get('token');note=request.form.get('note','').strip()
                if not token or not note:raise core.RuleError('Укажите пояснение к исправлению.')
                if g.db.execute('SELECT 1 FROM supply_resolutions WHERE token=?',(token,)).fetchone():return
                iid=core.integer(request.form.get('item_id'));item=g.db.execute('SELECT * FROM shipment_items WHERE id=? AND shipment_id=?',(iid,sid)).fetchone()
                if not item:raise core.RuleError('Позиция другой поставки.')
                if not g.db.execute('SELECT 1 FROM supply_receipts WHERE shipment_id=?',(sid,)).fetchone():raise core.RuleError('Сначала производство должно записать получение.')
                qty=core.scaled(request.form.get('qty'),1000,'Количество',True)
                material=core.require(g.db,'materials',item['material_id']) if item['material_id'] else None
                if material and material['unit'] in ('sht','pary') and qty%1000:raise core.RuleError('Штуки и пары должны быть целыми.')
                if action in ('return','loss','receive_remaining'):
                    move_pending(g.db,iid,qty,'receive' if action=='receive_remaining' else action,session['role'],note,token)
                    if action=='receive_remaining':g.db.execute('UPDATE supply_receipt_items SET credited_milli=credited_milli+?,qty_milli=qty_milli+? WHERE shipment_item_id=?',(qty,qty,iid))
                elif action=='extra':
                    receipt=g.db.execute('SELECT * FROM supply_receipt_items WHERE shipment_item_id=?',(iid,)).fetchone()
                    if not material or qty>receipt['qty_milli']-receipt['credited_milli']:raise core.RuleError('Количество больше полученного излишка.')
                    core.inventory_move(g.db,dict(kind='issue',material_id=material['id'],owner_customer_id=item['owner_customer_id'] or '',batch_id=item['batch_id'] or '',qty=f'{qty//1000}.{qty%1000:03}',token=token,hold_for_receipt='1',note=note),session['role'],iid)
                    move_pending(g.db,iid,qty,'receive',session['role'],note,token+':received')
                    g.db.execute('UPDATE supply_receipt_items SET credited_milli=credited_milli+? WHERE shipment_item_id=?',(qty,iid))
                else:raise core.RuleError('Неизвестное действие.')
                g.db.execute('INSERT INTO supply_resolutions(shipment_item_id,kind,qty_milli,note,actor,created_at,token) VALUES (?,?,?,?,?,?,?)',(iid,'receive' if action=='receive_remaining' else action,qty,note,session['role'],core.now(),token))
            return mutate(change,url_for('supply_detail',sid=sid),'Получение записано.' if action=='receive' else 'Расхождение уточнено. Остатки обновлены.')
        items=g.db.execute('''SELECT i.*,m.name material_name,mo.name model_name,c.name customer_name,
            ri.qty_milli received_milli,ri.credited_milli,p.qty_milli pending_milli
            FROM shipment_items i LEFT JOIN materials m ON m.id=i.material_id LEFT JOIN models mo ON mo.id=i.model_id
            LEFT JOIN customers c ON c.id=i.customer_id LEFT JOIN supply_receipt_items ri ON ri.shipment_item_id=i.id
            LEFT JOIN supply_pending_stock p ON p.shipment_item_id=i.id WHERE i.shipment_id=? ORDER BY i.id''',(sid,)).fetchall()
        receipt=g.db.execute('SELECT * FROM supply_receipts WHERE shipment_id=?',(sid,)).fetchone()
        return render_template('supply_detail.html',shipment=shipment,items=items,receipt=receipt,resolutions=g.db.execute('SELECT * FROM supply_resolutions WHERE shipment_item_id IN (SELECT id FROM shipment_items WHERE shipment_id=?) ORDER BY id DESC',(sid,)).fetchall())

    @app.route(prefix+'/supplies/return/new',methods=['GET','POST'])
    @access()
    def supply_return_new():
        if session['role']!='proizv':abort(403)
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                f=request.form;token=f.get('token')
                if not token:raise core.RuleError('Обновите форму передачи.')
                # The token uses a separate idempotence record for legacy documents.
                old=g.db.execute('SELECT entity_id FROM catalog_submissions WHERE token=? AND kind=?',(token,'shoe_return')).fetchone()
                if old:return url_for('doc_view',doc_id=old[0])
                bid=core.integer(f.get('batch_id'),'Партия');b=core.require(g.db,'production_batches',bid);qty=core.integer(f.get('qty'))
                good=g.db.execute("SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=? AND kind='good'",(bid,)).fetchone()[0]
                previous=g.db.execute("SELECT COALESCE(SUM(l.pairs_sent),0) FROM lines l JOIN documents d ON d.id=l.document_id WHERE l.batch_id=? AND d.kind='RETURN'",(bid,)).fetchone()[0]
                external=g.db.execute('SELECT COALESCE(SUM(factory_pairs),0) FROM deliveries WHERE batch_id=?',(bid,)).fetchone()[0]
                if qty>good-previous-external:raise core.RuleError('Недостаточно готовых пар, ещё не переданных со своего производства.')
                did=g.db.execute("INSERT INTO documents(kind,created_role,created_at,sent_at,status,note) VALUES ('RETURN','proizv',?,?,'sent',?)",(core.now(),core.now(),f.get('note',''))).lastrowid
                g.db.execute("INSERT INTO lines(document_id,customer_id,model_id,status,pairs_sent,batch_id) VALUES (?,?,?,'gotovoe',?,?)",(did,b['customer_id'],b['model_id'],qty,bid))
                g.db.execute('INSERT INTO catalog_submissions(token,kind,entity_id) VALUES (?,?,?)',(token,'shoe_return',did))
                core.audit(g.db,session['role'],'shoe_return',did,{'batch_id':bid,'qty_pairs':qty})
                return url_for('doc_view',doc_id=did)
            return mutate(change,url_for('supply_return_new',batch=request.form.get('batch_id')),'Готовая обувь передана на склад.')
        rows=g.db.execute('SELECT b.*,c.name customer_name,m.name model_name FROM production_batches b JOIN models m ON m.id=b.model_id JOIN customers c ON c.id=b.customer_id ORDER BY b.id DESC').fetchall()
        available=[]
        for row in rows:
            good=g.db.execute("SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=? AND kind='good'",(row['id'],)).fetchone()[0]
            sent=g.db.execute("SELECT COALESCE(SUM(pairs_sent),0) FROM lines l JOIN documents d ON d.id=l.document_id WHERE l.batch_id=? AND d.kind='RETURN'",(row['id'],)).fetchone()[0]
            external=g.db.execute('SELECT COALESCE(SUM(factory_pairs),0) FROM deliveries WHERE batch_id=?',(row['id'],)).fetchone()[0]
            if good>sent+external: available.append(dict(row,available=good-sent-external))
        if request.method=='GET' and not any(not request.args.get('batch',type=int) or b['id']==request.args.get('batch',type=int) for b in available):
            if __package__:from .business import start_action,prerequisite_redirect
            else:from business import start_action,prerequisite_redirect
            return prerequisite_redirect(start_action('production',batch=request.args.get('batch',type=int)))
        return render_template('supply_return_new.html',batches=available,batch_id=request.args.get('batch',type=int))

    @app.route(prefix+'/requests/<int:req_id>/reject',methods=['POST'])
    @access(True)
    def request_reject(req_id):
        def change():
            row=g.db.execute('SELECT * FROM requests WHERE id=?',(req_id,)).fetchone()
            if not row or row['created_role']!='proizv':raise core.RuleError('Заявка не найдена.')
            reason=request.form.get('reason','').strip()
            if not reason:raise core.RuleError('Укажите причину отклонения.')
            if row['status']=='shipped':return
            entries=app.jail_needed_items(req_id)
            for entry in entries:
                if entry['remaining']!=0:g.db.execute("UPDATE request_items SET status='rejected',placed=0,collected=NULL,rejection_note=? WHERE id=?",(reason,entry['row']['id']))
            app.jail_resolve_request(req_id)
            core.audit(g.db,session['role'],'request_reject',req_id,{'reason':reason})
        return mutate(change,url_for('documents'),'Необработанные позиции заявки отклонены.')
