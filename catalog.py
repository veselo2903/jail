"""Customer and model cards, agreed sale prices and optional shoe photographs."""
import os
from pathlib import Path
import re
import secrets
from flask import abort,g,request,session,url_for,render_template,send_file
if __package__:
    from . import business_core as core,db
else:
    import business_core as core
    import db


def text(form,key,label,limit=180,required=False):
    value=form.get(key,'').strip()
    if required and not value:raise core.RuleError('Введите '+label+'.')
    if len(value)>limit:raise core.RuleError(label.capitalize()+': слишком длинное значение.')
    return value


def once(conn,form,kind):
    token=form.get('token','')
    if not token or len(token)>128:raise core.RuleError('Обновите форму и повторите сохранение.')
    old=conn.execute('SELECT * FROM catalog_submissions WHERE token=?',(token,)).fetchone()
    if old and old['kind']!=kind:raise core.RuleError('Обновите форму и повторите сохранение.')
    return token,old['entity_id'] if old else None


def remember(conn,token,kind,rid):
    conn.execute('INSERT INTO catalog_submissions(token,kind,entity_id) VALUES (?,?,?)',(token,kind,rid))


def photo_folder():return Path(db.DB_PATH).parent/'model-photos'


def photograph():
    file=request.files.get('photo')
    if not file or not file.filename:return None
    data=file.read(5*1024*1024+1)
    if len(data)>5*1024*1024:raise core.RuleError('Фото должно быть не больше 5 МБ.')
    extension=None
    if data.startswith(b'\x89PNG\r\n\x1a\n') and data.endswith(b'IEND\xaeB`\x82'):extension='png'
    elif data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'):extension='jpg'
    elif len(data)>20 and data[:4]==b'RIFF' and data[8:12]==b'WEBP' and int.from_bytes(data[4:8],'little')+8==len(data):extension='webp'
    if not extension:raise core.RuleError('Выберите фото JPEG, PNG или WebP.')
    filename=secrets.token_hex(16)+'.'+extension
    folder=photo_folder();folder.mkdir(mode=0o750,parents=True,exist_ok=True)
    path=folder/filename
    with path.open('xb') as out:out.write(data)
    os.chmod(path,0o640)
    g.catalog_files=getattr(g,'catalog_files',[])+[path]
    return filename


def create_customer(conn,form,actor):
    kind='customer_create';token,old=once(conn,form,kind)
    if old:return old
    name=text(form,'name','название заказчика',required=True)
    if any(r['name'].casefold()==name.casefold() for r in conn.execute('SELECT name FROM customers WHERE archived=0')):raise core.RuleError('Такой заказчик уже добавлен. Откройте его карточку.')
    cid=conn.execute('INSERT INTO customers(name,contact,note) VALUES (?,?,?)',(name,text(form,'contact','контакт',300),text(form,'note','примечание',2000))).lastrowid
    remember(conn,token,kind,cid);core.audit(conn,actor,kind,cid,{'name':name})
    return cid


def save_model(conn,cid,form,actor,mid=None):
    core.require(conn,'customers',cid,True)
    kind='model_edit:'+str(mid) if mid else 'model_create:'+str(cid)
    token,old=once(conn,form,kind)
    if old:return old
    old_model=core.require(conn,'models',mid) if mid else None
    if old_model and old_model['customer_id']!=cid:raise core.RuleError('Эта модель принадлежит другому заказчику.')
    name=text(form,'name','название или артикул модели',required=True)
    duplicate=any(r['name'].casefold()==name.casefold() for r in conn.execute('SELECT name FROM models WHERE customer_id=? AND archived=0 AND id<>?',(cid,mid or 0)))
    if duplicate:raise core.RuleError('У этого заказчика уже есть такая модель. Измените существующую строку.')
    price=core.scaled(form['sale_price'],label='Цена заказчика за пару') if form.get('sale_price','').strip() else None
    note=text(form,'note','описание модели',2000)
    filename=photograph() or (old_model['photo_filename'] if old_model else None)
    if form.get('remove_photo')=='1' and not request.files.get('photo'):filename=None
    if mid:conn.execute('UPDATE models SET name=?,sale_price_cents=?,description=?,photo_filename=? WHERE id=?',(name,price,note,filename,mid))
    else:mid=conn.execute('INSERT INTO models(name,customer_id,sale_price_cents,description,photo_filename) VALUES (?,?,?,?,?)',(name,cid,price,note,filename)).lastrowid
    if (not old_model and price is not None) or (old_model and old_model['sale_price_cents']!=price):
        conn.execute('INSERT INTO model_sale_price_history(model_id,price_cents,at,actor) VALUES (?,?,?,?)',(mid,price,core.now(),actor))
    remember(conn,token,kind,mid);core.audit(conn,actor,kind,mid,{'sale_price_cents':price})
    return mid


