/* Same-role GET navigation. POSTs and logout always use the server normally. */
(function(){
 'use strict';
 if(window.jailNavigation)return;
 const root=document.body.dataset.roleRoot,cache=new Map(),assets=new Map(),ttl=30000,limit=24;
 let sequence=0,busy=false;
 const stats={hits:0,navigations:0,lastURL:'',lastCached:false};
 window.jailNavigation={stats,invalidate:()=>cache.clear(),ready:value=>{const entry=cache.get(key(new URL(value,location.href)));return !!entry?.ready&&Date.now()-entry.time<ttl}};
 function asset(src){
  if(!assets.has(src))assets.set(src,fetch(src,{credentials:'same-origin',cache:'force-cache'}).then(response=>{if(!response.ok)throw Error('Script unavailable');return response.text()}).catch(error=>{assets.delete(src);throw error}));
  return assets.get(src);
 }
 function eligible(value){
  const url=new URL(value,location.href);
  if(url.origin!==location.origin||!root||!url.pathname.startsWith(root+'/')||/\/(?:photos?|export|download)(?:\/|$)/.test(url.pathname))return null;
  return url;
 }
 function key(url){const clean=new URL(url);['ctx','resume','created'].forEach(name=>clean.searchParams.delete(name));clean.hash='';return clean.href}
 function load(url){
  const id=key(url),old=cache.get(id);
  if(old&&Date.now()-old.time<ttl)return old.promise;
  const entry={time:Date.now()};
  entry.promise=fetch(id,{credentials:'same-origin',headers:{'X-Jail-Prefetch':'1'},cache:'no-store'}).then(async response=>{
   if(!response.ok||!response.headers.get('content-type')?.includes('text/html'))throw Error('Normal navigation required');
   const html=await response.text(),page=new DOMParser().parseFromString(html,'text/html');
   if(page.body.dataset.roleRoot!==root||!page.querySelector('#biz-page-start')||page.querySelector('.flash,#biz-retry-fields'))throw Error('Fresh navigation required');
   const destination=new URL(response.url);
   if(!eligible(destination))throw Error('Role changed');
   const sources=new Map();
   await Promise.all([...page.body.querySelectorAll('script[src]')].map(async script=>sources.set(script.src,await asset(script.src))));
   entry.ready=true;return {html,url:destination.href,sources};
  }).catch(error=>{if(cache.get(id)===entry)cache.delete(id);throw error});
  cache.set(id,entry);
  while(cache.size>limit)cache.delete(cache.keys().next().value);
  return entry.promise;
 }
 function warm(link){
  if(!link||link.target||link.hasAttribute('download')||link.matches('.side-user-logout,[data-no-instant]'))return;
  const url=eligible(link.href);
  if(!url||url.pathname===location.pathname&&url.search===location.search)return;
  load(url).catch(()=>{});
 }
 function warmPage(){
  // Visible primary actions and menu links are fetched before the user clicks.
  const links=[...document.querySelectorAll('.biz-empty-action,.biz-header a.btn,.biz-actions a,.side a.nav,[data-section-hop],#section-return:not([hidden])'),...Array.from(document.querySelectorAll('.biz-customer-picker-row')).slice(0,12)];
  const run=()=>{const current=eligible(location.href);if(current)load(current).catch(()=>{});links.forEach(warm)};
  if('requestIdleCallback' in window)requestIdleCallback(run,{timeout:250});else setTimeout(run,0);
 }
 async function mount(result,target,mode){
  const page=new DOMParser().parseFromString(result.html,'text/html'),scripts=[...page.body.querySelectorAll('script')].filter(script=>!script.type||script.type==='text/javascript');
  scripts.forEach(script=>script.remove());
  // Scripts in the new document receive a fresh lifetime, so handlers never accumulate.
  window.jailPage.abort();window.jailPage=new AbortController();
  delete window.jailSupplyRestore;delete window.jailInventoryRestore;delete window.jailWorkLogRestore;
  page.body.dataset.navigationReady='0';
  document.title=page.title;document.body.replaceWith(document.importNode(page.body,true));
  const finalURL=new URL(result.url);['ctx','resume','created'].forEach(name=>{if(target.searchParams.has(name))finalURL.searchParams.set(name,target.searchParams.get(name))});finalURL.hash=target.hash;
  if(mode==='push')history.pushState({jail:true},'',finalURL);else history.replaceState({jail:true},'',finalURL);
  for(const source of scripts){
   const script=document.createElement('script');
   for(const attr of source.attributes)if(!['defer','async'].includes(attr.name))script.setAttribute(attr.name,attr.value);
   script.removeAttribute('src');script.textContent=source.src?result.sources.get(source.src)+'\n//# sourceURL='+source.src:source.textContent;document.body.appendChild(script);
  }
  if(finalURL.hash){const anchor=document.getElementById(decodeURIComponent(finalURL.hash.slice(1)));if(anchor)anchor.scrollIntoView()}
  else window.scrollTo(0,0);
  document.body.dataset.navigationReady='1';document.dispatchEvent(new Event('jail:page-ready'));warmPage();
 }
 async function visit(url,mode='push'){
  const serial=++sequence,cached=cache.has(key(url));busy=true;
  try{
   const result=await load(url);if(serial!==sequence)return;
   await mount(result,url,mode);stats.navigations++;stats.hits+=cached?1:0;stats.lastURL=location.href;stats.lastCached=cached;
  }catch(error){if(serial===sequence)location.assign(url.href)}
  finally{if(serial===sequence)busy=false}
 }
 document.addEventListener('pointerover',event=>warm(event.target.closest('a[href]')),{passive:true});
 document.addEventListener('focusin',event=>warm(event.target.closest('a[href]')));
 document.addEventListener('touchstart',event=>warm(event.target.closest('a[href]')),{passive:true});
 document.addEventListener('click',event=>{
  const link=event.target.closest('a[href]');
  if(event.defaultPrevented||event.button!==0||event.metaKey||event.ctrlKey||event.shiftKey||event.altKey||!link||link.target||link.hasAttribute('download')||link.matches('.side-user-logout,[data-no-instant]'))return;
  const url=eligible(link.href);if(!url||url.pathname===location.pathname&&url.search===location.search)return;
  event.preventDefault();
  // Section links add their draft context in their own click handler.
  queueMicrotask(()=>{const target=eligible(link.href);if(target)visit(target)});
 });
 document.addEventListener('submit',event=>{
  if(event.target.method?.toLowerCase()==='post'){cache.clear();return}
  if(event.defaultPrevented||event.target.target)return;
  const url=eligible(event.target.getAttribute('action')||location.href);if(!url)return;
  url.search=new URLSearchParams(new FormData(event.target)).toString();event.preventDefault();visit(url);
 });
 addEventListener('popstate',()=>{const url=eligible(location.href);if(url)visit(url,'replace');else location.reload()});
 // A background tab or a browser's back/forward cache may contain old production data.
 addEventListener('pageshow',event=>{if(event.persisted)cache.clear()});
 document.addEventListener('visibilitychange',()=>{if(document.hidden)cache.clear();else if(!busy)warmPage()});
 history.replaceState({jail:true},'',location.href);warmPage();
})();
