import base64,binascii,json,os,secrets,threading,time,urllib.request,urllib.error
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit,parse_qs
from admin_domain import AdminDB,AdminError
BASE=Path(__file__).parent
class AdminHTTP:
 def __init__(self,path=None):self.db=AdminDB(path or BASE/'runtime/b2b.sqlite3');self.chat=None;self.commerce=None;self.notifier=None;self.attempts={};self.lock=threading.Lock()
 def cookie_token(self,h):
  try:
   cookies=SimpleCookie();cookies.load(h.headers.get('Cookie',''));return cookies['naturalia_staff'].value if 'naturalia_staff' in cookies else ''
  except Exception:return ''
 def handle(self,h):
  url=urlsplit(h.path)
  if not url.path.startswith('/api/admin/'):return False
  try:
   port=h.server.server_address[1]
   if h.headers.get('Host') not in [f'127.0.0.1:{port}',f'localhost:{port}']:raise AdminError('此測試後台只允許本機存取',403)
   method=h.command;route=url.path[len('/api/admin/'):];token=self.cookie_token(h);user=self.db.session(token);body={}
   if method=='POST':
    if not h.allowed() or h.headers.get('X-Requested-With')!='NaturaliaAdmin':raise AdminError('請求來源無效',403)
    try:
     length=int(h.headers.get('Content-Length','0'))
     if not 0<length<=7*1024*1024:raise ValueError()
     body=json.loads(h.rfile.read(length))
     if not isinstance(body,dict):raise ValueError()
    except (ValueError,TypeError):raise AdminError('請求格式無效')
   if route=='setup-status' and method=='GET':h.send(200,{'needs_setup':self.db.needs_setup()});return True
   if route in ['setup','login'] and method=='POST':
    with self.lock:
     now=time.time();key=h.client_address[0];events=[t for t in self.attempts.get(key,[]) if now-t<60]
     if len(events)>=8:raise AdminError('登入嘗試過密，請稍後再試',429)
     self.attempts[key]=events+[now]
    if route=='setup':self.db.create_staff(body,setup=True)
    token=self.db.login(body);user=self.db.session(token)
    h.send(200,{'user':user},headers={'Set-Cookie':'naturalia_staff='+token+'; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=28800'});return True
   if not user:raise AdminError('請先登入後台',401)
   if method=='POST' and not secrets.compare_digest(h.headers.get('X-CSRF-Token',''),user['csrf']):raise AdminError('驗證已失效，請重新載入後台',403)
   if route=='me' and method=='GET':h.send(200,{'user':user});return True
   if route=='logout' and method=='POST':
    self.db.logout(token);h.send(200,{'ok':True},headers={'Set-Cookie':'naturalia_staff=; HttpOnly; SameSite=Strict; Path=/api/admin; Max-Age=0'});return True
   if route=='commerce' and method=='GET':h.send(200,self.commerce.snapshot(user));return True
   if route=='cs/read' and method=='POST':h.send(200,self.commerce.mark_read(body,user));return True
   if route=='cs/clear' and method=='POST':
    self.db.require(user,'admin','support')
    lock=self.chat.lock if self.chat else self.lock
    if not lock.acquire(blocking=False):raise AdminError('助手正在處理訊息，請稍後再清除',409)
    try:result=self.commerce.clear_history(body,user)
    finally:lock.release()
    h.send(200,result);return True
   if route=='cs/history' and method=='GET':
    self.db.require(user,'admin','support');h.send(200,self.commerce.history(parse_qs(url.query).get('scope',[''])[0]));return True
   if route in ['cs/bind','cs/state','prices'] and method=='POST':
    callback={'cs/bind':self.commerce.bind,'cs/state':self.commerce.state,'prices':self.commerce.price}[route]
    resume=route=='cs/state' and body.get('status')=='ai'
    if resume:self.db.require(user,'admin','support')
    result=None if resume else callback(body,user)
    if route=='cs/state':
     with self.db.db() as db:conv=db.execute('SELECT * FROM conversations WHERE id=?',(body.get('scope'),)).fetchone()
     if conv and conv['channel']=='whatsapp':
      try:
       key=(BASE/'runtime/whatsapp-service.key').read_text().strip()
       request=urllib.request.Request('http://127.0.0.1:'+os.getenv('WA_PORT','8767')+'/api/contact-scope',data=json.dumps({'account':conv['account'],'peer':conv['peer'],'paused':body['status']=='human'}).encode(),headers={'Content-Type':'application/json','X-Backend-Key':key})
       with urllib.request.urlopen(request,timeout=10):pass
      except Exception:raise AdminError('WhatsApp 服務未能同步，對話保持人工／原有狀態；請啟動服務後重試' if resume else '人工接手已記錄，但 WhatsApp 服務未能同步；請啟動服務後重試',503)
    if resume:result=callback(body,user)
    h.send(200,result);return True
   if route=='cs/reply' and method=='POST':
    conv,reply,fresh=self.commerce.prepare_reply(body,user)
    if fresh:
     status='sent'
     if conv['channel']=='whatsapp':
      try:
       key=(BASE/'runtime/whatsapp-service.key').read_text().strip()
       request=urllib.request.Request('http://127.0.0.1:'+os.getenv('WA_PORT','8767')+'/api/manual',data=json.dumps({'account':conv['account'],'peer':conv['peer'],'text':reply['text'],'request_id':reply['id']}).encode(),headers={'Content-Type':'application/json','X-Backend-Key':key})
       with urllib.request.urlopen(request,timeout=20) as response:status=json.load(response)['status']
      except Exception:status='uncertain'
     self.commerce.finish_reply(reply['id'],user,status);reply['status']=status
    h.send(200,{'status':reply['status']});return True
   if route.startswith('testchat/'):
    action=route.split('/')[-1]
    if {'history':'GET','message':'POST','reset':'POST'}.get(action)!=method:raise AdminError('操作不存在',404)
    if not self.chat:raise AdminError('測試對話服務未啟動',503)
    if method=='GET':body={'mode':parse_qs(url.query).get('mode',['retrieval'])[0],'customer':parse_qs(url.query).get('customer',[''])[0]}
    h.send(200,self.chat.handle(user,action,body));return True
   if route=='data' and method=='GET':h.send(200,self.db.snapshot(user));return True
   if route=='ledger.csv' and method=='GET':h.send(200,self.db.export_csv(user),'text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="naturalia-b2b-ledger.csv"'});return True
   if route.startswith('evidence/') and method=='GET':
    evidence=self.db.get_evidence(route.split('/')[-1],user)
    disposition='inline' if parse_qs(url.query).get('inline')==['1'] and evidence['mime'] in ['image/png','image/jpeg'] else 'attachment'
    h.send(200,evidence['body'],evidence['mime'],headers={'Content-Disposition':disposition+'; filename="receipt-'+evidence['id']+{'image/png':'.png','image/jpeg':'.jpg','application/pdf':'.pdf'}[evidence['mime']]+'"'});return True
   if route.startswith('whatsapp/'):
    action=route.split('/')[-1]
    self.db.require(user,'admin','support')
    if action in ['connect','disconnect','reset-connection','mode']:self.db.require(user,'admin')
    allowed={'status':'GET','connect':'POST','disconnect':'POST','reset-connection':'POST','mode':'POST','contact':'POST','approve':'POST','retry':'POST','simulate':'POST'}
    if allowed.get(action)!=method:raise AdminError('操作不存在',404)
    endpoint='http://127.0.0.1:'+str(int(os.getenv('WA_PORT','8767')))+'/api/'+action
    if action=='status':endpoint+='?test='+('1' if parse_qs(url.query).get('test')==['1'] else '0')
    secret=(BASE/'runtime/whatsapp-service.key').read_text().strip()
    request=urllib.request.Request(endpoint,data=json.dumps(body).encode() if method=='POST' else None,headers={'X-Backend-Key':secret,'Content-Type':'application/json'})
    try:
     with urllib.request.urlopen(request,timeout=20) as response:result=json.load(response)
    except urllib.error.HTTPError as e:
     raise AdminError('WhatsApp 操作未完成，請檢查連線及訊息狀態',e.code if e.code in [400,404,409] else 503)
    except (OSError,ValueError):raise AdminError('WhatsApp 服務未啟動，請啟動連線服務',503)
    if method=='POST':
     with self.db.db(True) as db:self.db.audit(db,user['id'],'whatsapp.'+action,body.get('id','channel'),{'mode':body.get('mode')})
    h.send(200,result);return True
   methods={'customers':self.db.create_customer,'plans':self.db.create_plan,'plans/deactivate':self.db.deactivate_plan,'plans/edit':self.db.edit_plan,'plans/state':self.db.plan_state,'topups':self.db.create_topup,'topups/review':self.db.review,'staff':self.db.create_staff}
   if method=='POST' and route in methods:
    result=methods[route](body,user)
    if route=='topups/review' and self.commerce and not result.get('duplicate'):self.commerce.notify_review(result['id'])
    if route=='topups/review':
     if self.notifier:
      try:self.notifier.process(result['id'])
      except Exception:pass # Review is committed; queued notification is retried independently.
     with self.db.db() as c:result['notifications']=[r['status'] for r in c.execute('SELECT status FROM review_notifications WHERE topup=?',(result['id'],))]
    h.send(200,result);return True
   if method=='POST' and route=='evidence':
    try:raw=base64.b64decode(body.get('data',''),validate=True)
    except (ValueError,TypeError,binascii.Error):raise AdminError('附件格式無效')
    h.send(200,self.db.save_evidence(body.get('name'),body.get('mime'),raw,user));return True
   raise AdminError('找不到操作',404)
  except AdminError as e:h.send(e.status,{'error':str(e)})
  except Exception:h.send(500,{'error':'操作未完成，請重新整理並核對記錄'})
  return True