def create_worker(conn,form):
    token,old=once(conn,form,'worker_create')
    if old:return old
    name=text(form,'name','имя сотрудника',required=True)
    number=text(form,'number','табельный номер',50) or str(conn.execute('SELECT COALESCE(MAX(CAST(number AS INTEGER)),0)+1 FROM workers').fetchone()[0])
    if conn.execute('SELECT 1 FROM workers WHERE number=? AND archived=0',(number,)).fetchone():raise core.RuleError('Такой табельный номер уже есть. Укажите другой.')
    wid=conn.execute('INSERT INTO workers(number,name) VALUES (?,?)',(number,name)).lastrowid
    remember(conn,token,'worker_create',wid)
    return wid


def references(conn,table,rid,ignore=()):
    """Check every foreign key, including old documents, inside the write transaction."""
    for entry in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        name=entry[0]
        if name in ignore:continue
        for fk in conn.execute('PRAGMA foreign_key_list("'+name+'")').fetchall():
            if fk[2]==table and conn.execute('SELECT 1 FROM "'+name+'" WHERE "'+fk[3]+'"=? LIMIT 1',(rid,)).fetchone():return True
    return False


MODEL_METADATA=('model_sale_price_history','model_operation_templates','model_material_templates','prices')
WORKER_METADATA=('worker_skills','batch_assignments')


def model_used(conn,mid):return references(conn,'models',mid,MODEL_METADATA)


def customer_used(conn,cid):
    if references(conn,'customers',cid,('models','model_material_templates')):return True
    return any(model_used(conn,r[0]) for r in conn.execute('SELECT id FROM models WHERE customer_id=?',(cid,)))


def remove_model(conn,mid):
    if model_used(conn,mid):raise core.RuleError('У модели есть история. Уберите её в архив.')
    for table in MODEL_METADATA:conn.execute('DELETE FROM '+table+' WHERE model_id=?',(mid,))
    conn.execute("DELETE FROM catalog_submissions WHERE (kind LIKE 'model_create:%' AND entity_id=?) OR kind=?",(mid,'model_edit:'+str(mid)))
    conn.execute('DELETE FROM models WHERE id=?',(mid,))


def lifecycle(conn,table,rid,action,actor):
    row=core.require(conn,table,rid)
    if action in ('archive','restore'):
        conn.execute('UPDATE '+table+' SET archived=? WHERE id=?',(action=='archive',rid))
    elif action=='delete':
        if table=='customers':
            if customer_used(conn,rid):raise core.RuleError('У заказчика есть история. Уберите его в архив.')
            for model in conn.execute('SELECT id FROM models WHERE customer_id=?',(rid,)).fetchall():remove_model(conn,model[0])
            conn.execute("DELETE FROM catalog_submissions WHERE kind='customer_create' AND entity_id=?",(rid,))
            conn.execute('DELETE FROM customers WHERE id=?',(rid,))
        elif table=='models':remove_model(conn,rid)
        else:
            metadata=WORKER_METADATA if table=='workers' else ()
            if references(conn,table,rid,metadata):raise core.RuleError('У записи есть история. Уберите её в архив.')
            if table=='workers':
                for name in metadata:conn.execute('DELETE FROM '+name+' WHERE worker_id=?',(rid,))
                conn.execute("DELETE FROM catalog_submissions WHERE kind='worker_create' AND entity_id=?",(rid,))
            if table=='materials':conn.execute("DELETE FROM catalog_submissions WHERE kind='material_create' AND entity_id=?",(rid,))
            conn.execute('DELETE FROM '+table+' WHERE id=?',(rid,))
    else:raise core.RuleError('Неизвестное действие.')
    core.audit(conn,actor,table+'_'+action,rid,dict(row))


