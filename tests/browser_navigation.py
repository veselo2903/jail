import base64,json,subprocess,time,urllib.request
from pathlib import Path
import websocket
process=subprocess.Popen(['/snap/bin/chromium','--headless','--no-sandbox','--disable-gpu','--no-first-run','--remote-allow-origins=*','--remote-debugging-port=18771','--user-data-dir=/tmp/jail-navigation-browser','about:blank'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
errors=[]
try:
    for _ in range(80):
        try:tabs=json.load(urllib.request.urlopen('http://127.0.0.1:18771/json'));break
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
        result=command('Page.navigate',{'url':'http://127.0.0.1:18769'+path});assert 'errorText' not in result,result;expected='/director/orders' if path=='/login/director' else path.split('?')[0];wait('location.pathname==='+json.dumps(expected)+'&&document.readyState==="complete"')
    def screenshot(name):
        shot=command('Page.captureScreenshot',{'captureBeyondViewport':True});Path('/tmp/jail-director-'+name+'.png').write_bytes(base64.b64decode(shot['data']))
    command('Page.enable');command('Runtime.enable');command('Network.enable');navigate('/login/director')
    wait("window.jailNavigation?.ready('/director/warehouse')&&window.jailNavigation.ready('/director/orders')&&window.jailNavigation.ready('/director/payroll/ledger')")
    js("window.navigationProbe='same-document';window.measuredTransition=null")
    command('Network.emulateNetworkConditions',{'offline':True,'latency':0,'downloadThroughput':0,'uploadThroughput':0})
    def click_menu(path):
        expression="new Promise(resolve=>{const start=performance.now();document.addEventListener('jail:page-ready',()=>requestAnimationFrame(()=>resolve({ms:performance.now()-start,path:location.pathname,probe:window.navigationProbe,cached:window.jailNavigation.stats.lastCached})),{once:true});document.querySelector('.side a[href=\""+path+"\"]').click()})"
        result=command('Runtime.evaluate',{'expression':expression,'awaitPromise':True,'returnByValue':True})
        assert 'exceptionDetails' not in result,result
        value=result['result']['value'];assert value['path']==path and value['probe']=='same-document' and value['cached'],value
        assert value['ms']<150,value
        return value
    first=click_menu('/director/warehouse')
    second=click_menu('/director/payroll/ledger')
    js('history.back()');wait("location.pathname==='/director/warehouse'&&window.jailNavigation.stats.navigations===3")
    js('history.forward()');wait("location.pathname==='/director/payroll/ledger'&&window.jailNavigation.stats.navigations===4")
    assert js("window.navigationProbe==='same-document'")
    command('Network.emulateNetworkConditions',{'offline':False,'latency':0,'downloadThroughput':-1,'uploadThroughput':-1})
    js("document.querySelector('.side a[href=\"/director/orders\"]').click()")
    wait("location.pathname==='/director/orders'&&!!document.querySelector('.orders-tabs,.biz-empty-action')")
    js("(document.querySelector('.orders-tabs a[href=\"/director/orders?view=customers\"]')||document.querySelector('.biz-empty-action')).click()")
    wait("(location.search.includes('view=customers')||location.pathname==='/director/customers/new')&&window.navigationProbe==='same-document'")
    command('Emulation.setDeviceMetricsOverride',{'width':390,'height':850,'deviceScaleFactor':1,'mobile':True})
    navigate('/director/warehouse')
    assert not js('document.documentElement.scrollWidth>innerWidth')
    assert js("document.querySelectorAll('.side a.nav').length") == 4
    screenshot('warehouse-mobile')
    assert not errors,errors
    print('PASS browser: fully prefetched transitions with network offline, no document reload, browser back/forward, no runtime errors:',first,second)
    ws.close()
finally:
    process.terminate();process.wait(timeout=10)
