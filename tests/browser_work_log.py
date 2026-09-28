import base64,json,subprocess,time,urllib.request
from pathlib import Path
import websocket
process=subprocess.Popen(['/snap/bin/chromium','--headless','--no-sandbox','--disable-gpu','--no-first-run','--remote-allow-origins=*','--remote-debugging-port=18773','--user-data-dir=/tmp/jail-work-log-browser','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
errors=[]
try:
    for _ in range(80):
        try:tabs=json.load(urllib.request.urlopen('http://127.0.0.1:18773/json'));break
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
            if js("document.body?.dataset.navigationReady!=='0'") and js(expression):return
            time.sleep(.1)
        raise AssertionError((expression,js('location.href'),js('document.body?.innerText.slice(0,500)')))
    def navigate(path):
        result=command('Page.navigate',{'url':'http://127.0.0.1:18769'+path});
        if result.get('errorText')=='net::ERR_ABORTED':
            time.sleep(.2);result=command('Page.navigate',{'url':'http://127.0.0.1:18769'+path})
        assert 'errorText' not in result,result;expected='/director/orders' if path=='/login/director' else '/proizv/batches' if path=='/login/proizv' else path.split('?')[0];wait('location.pathname==='+json.dumps(expected)+'&&document.readyState==="complete"')
    def screenshot(name):
        shot=command('Page.captureScreenshot',{'captureBeyondViewport':True});Path('/tmp/jail-director-'+name+'.png').write_bytes(base64.b64decode(shot['data']))
    command('Page.enable');command('Runtime.enable');navigate('/login/director')
    for name in ('Иван','Мария'):
        navigate('/director/staff/new')
        js("document.querySelector('[name=name]').value="+json.dumps(name)+";document.querySelector('form[method=post]').requestSubmit()")
        wait("location.pathname.startsWith('/director/payroll/workers/')&&document.readyState==='complete'")
    navigate('/login/proizv');navigate('/proizv/work-log')
    assert js("!!document.querySelector('#work-log-form')")
    assert js("!document.querySelector('[name=rate]')&&!document.body.textContent.includes('25,00 ₽')")
    assert js("document.querySelectorAll('.work-log-row').length===1")
    js("const row=document.querySelector('.work-log-row');row.querySelector('[name=worker_id]').value=row.querySelector('[name=worker_id] option:nth-child(2)').value;row.querySelector('[name=batch_id]').value=row.querySelector('[name=batch_id] option:nth-child(2)').value;row.querySelector('[name=batch_id]').dispatchEvent(new Event('change',{bubbles:true}));row.querySelector('[name=task_id]').value=row.querySelector('[name=task_id] option:nth-child(2)').value;row.querySelector('[name=task_id]').dispatchEvent(new Event('change',{bubbles:true}));row.querySelector('[name=qty]').value='2';document.querySelector('[data-work-add]').click()")
    assert js("document.querySelectorAll('.work-log-row').length===2")
    js("const row=document.querySelectorAll('.work-log-row')[1];row.querySelector('[name=worker_id]').value=row.querySelector('[name=worker_id] option:nth-child(3)').value;row.querySelector('[name=qty]').value='1';row.querySelector('[name=qty]').dispatchEvent(new Event('input',{bubbles:true}))")
    command('Emulation.setDeviceMetricsOverride',{'width':390,'height':900,'deviceScaleFactor':1,'mobile':True})
    assert not js('document.documentElement.scrollWidth>innerWidth')
    js("document.querySelector('#work-log-form').requestSubmit()")
    wait("location.pathname==='/proizv/work-log'&&document.readyState==='complete'&&document.querySelectorAll('.work-log-record').length===2")
    assert js("!document.body.textContent.includes('75,00 ₽')&&!document.querySelector('[name=rate]')")
    assert not errors,errors
    print('PASS browser: factory day sheet, two rows, responsive width, automatic save and financial privacy')
    ws.close()
finally:
    process.terminate();process.wait(timeout=10)
