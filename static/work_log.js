/* One day, one sheet: the factory enters quantities, the server calculates wages. */
(function(){
 'use strict';
 const form=document.getElementById('work-log-form');if(!form)return;
 const data=JSON.parse(document.getElementById('work-log-data').textContent),rows=document.getElementById('work-log-rows'),template=document.getElementById('work-log-row-template');
 function sync(row,options=false){
  const batch=row.querySelector('[name=batch_id]'),task=row.querySelector('[name=task_id]'),qty=row.querySelector('[name=qty]');
  if(options){
   const chosen=task.value;task.replaceChildren(new Option(batch.value?'Выберите операцию':'Сначала выберите партию',''));
   data.tasks.filter(t=>String(t.batch_id)===batch.value).forEach(t=>{const option=new Option(t.name+(t.ready?'':' · цена ещё не назначена'),t.id);option.disabled=!t.ready;task.add(option)});task.value=chosen;
  }
  const operation=data.tasks.find(t=>String(t.id)===task.value);row.querySelector('[data-work-remaining]').textContent=operation?'Осталось '+operation.remaining+' пар':'';
  if(operation)qty.max=operation.remaining;else qty.removeAttribute('max');
  const fields=[...row.querySelectorAll('select,input')],started=fields.some(field=>['worker_id','qty'].includes(field.name)&&field.value.trim());fields.forEach(field=>field.required=started);
 }
 function add(values){
  rows.appendChild(template.content.cloneNode(true));const row=rows.lastElementChild;
  if(values)for(const name of ['worker_id','batch_id','qty'])row.querySelector('[name='+name+']').value=values[name]||'';
  sync(row,true);if(values?.task_id)row.querySelector('[name=task_id]').value=values.task_id;sync(row);return row;
 }
 window.jailWorkLogRestore=fields=>{
  rows.replaceChildren();const size=Math.max(fields.worker_id?.length||0,fields.batch_id?.length||0,fields.qty?.length||0,1);
  for(let i=0;i<size;i++)add(Object.fromEntries(['worker_id','batch_id','task_id','qty'].map(name=>[name,fields[name]?.[i]||''])));
 };
 add({batch_id:data.batch,task_id:data.task});
 form.querySelector('[data-work-add]').addEventListener('click',()=>{const previous=rows.lastElementChild;add({batch_id:previous.querySelector('[name=batch_id]').value,task_id:previous.querySelector('[name=task_id]').value}).querySelector('[name=worker_id]').focus()});
 rows.addEventListener('change',event=>{const row=event.target.closest('.work-log-row');if(row)sync(row,event.target.name==='batch_id')});
 rows.addEventListener('input',event=>{const row=event.target.closest('.work-log-row');if(row)sync(row)});
 rows.addEventListener('click',event=>{if(!event.target.closest('[data-work-remove]'))return;const row=event.target.closest('.work-log-row');if(rows.children.length>1)row.remove();else{row.querySelectorAll('select,input').forEach(field=>field.value='');sync(row,true)}form.dispatchEvent(new Event('input',{bubbles:true}))});
 form.addEventListener('submit',event=>{const filled=[...rows.children].some(row=>row.querySelector('[name=qty]').value);if(!filled){event.preventDefault();rows.querySelector('[name=worker_id]').focus();rows.querySelector('[name=qty]').setCustomValidity('Укажите, сколько пар сделал сотрудник.');rows.querySelector('[name=qty]').reportValidity()}});
 rows.addEventListener('input',event=>{event.target.setCustomValidity?.('')});
 document.addEventListener('section:restore',()=>rows.querySelectorAll('.work-log-row').forEach(row=>sync(row)),{signal:window.jailPage.signal});
})();
