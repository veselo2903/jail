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


def register(bp,access,mutate,rows,choices,prefix):
    @bp.route(prefix+'/customers')
    @access(True)
    def customers():
        entries=rows('''SELECT c.*,(SELECT COUNT(*) FROM models WHERE customer_id=c.id AND archived=0) model_count,
          (SELECT COUNT(*) FROM orders WHERE customer_id=c.id AND status NOT IN ('completed','canceled')) order_count
          FROM customers c WHERE c.archived=0 ORDER BY c.name''')
        return render_template('business/customers.html',customers=entries)

    @bp.route(prefix+'/customers/new',methods=['GET','POST'])
    @access(True)
    def customer_new():
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                cid=create_customer(g.db,request.form,session['role'])
                return url_for('business.customer_detail',cid=cid)
            return mutate(change,url_for('business.customer_new'),'Заказчик добавлен. Теперь добавьте его модели.')
        return render_template('business/customer_new.html')

    @bp.route(prefix+'/customers/<int:cid>',methods=['GET','POST'])
    @access(True)
    def customer_detail(cid):
        customer=core.require(g.db,'customers',cid)
        if request.method=='POST' and not getattr(g,'render_failed_form',False):
            def change():
                core.require(g.db,'customers',cid,True)
                f=request.form;action=f.get('action')
                if action=='model_add':save_model(g.db,cid,f,session['role'])
                elif action=='model_edit':save_model(g.db,cid,f,session['role'],core.integer(f.get('model_id')))
                elif action=='customer_edit':
                    name=text(f,'name','название заказчика',required=True)
                    g.db.execute('UPDATE customers SET name=?,contact=?,note=? WHERE id=?',(name,text(f,'contact','контакт',300),text(f,'note','примечание',2000),cid))
                else:raise core.RuleError('Неизвестное действие.')
            return mutate(change,url_for('business.customer_detail',cid=cid),'Модель сохранена. Цена будет подставляться в новые заказы.' if request.form.get('action') in ('model_add','model_edit') else 'Заказчик сохранён.')
        models=rows('SELECT * FROM models WHERE customer_id=? AND archived=0 ORDER BY name',(cid,))
        orders=rows('SELECT o.*,COALESCE((SELECT SUM(total_cents) FROM order_items WHERE order_id=o.id),0) total FROM orders o WHERE customer_id=? ORDER BY o.id DESC',(cid,))
        history=rows('SELECT h.*,m.name FROM model_sale_price_history h JOIN models m ON m.id=h.model_id WHERE m.customer_id=? ORDER BY h.id DESC LIMIT 20',(cid,))
        return render_template('business/customer.html',customer=customer,models=models,orders=orders,price_history=history)

    @bp.route(prefix+'/models/<int:mid>/photo')
    @access()
    def model_photo(mid):
        model=core.require(g.db,'models',mid)
        filename=model['photo_filename']
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
                form=request.form
                if form.get('action')=='worker_edit':
                    wid=core.integer(form.get('worker_id'));core.require(g.db,'workers',wid,True)
                    name=text(form,'name','имя сотрудника',required=True);number=text(form,'number','табельный номер',50,True)
                    if g.db.execute('SELECT 1 FROM workers WHERE number=? AND archived=0 AND id<>?',(number,wid)).fetchone():raise core.RuleError('Такой табельный номер уже есть. Укажите другой.')
                    g.db.execute('UPDATE workers SET name=?,number=? WHERE id=?',(name,number,wid))
                else:create_worker(g.db,form)
            return mutate(change,url_for('business.staff'),'Сотрудник сохранён.' if request.form.get('action')=='worker_edit' else 'Сотрудник добавлен. Начисления появятся после записи выполненной работы.')
        return render_template('business/staff.html',workers=choices('workers'))
