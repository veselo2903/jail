/* Shared progressive enhancements for business forms. Server remains authoritative. */
(function(){
 'use strict';
 const order=document.getElementById('biz-order-form');
 if(order){
   const list=document.getElementById('biz-order-positions'),customer=document.getElementById('biz-customer');
   function refresh(){
     list.querySelectorAll('[data-order-position]').forEach((card,index)=>{
       card.querySelector('[data-position-number]').textContent=index+1;
       card.querySelector('[data-remove]').hidden=list.children.length<2;
       const select=card.querySelector('[name="model_id"]');
       select.querySelectorAll('option[data-customer]').forEach(opt=>{
         opt.disabled=opt.dataset.customer!==customer.value;
         opt.hidden=opt.disabled;
       });
       if(select.selectedOptions[0]?.disabled)select.value='';
       const qty=Number(card.querySelector('[name="qty"]').value),price=Number(card.querySelector('[name="price"]').value.replace(',','.'));
       const pairs=card.querySelector('[name="quantity_unit"]').value==='shoe'?qty/2:qty;
       const total=card.querySelector('[name="price_kind"]').value==='total'?price:price*pairs*(card.querySelector('[name="price_unit"]').value==='shoe'?2:1);
       const estimate=card.querySelector('[data-estimate]');
       estimate.textContent=qty&&Number.isFinite(total)?`${pairs.toLocaleString('ru')} пар · ${total.toLocaleString('ru',{minimumFractionDigits:2,maximumFractionDigits:2})} ₽ за позицию`:'';
       card.querySelector('[name="price_unit"]').closest('label').hidden=card.querySelector('[name="price_kind"]').value==='total';
     });
   }
   document.getElementById('biz-add-position').addEventListener('click',()=>{list.appendChild(document.getElementById('biz-position-template').content.cloneNode(true));refresh()});
   order.addEventListener('input',refresh);order.addEventListener('change',refresh);
   order.addEventListener('click',e=>{const remove=e.target.closest('[data-remove]');if(remove){remove.closest('[data-order-position]').remove();refresh()}});
   refresh();
 }
 document.querySelectorAll('[data-work-people]').forEach(list=>{
   const form=list.closest('form');
   function refresh(distribute=false){
     list.querySelectorAll('[data-remove]').forEach(el=>el.hidden=list.children.length<2);
     if(distribute){const inputs=list.querySelectorAll('[name="share"]'),part=Math.floor(10000/inputs.length);inputs.forEach((el,i)=>el.value=((i===inputs.length-1?10000-part*i:part)/100).toString())}
   }
   form.querySelector('[data-add-person]').addEventListener('click',()=>{
     const row=list.children[0].cloneNode(true);row.querySelector('select').value='';row.querySelector('input').value='';list.appendChild(row);refresh(true);
   });
   list.addEventListener('click',e=>{const remove=e.target.closest('[data-remove]');if(remove){remove.closest('.biz-person').remove();refresh(true)}});refresh();
 });
 document.querySelectorAll('[data-movement-kind]').forEach(select=>{
   function refresh(){const cost=select.closest('form').querySelector('[data-receipt-cost]');if(cost)cost.hidden=select.value!=='receipt'}
   select.addEventListener('change',refresh);refresh();
 });
 document.querySelectorAll('.biz-operation-pick').forEach(row=>{row.addEventListener('input',e=>{if(e.target.type!=='checkbox'&&e.target.value.trim())row.querySelector('input[type="checkbox"]').checked=true})});
 document.querySelectorAll('a[href^="#biz-"]').forEach(link=>link.addEventListener('click',()=>{const details=document.querySelector(link.getAttribute('href'));if(details?.tagName==='DETAILS')details.open=true}));
})();
