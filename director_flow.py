"""Director-facing stages derived from the existing manufacturing ledger."""
if __package__:
    from . import business_core as core
    from .sections import accepted_pairs
else:
    import business_core as core
    from sections import accepted_pairs

STAGES=('Подготовка','Производство','Готовая обувь','Отгрузка','Завершён')


def batch_state(conn,b):
    bid=b['id'];tasks=core.task_rows(conn,bid)
    plans=conn.execute('SELECT * FROM batch_material_plan WHERE batch_id=?',(bid,)).fetchall()
    good=conn.execute("SELECT COALESCE(SUM(qty_pairs),0) FROM production_outputs WHERE batch_id=? AND kind='good'",(bid,)).fetchone()[0]
    delivery=(conn.execute('SELECT COALESCE(SUM(qty_pairs),0) FROM deliveries WHERE batch_id=?',(bid,)).fetchone()[0],accepted_pairs(conn,bid))
    if __package__:
        from .sections import supply_unresolved
    else:
        from sections import supply_unresolved
    outstanding=[t for t in tasks if t['mode']!='skip' and t['done']<t['qty_pairs']]
    missing_rates=[t for t in tasks if t['mode']=='internal' and t['rate_cents'] is None]
    missing_materials=not plans and not b['no_materials']
    missing_estimates=[p for p in plans if not p['owner_customer_id'] and p['estimated_unit_cents'] is None]
    needs_worker=any(t['mode']=='internal' and t['done']<t['qty_pairs'] for t in tasks) and not conn.execute('SELECT 1 FROM workers WHERE archived=0 LIMIT 1').fetchone()
    prepared=bool(tasks) and not missing_rates
    started=b['status'] in ('working','completed')
    stage=0;label='Подготовить модель';target='biz-add-operation';title='Выберите операции для этой модели';text='Отметьте нужные операции и сколько мы платим сотруднику за одну пару.'
    if b['status']=='canceled':
        return dict(stage=None,label='Отменена',title='Модель отменена',text='История сохранена.',target=None,prepared=prepared,good=good,delivered=delivery[0],accepted=delivery[1],done=len(tasks)-len(outstanding),tasks=len(tasks))
    if b['status']=='closed':
        stage=4;label='Завершена';title='Работа по модели завершена';text='Обувь принята заказчиком. Здесь сохранены результаты.';target=None
    elif not tasks:pass
    elif missing_rates:
        target='biz-task-'+str(missing_rates[0]['id']);title='Укажите оплату сотрудникам';text='У операции «'+missing_rates[0]['name']+'» ещё нет цены за пару.';label='Указать оплату'
    elif good>=b['qty_pairs'] and delivery[0]<b['qty_pairs']:
        stage=2;target='biz-delivery';title='Обувь готова — запишите отправку заказчику';text='Укажите количество, которое действительно отгружаете.';label='Записать отгрузку'
    elif delivery[0]>=b['qty_pairs']:
        stage=3;target='biz-delivery';title='Отметьте приёмку заказчиком';text='Укажите дату, когда заказчик принял отправленную обувь.';label='Отметить приёмку'
        if delivery[1]>=b['qty_pairs']:
            stock=conn.execute('SELECT 1 FROM production_stock WHERE batch_id=? AND qty_milli>0',(bid,)).fetchone()
            if stock:
                target='biz-material-move';title='Заказчик принял обувь — закройте остатки материалов';text='Использованное количество нужно списать, оставшееся — вернуть на склад.';label='Разобраться с остатками'
            elif supply_unresolved(conn,bid):
                target='biz-supplies';title='Уточните получение материалов';text='Откройте поставки партии и запишите, что произошло с недостачей или излишком.';label='К поставкам партии'
            elif missing_materials or missing_estimates:
                target='biz-plan-material';title='Уточните план материалов';text='Укажите материалы и их оценку либо отметьте, что материалы не требуются.';label='К материалам'
            elif outstanding:
                target='biz-operations';title='Проверьте оставшиеся операции';text='Перед завершением должны быть записаны все выполненные задания.';label='Посмотреть операции'
            else:
                target='biz-close';title='Можно завершить работу по модели';text='Все пары приняты, задания выполнены, остатки материалов закрыты.';label='Завершить модель'
    elif not outstanding and started:
        stage=2;target='biz-output';title='Операции выполнены — отметьте готовую обувь';text='Запишите фактическое количество готовых качественных пар.';label='Записать готовые пары'
    elif started:
        stage=1;target='biz-operations';title='Модель в производстве';text='Склад передаёт материалы, производство записывает выполненную работу. Здесь видно, сколько уже сделано.';label='Посмотреть выполнение'
    else:
        target='biz-director-start';title='Модель подготовлена к производству';text='Операции и оплата указаны. Передайте задание в работу. Материалы и исполнителей можно уточнить здесь.';label='Передать в производство'
    return dict(stage=stage,label=label,target=target,title=title,text=text,prepared=prepared,good=good,delivered=delivery[0],accepted=delivery[1],done=len(tasks)-len(outstanding),tasks=len(tasks))


def order_state(conn,order,batches):
    entries=[dict(b,flow=batch_state(conn,b)) for b in map(dict,batches)]
    active=[b for b in entries if b['flow']['stage'] is not None]
    if order['status']=='canceled' or not active:
        return dict(stage=None,title='Заказ отменён',label='Отменён',next=None,batches=entries,good=sum(b['flow']['good'] for b in entries),qty=sum(b['qty_pairs'] for b in entries))
    stage=min(b['flow']['stage'] for b in active)
    next_batch=next((b for b in active if b['flow']['stage']==stage),None) if stage<4 else None
    return dict(stage=stage,title=STAGES[stage],label=STAGES[stage],next=next_batch,batches=entries,good=sum(b['flow']['good'] for b in entries),qty=sum(b['qty_pairs'] for b in entries))