def register(bp,access,mutate,rows,choices,prefix):
    @bp.route(prefix+'/customers')
    @access(True)
    def customers():
        archive=request.args.get('archive')=='1'
        entries=rows('SELECT * FROM customers WHERE archived=? ORDER BY name',(int(archive),))
        return render_template('business/customers.html',customers=entries,archive=archive,has_archive=bool(rows('SELECT 1 FROM customers WHERE archived=1 LIMIT 1')))

    @bp.route(prefix+'/customers/new',methods=['GET','POST'])
    @access(True)
    def customer_new():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                cid=create_customer(g.db,request.form,session['role']);g.created_entity=('customer',cid)
                return url_for('business.customer_detail',cid=cid)
            return mutate(change,url_for('business.customer_new'),'Заказчик сохранён.')
        return render_template('business/customer_new.html')

    @bp.route(prefix+'/customers/<int:cid>',methods=['GET','POST'])
    @access(True)
    def customer_detail(cid):
        customer=core.require(g.db,'customers',cid)
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                f=request.form;action=f.get('action')
                if action in ('archive','restore','delete'):
                    lifecycle(g.db,'customers',cid,action,session['role'])
                    if action=='delete':return url_for('business.customers')
                elif action=='customer_edit':
                    name=text(f,'name','название заказчика',required=True)
                    if any(r['name'].casefold()==name.casefold() for r in g.db.execute('SELECT name FROM customers WHERE id<>? AND archived=0',(cid,))):raise core.RuleError('Такой заказчик уже существует.')
                    g.db.execute('UPDATE customers SET name=?,contact=?,note=? WHERE id=?',(name,text(f,'contact','контакт',300),text(f,'note','примечание',2000),cid))
                else:raise core.RuleError('Модели создаются и изменяются в разделе «Модели».')
            return mutate(change,url_for('business.customer_detail',cid=cid),'Заказчик удалён.' if request.form.get('action')=='delete' else 'Заказчик сохранён.')
        return render_template('business/customer.html',customer=customer,model_count=rows('SELECT COUNT(*) n FROM models WHERE customer_id=?',(cid,))[0]['n'],used=customer_used(g.db,cid),order_count=rows('SELECT COUNT(*) n FROM orders WHERE customer_id=?',(cid,))[0]['n'])

    @bp.route(prefix+'/models')
    @access(True)
    def models():
        archive=request.args.get('archive')=='1';cid=request.args.get('customer',type=int)
        where='(m.archived=1 OR c.archived=1)' if archive else 'm.archived=0 AND (c.archived=0 OR c.id IS NULL)'
        if cid:where+=' AND m.customer_id=?'
        entries=rows('SELECT m.*,c.name customer_name,c.archived customer_archived FROM models m LEFT JOIN customers c ON c.id=m.customer_id WHERE '+where+' ORDER BY c.name,m.name',(cid,) if cid else ())
        return render_template('business/models.html',models=entries,customers=choices('customers'),customer_id=cid,archive=archive,has_archive=bool(rows('SELECT 1 FROM models m LEFT JOIN customers c ON c.id=m.customer_id WHERE m.archived=1 OR c.archived=1 LIMIT 1')))

    @bp.route(prefix+'/models/new',methods=['GET','POST'])
    @access(True)
    def model_new():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                cid=core.integer(request.form.get('customer_id'),'Заказчик');mid=save_model(g.db,cid,request.form,session['role']);g.created_entity=('model',mid)
                return url_for('business.model_detail',mid=mid)
            return mutate(change,url_for('business.model_new',customer=request.form.get('customer_id')),'Модель сохранена.')
        return render_template('business/model_new.html',customers=choices('customers'),customer_id=request.args.get('customer',type=int))

    @bp.route(prefix+'/models/<int:mid>',methods=['GET','POST'])
    @access(True)
    def model_detail(mid):
        model=core.require(g.db,'models',mid);customer=core.require(g.db,'customers',model['customer_id']) if model['customer_id'] else None
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                f=request.form;action=f.get('action')
                if action in ('archive','restore','delete'):
                    lifecycle(g.db,'models',mid,action,session['role'])
                    if action=='delete':return url_for('business.models',customer=model['customer_id'])
                elif action=='model_edit':
                    cid=core.integer(f.get('customer_id'),'Заказчик')
                    if cid!=model['customer_id']:
                        if model_used(g.db,mid):raise core.RuleError('Модель уже используется. Для другого заказчика создайте новую модель.')
                        core.require(g.db,'customers',cid,True);g.db.execute('UPDATE models SET customer_id=? WHERE id=?',(cid,mid))
                    save_model(g.db,cid,f,session['role'],mid)
                elif action=='template':
                    bid=core.integer(f.get('batch_id'),'Партия');b=core.require(g.db,'production_batches',bid)
                    if b['model_id']!=mid:raise core.RuleError('Выберите партию этой модели.')
                    core.save_template(g.db,bid);core.audit(g.db,session['role'],'model_template',mid,{'batch_id':bid})
                else:raise core.RuleError('Неизвестное действие.')
            return mutate(change,url_for('business.model_detail',mid=mid),'Модель удалена.' if request.form.get('action')=='delete' else 'Модель сохранена. Уже принятые заказы не изменились.')
        return render_template('business/model.html',model=model,customer=customer,customers=choices('customers'),used=model_used(g.db,mid),history=rows('SELECT * FROM model_sale_price_history WHERE model_id=? ORDER BY id DESC',(mid,)),samples=rows('SELECT id,qty_pairs,status FROM production_batches WHERE model_id=? ORDER BY id DESC',(mid,)),sample_id=request.args.get('sample',type=int))

    @bp.route(prefix+'/models/<int:mid>/photo')
    @access()
    def model_photo(mid):
        model=core.require(g.db,'models',mid);filename=model['photo_filename']
        if not filename or not re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|webp)',filename):abort(404)
        path=photo_folder()/filename
        if not path.is_file():abort(404)
        response=send_file(path,mimetype={'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[filename.rsplit('.',1)[1]],conditional=True)
        response.headers['X-Content-Type-Options']='nosniff';response.headers['Cache-Control']='private, max-age=1800'
        return response

    @bp.route(prefix+'/staff',methods=['GET','POST'])
    @access(True)
    def staff():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                wid=create_worker(g.db,request.form);g.created_entity=('worker',wid)
                return url_for('business.staff_detail',wid=wid)
            return mutate(change,url_for('business.staff'),'Сотрудник сохранён.')
        archive=request.args.get('archive')=='1'
        return render_template('business/staff.html',workers=rows('SELECT * FROM workers WHERE archived=? ORDER BY name',(int(archive),)),archive=archive,has_archive=bool(rows('SELECT 1 FROM workers WHERE archived=1 LIMIT 1')))

    @bp.route(prefix+'/staff/<int:wid>',methods=['GET','POST'])
    @access(True)
    def staff_detail(wid):
        worker=core.require(g.db,'workers',wid)
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                f=request.form;action=f.get('action')
                if action in ('archive','restore','delete'):
                    lifecycle(g.db,'workers',wid,action,session['role'])
                    if action=='delete':return url_for('business.staff')
                elif action=='worker_edit':
                    name=text(f,'name','имя сотрудника',required=True);number=text(f,'number','табельный номер',50,True)
                    if g.db.execute('SELECT 1 FROM workers WHERE number=? AND archived=0 AND id<>?',(number,wid)).fetchone():raise core.RuleError('Такой табельный номер уже есть.')
                    g.db.execute('UPDATE workers SET name=?,number=? WHERE id=?',(name,number,wid))
                elif action=='skills':
                    selected={core.integer(value,'Операция') for value in f.getlist('operation_id')}
                    if selected-set(r[0] for r in g.db.execute('SELECT id FROM operations')):raise core.RuleError('Операция не найдена.')
                    g.db.execute('DELETE FROM worker_skills WHERE worker_id=?',(wid,))
                    g.db.executemany('INSERT INTO worker_skills(worker_id,operation_id) VALUES (?,?)',[(wid,op) for op in selected])
                else:raise core.RuleError('Неизвестное действие.')
            return mutate(change,url_for('business.staff_detail',wid=wid),'Сотрудник удалён.' if request.form.get('action')=='delete' else 'Сотрудник сохранён.')
        return render_template('business/staff_detail.html',worker=worker,used=references(g.db,'workers',wid,WORKER_METADATA),skills={r[0] for r in g.db.execute('SELECT operation_id FROM worker_skills WHERE worker_id=?',(wid,))},operations=rows('SELECT * FROM operations ORDER BY ord,id'))
