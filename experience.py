"""Presentation helpers and atomic shortcuts for first use; no accounting rules."""
from flask import current_app, g, request, session
from werkzeug.datastructures import MultiDict
if __package__:
    from . import business_core as core
else:
    import business_core as core


def remember_form():
    fields={key:request.form.getlist(key) for key in request.form if key!='csrf_token'}
    retry={'path':request.path,'fields':fields}
    state=dict(session);state['retry_form']=retry
    serializer=current_app.session_interface.get_signing_serializer(current_app)
    # Check actual compressed signed cookie size, including existing session data.
    if len(serializer.dumps(state))<=3500:
        session['retry_form']=retry
        return True
    g.failed_fields=fields
    return False


def retry_form():
    if request.headers.get('X-Jail-Prefetch')=='1':return None
    if getattr(g,'failed_fields',None): return g.failed_fields
    draft=session.get('retry_form')
    if request.method=='GET' and draft and draft['path']==request.path:
        return session.pop('retry_form')['fields']
    return None


def plan(conn,bid,form):
    if form.get('material_id')=='new':raise core.RuleError('Добавьте материал в разделе «Материалы».')
    return core.plan_material(conn,bid,form)


def receipt(conn,form,actor,default_batch=None):
    fields=form.to_dict()
    if default_batch:fields['batch_id']=str(default_batch)
    if fields.get('material_id')=='new':raise core.RuleError('Добавьте материал в разделе «Материалы».')
    return core.inventory_move(conn,fields,actor)


def work(conn,bid,form,actor):
    if 'new' in form.getlist('worker_id'):raise core.RuleError('Добавьте сотрудника в разделе «Сотрудники».')
    return core.accept_work(conn,bid,form,actor)


def batch_guidance(batch,tasks,plans,stocks,outputs,deliveries,manager,worker_count,material_started=False):
    good=sum(r['qty_pairs'] for r in outputs if r['kind']=='good')
    delivered=sum(r['qty_pairs'] for r in deliveries)
    accepted=sum(r['accepted_qty'] if 'accepted_qty' in r.keys() else r['qty_pairs'] if r['accepted_on'] else 0 for r in deliveries)
    remaining=[t for t in tasks if t['mode']=='internal' and t['done']<t['qty_pairs']]
    eligible=[t for t in remaining if t['rate_cents'] is not None]
    rates_missing=[t for t in remaining if t['rate_cents'] is None]
    unfinished=[t for t in tasks if t['mode']!='skip' and t['done']<t['qty_pairs']]
    closed=batch['status'] in ('closed','canceled')
    pane='tasks';target=None;title='';text='';label=''
    if closed:
        pane='finish';title='Партия '+('закрыта' if batch['status']=='closed' else 'отменена')
        text='Здесь сохранены результаты и история этой партии.'
    elif not manager and not tasks:
        title='Склад ещё готовит задания для партии';text='Посмотрите сведения о партии. Недостающие материалы можно запросить у склада.'
    elif manager and not tasks:
        title='Укажите, что нужно сделать с этой моделью';text='Выберите операции и цену работы за одну пару.';target='biz-add-operation';label='Выбрать операции'
    elif manager and rates_missing:
        title='Для работы осталось задать расценки';text='Укажите цену за пару у операций без расценки.';target='biz-task-'+str(rates_missing[0]['id']);label='Указать цену'
    elif manager and not plans and not batch['no_materials']:
        pane='materials';title='Укажите материалы для этой партии';text='Добавьте, что потребуется на всё количество пар, или отметьте, что материалы не нужны.';target='biz-plan-material';label='Добавить материалы'
    elif manager and plans and not material_started:
        pane='materials';title='Подготовьте материалы для производства';text='План заполнен. Передайте материалы со склада в эту партию; работа будет записываться в разделе «Задания».';target='biz-material-move';label='Передать материалы'
    elif eligible and (worker_count or manager):
        title='Можно записывать выполненную работу';text='Выберите операцию, сотрудника и сколько качественных пар он сделал.';target='biz-work';label='Записать работу'
    elif eligible and not worker_count and not manager:
        title='Попросите директора добавить сотрудника';text='После добавления сотрудника можно будет записать его работу по операциям.'
    elif remaining and not manager:
        title='Склад должен задать расценки';text='Пока можно посмотреть задания и запросить недостающие материалы.'
    elif unfinished:
        title='Завершите оставшиеся задания';text='Откройте операцию: там видно, кто её выполняет и сколько пар осталось.'
    elif good<batch['qty_pairs']:
        pane='finish';title='Отметьте готовую обувь';text='Работа по операциям завершена. Укажите, сколько готовых качественных пар получилось.';target='biz-output';label='Записать готовые пары'
    elif manager and delivered<good:
        pane='finish';title='Готовые пары можно отгрузить заказчику';text='Запишите количество, которое действительно отправляете.';target='biz-delivery';label='Записать отгрузку'
    elif manager and accepted<delivered:
        pane='finish';title='Отметьте, что заказчик принял отгрузку';text='Откройте отправленную отгрузку и укажите дату приёмки.';target='biz-delivery';label='Отметить приёмку'
    elif manager and stocks:
        pane='materials';title='Осталось разобраться с материалами';text='Запишите использованное количество, а остаток верните на склад.';target='biz-material-move';label='Расход или возврат'
    elif manager and any(p['estimated_unit_cents'] is None and p['owner_customer_id'] is None for p in plans):
        pane='materials';title='Уточните стоимость материалов предприятия';text='Перед закрытием укажите оценку материалов в плане партии.';target='biz-plan-material';label='Уточнить план материалов'
    elif manager and accepted>=batch['qty_pairs']:
        pane='finish';title='Партия готова к завершению';text='Проверьте итоги и закройте партию.';target='biz-close';label='К завершению партии'
    else:
        pane='finish';title='Готовые пары ждут отгрузки';text='Склад отметит отправку и приёмку заказчиком.'
    return dict(pane=pane,target=target,title=title,text=text,label=label,good=good,delivered=delivered,
                accepted=accepted,eligible=eligible,rates_missing=rates_missing,unfinished=unfinished)
