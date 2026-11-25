"""Independent B2B sandbox ledger. No SHOPLINE dependencies or monetary side effects."""
import csv,hashlib,hmac,io,json,re,secrets,sqlite3,time
from contextlib import contextmanager
from datetime import datetime,timedelta,timezone
from decimal import Decimal
from pathlib import Path
HK=timezone(timedelta(hours=8))
class AdminError(Exception):
 def __init__(self,message,status=400):super().__init__(message);self.status=status

def text(value,name,limit=200,required=True):
 if not isinstance(value,str) or len(value.strip())>limit or (required and not value.strip()):raise AdminError(name+'格式不正確')
 return value.strip()
def money(value):
 if not isinstance(value,str) or not re.fullmatch(r'\d{1,9}(?:\.\d{1,2})?',value):raise AdminError('金額必須為非負數，最多兩位小數')
 return int(Decimal(value)*100)
def days(value):
 if value in [None,'']:return None
 if isinstance(value,bool) or not re.fullmatch(r'[1-9]\d{0,3}',str(value)):raise AdminError('有效期須為 1 至 9999 日，或留空表示不設到期')
 return int(value)
def password_hash(password,salt):return hashlib.pbkdf2_hmac('sha256',password.encode(),salt.encode(),600000).hex()
def digest(token):return hashlib.sha256(token.encode()).hexdigest()
class AdminDB:
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.db() as db:
   db.executescript('''
   CREATE TABLE IF NOT EXISTS staff(id TEXT PRIMARY KEY,username TEXT UNIQUE,name TEXT,role TEXT,salt TEXT,password TEXT,active INTEGER DEFAULT 1);
   CREATE TABLE IF NOT EXISTS staff_sessions(token TEXT PRIMARY KEY,staff TEXT,csrf TEXT,expires REAL);
   CREATE TABLE IF NOT EXISTS customers(id TEXT PRIMARY KEY,company TEXT,contact TEXT,phone TEXT,email TEXT,note TEXT,created REAL);
   CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY,name TEXT,version INTEGER,paid INTEGER,principal INTEGER,bonus INTEGER,principal_days INTEGER,bonus_days INTEGER,active INTEGER DEFAULT 1,created REAL);
   CREATE TABLE IF NOT EXISTS topups(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,customer TEXT REFERENCES customers(id),plan TEXT REFERENCES plans(id),snapshot TEXT,method TEXT,reference TEXT,evidence TEXT,evidence_id TEXT,status TEXT,created_by TEXT,reviewed_by TEXT,reason TEXT,created REAL,reviewed REAL);
   CREATE UNIQUE INDEX IF NOT EXISTS unique_payment_reference ON topups(method,reference) WHERE status!='rejected';
   CREATE TABLE IF NOT EXISTS lots(id TEXT PRIMARY KEY,topup TEXT REFERENCES topups(id),customer TEXT,kind TEXT,amount INTEGER CHECK(amount>=0),expires REAL,UNIQUE(topup,kind));
   CREATE TABLE IF NOT EXISTS ledger(id TEXT PRIMARY KEY,topup TEXT UNIQUE,customer TEXT,principal INTEGER,bonus INTEGER,actor TEXT,created REAL);
   CREATE TABLE IF NOT EXISTS verified_payments(method TEXT,reference TEXT,topup TEXT UNIQUE,PRIMARY KEY(method,reference));
   INSERT OR IGNORE INTO verified_payments SELECT method,reference,id FROM topups WHERE status='approved' AND reference NOT LIKE 'CHAT-%';
   CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,name TEXT,mime TEXT,body BLOB,actor TEXT,created REAL);
   CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,actor TEXT,action TEXT,target TEXT,detail TEXT,created REAL);
   CREATE TABLE IF NOT EXISTS review_notice_jobs(topup TEXT PRIMARY KEY,created REAL,processed INTEGER DEFAULT 0);
   CREATE TABLE IF NOT EXISTS review_notifications(id TEXT PRIMARY KEY,topup TEXT,scope TEXT,account TEXT,peer TEXT,message TEXT,status TEXT,attempts INTEGER DEFAULT 0,next_attempt REAL,message_id INTEGER,created REAL,updated REAL,UNIQUE(topup,scope));
   CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
   CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
   CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
   CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'immutable audit'); END;
   ''')
  with self.db(True) as db:
   if 'archived' not in [r['name'] for r in db.execute('PRAGMA table_info(plans)')]:db.execute('ALTER TABLE plans ADD COLUMN archived INTEGER NOT NULL DEFAULT 0')
  self.path.chmod(0o600)
 @contextmanager
 def db(self,write=False):
  db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row;db.execute('PRAGMA foreign_keys=ON')
  try:
   if write:db.execute('BEGIN IMMEDIATE')
   yield db;db.commit()
  except Exception:db.rollback();raise
  finally:db.close()
 def audit(self,db,actor,action,target,detail=None):db.execute('INSERT INTO audit(actor,action,target,detail,created) VALUES (?,?,?,?,?)',(actor,action,target,json.dumps(detail or {},ensure_ascii=False),time.time()))
 def require(self,user,*roles):
  if not user or user['role'] not in roles:raise AdminError('沒有此操作權限',403)
 def needs_setup(self):
  with self.db() as db:return not db.execute('SELECT 1 FROM staff LIMIT 1').fetchone()
 def create_staff(self,data,user=None,setup=False):
  if not setup:self.require(user,'admin')
  username=text(data.get('username'),'登入名稱',80).lower();name=text(data.get('name'),'員工名稱',80);password=data.get('password')
  if not isinstance(password,str) or not 8<=len(password)<=200:raise AdminError('密碼需要 8 至 200 個字元')
  role='admin' if setup else data.get('role')
  if role not in ['admin','finance','support']:raise AdminError('角色無效')
  salt=secrets.token_hex(16);hashed=password_hash(password,salt);uid=secrets.token_hex(12)
  with self.db(True) as db:
   if setup and db.execute('SELECT 1 FROM staff LIMIT 1').fetchone():raise AdminError('已建立管理員',409)
   try:db.execute('INSERT INTO staff VALUES (?,?,?,?,?,?,1)',(uid,username,name,role,salt,hashed))
   except sqlite3.IntegrityError:raise AdminError('登入名稱已存在',409)
   self.audit(db,uid if setup else user['id'],'staff.create',uid,{'role':role})
  return {'id':uid}
 def login(self,data):
  username=text(data.get('username'),'登入名稱',80).lower();password=data.get('password')
  if not isinstance(password,str) or len(password)>200:raise AdminError('登入失敗',401)
  with self.db(True) as db:
   row=db.execute('SELECT * FROM staff WHERE username=? AND active=1',(username,)).fetchone()
   actual=password_hash(password,row['salt'] if row else 'not-a-real-account')
   if not row or not hmac.compare_digest(actual,row['password']):raise AdminError('登入名稱或密碼不正確',401)
   token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
   db.execute('DELETE FROM staff_sessions WHERE expires<?',(time.time(),))
   db.execute('INSERT INTO staff_sessions VALUES (?,?,?,?)',(digest(token),row['id'],csrf,time.time()+28800))
   self.audit(db,row['id'],'staff.login',row['id'])
  return token
 def session(self,token):
  if not token:return None
  with self.db() as db:
   row=db.execute('SELECT s.id,s.name,s.username,s.role,t.csrf FROM staff_sessions t JOIN staff s ON s.id=t.staff WHERE t.token=? AND t.expires>? AND s.active=1',(digest(token),time.time())).fetchone()
   return dict(row) if row else None
 def logout(self,token):
  with self.db(True) as db:db.execute('DELETE FROM staff_sessions WHERE token=?',(digest(token),))
 def create_customer(self,data,user):
  self.require(user,'admin','finance','support');uid=secrets.token_hex(12)
  values=[text(data.get(k,''),label,limit,required) for k,label,limit,required in [('company','公司名稱',200,True),('contact','聯絡人',100,True),('phone','電話',40,False),('email','電郵',200,False),('note','備註',2000,False)]]
  with self.db(True) as db:
   db.execute('INSERT INTO customers VALUES (?,?,?,?,?,?,?)',(uid,*values,time.time()));self.audit(db,user['id'],'customer.create',uid)
  return {'id':uid}
 def create_plan(self,data,user):
  self.require(user,'admin','finance');name=text(data.get('name'),'方案名稱');paid=money(data.get('paid'));principal=money(data.get('principal'));bonus=money(data.get('bonus'))
  if paid<=0 or principal+bonus<=0:raise AdminError('實收及總額度須大於零')
  pd,bd=days(data.get('principal_days')),days(data.get('bonus_days'));uid=secrets.token_hex(12)
  with self.db(True) as db:
   version=db.execute('SELECT COALESCE(MAX(version),0)+1 FROM plans WHERE name=?',(name,)).fetchone()[0]
   db.execute('INSERT INTO plans(id,name,version,paid,principal,bonus,principal_days,bonus_days,active,created) VALUES (?,?,?,?,?,?,?,?,1,?)',(uid,name,version,paid,principal,bonus,pd,bd,time.time()));self.audit(db,user['id'],'plan.create',uid,{'version':version})
  return {'id':uid}
 def edit_plan(self,data,user):
  self.require(user,'admin','finance');name=text(data.get('name'),'方案名稱');paid=money(data.get('paid'));principal=money(data.get('principal'));bonus=money(data.get('bonus'))
  if paid<=0 or principal+bonus<=0:raise AdminError('實收及總額度須大於零')
  pd,bd=days(data.get('principal_days')),days(data.get('bonus_days'))
  with self.db(True) as db:
   row=db.execute('SELECT * FROM plans WHERE id=? AND archived=0',(data.get('id'),)).fetchone()
   if not row:raise AdminError('找不到方案',404)
   if data.get('version')!=row['version']:raise AdminError('方案已被修改，請重新整理後再編輯',409)
   db.execute('UPDATE plans SET name=?,paid=?,principal=?,bonus=?,principal_days=?,bonus_days=?,version=version+1 WHERE id=?',(name,paid,principal,bonus,pd,bd,row['id']))
   self.audit(db,user['id'],'plan.edit',row['id'],{'before':dict(row),'after':{'name':name,'paid':paid,'principal':principal,'bonus':bonus,'principal_days':pd,'bonus_days':bd}})
  return {'id':row['id']}
 def plan_state(self,data,user):
  self.require(user,'admin','finance');action=data.get('action')
  if action not in ['activate','deactivate','delete','restore']:raise AdminError('操作無效')
  with self.db(True) as db:
   row=db.execute('SELECT * FROM plans WHERE id=?',(data.get('id'),)).fetchone()
   if not row:raise AdminError('找不到方案',404)
   if action in ['activate','deactivate'] and row['archived']:raise AdminError('請先還原方案')
   archived=1 if action=='delete' else 0 if action=='restore' else row['archived'];active=1 if action=='activate' else 0
   db.execute('UPDATE plans SET archived=?,active=? WHERE id=?',(archived,active,row['id']));self.audit(db,user['id'],'plan.'+action,row['id'])
  return {'ok':True}
 def deactivate_plan(self,data,user):return self.plan_state(dict(data,action='deactivate'),user)
 def create_topup(self,data,user,trusted_snapshot=None):
  self.require(user,'admin','finance','support')
  key=text(data.get('request_key'),'請求編號',100);method=data.get('method')
  if method not in ['bank','payme','alipay','cash','card']:raise AdminError('付款方式無效')
  reference=text(data.get('reference'),'付款流水／收據編號',120)
  note=text(data.get('evidence'),'核款說明',2000);attachment=data.get('evidence_id') or None
  with self.db(True) as db:
   existing=db.execute('SELECT * FROM topups WHERE request_key=?',(key,)).fetchone()
   if existing:
    if any(existing[k]!=data.get(k) for k in ['customer','plan','method','reference','evidence']) or existing['evidence_id']!=attachment:raise AdminError('請求編號已用於另一申請',409)
    return {'id':existing['id'],'duplicate':True}
   customer=db.execute('SELECT id FROM customers WHERE id=?',(data.get('customer'),)).fetchone()
   plan=db.execute('SELECT * FROM plans WHERE id=?'+('' if trusted_snapshot else ' AND active=1 AND archived=0'),(data.get('plan'),)).fetchone()
   if trusted_snapshot:
    custom=trusted_snapshot.get('kind')=='custom_amount' and data.get('plan') is None and trusted_snapshot.get('id') is None
    if custom:
     amount=trusted_snapshot.get('paid')
     if type(amount) is not int or not 0<amount<=99999999999 or trusted_snapshot.get('principal')!=amount or trusted_snapshot.get('bonus')!=0 or any(trusted_snapshot.get(k) is not None for k in ['principal_days','bonus_days']):raise AdminError('充值金額資料無效')
    elif not plan or trusted_snapshot.get('id')!=plan['id']:raise AdminError('充值方案資料無效')
    plan=trusted_snapshot
   if not customer or not plan:raise AdminError('客戶或方案無效')
   if attachment and not db.execute('SELECT 1 FROM evidence WHERE id=?',(attachment,)).fetchone():raise AdminError('附件不存在')
   uid=secrets.token_hex(12)
   try:db.execute('INSERT INTO topups VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(uid,key,customer['id'],plan['id'],json.dumps(dict(plan)),method,reference,note,attachment,'pending',user['id'],None,None,time.time(),None))
   except sqlite3.IntegrityError:raise AdminError('此付款流水／收據編號已有申請，請先核對',409)
   self.audit(db,user['id'],'topup.submit',uid)
  return {'id':uid}
 def review(self,data,user):
  self.require(user,'admin','finance');decision=data.get('decision');reason=text(data.get('reason'),'核款備註',2000)
  if decision not in ['approved','rejected']:raise AdminError('審批結果無效')
  if decision=='approved' and data.get('payment_verified') is not True:raise AdminError('須先核實實際收款')
  with self.db(True) as db:
   row=db.execute('SELECT * FROM topups WHERE id=?',(data.get('id'),)).fetchone()
   if not row:raise AdminError('找不到申請',404)
   if row['status']==decision:return {'id':row['id'],'duplicate':True}
   if row['status']!='pending':raise AdminError('申請已處理，不能更改結果',409)
   plan=json.loads(row['snapshot']);now=time.time()
   if decision=='approved':
    if money(data.get('verified_paid'))!=plan['paid']:raise AdminError('實收金額與方案不符')
    verified_reference=text(data.get('verified_reference'),'收款平台實際交易編號',120) if row['reference'].startswith('CHAT-') else row['reference']
    try:db.execute('INSERT INTO verified_payments VALUES (?,?,?)',(row['method'],verified_reference,row['id']))
    except sqlite3.IntegrityError:raise AdminError('此收款平台交易編號已核款入帳，請勿重複批准',409)
    for kind in ['principal','bonus']:
     count=plan[kind+'_days'];expiry=(datetime.fromtimestamp(now,HK)+timedelta(days=count)).timestamp() if count else None
     db.execute('INSERT INTO lots VALUES (?,?,?,?,?,?)',(secrets.token_hex(12),row['id'],row['customer'],kind,plan[kind],expiry))
    db.execute('INSERT INTO ledger VALUES (?,?,?,?,?,?,?)',(secrets.token_hex(12),row['id'],row['customer'],plan['principal'],plan['bonus'],user['id'],now))
   db.execute('UPDATE topups SET status=?,reviewed_by=?,reason=?,reviewed=? WHERE id=?',(decision,user['id'],reason,now,row['id']))
   db.execute('INSERT OR IGNORE INTO review_notice_jobs(topup,created) VALUES (?,?)',(row['id'],now))
   self.audit(db,user['id'],'topup.'+decision,row['id'],{'reason':reason,'principal':plan['principal'] if decision=='approved' else 0,'bonus':plan['bonus'] if decision=='approved' else 0})
  return {'id':row['id'],'duplicate':False}
 def save_evidence(self,name,mime,body,user):
  self.require(user,'admin','finance','support');name=text(name,'檔名',200)
  if not body or len(body)>5*1024*1024:raise AdminError('附件上限為 5 MB')
  signatures={'image/png':b'\x89PNG\r\n\x1a\n','image/jpeg':b'\xff\xd8\xff','application/pdf':b'%PDF-'}
  if mime not in signatures or not body.startswith(signatures[mime]):raise AdminError('只接受有效 PNG、JPEG 或 PDF')
  uid=secrets.token_hex(12)
  with self.db(True) as db:
   db.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?)',(uid,name,mime,body,user['id'],time.time()));self.audit(db,user['id'],'evidence.upload',uid)
  return {'id':uid}
 def get_evidence(self,uid,user):
  self.require(user,'admin','finance','support')
  with self.db() as db:
   row=db.execute('SELECT * FROM evidence WHERE id=?',(uid,)).fetchone()
   if not row:raise AdminError('附件不存在',404)
   return dict(row)
 def snapshot(self,user):
  self.require(user,'admin','finance','support')
  with self.db() as db:
   customers=[dict(x) for x in db.execute('SELECT * FROM customers ORDER BY created DESC')]
   spent="-COALESCE((SELECT SUM(s.amount) FROM wallet_spends s WHERE s.lot=l.id),0)" if db.execute("SELECT 1 FROM sqlite_master WHERE name='wallet_spends'").fetchone() else ''
   for c in customers:
    amounts=db.execute('SELECT kind,SUM(amount'+spent+') AS amount FROM lots l WHERE customer=? AND (expires IS NULL OR expires>?) GROUP BY kind',(c['id'],time.time())).fetchall()
    c.update({'principal':0,'bonus':0});c.update({x['kind']:x['amount'] for x in amounts});c['balance']=c['principal']+c['bonus']
   plans=[dict(x) for x in db.execute('SELECT * FROM plans ORDER BY created DESC')]
   topups=[dict(x) for x in db.execute('SELECT t.*,c.company,c.phone,e.mime AS evidence_mime FROM topups t JOIN customers c ON c.id=t.customer LEFT JOIN evidence e ON e.id=t.evidence_id ORDER BY t.created DESC')]
   for t in topups:
    t['snapshot']=json.loads(t['snapshot'])
    t['notifications']=[r['status'] for r in db.execute('SELECT status FROM review_notifications WHERE topup=?',(t['id'],))]
    job=db.execute('SELECT processed FROM review_notice_jobs WHERE topup=?',(t['id'],)).fetchone()
    t['notification_job']=('processed' if job['processed'] else 'pending') if job else None
   ledger=[dict(x) for x in db.execute('SELECT l.*,c.company,t.reference,t.method FROM ledger l JOIN customers c ON c.id=l.customer JOIN topups t ON t.id=l.topup ORDER BY l.created DESC')]
   audit=[dict(x) for x in db.execute('SELECT a.*,s.name AS actor_name FROM audit a LEFT JOIN staff s ON s.id=a.actor ORDER BY a.id DESC LIMIT 200')] if user['role'] in ['admin','finance'] else []
   staff=[dict(x) for x in db.execute('SELECT id,username,name,role,active FROM staff')] if user['role']=='admin' else []
  return {'customers':customers,'plans':plans,'topups':topups,'ledger':ledger,'audit':audit,'staff':staff,'sandbox':True}
 def export_csv(self,user):
  self.require(user,'admin','finance');snapshot=self.snapshot(user);out=io.StringIO();writer=csv.writer(out);writer.writerow(['入帳編號','公司','充值申請','付款參考','本金 HKD','贈額 HKD','時間（香港）'])
  for r in snapshot['ledger']:
   safe=lambda v:"'"+str(v) if str(v).lstrip().startswith(('=','+','-','@','\t','\r')) else str(v)
   writer.writerow([safe(r['id']),safe(r['company']),safe(r['topup']),safe(r['reference']),f"{r['principal']/100:.2f}",f"{r['bonus']/100:.2f}",datetime.fromtimestamp(r['created'],HK).isoformat()])
  return ('\ufeff'+out.getvalue()).encode()
