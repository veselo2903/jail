/* Choose a customer's models once; unselected rows never enter the order. */
(function(){
 const form=document.querySelector('[data-director-order]');if(!form)return;
 const customer=form.querySelector('#biz-customer'),fresh=form.querySelector('[data-new-order-models]');
 if(customer.options.length===2&&!customer.value)customer.value='new';
 function add(){fresh.appendChild(document.getElementById('biz-director-model-template').content.cloneNode(true));refresh();fresh.lastElementChild.querySelector('[name=new_model]').focus()}
 function refresh(){
   const cid=customer.value,isNew=cid==='new';let total=0,pairsTotal=0,count=0,complete=true;
   const customerName=form.querySelector('[data-new-customer]');customerName.hidden=!isNew;customerName.querySelector('input').required=isNew;
   form.querySelector('[data-choose-customer]').hidden=Boolean(cid);
   const existing=Array.from(form.querySelectorAll('[data-owner]')).filter(row=>row.dataset.owner===cid);
   form.querySelector('[data-no-customer-models]').hidden=!cid||existing.length>0||fresh.children.length>0;
   form.querySelector('[data-add-order-model]').hidden=!cid;
   form.querySelectorAll('[data-director-model]').forEach(row=>{
     const available=row.hasAttribute('data-new-order-model')?Boolean(cid):row.dataset.owner===cid;
     const chosen=available&&(row.hasAttribute('data-new-order-model')||row.querySelector('[data-choose-model]').checked);
     row.hidden=!available;row.classList.toggle('is-selected',chosen);row.querySelector('[data-model-fields]').hidden=!chosen;
     row.querySelectorAll('[name]').forEach(input=>input.disabled=!chosen);
     if(!chosen)return;
     count++;const qty=row.querySelector('[name=qty]'),price=row.querySelector('[name=price]'),shoes=row.querySelector('[name=quantity_unit]').value==='shoe';
     const pairs=Number(qty.value)/(shoes?2:1),value=Number(price.value.replace(',','.'));
     qty.closest('label').querySelector('span').textContent=shoes?'Сколько ботинок заказали':'Сколько пар заказали';
     const totalKind=row.querySelector('[name=price_kind]').value==='total',shoePrice=row.querySelector('[name=price_unit]').value==='shoe';
     row.querySelector('[name=price_kind]').options[0].textContent=shoePrice?'За один ботинок':'За одну пару';
     row.querySelector('[name=price_unit]').closest('label').hidden=totalKind;
     const amount=totalKind?value:pairs*value*(shoePrice?2:1),valid=pairs>0&&Number.isFinite(amount)&&price.value.trim();
     row.querySelector('[data-estimate]').textContent=valid?pairs.toLocaleString('ru')+' пар · '+amount.toLocaleString('ru',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽ за эту модель':'';
     if(valid){total+=amount;pairsTotal+=pairs}else complete=false;
   });
   const totalBox=form.querySelector('[data-order-total]');totalBox.textContent=count&&complete?pairsTotal.toLocaleString('ru')+' пар · '+total.toLocaleString('ru',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽ за заказ':count?'Укажите количество и цену выбранных моделей.':'Отметьте модели и укажите количество.';
 }
 form.querySelector('[data-add-order-model]').addEventListener('click',add);
 form.addEventListener('input',refresh);form.addEventListener('change',refresh);
 form.addEventListener('click',e=>{const remove=e.target.closest('[data-remove-new-model]');if(remove){remove.closest('[data-new-order-model]').remove();refresh()}});
 form.addEventListener('submit',e=>{if(!Array.from(form.querySelectorAll('[data-director-model]')).some(r=>!r.hidden&&(r.hasAttribute('data-new-order-model')||r.querySelector('[data-choose-model]').checked))){e.preventDefault();const hint=form.querySelector('[data-order-total]');hint.textContent='Выберите хотя бы одну модель обуви.';hint.scrollIntoView({block:'center',behavior:'smooth'})}});
 refresh();
})();
