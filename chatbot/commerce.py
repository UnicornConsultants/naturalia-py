"""Deterministic B2B wallet/chat workflow. The model cannot authorize money movement."""
import base64,hashlib,json,re,secrets,time,unicodedata
from admin_domain import AdminError,money,text
from chat_inputs import product_command,recharge_input
from purchase_flow import PurchaseFlow
from purchase_inputs import quantity as purchase_quantity
from shopping_budget import shopping_budget,within_budget
from dialogue import DialogueFlow,wants_cs,previous_dialogue
WELCOME='你好，歡迎嚟到 Naturalia！🌿 我可以幫你按功效、品牌或預算搵產品，亦可以協助充值同以充值額購物。想先搵產品，定辦理充值？'
SYSTEM={'id':'chat-service','role':'support'}
def phone(value):
 value=re.sub(r'[\s()+-]','',value or '')
 if re.fullmatch(r'\d{8}',value):value='852'+value
 return value if re.fullmatch(r'[1-9]\d{7,14}',value) else ''
def hk(cents):return 'HK$'+format(cents/100,',.2f')
class Commerce(DialogueFlow,PurchaseFlow):
 def __init__(self,db,catalog):
  self.db=db;self.catalog=catalog
  with db.db() as c:c.executescript('''
  CREATE TABLE IF NOT EXISTS conversation_contacts(scope TEXT PRIMARY KEY,phone TEXT);
  CREATE TABLE IF NOT EXISTS conversation_reads(scope TEXT,staff TEXT,last_id INTEGER,PRIMARY KEY(scope,staff));
  CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,channel TEXT,account TEXT,peer TEXT,customer TEXT,status TEXT DEFAULT 'ai',reason TEXT,created REAL,updated REAL);
  CREATE TABLE IF NOT EXISTS conversation_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT,role TEXT,content TEXT,payload TEXT,created REAL);
  CREATE TABLE IF NOT EXISTS conversation_events(scope TEXT,event TEXT,result TEXT,PRIMARY KEY(scope,event));
  CREATE TABLE IF NOT EXISTS chat_choices(scope TEXT,kind TEXT,number INTEGER,target TEXT,expires REAL,PRIMARY KEY(scope,kind,number),UNIQUE(scope,kind,target));
  CREATE TABLE IF NOT EXISTS product_codes(product TEXT PRIMARY KEY,code TEXT UNIQUE NOT NULL);
  CREATE TABLE IF NOT EXISTS payment_intents(id TEXT PRIMARY KEY,scope TEXT,customer TEXT,plan TEXT,method TEXT,snapshot TEXT,topup TEXT,created REAL);
  CREATE TABLE IF NOT EXISTS topup_drafts(scope TEXT PRIMARY KEY,amount INTEGER,evidence TEXT,updated REAL);
  CREATE TABLE IF NOT EXISTS receipt_hashes(digest TEXT PRIMARY KEY,intent TEXT);
  CREATE TABLE IF NOT EXISTS b2b_prices(product TEXT PRIMARY KEY,cents INTEGER,active INTEGER,updated REAL);
  CREATE TABLE IF NOT EXISTS purchase_quotes(id TEXT PRIMARY KEY,scope TEXT,customer TEXT,product TEXT,title TEXT,amount INTEGER,expires REAL,order_id TEXT);
  CREATE TABLE IF NOT EXISTS purchase_context(scope TEXT PRIMARY KEY,product TEXT,quantity INTEGER,quote TEXT,phase TEXT,updated REAL);
  CREATE TABLE IF NOT EXISTS wallet_orders(id TEXT PRIMARY KEY,customer TEXT,scope TEXT,product TEXT,title TEXT,amount INTEGER,balance_after INTEGER,status TEXT,created REAL);
  CREATE TABLE IF NOT EXISTS wallet_spends(id INTEGER PRIMARY KEY,order_id TEXT REFERENCES wallet_orders(id),lot TEXT REFERENCES lots(id),amount INTEGER CHECK(amount>0));
  CREATE TABLE IF NOT EXISTS cs_replies(id TEXT PRIMARY KEY,scope TEXT,staff TEXT,text TEXT,status TEXT,created REAL);
  CREATE TRIGGER IF NOT EXISTS spends_no_update BEFORE UPDATE ON wallet_spends BEGIN SELECT RAISE(ABORT,'immutable spending'); END;
  CREATE TRIGGER IF NOT EXISTS spends_no_delete BEFORE DELETE ON wallet_spends BEGIN SELECT RAISE(ABORT,'immutable spending'); END;
  ''')
  with db.db(True) as c:
   for table in ['purchase_quotes','wallet_orders']:
    columns=[r['name'] for r in c.execute('PRAGMA table_info('+table+')')]
    if 'quantity' not in columns:c.execute('ALTER TABLE '+table+' ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1')
    if 'unit_price' not in columns:
     c.execute('ALTER TABLE '+table+' ADD COLUMN unit_price INTEGER NOT NULL DEFAULT 0')
     c.execute('UPDATE '+table+' SET unit_price=amount')
   for column in ['method','intent']:
    if column not in [r['name'] for r in c.execute('PRAGMA table_info(topup_drafts)')]:c.execute('ALTER TABLE topup_drafts ADD COLUMN '+column+' TEXT')
   # Read cursors must never overtake reused IDs after retention cleanup or a chat reset.
   schema=c.execute("SELECT sql FROM sqlite_master WHERE name='conversation_messages'").fetchone()[0]
   if 'AUTOINCREMENT' not in schema.upper():
    c.execute('ALTER TABLE conversation_messages RENAME TO conversation_messages_legacy')
    c.execute('CREATE TABLE conversation_messages(id INTEGER PRIMARY KEY AUTOINCREMENT,scope TEXT,role TEXT,content TEXT,payload TEXT,created REAL)')
    c.execute('INSERT INTO conversation_messages SELECT * FROM conversation_messages_legacy')
    c.execute('DROP TABLE conversation_messages_legacy')
   last=c.execute('SELECT COALESCE(MAX(last_id),0) FROM conversation_reads').fetchone()[0]
   if not c.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='conversation_messages'",(last,)).rowcount:c.execute("INSERT INTO sqlite_sequence(name,seq) VALUES ('conversation_messages',?)",(last,))
   c.execute('CREATE INDEX IF NOT EXISTS conversation_messages_scope_id ON conversation_messages(scope,id)')
   c.execute("UPDATE cs_replies SET status='uncertain' WHERE status='sending'")
 def balance(self,c,customer):
  return c.execute('SELECT COALESCE(SUM(amount-COALESCE((SELECT SUM(s.amount) FROM wallet_spends s WHERE s.lot=l.id),0)),0) FROM lots l WHERE customer=? AND (expires IS NULL OR expires>?)',(customer,time.time())).fetchone()[0]
 def ensure(self,scope,channel='web',account='',peer='',customer=None):
  with self.db.db(True) as c:
   if not c.execute('SELECT 1 FROM conversations WHERE id=?',(scope,)).fetchone():
    if channel=='whatsapp':customer=None
    c.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?)',(scope,channel,account,peer,customer,'ai',None,time.time(),time.time()))
   self.match_phone(c,scope)
   return dict(c.execute('SELECT * FROM conversations WHERE id=?',(scope,)).fetchone())
 def match_phone(self,c,scope):
  conv=c.execute('SELECT * FROM conversations WHERE id=?',(scope,)).fetchone()
  if conv['channel']!='whatsapp' or conv['customer']:return
  # Only transport-authenticated PN JIDs or its saved LID-to-PN mapping are identity evidence.
  trusted=self.transport_phone(c,conv)
  if not trusted:return
  matches=[r['id'] for r in c.execute('SELECT id,phone FROM customers') if phone(r['phone'])==trusted]
  if not matches:
   uid=secrets.token_hex(12)
   c.execute('INSERT INTO customers VALUES (?,?,?,?,?,?,?)',(uid,'WhatsApp 客戶 +'+trusted,'WhatsApp 客戶',trusted,'','由 WhatsApp 可信電話自動開戶',time.time()))
   self.db.audit(c,'whatsapp-transport','customer.auto_create',uid,{'source':'trusted_whatsapp_phone','scope':scope,'opening_balance':0})
   matches=[uid]
  if len(matches)!=1:return
  resume=conv['status']=='waiting' and conv['reason']=='請核實電話並綁定 B2B 帳戶'
  c.execute('UPDATE conversations SET customer=?,status=?,reason=? WHERE id=?',(matches[0],'ai' if resume else conv['status'],None if resume else conv['reason'],scope))
  self.db.audit(c,'whatsapp-transport','chat.phone_match',scope,{'customer':matches[0],'source':'trusted_whatsapp_phone','resumed_identity_wait':resume})
 def transport_phone(self,c,conv):
  contact=c.execute('SELECT phone FROM conversation_contacts WHERE scope=?',(conv['id'],)).fetchone()
  return phone(conv['peer'].split('@')[0]) if re.fullmatch(r'\d+@s\.whatsapp\.net',conv['peer']) else phone(contact['phone']) if contact else ''
 def missing_account_message(self,conv):
  if conv['channel']!='whatsapp':return '此對話尚未綁定已核實的 B2B 帳戶，'
  with self.db.db() as c:
   trusted=self.transport_phone(c,conv)
   if not trusted:return 'WhatsApp 暫未提供可確認的電話資料，'
   matches=[r['id'] for r in c.execute('SELECT id,phone FROM customers') if phone(r['phone'])==trusted]
  return '已按你的 WhatsApp 電話查詢，後台未有對應的 B2B 帳戶，' if not matches else '後台有多個 B2B 帳戶使用同一電話，需要先確認使用哪個帳戶，'
 def history(self,scope):
  with self.db.db(True) as c:c.execute('DELETE FROM conversation_messages WHERE created<?',(time.time()-7*86400,))
  with self.db.db() as c:
   rows=c.execute('SELECT * FROM conversation_messages WHERE scope=? ORDER BY id DESC LIMIT 100',(scope,)).fetchall()
   state=c.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
  return {'messages':[{'id':r['id'],'role':r['role'],'content':r['content'],'payload':json.loads(r['payload']) if r['payload'] else None} for r in reversed(rows)],'status':state['status'] if state else 'ai','welcome':WELCOME}
 def result(self,answer,**kwargs):return {'answer':answer,'products':[],'mode':'workflow',**kwargs}
 def shortcuts(self,scope,question):
  action,result=self.checkout_input(scope,question)
  if action is not None:return action,result
  q=unicodedata.normalize('NFKC',question).strip().strip('「」『』“”"\'')
  decorated=product_command(question)
  if decorated:
   verb,code=decorated
   with self.db.db() as c:known=c.execute('SELECT 1 FROM product_codes WHERE code=?',(code.upper(),)).fetchone()
   if known or code.isupper() or any(ch.isdigit() for ch in code):q=verb+' '+code
  elif re.fullmatch(r'(?:買\s*)?[A-Za-z][A-Za-z0-9]{4,5}[呀啊啦!！。\s]*',q):
   # A mistyped code is not a new product search; ask for the exact four characters.
   candidate=re.sub(r'^買\s*','',q)[:4].upper()
   with self.db.db() as c:known=c.execute('SELECT 1 FROM product_codes WHERE code=?',(candidate,)).fetchone()
   if known:return {},self.purchase_help(scope,'商品碼應為 4 個字元。你係咪想選「'+candidate+'」？請回覆正確商品碼，今次未有扣款。')
  bare=re.fullmatch(r'[A-Za-z][A-Za-z0-9]{3}',q)
  if bare:
   with self.db.db() as c:known=c.execute('SELECT 1 FROM product_codes WHERE code=?',(q.upper(),)).fetchone()
   # Known codes also accept lowercase. Ordinary words such as "mask" still reach search.
   if known or q.isupper() or any(ch.isdigit() for ch in q):q='買 '+q
  buy=re.fullmatch(r'(買|买|購買|以充值額購買|詳情|详情|有意購買)\s*([A-Za-z][A-Za-z0-9]{3}|\d{1,6})',q)
  topup=re.fullmatch(r'充值方案\s*(\d{1,6})\s*(payme|alipay|支付寶|支付宝)',q,re.I)
  legacy=re.fullmatch(r'充值\s*(\d{1,6})\s*(payme|alipay|支付寶|支付宝)',q,re.I)
  if legacy:
   with self.db.db() as c:old=c.execute("SELECT 1 FROM chat_choices WHERE scope=? AND kind='plan' AND number=?",(scope,int(legacy[1]))).fetchone()
   if old:return {},self.options(None,scope)
  confirm=re.fullmatch(r'(?:確認購買|确认购买|確認|确认)\s*(\d{1,6})',q)
  if not any([buy,topup,confirm]):return {},None
  kind='product' if buy else 'plan' if topup else 'quote'
  value=buy[2].upper() if buy else topup[1] if topup else confirm[1]
  with self.db.db() as c:
   if buy and not value.isdigit():choice=c.execute("SELECT v.target FROM chat_choices v JOIN product_codes p ON p.product=v.target WHERE v.scope=? AND v.kind='product' AND p.code=? AND v.expires>?",(scope,value,time.time())).fetchone()
   else:choice=c.execute('SELECT target FROM chat_choices WHERE scope=? AND kind=? AND number=? AND expires>?',(scope,kind,int(value),time.time())).fetchone()
   if choice and kind=='plan' and not c.execute('SELECT 1 FROM plans WHERE id=? AND active=1',(choice['target'],)).fetchone():return {},self.result('呢個充值方案已停用，請回覆「充值」輸入充值金額。')
  if not choice:return {},self.purchase_help(scope,'搵唔到呢個選項，或者選項已過期，未有扣款。')
  if buy:return {'type':'detail' if buy[1] in ['詳情','详情','有意購買'] else 'buy','product':choice['target']},None
  if topup:return {'type':'topup','plan':choice['target'],'method':'payme' if topup[2].lower()=='payme' else 'alipay'},None
  return {'type':'confirm','quote':choice['target']},None
 def add_shortcuts(self,scope,result):
  def number(c,kind,target):
   row=c.execute('SELECT number FROM chat_choices WHERE scope=? AND kind=? AND target=?',(scope,kind,target)).fetchone()
   if row:
    c.execute('UPDATE chat_choices SET expires=? WHERE scope=? AND kind=? AND target=?',(time.time()+7*86400,scope,kind,target));return row['number']
   n=c.execute('SELECT COALESCE(MAX(number),0)+1 FROM chat_choices WHERE scope=? AND kind=?',(scope,kind)).fetchone()[0]
   c.execute('INSERT INTO chat_choices VALUES (?,?,?,?,?)',(scope,kind,n,target,time.time()+7*86400));return n
  with self.db.db(True) as c:
   result['products']=[dict(p) for p in result.get('products',[])[:3]]
   shown=[]
   for p in result['products']:
    price=self.effective_price(c,p['id'])
    if result.get('budget') and (not price or not within_budget(price['cents'],result['budget'])):continue
    if price:p['price']=hk(price['cents'])
    shown.append(p)
    number(c,'product',p['id'])
    row=c.execute('SELECT code FROM product_codes WHERE product=?',(p['id'],)).fetchone()
    if row:p['choice']=row['code']
    else:
     while True:
      code=secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')+''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(3))
      if not c.execute('SELECT 1 FROM product_codes WHERE code=?',(code,)).fetchone():break
     c.execute('INSERT INTO product_codes VALUES (?,?)',(p['id'],code));p['choice']=code
   if result.get('budget') and result['products'] and not shown:result['answer']='商品售價已更新，暫時未找到符合今次預算的商品。你想提高預算，定改搵其他產品類型？'
   result['products']=shown
   for p in result.get('topup_plans',[]):p['choice']=number(c,'plan',p['id'])
   if result.get('quote'):result['quote']['choice']=number(c,'quote',result['quote']['id'])
  return result
 def request_handover(self,c,scope,reason):
  changed=c.execute("UPDATE conversations SET status='waiting',reason=?,updated=? WHERE id=? AND status='ai'",(reason,time.time(),scope)).rowcount
  if changed:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
  return bool(changed)
 def handover_notice(self):
  return self.result('呢個問題需要 CS 跟進，我已通知客服並暫停 AI 回覆。你可以繼續留言；CS 會喺呢個對話回覆你。',handoff=True)
 def held_reply(self,channel):
  return self.result('訊息已收到，會由 CS 在此對話跟進；客服交回 AI 後才會恢復自動導購。',handoff=True,suppress_delivery=channel=='whatsapp')
 def guard_cached_reply(self,scope,result,channel):
  with self.db.db() as c:row=c.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
  if row and row['status']!='ai' and (row['status']!='waiting' or not result.get('handoff')):return self.held_reply(channel)
  return result
 def handover(self,scope,reason):
  with self.db.db(True) as c:changed=self.request_handover(c,scope,reason)
  return self.handover_notice() if changed else self.held_reply('whatsapp')
 def bind(self,data,user):
  self.db.require(user,'admin','support')
  if data.get('verified') is not True:raise AdminError('請先透過可信渠道核實電話及帳戶擁有人')
  with self.db.db(True) as c:
   row=c.execute('SELECT * FROM customers WHERE id=?',(data.get('customer'),)).fetchone()
   if not row or not phone(row['phone']):raise AdminError('請先建立有效電話的 B2B 客戶')
   if not c.execute('UPDATE conversations SET customer=?,updated=? WHERE id=?',(row['id'],time.time(),data.get('scope'))).rowcount:raise AdminError('找不到對話',404)
   self.db.audit(c,user['id'],'chat.bind',data['scope'],{'customer':row['id'],'phone_verified':True})
  return {'ok':True}
 def state(self,data,user):
  self.db.require(user,'admin','support');status=data.get('status')
  if status not in ['human','ai']:raise AdminError('狀態無效')
  with self.db.db(True) as c:
   if not c.execute('UPDATE conversations SET status=?,updated=? WHERE id=?',(status,time.time(),data.get('scope'))).rowcount:raise AdminError('找不到對話',404)
   if status=='human':c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(data.get('scope'),))
   self.db.audit(c,user['id'],'chat.'+status,data['scope'])
  return {'ok':True}
 def snapshot(self,user):
  self.db.require(user,'admin','finance','support')
  with self.db.db() as c:
   chats=[dict(r) for r in c.execute('SELECT v.*,u.company,u.phone FROM conversations v LEFT JOIN customers u ON u.id=v.customer ORDER BY updated DESC')] if user['role']!='finance' else []
   for conv in chats:
    contact=c.execute('SELECT phone FROM conversation_contacts WHERE scope=?',(conv['id'],)).fetchone()
    conv['contact_phone']=contact['phone'] if contact else (phone(conv['peer'].split('@')[0]) if conv['channel']=='whatsapp' and conv['peer'].endswith('@s.whatsapp.net') else '')
    cursor=c.execute('SELECT last_id FROM conversation_reads WHERE scope=? AND staff=?',(conv['id'],user.get('id',''))).fetchone()
    conv['unread_count']=c.execute("SELECT COUNT(*) FROM conversation_messages WHERE scope=? AND role='user' AND id>?",(conv['id'],cursor['last_id'] if cursor else 0)).fetchone()[0]
    activity=c.execute('SELECT MAX(created) FROM conversation_messages WHERE scope=?',(conv['id'],)).fetchone()[0];conv['last_message_at']=activity or conv['created']
    preview=c.execute('SELECT content FROM conversation_messages WHERE scope=? ORDER BY id DESC LIMIT 1',(conv['id'],)).fetchone();conv['preview']=preview['content'][:100] if preview else ''
   orders=[dict(r) for r in c.execute('SELECT o.*,u.company,u.phone FROM wallet_orders o JOIN customers u ON u.id=o.customer ORDER BY created DESC LIMIT 100')]
   prices=[dict(r) for r in c.execute('SELECT * FROM b2b_prices')]
   pending_items=[dict(r) for r in c.execute("SELECT t.id,t.created,c.company,c.phone FROM topups t JOIN customers c ON c.id=t.customer WHERE t.status='pending' ORDER BY t.created DESC")]
   pending=len(pending_items)
   waiting_items=[dict(r) for r in c.execute("SELECT v.id,v.channel,v.peer,v.reason,v.updated,c.company,c.phone FROM conversations v LEFT JOIN customers c ON c.id=v.customer WHERE v.status='waiting' ORDER BY v.updated DESC")] if user['role'] in ['admin','support'] else []
   waiting=len(waiting_items)
  chats.sort(key=lambda v:v['last_message_at'],reverse=True)
  return {'cs_attention_count':sum(1 for v in chats if v['unread_count'] or v['status']=='waiting'),'conversations':chats,'orders':orders,'prices':prices,'pending_topups':pending,'pending_topup_items':pending_items,'waiting_cs':waiting,'waiting_cs_items':waiting_items,'products':[{'id':p['id'],'title':p['title']} for p in self.catalog.items]}
 def price(self,data,user):
  self.db.require(user,'admin','finance');product=data.get('product');amount=money(data.get('price'))
  if amount<=0 or not any(p['id']==product for p in self.catalog.items):raise AdminError('商品或售價無效')
  with self.db.db(True) as c:
   c.execute('INSERT OR REPLACE INTO b2b_prices VALUES (?,?,?,?)',(product,amount,int(data.get('active') is True),time.time()));self.db.audit(c,user['id'],'price.set',product,{'cents':amount,'active':data.get('active') is True})
  return {'ok':True}
 def draft(self,scope,**changes):
  with self.db.db(True) as c:
   row=c.execute('SELECT * FROM topup_drafts WHERE scope=? AND updated>?',(scope,time.time()-86400,)).fetchone()
   value=dict(row) if row else {'scope':scope,'amount':None,'evidence':None,'method':None,'intent':None,'updated':time.time()}
   value.update(changes);value['updated']=time.time()
   c.execute('INSERT OR REPLACE INTO topup_drafts(scope,amount,evidence,method,intent,updated) VALUES (?,?,?,?,?,?)',tuple(value[k] for k in ['scope','amount','evidence','method','intent','updated']))
  return value
 def options(self,customer,scope=None):
  if scope:
   self.draft(scope)
   if customer:return self.custom_topup(scope,customer,{'type':'topup_continue'})
  return self.result('你想充值幾多港幣？直接回覆金額，例如「500」。',topup_amount_input=True)
 def topup_status(self,row):
  if row['status']=='pending':return self.result('此充值申請已提交，正在等候核款，唔需要再次過數或重傳截圖。申請編號：'+row['id']+'。審核完成會主動通知你。',topup_status='pending',topup_id=row['id'])
  if row['status']=='rejected':return self.result('上次充值申請未獲批准，原因：'+row['reason']+'。你可以直接傳正確的過數截圖重新申請，再補充金額及付款方式；未有增加充值額。',topup_status='rejected',topup_id=row['id'])
  return self.result('此充值申請已核款入帳，唔需要再次提交。申請編號：'+row['id']+'。回覆「餘額」可查目前可用充值額。',topup_status='approved',topup_id=row['id'])
 def custom_topup(self,scope,customer,action):
  kind=action['type'];now=time.time()
  with self.db.db() as c:
   active=c.execute('SELECT * FROM topup_drafts WHERE scope=? AND updated>?',(scope,now-86400,)).fetchone()
   latest=c.execute('SELECT i.*,t.status,t.reason FROM payment_intents i LEFT JOIN topups t ON t.id=i.topup WHERE i.scope=? ORDER BY i.created DESC LIMIT 1',(scope,)).fetchone()
  if kind in ['cancel_topup','restart_topup']:
   with self.db.db(True) as c:
    c.execute('DELETE FROM topup_drafts WHERE scope=?',(scope,))
    c.execute('UPDATE payment_intents SET created=0 WHERE scope=? AND topup IS NULL',(scope,))
   return self.options(customer,scope) if kind=='restart_topup' else self.result('已取消今次尚未提交的充值操作，未有新增充值額。')
  if kind=='topup_progress' and not active and latest and latest['topup']:
   return self.topup_status({'id':latest['topup'],'status':latest['status'],'reason':latest['reason']})
  if not active and latest and not latest['topup'] and latest['created']>now-86400 and kind in ['topup_progress','topup_method']:
   active=self.draft(scope,amount=json.loads(latest['snapshot'])['paid'],method=latest['method'],intent=latest['id'])
  if kind=='topup_invalid':
   self.draft(scope)
   return self.result('請提供一個大於 0 的港幣金額，最多兩位小數，例如「500」或「500.50」。',topup_amount_input=True)
  changes={}
  if kind=='topup_amount':
   try:amount=money(action.get('amount'))
   except AdminError:amount=0
   if amount<=0:return self.custom_topup(scope,customer,{'type':'topup_invalid'})
   changes.update(amount=amount)
   if not active or active['amount']!=amount:changes['intent']=None
  if action.get('method') in ['payme','alipay']:
   changes['method']=action['method']
   if not active or active['method']!=action['method']:changes['intent']=None
  draft=self.draft(scope,**changes);amount=draft['amount'];method=draft['method'];proof=bool(draft['evidence'])
  label={'payme':'PayMe','alipay':'Alipay'}.get(method)
  if not amount:
   prefix=('已保留你傳來的過數截圖，唔使再傳。' if proof else '')+('已記低付款方式係 '+label+'。' if label else '')
   return self.result(prefix+'你想充值幾多港幣？直接回覆金額，例如「500」。',topup_amount_input=True)
  if not label:
   options=[{'method':m,'amount':amount} for m in ['payme','alipay']]
   if not proof and kind!='topup_progress':
    for option in options:option['qr']='/payment-qr/'+option['method']
   answer=('收到過數截圖，唔使再傳截圖。' if proof else '')+'充值金額：'+hk(amount)+'。你用 PayMe 定 Alipay 過數？回覆付款方式'+('後會自動提交核款。' if proof else '，付款後請傳過數截圖；核款後才入帳。')
   return self.result(answer,topup_methods=options)
  with self.db.db(True) as c:
   intent=c.execute('SELECT * FROM payment_intents WHERE id=? AND scope=? AND customer=?',(draft['intent'],scope,customer)).fetchone()
   if not intent:
    uid=secrets.token_hex(12);snapshot={'id':None,'kind':'custom_amount','name':'自訂金額充值','version':1,'paid':amount,'principal':amount,'bonus':0,'principal_days':None,'bonus_days':None}
    c.execute('INSERT INTO payment_intents VALUES (?,?,?,?,?,?,NULL,?)',(uid,scope,customer,None,method,json.dumps(snapshot),now))
    c.execute('UPDATE topup_drafts SET intent=? WHERE scope=?',(uid,scope))
   else:uid=intent['id']
   evidence=c.execute('SELECT mime,body FROM evidence WHERE id=?',(draft['evidence'],)).fetchone() if proof else None
  if evidence:
   result=self.receipt(scope,uid,{'mime':evidence['mime'],'data':base64.b64encode(evidence['body']).decode()})
   if result.get('topup_status') in ['pending','approved']:
    with self.db.db(True) as c:c.execute('DELETE FROM topup_drafts WHERE scope=? AND intent=?',(scope,uid))
   return result
  if kind=='topup_progress':return self.result('已記低 '+label+' 充值 '+hk(amount)+'。請傳過數截圖，我收到後就會建立待核款申請；暫時未有入帳。')
  return self.result('請用 '+label+' 支付 '+hk(amount)+'，核對收款人後過數，再傳過數截圖。核款後充值額增加 '+hk(amount)+'，截圖不等於已入帳。',payment={'id':uid,'method':method,'amount':amount,'qr':'/payment-qr/'+method})
 def accept_receipt(self,scope,customer,attachment,caption=''):
  with self.db.db() as c:
   active=c.execute('SELECT 1 FROM topup_drafts WHERE scope=? AND updated>?',(scope,time.time()-86400,)).fetchone()
   latest=c.execute('SELECT * FROM payment_intents WHERE scope=? ORDER BY created DESC LIMIT 1',(scope,)).fetchone()
  if not active and latest and not latest['topup'] and latest['created']>time.time()-86400:
   self.draft(scope,amount=json.loads(latest['snapshot'])['paid'],method=latest['method'],intent=latest['id'])
  try:raw=base64.b64decode(attachment.get('data',''),validate=True)
  except Exception:raise AdminError('截圖格式無效')
  if attachment.get('mime') not in ['image/png','image/jpeg']:raise AdminError('請上載 PNG 或 JPEG 過數截圖')
  with self.db.db() as c:
   previous=c.execute('SELECT i.scope,t.* FROM receipt_hashes h JOIN payment_intents i ON i.id=h.intent JOIN topups t ON t.id=i.topup WHERE h.digest=?',(hashlib.sha256(raw).hexdigest(),)).fetchone()
  if previous and previous['status'] in ['pending','approved']:
   if previous['scope']==scope and previous['customer']==customer:return self.topup_status(previous)
   return self.result('此截圖已用於另一充值申請，請聯絡 CS 核對，未有新增申請或入帳。',needs_cs=True)
  evidence=self.db.save_evidence('payment-screenshot',attachment['mime'],raw,SYSTEM)
  self.draft(scope,evidence=evidence['id'])
  parsed=recharge_input(caption,True) or {}
  action=parsed if parsed.get('type') in ['topup_amount','topup_method','topup_progress'] else {'type':'topup_continue'}
  return self.custom_topup(scope,customer,action)
 def effective_price(self,c,product):
  override=c.execute('SELECT * FROM b2b_prices WHERE product=?',(product,)).fetchone()
  if override:return {'cents':override['cents']} if override['active'] else None
  item=next((p for p in self.catalog.items if p['id']==product),None)
  cents=item.get('_price_cents') if item else None
  return {'cents':cents} if isinstance(cents,int) and not isinstance(cents,bool) and cents>0 else None
 def quote(self,scope,customer,product,quantity=1):
  quantity=purchase_quantity(quantity)
  if quantity is None:
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
   return self.purchase_help(scope,'數量請用 1 至 99 的整數，例如「買 2 個」。未有扣款。',product)
  with self.db.db(True) as c:
   p=self.effective_price(c,product);item=next((p for p in self.catalog.items if p['id']==product),None)
   balance=self.balance(c,customer)
   if not item:return self.result('搵唔到呢款商品，未有扣款。請重新選擇商品。')
   products=[{k:v for k,v in item.items() if not k.startswith('_')}];selection={'product':product,'quantity':quantity}
   c.execute('INSERT OR REPLACE INTO purchase_context VALUES (?,?,?,NULL,?,?)',(scope,product,quantity,'selected',time.time()))
   if not p:return self.result('已查到可用充值額 '+hk(balance)+'，但此商品暫未提供有效售價或已暫停購買，請交由 CS 跟進。',needs_cs=True,balance=balance,balance_status='price_unavailable',products=products,selection=selection)
   total=p['cents']*quantity
   summary='「'+item['title']+'」× '+str(quantity)+'，單價 '+hk(p['cents'])+'，合共 '+hk(total)+'。'
   if balance<total:
    c.execute('INSERT OR IGNORE INTO topup_drafts(scope,amount,evidence,updated) VALUES (?,NULL,NULL,?)',(scope,time.time()))
    return self.result('充值額不足：可用 '+hk(balance)+'。'+summary+'尚欠 '+hk(total-balance)+'。\n你想充值幾多港幣？直接回覆金額，例如「'+str((total-balance)/100)+'」。確認金額後提供 PayMe 及 Alipay QR Code。',insufficient=True,balance_status='insufficient',balance=balance,shortfall=total-balance,topup_amount_input=True,products=products,selection=selection)
   uid=secrets.token_hex(16)
   c.execute('INSERT INTO purchase_quotes(id,scope,customer,product,title,amount,expires,order_id,quantity,unit_price) VALUES (?,?,?,?,?,?,?,NULL,?,?)',(uid,scope,customer,product,item['title'],total,time.time()+600,quantity,p['cents']))
   c.execute("UPDATE purchase_context SET quote=?,phase='quote',updated=? WHERE scope=?",(uid,time.time(),scope))
  return self.result('充值額足夠：目前可用 '+hk(balance)+'。\n請確認購買'+summary+'確認後預計餘額 '+hk(balance-total)+'；報價有效 10 分鐘。\n回覆「確認」即可購買；改數量可回覆「買 2 個」，或回覆「取消購買」。',balance_status='sufficient',balance=balance,products=products,selection=selection,quote={'id':uid,'product':product,'amount':total,'unit_price':p['cents'],'quantity':quantity,'title':item['title']})
 def purchase(self,scope,customer,quote_id):
  with self.db.db(True) as c:
   q=c.execute('SELECT * FROM purchase_quotes WHERE id=? AND scope=? AND customer=?',(quote_id,scope,customer)).fetchone()
   if not q:raise AdminError('購買確認無效')
   if q['order_id']:
    order=dict(c.execute('SELECT * FROM wallet_orders WHERE id=?',(q['order_id'],)).fetchone());return self.order_result(order)
   state=c.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
   if not state or state['status']!='ai':raise AdminError('此對話已由 CS 接手，請由 CS 核對後交回 AI 再購買')
   ctx=c.execute('SELECT * FROM purchase_context WHERE scope=?',(scope,)).fetchone()
   if ctx and (ctx['quote']!=quote_id or ctx['phase']!='quote'):raise AdminError('商品或數量已改動，舊確認已失效')
   if q['expires']<time.time():raise AdminError('報價已過期，請重新選擇商品')
   price=self.effective_price(c,q['product'])
   if not price or price['cents']!=q['unit_price'] or q['amount']!=q['unit_price']*q['quantity']:raise AdminError('售價已更新，請重新確認購買')
   balance=self.balance(c,customer)
   if balance<q['amount']:
    c.execute('INSERT OR IGNORE INTO topup_drafts(scope,amount,evidence,updated) VALUES (?,NULL,NULL,?)',(scope,time.time()))
    c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=?",(scope,))
    products=[{k:v for k,v in p.items() if not k.startswith('_')} for p in self.catalog.items if p['id']==q['product']]
    return self.result('充值額不足，未有扣款。'+q['title']+' × '+str(q['quantity'])+' 合共 '+hk(q['amount'])+'，目前可用 '+hk(balance)+'。你想充值幾多港幣？請直接回覆金額，核款後再購買。',insufficient=True,topup_amount_input=True,products=products,selection={'product':q['product'],'quantity':q['quantity']},balance=balance,shortfall=q['amount']-balance)
   uid='B2B-'+secrets.token_hex(6).upper();remaining=q['amount'];now=time.time()
   c.execute('INSERT INTO wallet_orders(id,customer,scope,product,title,amount,balance_after,status,created,quantity,unit_price) VALUES (?,?,?,?,?,?,?,?,?,?,?)',(uid,customer,scope,q['product'],q['title'],q['amount'],balance-q['amount'],'待 CS 安排交付',now,q['quantity'],q['unit_price']))
   lots=c.execute('SELECT l.*,amount-COALESCE((SELECT SUM(s.amount) FROM wallet_spends s WHERE s.lot=l.id),0) AS available FROM lots l WHERE customer=? AND (expires IS NULL OR expires>?) ORDER BY expires IS NULL,expires,CASE kind WHEN \'bonus\' THEN 0 ELSE 1 END,id',(customer,now)).fetchall()
   for lot in lots:
    take=min(remaining,lot['available'])
    if take>0:c.execute('INSERT INTO wallet_spends(order_id,lot,amount) VALUES (?,?,?)',(uid,lot['id'],take));remaining-=take
    if not remaining:break
   if remaining:raise AdminError('餘額更新中，未有扣款，請重試',409)
   c.execute('UPDATE purchase_quotes SET order_id=? WHERE id=?',(uid,q['id']));c.execute("UPDATE purchase_context SET phase='completed',updated=? WHERE scope=? AND quote=?",(now,scope,quote_id));self.db.audit(c,'chat-service','order.purchase',uid,{'amount':q['amount'],'customer':customer,'quantity':q['quantity'],'unit_price':q['unit_price']})
   order=dict(c.execute('SELECT * FROM wallet_orders WHERE id=?',(uid,)).fetchone())
  return self.order_result(order)
 def order_result(self,o):return self.result('已用充值額購買。\n訂單：'+o['id']+'\n商品：'+o['title']+' × '+str(o.get('quantity',1))+'\n單價：'+hk(o.get('unit_price',o['amount']))+'\n已扣：'+hk(o['amount'])+'\n交易後餘額：'+hk(o['balance_after'])+'\n狀態：'+o['status']+'\n售後、投訴或退貨，請喺對話選擇「聯絡 CS」。',order=o)
 def receipt(self,scope,intent_id,attachment):
  with self.db.db() as c:intent=c.execute('SELECT * FROM payment_intents WHERE id=? AND scope=?',(intent_id,scope)).fetchone()
  if not intent:
   if intent_id:raise AdminError('付款資料無效或不屬於此對話')
   return self.result('請先告訴我充值金額及使用 PayMe 還是 Alipay，再傳過數截圖。',topup_amount_input=True)
  with self.db.db() as c:existing=c.execute('SELECT * FROM topups WHERE request_key=?',('chat-'+intent_id,)).fetchone()
  if existing:
   with self.db.db(True) as c:c.execute('UPDATE payment_intents SET topup=? WHERE id=?',(existing['id'],intent_id))
   return self.topup_status(existing)
  if intent['topup']:
   with self.db.db() as c:previous=c.execute('SELECT * FROM topups WHERE id=?',(intent['topup'],)).fetchone()
   if previous:return self.topup_status(previous)
  try:raw=base64.b64decode(attachment.get('data',''),validate=True)
  except Exception:raise AdminError('截圖格式無效')
  if attachment.get('mime') not in ['image/png','image/jpeg']:raise AdminError('請上載 PNG 或 JPEG 過數截圖')
  digest=hashlib.sha256(raw).hexdigest()
  with self.db.db(True) as c:
   previous=c.execute('SELECT intent FROM receipt_hashes WHERE digest=?',(digest,)).fetchone()
   if previous and previous['intent']!=intent_id:
    old=c.execute('SELECT i.scope,i.customer,t.status,t.id,t.reason FROM payment_intents i LEFT JOIN topups t ON t.id=i.topup WHERE i.id=?',(previous['intent'],)).fetchone()
    if old and old['scope']==scope and old['customer']==intent['customer'] and old['status']=='rejected':
     c.execute('UPDATE receipt_hashes SET intent=? WHERE digest=?',(intent_id,digest))
     self.db.audit(c,'chat-service','topup.receipt_resubmit',intent_id,{'previous_topup':old['id']})
    elif old and old['scope']==scope and old['customer']==intent['customer'] and old['status'] in ['pending','approved']:return self.topup_status(old)
    else:return self.result('此截圖已用於另一充值申請，請聯絡 CS 核對，未有新增申請或入帳。',needs_cs=True)
   c.execute('INSERT OR IGNORE INTO receipt_hashes VALUES (?,?)',(digest,intent_id))
  evidence=self.db.save_evidence('payment-screenshot',attachment['mime'],raw,SYSTEM)
  result=self.db.create_topup({'request_key':'chat-'+intent_id,'customer':intent['customer'],'plan':intent['plan'],'method':intent['method'],'reference':'CHAT-'+intent_id,'evidence':'客人上載過數截圖；核款員必須核對收款帳戶的實際流水及重複付款。','evidence_id':evidence['id']},SYSTEM,trusted_snapshot=json.loads(intent['snapshot']))
  with self.db.db(True) as c:
   c.execute('UPDATE payment_intents SET topup=? WHERE id=?',(result['id'],intent_id))
   c.execute('DELETE FROM topup_drafts WHERE scope=? AND intent=?',(scope,intent_id))
  return self.result('已收到過數截圖並通知後台核款。申請編號：'+result['id']+'。審核完成前不會增加餘額；你可稍後查詢「餘額」。',topup_status='pending',topup_id=result['id'])
 def respond(self,scope,question,event,action=None,attachment=None,use_model=True,channel='web',account='',peer='',customer=None,generate=True,user_recorded=False,handoff_requested=False):
  if not isinstance(event,str) or not 1<=len(event)<=200:raise AdminError('訊息編號無效')
  if action is not None and not isinstance(action,dict):raise AdminError('操作格式無效')
  conv=self.ensure(scope,channel,account,peer,customer)
  with self.db.db() as c:cached=c.execute('SELECT result FROM conversation_events WHERE scope=? AND event=?',(scope,event)).fetchone()
  if cached:return self.guard_cached_reply(scope,json.loads(cached['result']),channel)
  user_payload=None
  if attachment and not user_recorded:
   try:raw=base64.b64decode(attachment.get('data',''),validate=True)
   except Exception:raise AdminError('圖片格式無效')
   evidence=self.db.save_evidence('chat-image',attachment.get('mime'),raw,SYSTEM)
   user_payload=json.dumps({'answer':question,'mode':'customer_attachment','evidence_id':evidence['id']})
  history=self.history(scope)['messages'];action=action or {};kind=action.get('type','');cust=conv['customer'];result=None
  if user_recorded and history and history[-1]['role']=='user' and history[-1]['content']==question:history=history[:-1]
  route={'query':question}
  if not kind and conv['status']=='ai' and generate:
   route=self.route_turn(scope,question,history,attachment)
   if not route['action'] and route['result'] is None and not re.fullmatch(r'\s*(你好|hi|hello|開始|開始對話)[！!。\s]*',question,re.I):
    route=self.language_fallback(scope,question,history,use_model) or route
   action=route['action'];result=route['result'];kind=action.get('type','');attachment=route.get('attachment',attachment)
  # Model interpretation can outlive a staff takeover. Re-read before any operation.
  with self.db.db() as c:current=c.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
  if handoff_requested and generate and current['status']=='waiting':result=self.handover_notice()
  elif current['status']!='ai' or not generate:result=self.held_reply(channel)
  elif kind=='cs' or wants_cs(question):result=self.handover(scope,action.get('reason') or '客人要求 CS／售後')
  elif result is not None:pass
  elif kind=='review_status':result=self.read_topup_status(scope)
  elif kind=='cancel_purchase':_,result=self.checkout_input(scope,'取消購買')
  elif kind=='product_price':
   with self.db.db() as c:price=self.effective_price(c,action.get('product'))
   result=self.result(('目前單價係 '+hk(price['cents'])+'。想買可以回覆「買呢個」，多件可講「買2個」，確認報價後才扣款。') if price else '此商品暫未有有效售價，請交 CS 核對。',products=self.selected_products(scope,action.get('product')),needs_cs=not price)
  elif kind=='detail':
   item=next((p for p in self.catalog.items if p['id']==action.get('product')),None)
   result=self.result('可以按商品連結查看詳情，或選擇以充值額購買。')
   if item:
    result['products']=[{k:v for k,v in item.items() if not k.startswith('_')}]
    with self.db.db(True) as c:c.execute('INSERT OR REPLACE INTO purchase_context VALUES (?,?,1,NULL,?,?)',(scope,item['id'],'selected',time.time()))
    result['selection']={'product':item['id'],'quantity':1}
  elif kind in ['buy','confirm','topup','receipt','balance','plans','topup_amount','topup_method','topup_invalid','cancel_topup','topup_progress','restart_topup']:
   if not cust:
    result=self.handover(scope,'請核實電話並綁定 B2B 帳戶')
    selected=next((p for p in self.catalog.items if p['id']==action.get('product')),None) if kind=='buy' else None
    result['answer']=('已選擇「'+selected['title']+'」。\n' if selected else '')+self.missing_account_message(conv)+'暫時無法查閱充值餘額或扣款。我已通知 CS 協助核實及綁定帳戶，完成後即可繼續。今次未有扣款。'
   else:
    if kind in ['topup_amount','topup_method','topup_invalid','cancel_topup','topup_progress','restart_topup']:result=self.custom_topup(scope,cust,action)
    elif kind=='balance':
     with self.db.db() as c:balance=self.balance(c,cust)
     result=self.result('目前可用充值額：'+hk(balance)+'。待核款充值未計入；過期額度不計入可用餘額。')
    elif kind=='buy':result=self.quote(scope,cust,action.get('product'),action.get('quantity',1))
    elif kind=='confirm':result=self.confirm_purchase(scope,cust,action.get('quote'))
    elif kind=='topup':
     with self.db.db(True) as c:
      plan=c.execute('SELECT * FROM plans WHERE id=? AND active=1',(action.get('plan'),)).fetchone();method=action.get('method')
      if not plan or method not in ['payme','alipay']:raise AdminError('請選有效方案及 PayMe／Alipay')
      uid=secrets.token_hex(12);c.execute('INSERT INTO payment_intents VALUES (?,?,?,?,?,?,NULL,?)',(uid,scope,cust,plan['id'],method,json.dumps(dict(plan)),time.time()))
      c.execute('DELETE FROM topup_drafts WHERE scope=?',(scope,))
     result=self.result('請用 '+('PayMe' if method=='payme' else 'Alipay')+' 支付 '+hk(plan['paid'])+'，核對收款人後過數，再上載過數截圖。本金 '+hk(plan['principal'])+'，贈額 '+hk(plan['bonus'])+'。截圖不等於入帳，須待核款。',payment={'id':uid,'method':method,'amount':plan['paid'],'qr':'/payment-qr/'+method})
    elif kind=='receipt':
     result=self.receipt(scope,action['intent'],attachment or {}) if action.get('intent') else self.accept_receipt(scope,cust,attachment or {},question)
    else:result=self.options(cust,scope)
  elif re.fullmatch(r'\s*(你好|hi|hello|開始|開始對話)[！!。\s]*',question,re.I):result=self.result(WELCOME)
  else:
   ctx=self.purchase_context(scope)
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='browsing' WHERE scope=?",(scope,))
   options={}
   search_query=route.get('query',question)
   if shopping_budget(search_query):
    with self.db.db() as c:options['price_overrides']={r['product']:r['cents'] if r['active'] else None for r in c.execute('SELECT * FROM b2b_prices')}
   if route.get('exclude_ids'):options['exclude_ids']=route['exclude_ids']
   search_history=[] if route.get('contextualized') else [{'role':m['role'],'content':m['content']} for m in history][-10:]
   result=self.catalog.chat(search_query,search_history,use_model=use_model,**options)
   with self.db.db() as c:current=c.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
   if current['status']!='ai':result=self.result('CS 已接手此對話，請等候人工回覆。',handoff=True,suppress_delivery=channel=='whatsapp')
   elif route.get('exclude_ids') and not result.get('products'):
    result=self.result('暫時未找到符合今次條件的其他商品。你想調整預算，定改搵另一類產品？',dialogue={**previous_dialogue(history),'prompt':'product'})
   elif result.get('budget'):pass
   elif result.get('mode') in ['unavailable','handoff'] or (not result.get('products') and result.get('mode')!='knowledge'):
    if ctx and ctx['phase'] in ['quote','selected']:result=self.purchase_help(scope,'我未能理解呢句購買回覆，今次未有扣款。',ctx['product'])
    else:result=self.handover(scope,'資料未能回答：'+question[:150])
  if result.pop('needs_cs',False):
   message=result['answer'];handoff=self.handover(scope,message);result.update(handoff=True,answer=message+'\n'+handoff['answer'])
  if channel=='whatsapp' and not history and result['answer']!=WELCOME:result['answer']=WELCOME+'\n\n'+result['answer']
  result=self.add_shortcuts(scope,result)
  result=self.finish_dialogue(scope,result,question,route,history,kind)
  with self.db.db(True) as c:
   if not user_recorded:c.execute('INSERT INTO conversation_messages(scope,role,content,payload,created) VALUES (?,?,?,?,?)',(scope,'user',question,user_payload,time.time()))
   if not result.get('suppress_delivery'):c.execute('INSERT INTO conversation_messages(scope,role,content,payload,created) VALUES (?,?,?,?,?)',(scope,'assistant',result['answer'],json.dumps(result,ensure_ascii=False),time.time()))
   c.execute('INSERT INTO conversation_events VALUES (?,?,?)',(scope,event,json.dumps(result,ensure_ascii=False)));c.execute('UPDATE conversations SET updated=? WHERE id=?',(time.time(),scope))
  return result
 def prepare_reply(self,data,user):
  self.db.require(user,'admin','support');message=text(data.get('text'),'回覆內容',3000);key=text(data.get('request_id'),'請求編號',100)
  with self.db.db(True) as c:
   row=c.execute('SELECT * FROM conversations WHERE id=?',(data.get('scope'),)).fetchone()
   if not row:raise AdminError('找不到對話',404)
   old=c.execute('SELECT * FROM cs_replies WHERE id=?',(key,)).fetchone()
   if old:
    if old['scope']!=row['id'] or old['text']!=message:raise AdminError('請求編號已被使用',409)
    return dict(row),dict(old),False
   c.execute("UPDATE conversations SET status='human',updated=? WHERE id=?",(time.time(),row['id']))
   c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(row['id'],))
   c.execute('INSERT INTO cs_replies VALUES (?,?,?,?,?,?)',(key,row['id'],user['id'],message,'sending',time.time()))
  return dict(row),{'id':key,'text':message,'status':'sending'},True
 def finish_reply(self,key,user,status):
  with self.db.db(True) as c:
   row=c.execute('SELECT * FROM cs_replies WHERE id=?',(key,)).fetchone()
   if row['status']!='sending':return
   c.execute('UPDATE cs_replies SET status=? WHERE id=?',(status,key))
   payload={'answer':row['text'],'products':[],'mode':'cs','delivery':status}
   c.execute('INSERT INTO conversation_messages(scope,role,content,payload,created) VALUES (?,?,?,?,?)',(row['scope'],'assistant',row['text'],json.dumps(payload,ensure_ascii=False),time.time()))
   self.db.audit(c,user['id'],'cs.reply.'+status,row['scope'])
 def notify_review(self,topup):
  with self.db.db(True) as c:
   t=c.execute("SELECT * FROM topups WHERE id=? AND status IN ('approved','rejected')",(topup,)).fetchone()
   if not t:return
   rows=c.execute('SELECT DISTINCT v.* FROM payment_intents i JOIN conversations v ON v.id=i.scope WHERE i.topup=?',(topup,)).fetchall()
   for row in rows:
    if c.execute('SELECT 1 FROM review_notifications WHERE topup=? AND scope=?',(topup,row['id'])).fetchone():continue
    plan=json.loads(t['snapshot'])
    message=('充值成功，已核款入帳！\n今次已加入 '+hk(plan['principal']+plan['bonus'])+'，核款時可用充值額：'+hk(self.balance(c,t['customer']))+'。' if t['status']=='approved' else '今次充值申請未獲批准，未有增加充值額。\n原因：'+t['reason']+'\n如需協助，請喺此對話聯絡 CS。')+'\n申請編號：'+topup
    status='pending' if row['channel']=='whatsapp' else 'sent';uid='topup-'+topup+'-'+hashlib.sha256(row['id'].encode()).hexdigest()[:16];now=time.time()
    result=self.result(message,notification=True,delivery=status)
    mid=c.execute('INSERT INTO conversation_messages(scope,role,content,payload,created) VALUES (?,?,?,?,?)',(row['id'],'assistant',message,json.dumps(result,ensure_ascii=False),now)).lastrowid
    c.execute('INSERT INTO review_notifications VALUES (?,?,?,?,?,?,?,0,?,?,?,?)',(uid,topup,row['id'],row['account'],row['peer'],message,status,now,mid,now,now))
    c.execute('UPDATE conversations SET updated=? WHERE id=?',(now,row['id']))
   c.execute('UPDATE review_notice_jobs SET processed=1 WHERE topup=?',(topup,))

 def record_phone(self,scope,value):
  # Called only by the authenticated WhatsApp transport; never trust typed digits as identity.
  if not isinstance(value,str) or not re.fullmatch(r'[1-9]\d{7,14}',value):return
  with self.db.db(True) as c:
   c.execute('INSERT OR REPLACE INTO conversation_contacts VALUES (?,?)',(scope,value))
   if c.execute('SELECT 1 FROM conversations WHERE id=?',(scope,)).fetchone():self.match_phone(c,scope)
 def mark_read(self,data,user):
  self.db.require(user,'admin','support');scope=data.get('scope');last=data.get('last_id')
  if not isinstance(last,int) or isinstance(last,bool) or last<0:raise AdminError('已讀位置無效')
  with self.db.db(True) as c:
   if not c.execute('SELECT 1 FROM conversation_messages WHERE scope=? AND id=?',(scope,last)).fetchone():raise AdminError('訊息不存在',404)
   c.execute('INSERT INTO conversation_reads VALUES (?,?,?) ON CONFLICT(scope,staff) DO UPDATE SET last_id=MAX(last_id,excluded.last_id)',(scope,user['id'],last))
  return {'ok':True}
 def clear_history(self,data,user):
  self.db.require(user,'admin','support')
  if data.get('confirmed') is not True:raise AdminError('請先確認清除這個對話')
  scope=text(data.get('scope'),'對話',200);last=data.get('last_id')
  if not isinstance(last,int) or isinstance(last,bool) or last<0:raise AdminError('對話版本無效')
  with self.db.db(True) as c:
   if not c.execute('SELECT 1 FROM conversations WHERE id=?',(scope,)).fetchone():raise AdminError('找不到對話',404)
   current=c.execute('SELECT COALESCE(MAX(id),0) FROM conversation_messages WHERE scope=?',(scope,)).fetchone()[0]
   if current!=last:raise AdminError('對話有新訊息，請重新查看後再清除',409)
   if c.execute("SELECT 1 FROM cs_replies WHERE scope=? AND status='sending'",(scope,)).fetchone():raise AdminError('人工回覆正在送出，請稍後再清除',409)
   deleted=c.execute('DELETE FROM conversation_messages WHERE scope=?',(scope,)).rowcount
   c.execute('DELETE FROM conversation_reads WHERE scope=?',(scope,))
   c.execute('DELETE FROM topup_drafts WHERE scope=?',(scope,))
   c.execute('UPDATE chat_choices SET expires=0 WHERE scope=?',(scope,))
   c.execute('DELETE FROM purchase_context WHERE scope=?',(scope,))
   c.execute('UPDATE purchase_quotes SET expires=0 WHERE scope=? AND order_id IS NULL',(scope,))
   c.execute('DELETE FROM payment_intents WHERE scope=? AND topup IS NULL AND id NOT IN (SELECT intent FROM receipt_hashes)',(scope,))
   # Keep event deduplication, submitted payment evidence, customer identity and ledgers.
   self.db.audit(c,user['id'],'chat.clear',scope,{'messages_deleted':deleted,'status_preserved':True})
  return {'ok':True,'messages_deleted':deleted}
