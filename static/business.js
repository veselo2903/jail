/* Optional interface helpers. Accounting and access checks remain on the server. */
(function(){
 'use strict';
 const retry=document.getElementById('biz-retry-fields');
 if(retry){
   const fields=JSON.parse(retry.textContent),action=fields.action?.[0];
   const forms=Array.from(document.querySelectorAll('form[method="post"]'));
   const identifiers=['plan_id','assignment_id','acceptance_id','delivery_id','payment_id'];
   if(action==='operation_edit'||action==='assignment'||action==='external')identifiers.push('task_id');
   if(action==='model_edit')identifiers.push('model_id');
   if(action==='worker_edit')identifiers.push('worker_id');
   const form=forms.find(f=>f.querySelector('[name="action"]')?.value===action&&identifiers.every(key=>!fields[key]||f.querySelector(`[name="${key}"]`)?.value===fields[key][0])) || forms.find(f=>Array.from(f.querySelectorAll('button[name="action"]')).some(b=>b.value===action)&&identifiers.every(key=>!fields[key]||f.querySelector(`[name="${key}"]`)?.value===fields[key][0]));
   if(form){
     const crew=form.querySelector('[data-work-people]');
     if(crew)while(crew.children.length<(fields.worker_id?.length||1))crew.appendChild(crew.children[0].cloneNode(true));
     for(const [key,values] of Object.entries(fields)){
       const elements=Array.from(form.elements).filter(e=>e.name===key&&e.tagName!=='BUTTON');
       elements.forEach((el,i)=>{if(el.type==='checkbox'||el.type==='radio')el.checked=values.includes(el.value);else el.value=values[i]??values[0]??''});
     }
     form.querySelectorAll('input[type="checkbox"]').forEach(el=>{if(!fields[el.name])el.checked=false});
     for(let el=form.parentElement;el;el=el.parentElement)if(el.tagName==='DETAILS')el.open=true;
     form.dataset.retry='true';
   }
 }
 const tabs=document.querySelector('.biz-tabs');
 function showPane(name,focus=false){
   if(!tabs)return;
   document.querySelectorAll('[data-biz-pane]').forEach(p=>p.hidden=p.dataset.bizPane!==name);
   tabs.querySelectorAll('[data-pane-button]').forEach(b=>{const on=b.dataset.paneButton===name;b.classList.toggle('on',on);b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;if(on&&focus)b.focus()});
 }
 function openTarget(target,scroll=false){
   if(!target)return;
   const pane=target.closest('[data-biz-pane]');if(pane)showPane(pane.dataset.bizPane);
   for(let el=target;el;el=el.parentElement)if(el.tagName==='DETAILS')el.open=true;
   if(target.classList.contains('biz-task')){const conditions=target.querySelector('.biz-subspoiler');if(conditions)conditions.open=true}
   if(scroll)target.scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});
 }
 if(tabs){
   tabs.setAttribute('role','tablist');tabs.style.setProperty('--tab-count',tabs.querySelectorAll('[data-pane-button]').length);
   tabs.querySelectorAll('[data-pane-button]').forEach(b=>{
     const name=b.dataset.paneButton,pane=document.querySelector(`[data-biz-pane="${name}"]`);
     b.id='biz-tab-'+name;b.setAttribute('role','tab');b.setAttribute('aria-controls','biz-pane-'+name);
     pane.id='biz-pane-'+name;pane.setAttribute('role','tabpanel');pane.setAttribute('aria-labelledby',b.id);
     b.addEventListener('click',()=>showPane(name));
     b.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const buttons=Array.from(tabs.querySelectorAll('button')),index=buttons.indexOf(b),next=e.key==='Home'?0:e.key==='End'?buttons.length-1:(index+(e.key==='ArrowRight'?1:-1)+buttons.length)%buttons.length;showPane(buttons[next].dataset.paneButton,true)});
   });
   showPane(document.querySelector('form[data-retry]')?.closest('[data-biz-pane]')?.dataset.bizPane||tabs.dataset.defaultPane);
 }
 const order=document.getElementById('biz-order-form');
 if(order&&!order.hasAttribute('data-director-order')){
   const list=document.getElementById('biz-order-positions'),customer=document.getElementById('biz-customer');
   function refresh(){
     const customerNew=customer.value==='new',newCustomer=order.querySelector('[data-new-customer]');
     newCustomer.hidden=!customerNew;newCustomer.querySelector('input').required=customerNew;
     list.querySelectorAll('[data-order-position]').forEach((card,index)=>{
       card.querySelector('[data-position-number]').textContent=index+1;
       card.querySelector('[data-remove]').hidden=list.children.length<2;
       const select=card.querySelector('[name="model_id"]'),newModel=card.querySelector('[name="new_model"]');
       select.querySelectorAll('option[data-customer]').forEach(opt=>{opt.disabled=customerNew||!customer.value||Boolean(opt.dataset.customer&&opt.dataset.customer!==customer.value);opt.hidden=opt.disabled});
       if(select.selectedOptions[0]?.disabled)select.value='';
       const available=Array.from(select.options).filter(o=>o.dataset.customer===customer.value&&!o.disabled);
       if(customerNew||(customer.value&&!available.length&&!select.value))select.value='new';
       newModel.closest('label').hidden=select.value!=='new';newModel.required=select.value==='new';
       if(select.value!=='new')newModel.value='';
       const claim=card.querySelector('[data-claim-model]'),unowned=select.selectedOptions[0]?.dataset.customer==='';
       claim.hidden=!unowned;claim.querySelector('input').name='claim_model_'+index;claim.querySelector('input').required=unowned;
       if(!unowned)claim.querySelector('input').checked=false;
       const qtyField=card.querySelector('[name="qty"]'),qty=Number(qtyField.value),price=Number(card.querySelector('[name="price"]').value.replace(',','.'));
       const shoes=card.querySelector('[name="quantity_unit"]').value==='shoe',pairs=shoes?qty/2:qty;
       qtyField.closest('label').querySelector('span').textContent=shoes?'Количество, ботинок':'Количество, пар';
       const totalKind=card.querySelector('[name="price_kind"]').value==='total',priceUnit=card.querySelector('[name="price_unit"]');
       card.querySelector('[name="price_kind"]').options[0].textContent=priceUnit.value==='shoe'?'За один ботинок':'За одну пару';
       const total=totalKind?price:price*pairs*(priceUnit.value==='shoe'?2:1);
       card.querySelector('[data-estimate]').textContent=qty&&Number.isFinite(total)&&card.querySelector('[name="price"]').value?`${pairs.toLocaleString('ru')} пар · ${total.toLocaleString('ru',{minimumFractionDigits:2,maximumFractionDigits:2})} ₽ от заказчика`:'';
       priceUnit.closest('label').hidden=totalKind;
     });
   }
   document.getElementById('biz-add-position').addEventListener('click',()=>{list.appendChild(document.getElementById('biz-position-template').content.cloneNode(true));refresh();list.lastElementChild.querySelector('select').focus()});
   order.addEventListener('input',refresh);order.addEventListener('change',refresh);
   order.addEventListener('click',e=>{const remove=e.target.closest('[data-remove]');if(remove){remove.closest('[data-order-position]').remove();refresh()}});
   refresh();
 }
 document.querySelectorAll('[data-work-people]').forEach(list=>{
   const form=list.closest('form');
   function refresh(distribute=false){
     const many=list.children.length>1;
     list.querySelectorAll('[data-remove]').forEach(el=>el.hidden=!many);
     list.querySelectorAll('[data-crew-share]').forEach(el=>el.hidden=!many);
     list.classList.toggle('biz-single-person',!many);form.querySelector('[data-crew-hint]').hidden=!many;
     if(distribute||!many){const inputs=list.querySelectorAll('[name="share"]'),part=Math.floor(10000/inputs.length);inputs.forEach((el,i)=>el.value=((i===inputs.length-1?10000-part*i:part)/100).toString())}
     const newWorker=form.querySelector('[data-new-worker]');
     if(newWorker){newWorker.hidden=!Array.from(list.querySelectorAll('select')).some(s=>s.value==='new');newWorker.querySelector('[name="new_worker_name"]').required=!newWorker.hidden}
   }
   form.querySelector('[data-add-person]').addEventListener('click',()=>{const row=list.children[0].cloneNode(true);row.querySelector('select').value='';row.querySelector('input').value='';row.querySelector('option[value="new"]')?.remove();list.appendChild(row);refresh(true);row.querySelector('select').focus()});
   list.addEventListener('click',e=>{const remove=e.target.closest('[data-remove]');if(remove){remove.closest('.biz-person').remove();refresh(true)}});list.addEventListener('change',()=>refresh());refresh();
   const task=form.querySelector('[name="task_id"]'),qty=form.querySelector('[name="qty"]');
   task.addEventListener('change',()=>{const remaining=task.selectedOptions[0]?.dataset.remaining;if(remaining)qty.max=remaining;else qty.removeAttribute('max')});task.dispatchEvent(new Event('change'));
 });
 document.querySelectorAll('[data-record-task]').forEach(button=>button.addEventListener('click',()=>{const target=document.getElementById('biz-work'),select=target.querySelector('[name="task_id"]');select.value=button.dataset.recordTask;select.dispatchEvent(new Event('change'));openTarget(target,true);target.querySelector('[name="qty"]').focus({preventScroll:true})}));
 document.querySelectorAll('[data-movement-kind]').forEach(select=>{
   const form=select.closest('form'),material=form.querySelector('[name="material_id"]'),owner=form.querySelector('[name="owner_customer_id"]');
   const initial=new URLSearchParams(location.search).get('material');if(initial&&!form.dataset.retry)material.value=initial;
   function refresh(){
     const receipt=select.value==='receipt',cost=form.querySelector('[data-receipt-cost]'),newOption=material.querySelector('option[value="new"]');
     if(newOption){newOption.hidden=!receipt;newOption.disabled=!receipt;if(!receipt&&material.value==='new')material.value=''}
     const isNew=material.value==='new';form.querySelectorAll('[data-new-material]').forEach(el=>{el.hidden=!isNew});
     const name=form.querySelector('[name="new_material"]');if(name)name.required=isNew;
     if(cost){cost.hidden=!receipt||Boolean(owner.value);cost.querySelector('input').required=!cost.hidden}
     const batch=form.querySelector('[data-movement-batch]');if(batch){batch.hidden=receipt;batch.querySelector('select').disabled=receipt;batch.querySelector('select').required=select.value==='allocate'}
     const unit=isNew?form.querySelector('[name="new_unit"]').selectedOptions[0].textContent:material.selectedOptions[0]?.dataset.unit||'';
     form.querySelector('[data-movement-qty]').textContent='Количество'+(unit?', '+unit:'');
     const labels={receipt:'Записать поступление на склад',issue:'Передать в производство',consume:'Записать использованный материал',return:'Вернуть материал на склад',loss:'Записать потери материала',allocate:'Выделить общие материалы в партию'};
     const help={receipt:'Материал появится в остатках склада. Затем его можно передать в производство.',issue:'Материал переместится со склада в выбранную партию или в общие материалы производства.',consume:'Укажите, сколько действительно использовано. Остаток партии уменьшится.',return:'Неиспользованный материал вернётся из производства на склад.',loss:'Укажите испорченный или утраченный материал. Остаток уменьшится.',allocate:'Материал переместится из общего остатка производства в выбранную партию.'};
     form.querySelector('[data-movement-submit]').textContent=labels[select.value];form.querySelector('[data-movement-help]').textContent=help[select.value];
     const data=document.getElementById('biz-stock-data');const available=form.querySelector('[data-material-available]');
     if(data&&!receipt){const stock=JSON.parse(data.textContent),bid=form.querySelector('[name="batch_id"]')?.value||stock.batch_id,source=select.value==='issue'?stock.warehouse:select.value==='allocate'?stock.pool:bid?stock.production:stock.pool;const row=(source||[]).find(r=>String(r.material_id)===material.value&&String(r.owner_customer_id||'')===owner.value&&(!r.batch_id||String(r.batch_id)===String(bid)));available.textContent=material.value?'Остаток '+(select.value==='issue'?'на складе':'в производстве')+': '+((row?.qty_milli||0)/1000).toLocaleString('ru')+' '+unit:''}else available.textContent='';
   }
   form.addEventListener('change',refresh);refresh();
 });
 document.querySelectorAll('[data-model-photo]').forEach(input=>input.addEventListener('change',()=>{input.setCustomValidity(input.files[0]?.size>5*1024*1024?'Фото должно быть не больше 5 МБ. Выберите изображение меньшего размера.':'')}));
 document.querySelectorAll('[data-material-plan]').forEach(form=>{
   const material=form.querySelector('[name="material_id"]');
   function refresh(){
     const isNew=material.value==='new';form.querySelectorAll('[data-plan-new]').forEach(el=>el.hidden=!isNew);if(form.querySelector('[name="new_material"]'))form.querySelector('[name="new_material"]').required=isNew;
     const unit=isNew?form.querySelector('[name="new_unit"]').selectedOptions[0].textContent:material.selectedOptions[0]?.dataset.unit;
     form.querySelector('[name="qty"]').closest('label').querySelector('span').textContent='Всего требуется на партию'+(unit?', '+unit:'');
     const price=form.querySelector('[name="price"]');price.closest('label').hidden=Boolean(form.querySelector('[name="owner_customer_id"]').value);price.closest('label').querySelector('span').textContent='Оценка за '+(unit||'единицу')+', ₽';
   }
   form.addEventListener('change',refresh);refresh();
   document.querySelectorAll('[data-plan-edit]').forEach(button=>button.addEventListener('click',()=>{const fields=JSON.parse(button.dataset.planEdit);for(const [name,value] of Object.entries(fields))form.querySelector(`[name="${name}"]`).value=value;refresh();openTarget(form,true);form.querySelector('[name="qty"]').focus({preventScroll:true})}));
 });
 document.querySelectorAll('[data-pay-balance]').forEach(form=>{
   const amount=form.querySelector('[name="amount"]'),preview=form.querySelector('[data-pay-preview]'),balance=Number(form.dataset.payBalance);
   function refresh(){const value=Number(amount.value.replace(',','.')),remaining=balance-Math.round(value*100);preview.hidden=!amount.value||!Number.isFinite(value)||value<=0;preview.textContent=(remaining<0?'После выплаты: аванс ':'После выплаты останется долг ')+(Math.abs(remaining)/100).toLocaleString('ru',{minimumFractionDigits:2,maximumFractionDigits:2})+' ₽';form.querySelector('[name="kind"]').options[0].textContent='Выплата заработанного'}
   form.addEventListener('input',refresh);refresh();
 });
 document.querySelectorAll('.biz-operation-pick').forEach(row=>{row.addEventListener('input',e=>{if(e.target.type!=='checkbox'&&e.target.value.trim())row.querySelector('input[type="checkbox"]').checked=true})});
 document.addEventListener('click',e=>{const link=e.target.closest('a[href^="#biz-"]');if(link){e.preventDefault();const target=document.getElementById(link.getAttribute('href').slice(1));openTarget(target,true);history.replaceState(null,'',link.getAttribute('href'))}},{signal:window.jailPage.signal});
 if(location.hash.startsWith('#biz-'))openTarget(document.getElementById(location.hash.slice(1)));
 document.querySelectorAll('form[data-retry]').forEach(form=>{openTarget(form);requestAnimationFrame(()=>{const input=form.querySelector('input:not([type="hidden"]),select');input?.focus({preventScroll:true})})});
})();
