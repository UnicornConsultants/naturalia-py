import json,os,threading,time,secrets
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit
from engine import Catalog
from storage import Store
from whatsapp_backend import WhatsAppBackend
from admin_http import AdminHTTP
from admin_chat import AdminChat
from commerce import Commerce
from review_notifications import ReviewNotifier
from admin_domain import AdminError
import kimi
BASE=Path(__file__).parent;STATIC=BASE/'static'
catalog=Catalog()
try:catalog.prepare_vectors()
except Exception as e:print('Semantic setup unavailable:',type(e).__name__)
store=Store(BASE/'runtime/chat.sqlite3');store.purge()
settings_path=BASE/'runtime/backend-settings.json'
settings=json.loads(settings_path.read_text()) if settings_path.exists() else {}
admin=AdminHTTP(os.getenv('B2B_DB_PATH') or settings.get('b2b_db'))
chat_lock=threading.Lock();rates={}
commerce=Commerce(admin.db,catalog);admin.commerce=commerce
admin.chat=AdminChat(admin.db,catalog,chat_lock,commerce)
whatsapp=WhatsAppBackend(store,catalog,commerce)
if os.getenv('REVIEW_NOTIFICATIONS_ENABLED','1')=='1':admin.notifier=ReviewNotifier(commerce)
class Handler(BaseHTTPRequestHandler):
 def send(self,status,body,kind='application/json; charset=utf-8',headers=None):
  if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode()
  self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.send_header('X-Content-Type-Options','nosniff');self.send_header('Cache-Control','no-store');self.send_header('Referrer-Policy','no-referrer')
  if self.path.startswith(('/admin','/api/admin/')):self.send_header('X-Frame-Options','DENY')
  for key,value in (headers or {}).items():self.send_header(key,value)
  self.end_headers();self.wfile.write(body)
 def token(self):return self.headers.get('Authorization','').removeprefix('Bearer ')
 def allowed(self):
  origin=self.headers.get('Origin')
  return not origin or urlsplit(origin).netloc==self.headers.get('Host')
 def do_GET(self):
  if admin.handle(self):return
  path=urlsplit(self.path).path
  if path.startswith('/product-images/'):
   media=catalog.media.get(path.removeprefix('/product-images/'))
   if not media:return self.send(404,{'error':'Product image not found'})
   return self.send(200,media[0].read_bytes(),media[1])
  if path in ['/payment-qr/payme','/payment-qr/alipay']:
   file=BASE.parent/'info'/('PayMe.jpeg' if path.endswith('payme') else 'alipay.jpeg');return self.send(200,file.read_bytes(),'image/jpeg')
  if path=='/api/health':return self.send(200,{'products':len(catalog.items),'semantic':catalog.vectors is not None,'generation_configured':kimi.configured(),'provider':'kimi','member_services':False,'b2b_wallet':True,'cs':True})
  if path=='/api/history':
   if not store.valid(self.token()):return self.send(401,{'error':'Session expired'})
   return self.send(200,commerce.history('web:'+store.key(self.token())))
  files={'/chat-workflow.js':('chat-workflow.js','text/javascript; charset=utf-8'),'/admin':('admin.html','text/html; charset=utf-8'),'/admin.js':('admin.js','text/javascript; charset=utf-8'),'/admin.css':('admin.css','text/css; charset=utf-8'),'/':('index.html','text/html; charset=utf-8'),'/embed':('index.html','text/html; charset=utf-8'),'/demo':('demo.html','text/html; charset=utf-8'),'/widget.js':('widget.js','text/javascript; charset=utf-8'),'/app.js':('app.js','text/javascript; charset=utf-8'),'/style.css':('style.css','text/css; charset=utf-8')}
  if path not in files:return self.send(404,{'error':'Not found'})
  name,kind=files[path];self.send(200,(STATIC/name).read_bytes(),kind)
 def do_POST(self):
  if admin.handle(self):return
  if urlsplit(self.path).path in ['/internal/whatsapp/reply','/internal/whatsapp/observe','/internal/whatsapp/delivery']:
   if not whatsapp.authorized(self.headers.get('X-WA-Secret')):return self.send(401,{'error':'Unauthorized'})
   try:
    size=int(self.headers.get('Content-Length','0'))
    if not 0<size<=7*1024*1024:raise ValueError()
    data=json.loads(self.rfile.read(size))
   except (ValueError,TypeError):return self.send(400,{'error':'Invalid request'})
   # Recording inbound messages must remain available while another chat waits for the model.
   path=urlsplit(self.path).path;recording_or_checking=not path.endswith('/reply')
   if not recording_or_checking and not chat_lock.acquire(blocking=False):return self.send(429,{'error':'Assistant busy'})
   try:self.send(200,whatsapp.delivery_allowed(data) if path.endswith('/delivery') else whatsapp.observe(data) if path.endswith('/observe') else whatsapp.handle(data))
   except AdminError as e:self.send(e.status,{'error':str(e)})
   except (ValueError,AttributeError):self.send(400,{'error':'Invalid request'})
   except Exception:self.send(503,{'error':'Reply unavailable'})
   finally:
    if not recording_or_checking:chat_lock.release()
   return
  if not self.allowed():return self.send(403,{'error':'Origin not allowed'})
  path=urlsplit(self.path).path
  if path=='/api/session':
   now=time.time();key=self.client_address[0];events=[t for t in rates.get(key,[]) if now-t<60]
   if len(events)>=30:return self.send(429,{'error':'Try later'})
   rates[key]=events+[now];store.purge();return self.send(201,{'token':store.create()})
  if path not in ['/api/chat','/api/reset']:return self.send(404,{'error':'Not found'})
  token=self.token()
  if not store.valid(token):return self.send(401,{'error':'Session expired'})
  if path=='/api/reset':
   scope='web:'+store.key(token)
   with admin.db.db(True) as db:db.execute('DELETE FROM conversation_messages WHERE scope=?',(scope,));db.execute('DELETE FROM conversation_events WHERE scope=?',(scope,));db.execute("UPDATE conversations SET status='closed' WHERE id=?",(scope,))
   store.delete(token);return self.send(200,{'token':store.create()})
  try:
   size=int(self.headers.get('Content-Length','0'))
   if size<1 or size>7*1024*1024:raise ValueError()
   data=json.loads(self.rfile.read(size));q=data['message']
   if not isinstance(q,str) or not q.strip() or len(q)>1000:raise ValueError()
  except (ValueError,KeyError,TypeError):return self.send(400,{'error':'Invalid request'})
  if not chat_lock.acquire(blocking=False):return self.send(429,{'error':'Assistant busy; try again'})
  try:
   history=[{'role':m['role'],'content':m['content'][:2000]} for m in store.history(token)][-10:]
   result=commerce.respond('web:'+store.key(token),q,data.get('event') or secrets.token_hex(16),action=data.get('action'),attachment=data.get('attachment'));self.send(200,result)
  except AdminError as e:self.send(e.status,{'error':str(e)})
  except Exception:self.send(503,{'error':'Search temporarily unavailable'})
  finally:chat_lock.release()
if __name__=='__main__':
 if admin.notifier:admin.notifier.start()
 port=int(os.getenv('PORT','8765'));print(f'Naturalia preview: http://127.0.0.1:{port}',flush=True);ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
