/* A section owns its forms. Navigation keeps unfinished fields in this tab. */
(function(){
 'use strict';
 const prefix='jail:sections:',params=new URLSearchParams(location.search),ttl=24*60*60*1000;
 function get(key){try{const item=JSON.parse(sessionStorage.getItem(prefix+key));return item&&Date.now()-item.time<ttl?item.value:null}catch{return null}}
 function put(key,value){try{sessionStorage.setItem(prefix+key,JSON.stringify({time:Date.now(),value}))}catch{}}
 function drop(key){try{sessionStorage.removeItem(prefix+key)}catch{}}
 function cleanURL(value){const url=new URL(value,location.href);if(url.origin!==location.origin||!url.pathname.startsWith(document.body.dataset.roleRoot+'/'))return null;return url}
 function pageKey(value=location.href){const url=new URL(value);['resume','created'].forEach(k=>url.searchParams.delete(k));return url.pathname+'?'+url.searchParams.toString()}
 function forms(){return Array.from(document.querySelectorAll('form[method="post"]'))}
 function signature(form,index){return form.id||[new URL(form.getAttribute('action')||location.href,location.href).pathname,form.querySelector('[name=action]')?.value||'',form.querySelector('[name=task_id],[name=plan_id],[name=payment_id],[name=acceptance_id]')?.value||'',index].join(':')}
 function capture(){return forms().map((form,index)=>({key:signature(form,index),fields:Array.from(form.elements).filter(el=>el.name&&!['csrf_token','token','send_token'].includes(el.name)&&el.type!=='file'&&el.tagName!=='BUTTON').map(el=>({name:el.name,value:el.value,type:el.type,checked:el.checked,model:el.closest('[data-model-id]')?.dataset.modelId||''})),chosen:Array.from(form.querySelectorAll('[data-model-id]')).filter(row=>row.querySelector('[data-choose-model]')?.checked).map(row=>row.dataset.modelId),open:Array.from(document.querySelectorAll('details[id][open]')).map(el=>el.id)}))}
 function restore(saved,added,picker){
  if(!saved)return;
  const all=forms();saved.forEach((record,index)=>{
   const form=all.find((f,i)=>signature(f,i)===record.key)||all[index];if(!form)return;
   const grouped={};record.fields.forEach(item=>(grouped[item.name]??=[]).push(item.value));
   if(form.id==='inventory-receipt-form'&&window.jailInventoryRestore)window.jailInventoryRestore(grouped);
   if(form.id==='work-log-form'&&window.jailWorkLogRestore)window.jailWorkLogRestore(grouped);
   if(form.id==='supply-form'&&window.jailSupplyRestore)window.jailSupplyRestore(grouped);
   const crew=form.querySelector('[data-work-people]');if(crew)while(crew.children.length<(grouped.worker_id?.length||1))form.querySelector('[data-add-person]').click();
   const counts={};for(const field of record.fields){
    const i=counts[field.name]||0;counts[field.name]=i+1;
    const scope=field.model?form.querySelector('[data-model-id="'+CSS.escape(field.model)+'"]'):form;if(!scope)continue;
    const matches=Array.from(scope.elements||scope.querySelectorAll('[name]')).filter(el=>el.name===field.name&&el.type!=='file'&&el.tagName!=='BUTTON');const el=matches[field.model?0:i];if(!el)continue;
    if(el.type==='checkbox'||el.type==='radio')el.checked=field.checked;else if(!el.readOnly&&!(form.id==='biz-model-form'&&el.name==='customer_id'))el.value=field.value;
   }
   form.querySelectorAll('[data-model-id]').forEach(row=>{row.querySelector('[data-choose-model]').checked=record.chosen.includes(row.dataset.modelId)});
   record.open?.forEach(id=>{const element=document.getElementById(id);if(element?.tagName==='DETAILS')element.open=true});
  });
  if(added&&picker){
   const [kind,id]=added.split(':');const form=all[picker.form]||all[0];
   if(form&&picker.name==='model_id'&&kind==='model'){
    const row=form.querySelector('[data-model-id="'+CSS.escape(id)+'"]');if(row){row.querySelector('[data-choose-model]').checked=true;form.querySelector('[name=customer_id]').value=row.dataset.owner}
   }else if(form){
    const expected={customer_id:'customer',material_id:'material',worker_id:'worker',extra_material_id:'material'}[picker.name];
    if((expected||(/material/.test(picker.name)?'material':null))===kind){const fields=Array.from(form.elements).filter(el=>el.name===picker.name);if(fields[picker.index||0])fields[picker.index||0].value=id}
   }
  }
  document.querySelectorAll('form').forEach(f=>{f.dispatchEvent(new Event('change',{bubbles:true}));f.dispatchEvent(new Event('input',{bubbles:true}))});document.dispatchEvent(new Event('section:restore'));
 }
 const pending=get('pending');if(pending&&document.querySelector('.flash[role=status]')&&!document.querySelector('.flash-error,#biz-retry-fields')){drop('draft:'+pending);drop('pending')}
 const resume=params.get('resume'),record=resume&&get('hop:'+resume);
 if(record){restore(record.forms,params.get('created'),record.picker);put('draft:'+pageKey(record.url),capture());drop('hop:'+resume);const url=new URL(location.href);url.searchParams.delete('resume');url.searchParams.delete('created');history.replaceState(null,'',url)}
 else if(!document.querySelector('#biz-retry-fields'))restore(get('draft:'+pageKey()));
 const ctx=params.get('ctx'),back=ctx&&get('hop:'+ctx),bar=document.getElementById('section-return');
 if(back&&bar){const url=cleanURL(back.url);if(url){url.searchParams.set('resume',ctx);const created=params.get('created');if(created)url.searchParams.set('created',created);bar.hidden=false;bar.href=url;bar.textContent='← Вернуться: '+back.label}}
 document.addEventListener('click',event=>{
  const discard=event.target.closest('[data-discard-draft]');if(discard){drop('draft:'+pageKey());drop('pending')}
  const link=event.target.closest('a[data-section-hop]');if(!link)return;const target=cleanURL(link.href);if(!target)return;
  const form=link.closest('form')||forms().find(f=>f.elements.namedItem(link.dataset.picker||''))||forms()[0];let picker=null;
  if(link.dataset.picker){let index=Number(link.dataset.pickerIndex||0);const row=link.closest('.shipment-extra-row,.biz-catalog-material-row,.sklad-party-material-row,.biz-receipt-row');const select=row?.querySelector('select[name="'+CSS.escape(link.dataset.picker)+'"]');if(select&&form)index=Array.from(form.elements).filter(e=>e.name===select.name).indexOf(select);picker={form:Math.max(0,forms().indexOf(form)),name:link.dataset.picker,index}};
  const key=crypto.randomUUID?crypto.randomUUID():Date.now().toString(36)+Math.random().toString(36).slice(2);
  const saved=capture();put('draft:'+pageKey(),saved);put('hop:'+key,{url:location.href,label:document.querySelector('h2')?.textContent||'к форме',forms:saved,picker});target.searchParams.set('ctx',key);link.href=target;
 },{signal:window.jailPage.signal});
 document.addEventListener('input',e=>{if(e.target.closest('form[method=post]'))put('draft:'+pageKey(),capture())},{signal:window.jailPage.signal});
 document.addEventListener('change',e=>{if(e.target.closest('form[method=post]'))put('draft:'+pageKey(),capture())},{signal:window.jailPage.signal});
 document.addEventListener('submit',e=>{
  if(e.target.dataset.confirm&&!confirm(e.target.dataset.confirm)){e.preventDefault();return}
  if(e.target.method==='post'&&!e.defaultPrevented){put('draft:'+pageKey(),capture());put('pending',pageKey())}
 },{signal:window.jailPage.signal});
})();
