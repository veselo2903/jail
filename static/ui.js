/* Общие элементы интерфейса jail: подтверждения, сообщения, поле с подсказками. */
(function(){
  "use strict";
  // ---- Окно подтверждения / сообщения (вместо браузерного confirm/alert) ----
  function modal(opts){
    return new Promise(function(resolve){
      var back=document.createElement("div");
      back.className="sheet-back ui-modal";
      back.innerHTML='<div class="sheet" role="dialog" aria-modal="true">'+
        '<div class="sheet-t"></div>'+(opts.sub?'<div class="sheet-sub"></div>':'')+
        '<div class="grid" style="margin-top:14px">'+
        '<button type="button" class="btn block ui-ok"></button>'+
        (opts.cancel===false?'':'<button type="button" class="btn grey block ui-cancel">Отмена</button>')+
        '</div></div>';
      back.querySelector(".sheet-t").textContent=opts.title;
      if(opts.sub)back.querySelector(".sheet-sub").textContent=opts.sub;
      var ok=back.querySelector(".ui-ok");ok.textContent=opts.ok||"Да";
      ok.classList.add(opts.danger?"red":"green");
      document.body.appendChild(back);
      requestAnimationFrame(function(){back.classList.add("open");});
      setTimeout(function(){ok.focus();},60);
      function done(v){back.classList.remove("open");document.removeEventListener("keydown",key);
        setTimeout(function(){back.remove();},220);resolve(v);}
      function key(e){if(e.key==="Escape")done(false);if(e.key==="Enter"){e.preventDefault();done(true);}}
      document.addEventListener("keydown",key);
      ok.addEventListener("click",function(){done(true);});
      var c=back.querySelector(".ui-cancel");if(c)c.addEventListener("click",function(){done(false);});
      back.addEventListener("click",function(e){if(e.target===back)done(false);});
    });
  }
  window.uiConfirm=function(title,opts){opts=opts||{};opts.title=title;return modal(opts);};
  window.uiAlert=function(title,sub){return modal({title:title,sub:sub,ok:"Понятно",cancel:false});};

  // Формы с data-confirm="Текст" спрашивают подтверждение красивым окном
  document.addEventListener("submit",function(e){
    var f=e.target;if(!f.dataset||!f.dataset.confirm||f.dataset.confirmed)return;
    e.preventDefault();
    uiConfirm(f.dataset.confirm,{ok:f.dataset.ok||"Да",danger:f.hasAttribute("data-danger"),sub:f.dataset.sub})
      .then(function(yes){if(yes){f.dataset.confirmed=1;
        if(f.requestSubmit)f.requestSubmit();else f.submit();delete f.dataset.confirmed;}});
  },true);

  // ---- Поле с подсказками: пишешь руками, программа показывает похожие ----
  function norm(s){return (s||"").toLowerCase().replace(/\s+/g," ").trim();}
  function initCombo(inp){
    if(inp._combo)return;inp._combo=1;
    var opts=[];try{opts=JSON.parse(inp.dataset.options||"[]");}catch(e){}
    var wrap=document.createElement("div");wrap.className="combo-wrap";
    inp.parentNode.insertBefore(wrap,inp);wrap.appendChild(inp);
    inp.setAttribute("autocomplete","off");
    var list=document.createElement("div");list.className="combo-list";list.hidden=true;wrap.appendChild(list);
    var act=-1;
    function render(){
      var v=norm(inp.value);
      var m=opts.filter(function(o){return !v||norm(o).indexOf(v)>=0;}).slice(0,8);
      var exact=opts.some(function(o){return norm(o)===v;});
      var html=m.map(function(o,i){return '<div class="combo-opt" data-v="'+o.replace(/"/g,"&quot;")+'">'+
        o.replace(/</g,"&lt;")+'</div>';}).join("");
      if(inp.dataset.ref){   // «+ Создать» всегда внизу списка (кроме точного совпадения)
        if(!exact)html+='<div class="combo-create">+ Создать'+(v?' «'+inp.value.trim().replace(/</g,"&lt;")+'»':'')+'</div>';
      }else if(v&&!exact)html+='<div class="combo-new">Новый: <b>'+inp.value.replace(/</g,"&lt;")+'</b></div>';
      list.innerHTML=html;list.hidden=!html;act=-1;
      var cr=list.querySelector(".combo-create");
      if(cr)cr.addEventListener("mousedown",function(e){e.preventDefault();list.hidden=true;createRef(inp);});
      list.querySelectorAll(".combo-opt").forEach(function(d){
        d.addEventListener("mousedown",function(e){e.preventDefault();inp.value=d.dataset.v;list.hidden=true;
          inp.dispatchEvent(new Event("change"));});});
    }
    inp._addOpt=function(name){if(opts.indexOf(name)<0)opts.push(name);};
    inp.addEventListener("focus",render);inp.addEventListener("input",render);
    inp.addEventListener("blur",function(){setTimeout(function(){list.hidden=true;},120);});
    inp.addEventListener("keydown",function(e){
      var items=list.querySelectorAll(".combo-opt");
      if(e.key==="Enter"&&inp.dataset.ref&&!list.hidden&&!items.length&&list.querySelector(".combo-create")&&inp.value.trim()){
        e.preventDefault();list.hidden=true;createRef(inp);return;}
      if(list.hidden||!items.length)return;
      if(e.key==="ArrowDown"||e.key==="ArrowUp"){e.preventDefault();
        act=(act+(e.key==="ArrowDown"?1:-1)+items.length)%items.length;
        items.forEach(function(d,i){d.classList.toggle("on",i===act);});}
      else if(e.key==="Enter"&&act>=0){e.preventDefault();inp.value=items[act].dataset.v;list.hidden=true;}
      else if(e.key==="Enter"&&inp.dataset.ref&&list.querySelector(".combo-create")&&inp.value.trim()&&!list.hidden){
        e.preventDefault();list.hidden=true;createRef(inp);}   // Enter на новом названии — сразу «+ Создать»
      else if(e.key==="Escape"){list.hidden=true;}
    });
  }
  // Материал из справочника: единица измерения выбирается сама
  document.addEventListener("change",unitFromCatalog,true);
  document.addEventListener("input",unitFromCatalog,true);
  function unitFromCatalog(e){
    var inp=e.target;if(!inp.dataset||!inp.dataset.units)return;
    var map={};try{map=JSON.parse(inp.dataset.units);}catch(x){}
    var v=norm(inp.value),unit=null;
    Object.keys(map).forEach(function(k){if(norm(k)===v)unit=map[k];});
    var f=inp.form;if(!f)return;
    f.querySelectorAll("input[name=unit]").forEach(function(r){r.disabled=!!unit&&r.value!==unit;if(unit&&r.value===unit)r.checked=true;});
  }

  // ---- Сообщения после действия: всплывают внизу и сами исчезают (клик — закрыть сразу) ----
  function initToasts(){
    document.querySelectorAll(".toasts .toast").forEach(function(t,i){
      requestAnimationFrame(function(){t.classList.add("in");});
      function hide(){t.classList.remove("in");t.classList.add("out");setTimeout(function(){t.remove();},180);}
      var timer=setTimeout(hide,5000+i*600);
      t.addEventListener("click",function(){clearTimeout(timer);hide();});
    });
  }

  // ---- Защита от двойного нажатия: после отправки формы кнопка становится неактивной ----
  document.addEventListener("submit",function(e){
    if(e.defaultPrevented)return;
    var f=e.target,b=e.submitter||f.querySelector("button[type=submit],button:not([type])");
    if(!b||f.classList.contains("ajax-add"))return;
    setTimeout(function(){b.disabled=true;b.classList.add("busy");},0);   // после того как форма собрала данные
  });
  window.addEventListener("pageshow",function(){   // вернулись «Назад» — кнопки снова активны
    document.querySelectorAll("button.busy").forEach(function(b){b.disabled=false;b.classList.remove("busy");});
  });

  // ---- Курсор сразу в первом поле, которое нужно заполнить при приёмке ----
  function initFocus(){
    if(document.querySelector("[autofocus]"))return;
    var f=document.querySelector("input.recv-in:placeholder-shown")||document.querySelector("form input[name^=recv_]");
    if(f){f.focus({preventScroll:true});if(f.select)f.select();}
  }


  // ---- «+ Создать»: окно с названием (и единицей для материала) → запись в справочник ----
  var REF_NAMES={customers:["Заказчик","Заказчик «%s» добавлен в справочник","Заказчик «%s» уже есть в справочнике"],
    models:["Модель","Модель «%s» добавлена в справочник","Модель «%s» уже есть в справочнике"],
    materials:["Материал","Материал «%s» добавлен в справочник","Материал «%s» уже есть в справочнике"]};
  var UNITS=["м2","шт","пары"];
  window.uiToast=function(msg){
    var box=document.querySelector(".toasts");
    if(!box){box=document.createElement("div");box.className="toasts";box.setAttribute("role","status");document.body.appendChild(box);}
    var t=document.createElement("div");t.className="toast";t.textContent=msg;box.appendChild(t);
    requestAnimationFrame(function(){t.classList.add("in");});
    function hide(){t.classList.remove("in");t.classList.add("out");setTimeout(function(){t.remove();},180);}
    var tm=setTimeout(hide,4000);t.addEventListener("click",function(){clearTimeout(tm);hide();});
  };
  function createRef(inp){
    var ref=inp.dataset.ref,names=REF_NAMES[ref]||["Запись","«%s» добавлено","«%s» уже есть"];
    var back=document.createElement("div");back.className="sheet-back ui-modal";
    back.innerHTML='<form class="sheet" role="dialog" aria-modal="true" novalidate>'+
      '<div class="sheet-t" style="color:var(--ink)">Новый: '+names[0].toLowerCase()+'</div>'+
      '<label>Название</label><input name="name" autocomplete="off">'+
      (ref==="materials"?'<label style="margin-top:12px;display:block">Единица измерения</label><div class="seg" data-field="unit">'+
        UNITS.map(function(x){return '<label class="seg-opt"><input type="radio" name="unit" value="'+x+'"><span>'+x+'</span></label>';}).join("")+'</div>':'')+
      '<div class="form-err" hidden style="margin-top:10px"></div>'+
      '<div class="grid" style="margin-top:14px"><button type="submit" class="btn block">Создать</button>'+
      '<button type="button" class="btn grey block ui-cancel">Отмена</button></div></form>';
    document.body.appendChild(back);
    var f=back.querySelector("form"),nm=f.querySelector("[name=name]"),err=f.querySelector(".form-err");
    nm.value=inp.value.trim();
    requestAnimationFrame(function(){back.classList.add("open");});
    setTimeout(function(){nm.focus();nm.select();},60);
    function close(){back.classList.remove("open");document.removeEventListener("keydown",key);setTimeout(function(){back.remove();},220);}
    function key(e){if(e.key==="Escape")close();}
    document.addEventListener("keydown",key);
    back.querySelector(".ui-cancel").addEventListener("click",close);
    back.addEventListener("click",function(e){if(e.target===back)close();});
    f.addEventListener("input",function(e){var c=e.target.closest(".invalid")||e.target;c.classList.remove("invalid");});
    f.addEventListener("change",function(e){var c=e.target.closest(".invalid");if(c)c.classList.remove("invalid");});
    f.addEventListener("submit",function(e){
      e.preventDefault();e.stopPropagation();err.hidden=true;
      var bad=[];if(!nm.value.trim())bad.push(nm);
      var seg=f.querySelector("[data-field=unit]");if(seg&&!f.querySelector("[name=unit]:checked"))bad.push(seg);
      if(bad.length){bad.forEach(function(x){x.classList.add("invalid");});err.textContent="Заполните выделенные поля.";err.hidden=false;return;}
      var fd=new FormData(f);
      fetch((document.body.dataset.root||"/")+"refs/"+ref+"/quick_add",{method:"POST",body:fd})
        .then(function(r){return r.json();}).then(function(d){
          if(!d.ok){err.textContent=d.error||"Не удалось создать";err.hidden=false;
            var x=d.field==="unit"?seg:nm;if(x)x.classList.add("invalid");return;}
          inp.value=d.name;if(inp._addOpt)inp._addOpt(d.name);
          if(d.unit&&inp.dataset.units!==undefined){var m={};try{m=JSON.parse(inp.dataset.units||"{}");}catch(x){}
            m[d.name]=d.unit;inp.dataset.units=JSON.stringify(m);}
          inp.classList.remove("invalid");inp.dispatchEvent(new Event("change",{bubbles:true}));
          close();uiToast((d.existed?names[2]:names[1]).replace("%s",d.name));
          inp.blur();var lst=inp.parentNode.querySelector(".combo-list");if(lst)lst.hidden=true;
        }).catch(function(){err.textContent="Не удалось создать — проверьте связь.";err.hidden=false;});
    });
  }
  // список операций закрывается кликом в любом другом месте
  document.addEventListener("click",function(e){
    document.querySelectorAll("details.ops-n[open]").forEach(function(d){if(!d.contains(e.target))d.open=false;});
  });
  window.uiCombo=initCombo;
  function scan(){document.querySelectorAll("input.combo").forEach(initCombo);initToasts();initFocus();}
  if(document.readyState!=="loading")scan();else document.addEventListener("DOMContentLoaded",scan);
})();
