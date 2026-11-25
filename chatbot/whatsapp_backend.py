"""Private channel adapter: service secret, account-scoped sessions and idempotent replies."""
import hashlib,json,secrets,threading,re
from dialogue import wants_cs
from pathlib import Path
class WhatsAppBackend:
 def __init__(self,store,catalog,commerce=None):
  self.commerce=commerce;self.store=store;self.catalog=catalog;self.lock=threading.Lock()
  path=Path(__file__).parent/'runtime/whatsapp-service.key';path.parent.mkdir(exist_ok=True)
  if not path.exists():path.write_text(secrets.token_urlsafe(48));path.chmod(0o600)
  self.secret=path.read_text().strip()
  with store.connect() as db:
   db.execute('CREATE TABLE IF NOT EXISTS wa_sessions (scope TEXT PRIMARY KEY,token TEXT)')
   db.execute('CREATE TABLE IF NOT EXISTS wa_replies (event TEXT PRIMARY KEY,result TEXT)')
 def authorized(self,value):return secrets.compare_digest(self.secret,value or '')
 def delivery_allowed(self,data):
  account,peer=data.get('account'),data.get('peer')
  if not all(isinstance(v,str) and 0<len(v)<=200 for v in [account,peer]):raise ValueError('Invalid input')
  if not self.commerce:return {'allowed':False}
  scope='wa:'+hashlib.sha256(json.dumps([False,account,peer]).encode()).hexdigest()
  with self.commerce.db.db() as db:row=db.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
  return {'allowed':bool(row and (row['status']=='ai' or (row['status']=='waiting' and data.get('handoff') is True)))}
 def handle(self,data):
  account,peer,event,text=(data.get(k) for k in ['account','peer','event','text']);test=data.get('test') is True
  if not all(isinstance(v,str) and 0<len(v)<=200 for v in [account,peer,event]) or not isinstance(text,str) or not 0<len(text.strip())<=1000:raise ValueError('Invalid input')
  scope=hashlib.sha256(json.dumps([test,account,peer]).encode()).hexdigest()
  event=hashlib.sha256(json.dumps([scope,event]).encode()).hexdigest()
  if self.commerce and not test:self.commerce.record_phone('wa:'+scope,data.get('phone'))
  with self.lock:
   with self.store.connect() as db:
    cached=db.execute('SELECT result FROM wa_replies WHERE event=?',(event,)).fetchone()
    if cached:
     result=json.loads(cached[0])
     return self.commerce.guard_cached_reply('wa:'+scope,result,'test' if test else 'whatsapp') if self.commerce else result
    row=db.execute('SELECT token FROM wa_sessions WHERE scope=?',(scope,)).fetchone()
   token=row[0] if row and self.store.valid(row[0]) else self.store.create()
   history=[{'role':m['role'],'content':m['content'][:2000]} for m in self.store.history(token)][-10:]
   recorded=False;handoff_requested=False
   if self.commerce and not test:
    with self.commerce.db.db() as db:
     if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wa_observed'").fetchone():recorded=bool(db.execute('SELECT 1 FROM wa_observed WHERE scope=? AND event=?',('wa:'+scope,data['event'])).fetchone())
     if recorded:handoff_requested=bool(db.execute("SELECT 1 FROM conversation_messages WHERE scope=? AND role='user' AND json_extract(payload,'$.cs_request_event')=?",('wa:'+scope,data['event'])).fetchone())
   result=self.commerce.respond('wa:'+scope,text,event,attachment=data.get('attachment'),use_model=not test,channel='test' if test else 'whatsapp',account=account,peer=peer,generate=data.get('generate',True),user_recorded=recorded,handoff_requested=handoff_requested) if self.commerce else self.catalog.chat(text,history,use_model=not test)
   with self.store.connect() as db:
    # Record the response and history together so a bridge retry never repeats generation.
    db.execute('INSERT OR REPLACE INTO wa_sessions VALUES (?,?)',(scope,token))
    db.execute('INSERT INTO wa_replies VALUES (?,?)',(event,json.dumps(result,ensure_ascii=False)))
    db.execute('INSERT INTO messages(session,role,content,payload) VALUES (?,?,?,?)',(self.store.key(token),'user',text,None))
    db.execute('INSERT INTO messages(session,role,content,payload) VALUES (?,?,?,?)',(self.store.key(token),'assistant',result['answer'],json.dumps(result,ensure_ascii=False)))
   return result
 def observe(self,data):
  account,peer,event,text=(data.get(k) for k in ['account','peer','event','text'])
  if not self.commerce or not all(isinstance(v,str) and 0<len(v)<=200 for v in [account,peer,event]) or not isinstance(text,str) or len(text)>1000:raise ValueError('Invalid input')
  scope=hashlib.sha256(json.dumps([False,account,peer]).encode()).hexdigest();scope='wa:'+scope
  self.commerce.ensure(scope,'whatsapp',account,peer)
  self.commerce.record_phone(scope,data.get('phone'))
  import time
  payload=None
  with self.commerce.db.db() as db:
   db.execute('CREATE TABLE IF NOT EXISTS wa_observed(scope TEXT,event TEXT,PRIMARY KEY(scope,event))')
   reply_event=hashlib.sha256(json.dumps([scope[3:],event]).encode()).hexdigest()
   if db.execute('SELECT 1 FROM conversation_events WHERE scope=? AND event=?',(scope,reply_event)).fetchone():return {'ok':True}
   if db.execute('SELECT 1 FROM wa_observed WHERE scope=? AND event=?',(scope,event)).fetchone():return {'ok':True}
  if data.get('attachment'):
   import base64
   evidence=self.commerce.db.save_evidence('whatsapp-image',data['attachment']['mime'],base64.b64decode(data['attachment']['data']),{'id':'chat-service','role':'support'})
   payload=json.dumps({'answer':text,'mode':'customer_attachment','evidence_id':evidence['id']})
  with self.commerce.db.db(True) as db:
   if db.execute('INSERT OR IGNORE INTO wa_observed VALUES (?,?)',(scope,event)).rowcount:
    if wants_cs(text) and self.commerce.request_handover(db,scope,'客人要求 CS／售後'):
     payload=json.dumps({'answer':text,'mode':'customer_attachment' if payload else 'customer',**(json.loads(payload) if payload else {}),'cs_request_event':event},ensure_ascii=False)
    db.execute('INSERT INTO conversation_messages(scope,role,content,payload,created) VALUES (?,?,?,?,?)',(scope,'user',text,payload,time.time()))
    db.execute('UPDATE conversations SET updated=? WHERE id=?',(time.time(),scope))
  return {'ok':True}
