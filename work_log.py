"""Daily factory quantities accrue wages from the director's dated operation rates."""
from flask import abort,g,request,session,url_for,render_template
from werkzeug.datastructures import MultiDict
if __package__:
    from . import business_core as core
else:
    import business_core as core


def register(bp,access,mutate,rows,choices,prefix):
    @bp.route(prefix+'/work-log',methods=['GET','POST'])
    @access()
    def work_log():
        factory=session['role']=='proizv'
        raw_date=request.form.get('worked_on') if request.method=='POST' else request.args.get('date')
        dated=core.day(raw_date)
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            if not factory:abort(403)
            def save():
                if request.form.get('action')=='reverse':
                    aid=core.integer(request.form.get('acceptance_id'),'Запись')
                    a=core.require(g.db,'work_acceptances',aid)
                    if a['actor']!='proizv' or a['worked_on']!=dated:abort(403)
                    core.reverse_work(g.db,aid,'Ошибка в дневной выработке',session['role'])
                    return
                if request.form.get('action')!='daily_work':raise core.RuleError('Неизвестное действие.')
                token=request.form.get('token','')
                if not token or len(token)>100:raise core.RuleError('Обновите форму выработки.')
                fields={k:request.form.getlist(k) for k in ('worker_id','batch_id','task_id','qty')}
                size=max(map(len,fields.values()),default=0)
                if size>100:raise core.RuleError('За один раз можно сохранить до 100 строк.')
                entries=[]
                for n in range(size):
                    values={k:v[n].strip() if n<len(v) else '' for k,v in fields.items()}
                    if not values['worker_id'] and not values['qty']:continue
                    try:
                        bid=core.integer(values['batch_id'],'Партия')
                        tid=core.integer(values['task_id'],'Операция')
                        task=core.require(g.db,'batch_operations',tid)
                        if task['batch_id']!=bid:raise core.RuleError('Операция другой партии.')
                        entries.append((n,bid,tid,values))
                    except core.RuleError as exc:raise core.RuleError('Строка '+str(n+1)+': '+str(exc)) from exc
                if not entries:raise core.RuleError('Заполните хотя бы одну строку выработки.')
                depth_cache={}
                def depth(tid):
                    if tid not in depth_cache:
                        predecessor=g.db.execute('SELECT predecessor_id FROM batch_operations WHERE id=?',(tid,)).fetchone()[0]
                        depth_cache[tid]=1+depth(predecessor) if predecessor else 0
                    return depth_cache[tid]
                # The paper sheet may list workers in any order. Save prior operations first.
                entries.sort(key=lambda entry:(depth(entry[2]),entry[0]))
                for n,bid,tid,values in entries:
                    try:
                        line=MultiDict(dict(token=token+':'+str(n),task_id=str(tid),worker_id=values['worker_id'],qty=values['qty'],worked_on=dated))
                        core.accept_work(g.db,bid,line,'proizv')
                    except core.RuleError as exc:raise core.RuleError('Строка '+str(n+1)+': '+str(exc)) from exc
            return mutate(save,url_for('business.work_log',date=dated),'Ошибка отменена. Начисление исправлено.' if request.form.get('action')=='reverse' else 'Выработка сохранена. Зарплата начислена автоматически.')
        tasks=rows('''SELECT t.id,t.batch_id,o.name,
          t.qty_pairs-COALESCE((SELECT SUM(qty_pairs) FROM work_acceptances WHERE batch_operation_id=t.id AND canceled_at IS NULL),0) remaining,
          (SELECT r.rate_cents FROM operation_rate_versions r WHERE r.batch_operation_id=t.id AND r.effective_date<=? ORDER BY r.effective_date DESC,r.version DESC LIMIT 1) dated_rate
          FROM batch_operations t JOIN operations o ON o.id=t.operation_id JOIN production_batches b ON b.id=t.batch_id
          WHERE t.mode='internal' AND b.status NOT IN ('closed','canceled') ORDER BY t.batch_id,t.position,t.id''',(dated,))
        task_data=[dict(t) for t in tasks if t['remaining']>0]
        # Factory HTML contains quantities and readiness, never wage rates or other financials.
        for t in task_data:t['ready']=t.pop('dated_rate') is not None
        batches=rows('''SELECT b.id,c.name customer_name,m.name model_name FROM production_batches b
          JOIN customers c ON c.id=b.customer_id JOIN models m ON m.id=b.model_id
          WHERE b.id IN (SELECT batch_id FROM batch_operations WHERE mode='internal') AND b.status NOT IN ('closed','canceled') ORDER BY b.id DESC''')
        history=rows('''SELECT a.id,a.qty_pairs,a.worked_on,a.canceled_at,a.actor,t.batch_id,o.name operation_name,m.name model_name,
          c.name customer_name,GROUP_CONCAT(w.name,', ') worker_name,SUM(s.amount_cents) amount_cents
          FROM work_acceptances a JOIN batch_operations t ON t.id=a.batch_operation_id JOIN operations o ON o.id=t.operation_id
          JOIN production_batches b ON b.id=t.batch_id JOIN customers c ON c.id=b.customer_id JOIN models m ON m.id=b.model_id
          JOIN work_shares s ON s.acceptance_id=a.id JOIN workers w ON w.id=s.worker_id WHERE a.worked_on=?
          GROUP BY a.id ORDER BY a.id DESC''',(dated,))
        totals=rows('''SELECT w.id,w.name,SUM(s.amount_cents) amount_cents,COUNT(*) positions FROM work_shares s
          JOIN work_acceptances a ON a.id=s.acceptance_id JOIN workers w ON w.id=s.worker_id
          WHERE a.worked_on=? AND a.canceled_at IS NULL GROUP BY w.id ORDER BY w.name''',(dated,)) if not factory else []
        return render_template('business/work_log.html',factory=factory,worked_on=dated,workers=choices('workers'),batches=batches,
          work_tasks=task_data,history=history,totals=totals,selected_batch=request.args.get('batch',type=int),selected_task=request.args.get('task',type=int))
