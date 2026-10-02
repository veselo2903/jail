/* Prefetch and instant navigation for read-only pages. */
(function(){
 if(window.jailNavigation)return;window.jailNavigation=true;
 const cache=new Map();let generation=0,dirty=false,navigating=0;
 function eligible(url){return url.origin===location.origin&&new RegExp('^'+document.body.dataset.root.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'(?:wh|stock|docs|refs|overview|debt|stats|discrepancies|acceptance|requests\/\d+)(?:[/?]|$)').test(url.pathname)&&!url.pathname.endsWith('/new');}
 function clear(){generation++;cache.clear();}
 document.addEventListener('input',()=>{dirty=true;clear();},true);
 document.addEventListener('submit',clear,true);
 window.addEventListener('jail-saved',()=>{dirty=false;clear();});
 async function preload(url){
  const key=url.href.split('#')[0];if(cache.has(key))return cache.get(key);
  const stamp=generation;
  const promise=fetch(key,{credentials:'same-origin',headers:{'X-Jail-Prefetch':'1'},cache:'no-store'}).then(async r=>{
   if(!r.ok||r.redirected)throw Error('navigation');const html=await r.text();if(stamp!==generation)throw Error('changed');return html;
  }).catch(e=>{cache.delete(key);throw e;});cache.set(key,promise);
  if(cache.size>12)cache.delete(cache.keys().next().value);return promise;
 }
 document.addEventListener('pointerover',e=>{const a=e.target.closest('a[href]');if(!a)return;const u=new URL(a.href);if(eligible(u)&&!dirty)preload(u).catch(()=>{});},{passive:true});
 function render(html,url){
  const doc=new DOMParser().parseFromString(html,'text/html');if(!doc.querySelector('.app'))throw Error('page');
  const old=document.querySelector('.app');old.replaceWith(doc.querySelector('.app'));
  const side=document.querySelector('.side'),next=doc.querySelector('.side');if(side&&next)side.replaceWith(next);
  document.title=doc.title;document.body.dataset.root=doc.body.dataset.root||document.body.dataset.root;
  document.querySelectorAll('.app script:not([src])').forEach(s=>{const n=document.createElement('script');n.textContent=s.textContent;s.replaceWith(n);});
  dirty=false;window.scrollTo(0,0);window.dispatchEvent(new Event('jail-page'));
 }
 document.addEventListener('click',async e=>{
  const a=e.target.closest('a[href]');if(!a||e.defaultPrevented||e.button||e.ctrlKey||e.metaKey||e.shiftKey||e.altKey||a.target||a.download||dirty)return;
  const u=new URL(a.href);if(!eligible(u)||u.href.split('#')[0]===location.href.split('#')[0]||u.hash)return;
  e.preventDefault();const visit=++navigating;try{const html=await preload(u);if(visit!==navigating)return;history.pushState({},'',u.href);render(html,u);}catch(_){location.href=u.href;}
 });
 window.addEventListener('popstate',()=>{location.reload();});
 function emptyActions(){document.querySelectorAll('.empty-action:not([data-ready])').forEach(box=>{
  box.dataset.ready='1';const form=box.querySelector('form');if(!form)return;
  const details=document.createElement('details'),summary=document.createElement('summary');summary.className='btn';
  const type=form.action.split('/').slice(-2)[0];summary.textContent={customers:'Добавить заказчика',models:'Добавить модель',materials:'Добавить материал',workers:'Добавить работника'}[type]||'Добавить';
  details.className='empty-create';details.append(summary);details.append(form);box.append(details);
  details.addEventListener('toggle',()=>{if(details.open)form.querySelector('input:not([type=hidden])')?.focus();});
 });}
 document.addEventListener('DOMContentLoaded',emptyActions);window.addEventListener('jail-page',emptyActions);
})();
