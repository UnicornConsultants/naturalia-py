import http.cookiejar,json,sys,tempfile,threading,unittest,urllib.request,urllib.error
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_http import AdminHTTP
from admin_chat import AdminChat
from commerce import Commerce
from unittest.mock import patch
class HTTPTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.admin=AdminHTTP(Path(self.tmp.name)/'db');adapter=self.admin
  class Handler(BaseHTTPRequestHandler):
   def log_message(self,*args):pass
   def allowed(self):return not self.headers.get('Origin') or self.headers.get('Origin')=='http://'+self.headers.get('Host')
   def send(self,status,body,kind='application/json',headers=None):
    if not isinstance(body,bytes):body=json.dumps(body).encode()
    self.send_response(status);self.send_header('Content-Type',kind)
    for key,value in (headers or {}).items():self.send_header(key,value)
    self.end_headers();self.wfile.write(body)
   def do_GET(self):adapter.handle(self)
   def do_POST(self):adapter.handle(self)
  self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
  self.base='http://127.0.0.1:'+str(self.server.server_port)+'/api/admin/';self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()));self.csrf=''
 def tearDown(self):self.server.shutdown();self.server.server_close();self.thread.join();self.tmp.cleanup()
 def req(self,path,body=None,**headers):
  headers={'X-Requested-With':'NaturaliaAdmin','X-CSRF-Token':self.csrf,'Content-Type':'application/json',**headers}
  return self.opener.open(urllib.request.Request(self.base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers))
 def setup_user(self):
  with self.req('setup',{'name':'Tester','username':'tester','password':'fixture-password-123'}) as r:self.csrf=json.load(r)['user']['csrf']
 def test_auth_csrf_roles_and_export(self):
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('data')
  self.assertEqual(error.exception.code,401);self.setup_user()
  with self.req('me') as r:self.assertEqual(json.load(r)['user']['role'],'admin')
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('customers',{'company':'Test','contact':'Test'},**{'X-CSRF-Token':'wrong'})
  self.assertEqual(error.exception.code,403)
  with self.req('customers',{'company':'Test','contact':'Test'}) as r:customer=json.load(r)['id']
  with self.req('plans',{'name':'Test','paid':'10','principal':'10','bonus':'2'}) as r:plan=json.load(r)['id']
  with self.req('topups',{'customer':customer,'plan':plan,'request_key':'HTTP-1','method':'cash','reference':'HTTP-1','evidence':'Test receipt'}) as r:topup=json.load(r)['id']
  with self.req('topups/review',{'id':topup,'decision':'approved','reason':'Fixture only','payment_verified':True,'verified_paid':'10'}):pass
  with self.req('ledger.csv') as r:self.assertIn('10.00',r.read().decode())
  with self.req('data') as r:self.assertEqual(json.load(r)['customers'][0]['balance'],1200)
  with self.req('logout',{}):pass
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('me')
  self.assertEqual(error.exception.code,401)
 def test_testchat_routes(self):
  class FakeCatalog:
   def chat(self,q,history,use_model):return {'answer':'offline reply','products':[]}
  self.admin.chat=AdminChat(self.admin.db,FakeCatalog())
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('testchat/history')
  self.assertEqual(error.exception.code,401);self.setup_user()
  with self.req('testchat/message',{'message':'套裝','mode':'retrieval'}) as r:self.assertEqual(len(json.load(r)['messages']),2)
  with self.req('testchat/history?mode=retrieval') as r:self.assertEqual(len(json.load(r)['messages']),2)
  with self.req('testchat/reset',{'mode':'retrieval'}) as r:self.assertEqual(json.load(r)['messages'],[])
 def test_failed_whatsapp_resume_keeps_human_state(self):
  class Catalog:items=[]
  self.admin.commerce=Commerce(self.admin.db,Catalog());self.setup_user()
  self.admin.commerce.ensure('wa-test','whatsapp','account','12345678@lid')
  with self.admin.db.db(True) as db:db.execute("UPDATE conversations SET status='human' WHERE id='wa-test'")
  with patch('admin_http.urllib.request.urlopen',side_effect=OSError('offline')):
   with self.assertRaises(urllib.error.HTTPError) as error:self.req('cs/state',{'scope':'wa-test','status':'ai'})
  self.assertEqual(error.exception.code,503)
  self.assertEqual(self.admin.commerce.history('wa-test')['status'],'human')
 def test_cross_origin_and_wrong_host_rejected(self):
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('setup',{},Origin='https://unrelated.invalid')
  self.assertEqual(error.exception.code,403)
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('setup-status',Host='attacker.invalid')
  self.assertEqual(error.exception.code,403)
 def test_clear_conversation_route_requires_auth_csrf_and_idle_assistant(self):
  class Catalog:items=[]
  self.admin.commerce=Commerce(self.admin.db,Catalog())
  self.admin.commerce.respond('clear-test','hi','one',use_model=False)
  body={'scope':'clear-test','confirmed':True,'last_id':self.admin.commerce.history('clear-test')['messages'][-1]['id']}
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('cs/clear',body)
  self.assertEqual(error.exception.code,401);self.setup_user()
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('cs/clear',body,**{'X-CSRF-Token':'wrong'})
  self.assertEqual(error.exception.code,403)
  self.admin.lock.acquire()
  try:
   with self.assertRaises(urllib.error.HTTPError) as error:self.req('cs/clear',body)
   self.assertEqual(error.exception.code,409)
  finally:self.admin.lock.release()
  with self.req('cs/clear',body) as response:self.assertEqual(json.load(response)['messages_deleted'],2)
  self.assertEqual(self.admin.commerce.history('clear-test')['messages'],[])
 def test_evidence_inline_preview_preserves_download_and_auth(self):
  proof=self.admin.db.save_evidence('proof','image/png',b'\x89PNG\r\n\x1a\nfixture',{'id':'finance','role':'finance'})['id']
  with self.assertRaises(urllib.error.HTTPError) as error:self.req('evidence/'+proof+'?inline=1')
  self.assertEqual(error.exception.code,401);self.setup_user()
  with self.req('evidence/'+proof+'?inline=1') as r:
   self.assertTrue(r.headers['Content-Disposition'].startswith('inline;'));self.assertEqual(r.headers['Content-Type'],'image/png')
  with self.req('evidence/'+proof) as r:self.assertTrue(r.headers['Content-Disposition'].startswith('attachment;'))
  pdf=self.admin.db.save_evidence('pdf','application/pdf',b'%PDF-fixture',{'id':'finance','role':'finance'})['id']
  with self.req('evidence/'+pdf+'?inline=1') as r:self.assertTrue(r.headers['Content-Disposition'].startswith('attachment;'))
if __name__=='__main__':unittest.main()
