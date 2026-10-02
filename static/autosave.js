window.jailAutoSave=function(form,url){
 const status=document.createElement('div');status.className='save-status';status.setAttribute('role','status');form.append(status);
 let timer=null,active=null,pending=false,conflict=false;
 function message(text,error=false){status.textContent=text;status.classList.toggle('failed',error);}
 async function send(){
  if(conflict)return false;
  if(active){pending=true;await active;if(conflict)return false;return send();}
  pending=false;message('Сохраняется…');const data=new FormData(form);
  active=(async()=>{try{
   const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);
   let response;try{response=await fetch(url,{method:'POST',body:data,signal:controller.signal});}finally{clearTimeout(timeout);}
   const result=await response.json();if(!response.ok||!result.ok){if(response.status===409)conflict=true;throw Error(result.error||'Не удалось сохранить');}
   Object.entries(result.revisions||{}).forEach(([id,v])=>{const field=form.elements.namedItem('rev_'+id);if(field)field.value=v;});
   if(!pending){message('Сохранено');window.dispatchEvent(new Event('jail-saved'));}return true;
  }catch(e){message(e.name==='AbortError'?'Нет ответа сервера. Проверьте связь.':(e.message==='Failed to fetch'?'Нет связи. Значения ещё не сохранены.':e.message),true);return false;}})();
  const ok=await active;active=null;if(pending&&ok)return send();return ok;
 }
 return {schedule(){clearTimeout(timer);pending=true;message('Есть несохранённые значения');timer=setTimeout(send,600);},async flush(){clearTimeout(timer);return send();}};
};
