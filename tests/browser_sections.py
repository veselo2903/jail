import base64,json,subprocess,time,urllib.request
from pathlib import Path
import websocket
process=subprocess.Popen(['/snap/bin/chromium','--headless','--no-sandbox','--disable-gpu','--no-first-run','--remote-allow-origins=*','--remote-debugging-port=18770','--user-data-dir=/tmp/jail-sections-browser','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
errors=[]
try:
    for _ in range(80):
        try:tabs=json.load(urllib.request.urlopen('http://127.0.0.1:18770/json'));break
        except Exception:time.sleep(.1)
    ws=websocket.create_connection(tabs[0]['webSocketDebuggerUrl']);serial=0
    def command(method,params=None):
        global serial
        serial+=1;ws.send(json.dumps({'id':serial,'method':method,'params':params or {}}))
        while True:
            r=json.loads(ws.recv())
            if r.get('method')=='Runtime.exceptionThrown':errors.append(r['params'])
            if r.get('id')==serial:
                assert 'error' not in r,r
                return r.get('result',{})
    def js(expression):
        r=command('Runtime.evaluate',{'expression':'(()=>{return eval('+json.dumps(expression)+')})()','returnByValue':True});assert 'exceptionDetails' not in r,r
        return r['result'].get('value')
    def wait(expression):
        for _ in range(80):
            if js(expression):return
            time.sleep(.1)
        raise AssertionError((expression,js('location.href'),js('document.body?.innerText.slice(0,500)')))
    def navigate(path):
        result=command('Page.navigate',{'url':'http://127.0.0.1:18769'+path});assert 'errorText' not in result,result;expected='/director/orders' if path=='/login/director' else path.split('?')[0];wait('location.pathname==='+json.dumps(expected)+'&&document.readyState==="complete"')
    def screenshot(name):
        shot=command('Page.captureScreenshot',{'captureBeyondViewport':True});Path('/tmp/jail-director-'+name+'.png').write_bytes(base64.b64decode(shot['data']))
    command('Page.enable');command('Runtime.enable');navigate('/login/director');js('sessionStorage.clear()')
    for path in ('customers','models','orders','batches','inventory','staff','payroll/ledger','requests','supplies','deliveries','customer-payments'):
        navigate('/director/'+path)
        for width in (1440,390):
            command('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<720})
            assert js("document.querySelectorAll('.biz-empty-action').length===1&&!document.querySelector('.wrap h2,.wrap table,.wrap form')"),path
            assert js("const button=document.querySelector('.biz-empty-action').getBoundingClientRect(),empty=document.querySelector('.biz-empty').getBoundingClientRect();Math.abs((button.x+button.width/2)-(empty.x+empty.width/2))<1&&Math.abs((button.y+button.height/2)-(empty.y+empty.height/2))<1&&! (document.documentElement.scrollWidth>innerWidth)"),(path,width)
            if path=='inventory':screenshot('empty-materials-'+str(width))
    command('Emulation.setDeviceMetricsOverride',{'width':1440,'height':900,'deviceScaleFactor':1,'mobile':False})
    navigate('/director/orders/new')
    js("document.querySelector('.biz-empty-action').click()")
    wait("location.pathname==='/director/customers/new'&&document.readyState==='complete'")
    js("document.querySelector('[name=name]').value='Фирма Север';document.querySelector('form[method=post]').requestSubmit()")
    wait("location.pathname==='/director/customers/1'&&document.readyState==='complete'&&!document.querySelector('#section-return').hidden")
    js("document.querySelector('#section-return').click()")
    wait("location.pathname==='/director/orders/new'&&document.readyState==='complete'&&!!document.querySelector('.biz-empty-action')")
    js("(document.querySelector('[data-model-create]')||document.querySelector('.biz-empty-action')).click()")
    wait("location.pathname==='/director/models/new'&&document.readyState==='complete'")
    assert js("document.querySelector('[name=customer_id]').value==='1'")
    js("document.querySelector('[name=name]').value='Кроссовки 714';document.querySelector('[name=sale_price]').value='900';document.querySelector('form[method=post]').requestSubmit()")
    wait("location.pathname==='/director/models/1'&&document.readyState==='complete'")
    js("document.querySelector('#section-return').click()")
    wait("location.pathname==='/director/orders/new'&&document.readyState==='complete'&&document.querySelector('[data-choose-model]')?.checked")
    js("const row=document.querySelector('[data-model-id=\"1\"]');row.querySelector('[name=qty]').value='80';row.querySelector('[name=price]').value='850';row.querySelector('[name=specification]').value='Размеры 38–42';row.querySelector('[name=qty]').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-model-create]').click()")
    wait("location.pathname==='/director/models/new'&&document.readyState==='complete'")
    js("document.querySelector('[name=name]').value='Ботинки 285';document.querySelector('[name=sale_price]').value='1100';document.querySelector('form[method=post]').requestSubmit()")
    wait("location.pathname==='/director/models/2'&&document.readyState==='complete'")
    js("document.querySelector('#section-return').click()")
    wait("location.pathname==='/director/orders/new'&&document.readyState==='complete'&&document.querySelectorAll('[data-choose-model]:checked').length===2")
    assert js("const row=document.querySelector('[data-model-id=\"1\"]');row.querySelector('[name=qty]').value==='80'&&row.querySelector('[name=price]').value==='850'&&row.querySelector('[name=specification]').value==='Размеры 38–42'")
    js("const row=document.querySelector('[data-model-id=\"2\"]');row.querySelector('[name=qty]').value='20';row.querySelector('[name=qty]').dispatchEvent(new Event('input',{bubbles:true}))")
    assert js("document.querySelector('[data-order-total]').textContent.includes('90')&&document.querySelector('#biz-order-form').checkValidity()")
    def widths(name):
        for width in (1440,390):
            command('Emulation.setDeviceMetricsOverride',{'width':width,'height':900,'deviceScaleFactor':1,'mobile':width<720})
            screenshot(name+'-'+str(width));assert not js('document.documentElement.scrollWidth>innerWidth'),(name,width)
    widths('order-form')
    command('Page.reload');wait("document.readyState==='complete'&&document.querySelector('[data-model-id=\"1\"] [name=qty]').value==='80'")
    js("document.querySelector('#biz-order-form').requestSubmit()")
    wait("location.pathname==='/director/orders/1'&&document.readyState==='complete'")
    widths('order')
    navigate('/director/orders/new?customer=1')
    assert js("!Array.from(document.querySelectorAll('[data-choose-model]')).some(x=>x.checked)")
    navigate('/director/batches/1')
    js("const row=document.querySelector('.biz-operation-pick');row.querySelector('input[type=checkbox]').click();row.querySelector('[inputmode=decimal]').value='25';row.closest('form').requestSubmit()")
    wait("document.readyState==='complete'&&!!document.querySelector('.biz-task')")
    widths('batch')
    navigate('/director/supplies/new?batch=1')
    wait("document.readyState==='complete'&&document.querySelector('.shipment-party')?.querySelector('[data-linked-batch]').value==='1'")
    js("document.querySelector('[name=ship_note]').value='Две позиции';document.querySelector('.sklad-party-material-row [data-section-hop]').click()")
    wait("location.pathname==='/director/inventory/new'&&document.readyState==='complete'")
    js("document.querySelector('[name=name]').value='Клей';document.querySelector('[name=unit]').value='kg';document.querySelector('form[method=post]').requestSubmit()")
    wait("location.pathname==='/director/inventory/materials/1'&&document.readyState==='complete'")
    js("document.querySelector('#section-return').click()")
    wait("location.pathname==='/director/supplies/new'&&document.readyState==='complete'&&document.querySelector('.sklad-party-material-row select').value==='1'")
    assert js("document.querySelector('[name=ship_note]').value==='Две позиции'&&document.querySelector('.shipment-party [data-linked-batch]').value==='1'")
    js("document.querySelector('.sklad-party-material-row [name^=party_material_qty]').value='12.5';document.querySelector('[data-add-party]').click();const rows=document.querySelectorAll('.shipment-party');rows[1].querySelector('[data-linked-batch]').value='2';rows[1].querySelector('[data-linked-batch]').dispatchEvent(new Event('change',{bubbles:true}));rows[1].querySelector('[data-section-hop]').click()")
    wait("location.pathname==='/director/inventory/new'&&document.readyState==='complete'")
    js("document.querySelector('[name=name]').value='Подошва';document.querySelector('[name=unit]').value='pary';document.querySelector('form[method=post]').requestSubmit()")
    wait("location.pathname==='/director/inventory/materials/2'&&document.readyState==='complete'")
    js("document.querySelector('#section-return').click()")
    wait("location.pathname==='/director/supplies/new'&&document.readyState==='complete'&&document.querySelectorAll('.shipment-party').length===2")
    assert js("const rows=document.querySelectorAll('.shipment-party');rows[0].querySelector('[name^=party_material_qty]').value==='12.5'&&rows[1].querySelector('[data-linked-batch]').value==='2'&&rows[1].querySelector('.sklad-party-material-row select').value==='2'")
    js("document.querySelector('.sklad-unassigned-materials').open=true")
    widths('supply')
    for path in ('/director/customers/1','/director/models/1','/director/inventory','/director/staff','/director/customer-payments','/director/deliveries'):
        navigate(path);widths(path.replace('/','-'))
    navigate('/director/inventory')
    js("document.querySelector('#biz-material-move').open=true;document.querySelector('[data-receipt-add]').click();const rows=document.querySelectorAll('.biz-receipt-row');rows[0].querySelector('[name=material_id]').value='1';rows[0].querySelector('[name=qty]').value='5';rows[0].querySelector('[name=cost]').value='50';rows[1].querySelector('[name=material_id]').value='2';rows[1].querySelector('[name=qty]').value='7';rows[1].querySelector('[name=cost]').value='70';rows[1].querySelector('[data-section-hop]').click()")
    wait("location.pathname==='/director/inventory/new'&&document.readyState==='complete'")
    js("document.querySelector('[name=name]').value='Нитки';document.querySelector('[name=unit]').value='kg';document.querySelector('form[method=post]').requestSubmit()")
    wait("location.pathname==='/director/inventory/materials/3'&&document.readyState==='complete'")
    js("document.querySelector('#section-return').click()")
    wait("location.pathname==='/director/inventory'&&document.readyState==='complete'&&document.querySelectorAll('.biz-receipt-row').length===2")
    assert js("const rows=document.querySelectorAll('.biz-receipt-row');rows[0].querySelector('[name=qty]').value==='5'&&rows[0].querySelector('[name=cost]').value==='50'&&rows[1].querySelector('[name=material_id]').value==='3'&&rows[1].querySelector('[name=qty]').value==='7'&&rows[1].querySelector('[name=cost]').value==='70'")
    widths('multiple-receipt')
    assert not errors,errors
    print('PASS browser: customer/model/material navigation with original form preservation, model ordering, reload, success clearing, dynamic supply rows, desktop/mobile layout and no runtime errors')
    ws.close()
finally:
    process.terminate();process.wait(timeout=10)
