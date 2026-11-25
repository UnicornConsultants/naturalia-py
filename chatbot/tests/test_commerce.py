import base64,concurrent.futures,secrets,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_domain import AdminDB,AdminError
from commerce import Commerce,WELCOME
from unittest.mock import patch
class Catalog:
 items=[{'id':'p1','title':'測試護膚套裝','price':'HK$9,999','_price_cents':999900,'url':'https://www.naturalia-group.com/products/test'}]
 def __init__(self):self.calls=0
 def chat(self,q,history,use_model=True):
  self.calls+=1
  return {'answer':'找到套裝','products':self.items if '套裝' in q else [],'mode':'retrieval'}
class CommerceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=AdminDB(Path(self.tmp.name)/'db');self.catalog=Catalog();self.c=Commerce(self.db,self.catalog);self.admin={'id':'admin','role':'admin'}
  self.customer=self.db.create_customer({'company':'測試公司','contact':'測試','phone':'91234567'},self.admin)['id']
  self.plan=self.db.create_plan({'name':'測試方案','paid':'100','principal':'100','bonus':'20'},self.admin)['id']
  self.c.ensure('test','test',customer=self.customer)
 def tearDown(self):self.tmp.cleanup()
 def call(self,q='操作',action=None,scope='test',**kwargs):return self.c.respond(scope,q,secrets.token_hex(12),action=action,use_model=False,**kwargs)
 def fund(self):
  request=self.db.create_topup({'request_key':secrets.token_hex(12),'customer':self.customer,'plan':self.plan,'method':'payme','reference':secrets.token_hex(12),'evidence':'fixture'},self.admin)
  self.db.review({'id':request['id'],'decision':'approved','reason':'已核對測試流水','payment_verified':True,'verified_paid':'100'},self.admin)
 def test_receipt_review_purchase_idempotency_and_balance(self):
  self.assertEqual(self.call('你好')['answer'],WELCOME)
  self.assertTrue(self.call(action={'type':'plans'})['topup_amount_input'])
  pay=self.call(action={'type':'topup','plan':self.plan,'method':'payme'})['payment'];self.assertEqual(pay['qr'],'/payment-qr/payme')
  self.call(action={'type':'receipt','intent':pay['id']},attachment={'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()})
  s=self.c.snapshot(self.admin);self.assertEqual(s['pending_topups'],1);self.assertEqual(s['pending_topup_items'][0]['company'],'測試公司');self.assertEqual(s['pending_topup_items'][0]['phone'],'91234567')
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),0)
  topup=self.db.snapshot(self.admin)['topups'][0]
  self.assertTrue(topup['evidence_id']);self.db.review({'id':topup['id'],'decision':'approved','reason':'人工核款','verified_reference':'platform-tx-1','payment_verified':True,'verified_paid':'100'},self.admin)
  self.assertEqual(self.c.snapshot(self.admin)['pending_topup_items'],[]);self.c.notify_review(topup['id']);self.assertIn('已核款入帳',self.c.history('test')['messages'][-1]['content'])
  self.c.price({'product':'p1','price':'80','active':True},self.admin)
  quote=self.call(action={'type':'buy','product':'p1'})['quote']
  self.assertEqual(quote['amount'],8000)
  result=self.call(action={'type':'confirm','quote':quote['id']});self.assertEqual(result['order']['balance_after'],4000)
  duplicate=self.call(action={'type':'confirm','quote':quote['id']});self.assertEqual(result['order']['id'],duplicate['order']['id'])
  self.assertEqual(self.db.snapshot(self.admin)['customers'][0]['balance'],4000)
  self.assertTrue(self.call(action={'type':'buy','product':'p1'})['insufficient'])
 def test_expiry_price_change_concurrency_and_no_overdraw(self):
  self.fund();self.c.price({'product':'p1','price':'80','active':True},self.admin)
  a=self.call(action={'type':'buy','product':'p1'})['quote']['id'];b=self.call(action={'type':'buy','product':'p1'})['quote']['id']
  def attempt(q):
   try:return self.c.purchase('test',self.customer,q)
   except AdminError as error:return {'error':str(error)}
  with concurrent.futures.ThreadPoolExecutor() as pool:results=list(pool.map(attempt,[a,b,a,b]))
  self.assertEqual(sum('error' in r for r in results),2) # Superseded quote cannot charge.
  self.assertEqual(len({r['order']['id'] for r in results if 'order' in r}),1)
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),4000)
  self.c.price({'product':'p1','price':'20','active':True},self.admin);quote=self.call(action={'type':'buy','product':'p1'})['quote']['id']
  self.c.price({'product':'p1','price':'30','active':True},self.admin)
  with self.assertRaises(AdminError):self.c.purchase('test',self.customer,quote)
  with self.db.db(True) as db:db.execute('UPDATE lots SET expires=?',(time.time()-1,))
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),0)
 def test_identity_scope_receipt_and_role_boundaries(self):
  self.fund();self.assertTrue(self.call(action={'type':'balance'},scope='web-guest')['handoff'])
  self.assertIsNone(self.c.ensure('lid','whatsapp','a','85291234567@lid')['customer'])
  self.assertEqual(self.c.ensure('phone','whatsapp','a','85291234567@s.whatsapp.net')['customer'],self.customer)
  self.c.price({'product':'p1','price':'10','active':True},self.admin);quote=self.call(action={'type':'buy','product':'p1'})['quote']['id']
  with self.assertRaises(AdminError):self.c.purchase('other',self.customer,quote)
  payment=self.call(action={'type':'topup','plan':self.plan,'method':'alipay'})['payment']
  with self.assertRaises(AdminError):self.c.receipt('other',payment['id'],{})
  with self.assertRaises(AdminError):self.c.price({'product':'p1','price':'1','active':True},{'role':'support'})
  with self.assertRaises(AdminError):self.c.bind({'scope':'web-guest','customer':self.customer},self.admin)
 def test_cs_handover_resume_and_duplicate_manual_reply(self):
  result=self.call('我要退貨');self.assertTrue(result['handoff']);self.assertEqual(self.catalog.calls,0)
  alerts=self.c.snapshot(self.admin);self.assertEqual(alerts['waiting_cs'],1);self.assertEqual(alerts['waiting_cs_items'][0]['id'],'test');self.assertEqual(self.c.snapshot({'role':'finance'})['waiting_cs_items'],[])
  self.call('護膚套裝');self.assertEqual(self.catalog.calls,0)
  conv,reply,fresh=self.c.prepare_reply({'scope':'test','text':'CS 已接手','request_id':'reply-1'},self.admin)
  self.assertTrue(fresh);self.assertEqual(self.c.snapshot(self.admin)['waiting_cs'],0);self.c.finish_reply(reply['id'],self.admin,'sent');self.assertFalse(self.c.prepare_reply({'scope':'test','text':'CS 已接手','request_id':'reply-1'},self.admin)[2])
  self.assertEqual(self.c.history('test')['messages'][-1]['content'],'CS 已接手')
  self.c.state({'scope':'test','status':'ai'},self.admin);self.call('護膚套裝');self.assertEqual(self.catalog.calls,1)
  self.assertTrue(self.call('火星有幾多人')['handoff'])
 def test_issued_payment_keeps_terms_after_plan_edit_and_delete(self):
  payment=self.call(action={'type':'topup','plan':self.plan,'method':'payme'})['payment']
  self.db.edit_plan({'id':self.plan,'version':1,'name':'新條款','paid':'200','principal':'200','bonus':'0'},self.admin)
  self.db.plan_state({'id':self.plan,'action':'delete'},self.admin)
  self.call(action={'type':'receipt','intent':payment['id']},attachment={'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode()})
  topup=self.db.snapshot(self.admin)['topups'][0]
  self.assertEqual(topup['snapshot']['paid'],10000);self.assertEqual(topup['snapshot']['bonus'],2000)
  self.assertTrue(self.c.options(self.customer)['topup_amount_input'])
 def test_same_bank_transaction_cannot_credit_twice(self):
  requests=[]
  for i in range(2):
   request=self.db.create_topup({'request_key':'bank-'+str(i),'customer':self.customer,'plan':self.plan,'method':'payme','reference':'CHAT-fixture-'+str(i),'evidence':'Screenshot '+str(i)},self.admin);requests.append(request['id'])
  body={'id':requests[0],'decision':'approved','reason':'核實流水','verified_reference':'bank-tx-unique','payment_verified':True,'verified_paid':'100'}
  self.db.review(body,self.admin)
  with self.assertRaises(AdminError):self.db.review(dict(body,id=requests[1]),self.admin)
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),12000)
 def test_unread_is_per_staff_and_does_not_mark_later_messages(self):
  self.call('你好')
  admin={'id':'reader1','role':'admin'};other={'id':'reader2','role':'support'}
  conv=self.c.snapshot(admin)['conversations'][0];self.assertEqual(conv['unread_count'],1)
  cursor=self.c.history('test')['messages'][-1]['id']
  self.call('你好')
  self.c.mark_read({'scope':'test','last_id':cursor},admin)
  self.assertEqual(self.c.snapshot(admin)['conversations'][0]['unread_count'],1)
  self.assertEqual(self.c.snapshot(other)['conversations'][0]['unread_count'],2)
  with self.assertRaises(AdminError):self.c.mark_read({'scope':'other','last_id':cursor},admin)
  self.c.record_phone('test','85291234567')
  self.assertEqual(self.c.snapshot(admin)['conversations'][0]['contact_phone'],'85291234567')
  self.c.record_phone('test','183201863614703@lid')
  self.assertEqual(self.c.snapshot(admin)['conversations'][0]['contact_phone'],'85291234567')
 def test_unread_survives_message_cleanup_and_legacy_migration(self):
  self.call('hi');last=self.c.history('test')['messages'][-1]['id']
  self.c.mark_read({'scope':'test','last_id':last},self.admin)
  with self.db.db(True) as db:
   db.execute('DROP TABLE conversation_messages')
   db.execute('CREATE TABLE conversation_messages(id INTEGER PRIMARY KEY,scope TEXT,role TEXT,content TEXT,payload TEXT,created REAL)')
  self.c=Commerce(self.db,self.catalog)
  self.call('hi');new=self.c.history('test')['messages'][-1]['id']
  self.assertGreater(new,last);self.assertEqual(self.c.snapshot(self.admin)['conversations'][0]['unread_count'],1)
  self.c.mark_read({'scope':'test','last_id':new},self.admin)
  with self.db.db(True) as db:db.execute('DELETE FROM conversation_messages')
  self.call('hi');self.assertEqual(self.c.snapshot(self.admin)['conversations'][0]['unread_count'],1)
 def test_old_thread_with_recent_message_remains_visible_above_100_threads(self):
  self.call('hi')
  with self.db.db(True) as db:
   db.execute("UPDATE conversations SET updated=0 WHERE id='test'")
   for i in range(110):db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?)',('old-'+str(i),'web','','',None,'ai',None,1,2))
  snapshot=self.c.snapshot(self.admin)
  self.assertEqual(snapshot['conversations'][0]['id'],'test');self.assertEqual(snapshot['cs_attention_count'],1)
 def test_short_commands_keep_product_identity_and_require_confirmation(self):
  self.fund();self.c.price({'product':'p1','price':'80','active':True},self.admin)
  listing=self.call('護膚套裝');choice=listing['products'][0]['choice']
  self.assertRegex(choice,r'^[A-HJ-NP-Z][A-HJ-NP-Z2-9]{3}$')
  self.assertNotIn('choice',self.catalog.items[0])
  detail=self.call('有意購買 '+choice.lower());self.assertEqual(detail['products'][0]['id'],'p1')
  first=self.catalog.items[0];self.catalog.items=[dict(first,id='p2',title='另一套裝'),first]
  changed=self.call('護膚套裝')['products'];self.assertEqual(changed[1]['choice'],choice);self.assertNotEqual(changed[0]['choice'],choice)
  quote=self.call('買　'+str(choice))['quote']
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),12000)
  paid=self.call('確認購買 '+str(quote['choice']));self.assertEqual(paid['order']['amount'],8000)
  duplicate=self.call('確認 '+str(quote['choice']));self.assertEqual(duplicate['order']['id'],paid['order']['id'])
  self.assertEqual(self.call('護膚套裝')['products'][1]['choice'],choice)
  self.assertIn('搵唔到',self.call('買 '+str(choice),scope='other')['answer'])
  other=self.call('護膚套裝',scope='other')['products'];self.assertEqual(other[1]['choice'],choice)
  restored=Commerce(self.db,self.catalog);self.assertEqual(restored.add_shortcuts('test',{'products':[first]})['products'][0]['choice'],choice)
  with self.db.db(True) as db:db.execute('UPDATE chat_choices SET expires=0')
  self.assertIn('過期',self.call('買 '+str(choice))['answer'])
 def test_short_recharge_commands_and_retired_options(self):
  plans=self.c.add_shortcuts('test',{'topup_plans':[{'id':self.plan}]})['topup_plans'];n=plans[0]['choice']
  self.assertTrue(self.call('充值 '+str(n)+' PayMe')['topup_amount_input'])
  result=self.call('充值方案 '+str(n)+' PayMe');self.assertEqual(result['payment']['amount'],10000);self.assertEqual(result['payment']['method'],'payme')
  result=self.call('充值方案 '+str(n)+' 支付寶');self.assertEqual(result['payment']['method'],'alipay')
  self.db.plan_state({'id':self.plan,'action':'deactivate'},self.admin)
  self.assertIn('停用',self.call('充值方案 '+str(n)+' PayMe')['answer'])
 def test_product_short_code_collision_is_reallocated(self):
  with self.db.db(True) as db:db.execute('INSERT INTO product_codes VALUES (?,?)',('different-product','AAAA'))
  with patch('commerce.secrets.choice',side_effect=list('AAAABBBB')):
   result=self.c.add_shortcuts('test',{'products':[self.catalog.items[0]]})
  self.assertEqual(result['products'][0]['choice'],'BBBB')
 def test_lid_trusted_phone_matches_wallet_and_reports_sufficient_or_insufficient(self):
  scope='wa-mapped';self.c.ensure(scope,'whatsapp','a','123456789@lid')
  self.c.record_phone(scope,'85291234567');self.c.price({'product':'p1','price':'80','active':True},self.admin)
  code=self.call('護膚套裝',scope=scope,channel='whatsapp')['products'][0]['choice']
  result=self.call(code,scope=scope,channel='whatsapp')
  self.assertEqual(result['balance_status'],'insufficient');self.assertEqual(result['balance'],0);self.assertEqual(result['shortfall'],8000)
  self.assertEqual(self.c.ensure(scope)['customer'],self.customer);self.assertFalse(result.get('handoff',False))
  self.fund();result=self.call(code,scope=scope,channel='whatsapp')
  self.assertEqual(result['balance_status'],'sufficient');self.assertEqual(result['balance'],12000);self.assertIn('充值額足夠',result['answer'])
  with self.db.db() as db:
   self.assertEqual(self.c.balance(db,self.customer),12000)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM wallet_orders').fetchone()[0],0)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='chat.phone_match' AND target=?",(scope,)).fetchone()[0],1)
 def test_phone_lookup_missing_duplicate_and_untrusted_never_guesses_a_wallet(self):
  for scope,peer in [('missing','99999999@lid'),('duplicate','91234567@lid'),('unknown','91234567@lid')]:self.c.ensure(scope,'whatsapp','a',peer)
  self.db.create_customer({'company':'另一公司','contact':'測試','phone':'+852 9123 4567'},self.admin)
  self.c.record_phone('missing','85290000002');self.c.record_phone('duplicate','85291234567')
  self.assertIn('HK$0.00',self.call('餘額',scope='missing',channel='whatsapp')['answer'])
  for scope,expected in [('duplicate','多個 B2B 帳戶'),('unknown','暫未提供')]:
   result=self.call('餘額',scope=scope,channel='whatsapp');self.assertIn(expected,result['answer']);self.assertNotIn('HK$0',result['answer']);self.assertIsNone(self.c.ensure(scope)['customer'])
  self.c.ensure('web-phone','web');self.c.record_phone('web-phone','85291234567')
  self.assertIsNone(self.c.ensure('web-phone')['customer'])
 def test_account_created_later_resolves_only_identity_wait_not_manual_takeover(self):
  self.c.ensure('later','whatsapp','a','99887766@lid')
  self.call('餘額',scope='later',channel='whatsapp')
  self.c.record_phone('later','85290000003');customer=self.c.ensure('later')['customer']
  result=self.call('餘額',scope='later',channel='whatsapp');self.assertIn('HK$0.00',result['answer']);self.assertEqual(self.c.history('later')['status'],'ai')
  self.c.ensure('manual','whatsapp','a','33445566@lid');self.c.state({'scope':'manual','status':'human'},self.admin);self.c.record_phone('manual','85290000003')
  result=self.call('餘額',scope='manual',channel='whatsapp')
  self.assertTrue(result['suppress_delivery']);self.assertEqual(self.c.ensure('manual')['customer'],customer);self.assertEqual(self.c.history('manual')['status'],'human')
 def test_bare_product_code_and_spaced_commands_skip_the_model(self):
  self.fund();self.c.price({'product':'p1','price':'80','active':True},self.admin)
  code=self.call('護膚套裝')['products'][0]['choice'];before=self.catalog.calls
  for command in [code,code.lower(),'買'+code,'買 '+code,'「'+code+'」']:
   result=self.call(command);self.assertIn('quote',result);self.assertFalse(result.get('handoff',False))
  self.assertEqual(self.catalog.calls,before)
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),12000)
  self.assertEqual(self.c.shortcuts('test','mask'),({},None))
  with self.db.db(True) as db:db.execute('UPDATE chat_choices SET expires=0')
  self.assertIn('過期',self.call(code)['answer']);self.assertEqual(self.c.history('test')['status'],'ai')
 def test_unbound_code_explains_identity_requirement_and_human_remains_in_control(self):
  scope='unbound';code=self.call('護膚套裝',scope=scope)['products'][0]['choice'];before=self.catalog.calls
  result=self.call(code,scope=scope)
  self.assertTrue(result['handoff']);self.assertIn('已選擇「測試護膚套裝」',result['answer']);self.assertIn('尚未綁定',result['answer']);self.assertIn('未有扣款',result['answer'])
  self.assertEqual(self.catalog.calls,before)
  self.c.state({'scope':scope,'status':'human'},self.admin)
  result=self.call('買'+code,scope=scope,channel='whatsapp')
  self.assertTrue(result['suppress_delivery']);self.assertEqual(self.c.history(scope)['status'],'human')
 def test_short_quote_expiry_returns_explanation_without_debit(self):
  self.fund();self.c.price({'product':'p1','price':'80','active':True},self.admin)
  quote=self.call(action={'type':'buy','product':'p1'})['quote']
  with self.db.db(True) as db:db.execute('UPDATE purchase_quotes SET expires=0')
  result=self.call('確認購買 '+str(quote['choice']));self.assertIn('未有扣款',result['answer'])
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),12000)
 def test_catalog_price_is_default_and_same_as_displayed_price(self):
  self.fund();listing=self.call('護膚套裝');self.assertEqual(listing['products'][0]['price'],'HK$9,999.00')
  result=self.call(action={'type':'buy','product':'p1'});self.assertTrue(result['insufficient']);self.assertEqual(result['shortfall'],987900)
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),12000)
  self.catalog.items=[dict(self.catalog.items[0],_price_cents=8000,price='HK$80.00')]
  quote=self.call(action={'type':'buy','product':'p1'})['quote'];self.assertEqual(quote['amount'],8000)
  result=self.call(action={'type':'confirm','quote':quote['id']});self.assertEqual(result['order']['balance_after'],4000)
 def test_auto_account_is_unique_under_concurrent_phone_messages_and_can_top_up(self):
  scopes=['new-wa-'+str(n) for n in range(8)]
  def incoming(scope):
   self.c.ensure(scope,'whatsapp','a','7654321@lid');self.c.record_phone(scope,'85290000004');return self.c.ensure(scope)['customer']
  with concurrent.futures.ThreadPoolExecutor() as pool:customers=list(pool.map(incoming,scopes))
  self.assertEqual(len(set(customers)),1)
  with self.db.db() as db:
   self.assertEqual(self.c.balance(db,customers[0]),0);self.assertEqual(db.execute("SELECT COUNT(*) FROM customers WHERE phone='85290000004'").fetchone()[0],1)
   self.assertEqual(db.execute('SELECT COUNT(*) FROM lots WHERE customer=?',(customers[0],)).fetchone()[0],0)
  payment=self.call(action={'type':'topup','plan':self.plan,'method':'payme'},scope=scopes[0],channel='whatsapp')['payment']
  self.call(action={'type':'receipt','intent':payment['id']},scope=scopes[0],channel='whatsapp',attachment={'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\nnew-phone-test').decode()})
  topup=self.db.snapshot(self.admin)['topups'][0];self.assertEqual(topup['customer'],customers[0])
  self.db.review({'id':topup['id'],'decision':'approved','reason':'Fixture review','payment_verified':True,'verified_paid':'100','verified_reference':'new-phone-tx'},self.admin)
  self.assertIn('HK$120.00',self.call('餘額',scope=scopes[0],channel='whatsapp')['answer'])
 def test_catalog_price_change_invalidates_old_quote_and_override_matches_card(self):
  self.fund();self.catalog.items=[dict(self.catalog.items[0],_price_cents=8000)]
  q=self.call(action={'type':'buy','product':'p1'})['quote']
  self.catalog.items[0]['_price_cents']=9000
  with self.assertRaises(AdminError):self.c.purchase('test',self.customer,q['id'])
  self.c.price({'product':'p1','price':'70','active':True},self.admin)
  self.assertEqual(self.call('護膚套裝')['products'][0]['price'],'HK$70.00')
  self.assertEqual(self.call(action={'type':'buy','product':'p1'})['quote']['amount'],7000)
  self.c.price({'product':'p1','price':'70','active':False},self.admin)
  self.assertTrue(self.call(action={'type':'buy','product':'p1'})['handoff'])
 def test_clear_history_preserves_customer_payments_orders_and_other_chats(self):
  self.fund();self.c.price({'product':'p1','price':'20','active':True},self.admin)
  code=self.call('護膚套裝')['products'][0]['choice']
  quote=self.call(code)['quote'];self.call(action={'type':'confirm','quote':quote['id']})
  unused=self.call(code)['quote']
  self.call('hi',scope='other');self.c.state({'scope':'test','status':'human'},self.admin)
  with self.db.db() as db:
   tables=['customers','lots','ledger','topups','wallet_orders','wallet_spends','conversation_events']
   before={t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in tables}
   balance=self.c.balance(db,self.customer)
  last=self.c.history('test')['messages'][-1]['id']
  result=self.c.clear_history({'scope':'test','confirmed':True,'last_id':last},{'id':'support','role':'support'})
  self.assertGreater(result['messages_deleted'],0);self.assertEqual(self.c.history('test')['messages'],[])
  self.assertEqual(self.c.history('test')['status'],'human');self.assertEqual(len(self.c.history('other')['messages']),2)
  with self.db.db() as db:
   self.assertEqual(before,{t:[tuple(r) for r in db.execute('SELECT * FROM '+t)] for t in tables})
   self.assertEqual(self.c.balance(db,self.customer),balance)
   self.assertEqual(db.execute('SELECT expires FROM purchase_quotes WHERE id=?',(unused['id'],)).fetchone()[0],0)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='chat.clear'").fetchone()[0],1)
  self.c.state({'scope':'test','status':'ai'},self.admin)
  self.assertIn('過期',self.call(code)['answer'])
  self.assertEqual(self.call('hi')['answer'],WELCOME)
 def test_clear_history_requires_permission_confirmation_and_fresh_snapshot(self):
  self.call('hi');last=self.c.history('test')['messages'][-1]['id'];body={'scope':'test','last_id':last,'confirmed':True}
  with self.assertRaises(AdminError):self.c.clear_history(body,{'id':'finance','role':'finance'})
  with self.assertRaises(AdminError):self.c.clear_history(dict(body,confirmed=False),self.admin)
  self.call('hi')
  with self.assertRaises(AdminError) as error:self.c.clear_history(body,self.admin)
  self.assertEqual(error.exception.status,409);self.assertEqual(len(self.c.history('test')['messages']),4)
  self.c.prepare_reply({'scope':'test','text':'處理中','request_id':'pending-clear'},self.admin)
  body['last_id']=self.c.history('test')['messages'][-1]['id']
  with self.assertRaises(AdminError):self.c.clear_history(body,self.admin)
 def test_decorated_product_codes_do_not_reach_model_or_debit(self):
  with self.db.db(True) as db:db.execute("INSERT INTO product_codes VALUES ('p1','RTY6')")
  self.call('護膚套裝');before=self.catalog.calls
  for q in ['RTY6呀','rty6 啦','買 RTY6 啊！','我想買「RTY6」唔該','RTY6?']:
   with self.subTest(q=q):
    result=self.call(q);self.assertTrue(result['insufficient']);self.assertTrue(result['topup_amount_input']);self.assertNotIn('topup_plans',result)
  self.assertIn('4 個字元',self.call('RTY6a')['answer'])
  self.assertEqual(self.catalog.calls,before)
  with self.db.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM wallet_orders').fetchone()[0],0)
  self.assertEqual(self.c.shortcuts('other','RTY6呀')[0],{})
  self.assertEqual(self.c.shortcuts('test','唔要 RTY6'),({},None))
 def test_custom_recharge_amount_both_qrs_receipt_review_and_purchase(self):
  self.db.plan_state({'id':self.plan,'action':'deactivate'},self.admin)
  self.c.price({'product':'p1','price':'230','active':True},self.admin)
  code=self.call('護膚套裝')['products'][0]['choice'];result=self.call(code)
  self.assertTrue(result['topup_amount_input']);result=self.call('230.50')
  self.assertEqual([p['qr'] for p in result['topup_methods']],['/payment-qr/payme','/payment-qr/alipay'])
  self.assertNotIn('topup_plans',result);result=self.call('PayMe');self.assertEqual(result['payment']['amount'],23050)
  proof={'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\ncustom-proof').decode()}
  submitted=self.call(action={'type':'receipt'},attachment=proof)
  topup=self.db.snapshot(self.admin)['topups'][0];self.assertIsNone(topup['plan']);self.assertEqual(topup['snapshot']['principal'],23050);self.assertEqual(topup['snapshot']['bonus'],0)
  self.assertEqual(self.c.snapshot(self.admin)['pending_topups'],1)
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),0)
  self.db.review({'id':topup['id'],'decision':'approved','reason':'Fixture custom','payment_verified':True,'verified_paid':'230.50','verified_reference':'custom-tx'},self.admin)
  quote=self.call(code)['quote'];result=self.call(action={'type':'confirm','quote':quote['id']});self.assertEqual(result['order']['balance_after'],50)
 def test_custom_recharge_receipt_before_method_keeps_proof_and_rejects_bad_amount(self):
  self.assertTrue(self.call('充值')['topup_amount_input'])
  for amount in ['0','-10','1.001','500 600','9999999999']:
   result=self.call(amount);self.assertTrue(result['topup_amount_input']);self.assertNotIn('payment',result)
  result=self.call('我想入 HK$1,000.50');self.assertEqual(result['topup_methods'][0]['amount'],100050)
  proof={'mime':'image/jpeg','data':base64.b64encode(b'\xff\xd8\xffproof-first').decode()}
  result=self.call(action={'type':'receipt'},attachment=proof);self.assertIn('唔使再傳截圖',result['answer']);self.assertEqual(self.c.snapshot(self.admin)['pending_topups'],0)
  result=self.call('Alipay');self.assertIn('已收到過數截圖',result['answer']);self.assertEqual(self.c.snapshot(self.admin)['pending_topups'],1)
  topup=self.db.snapshot(self.admin)['topups'][0];self.assertEqual(topup['method'],'alipay');self.assertEqual(topup['snapshot']['paid'],100050)
  self.assertEqual(self.call('充值 500 PayMe')['payment']['amount'],50000)
 def test_custom_recharge_drafts_are_scoped_expire_and_clear(self):
  self.call('充值 0');self.assertEqual(len(self.call('500')['topup_methods']),2)
  self.c.ensure('other','test',customer=self.customer)
  self.assertTrue(self.call('Alipay',scope='other')['topup_amount_input'])
  with self.db.db(True) as db:db.execute("UPDATE topup_drafts SET updated=0 WHERE scope='test'")
  self.assertTrue(self.call('PayMe')['topup_amount_input'])
  self.call('100');self.call('取消充值')
  self.assertTrue(self.call('PayMe')['topup_amount_input'])
  last=self.c.history('test')['messages'][-1]['id'];self.c.clear_history({'scope':'test','confirmed':True,'last_id':last},self.admin)
  with self.db.db() as db:self.assertIsNone(db.execute("SELECT * FROM topup_drafts WHERE scope='test'").fetchone())
 def test_named_product_with_number_is_not_an_invalid_recharge_amount(self):
  from engine import Catalog as RealCatalog
  catalog=RealCatalog();self.c=Commerce(self.db,catalog)
  self.call('充值')
  result=self.call('Hi 我想購買 delight 3 Pro專業套裝')
  self.assertEqual([p['id'] for p in result['products']],['691879818842a6000ac34afc'])
  self.assertNotIn('topup_amount_input',result)
 def proof(self,label):return {'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\n'+label.encode()).decode()}
 def test_rejected_reapply_photo_method_amount_and_paid_followup(self):
  self.call('充值 1200 Alipay')
  old=self.call(action={'type':'receipt'},attachment=self.proof('old'))['topup_id']
  self.db.review({'id':old,'decision':'rejected','reason':'不是付款憑證'},self.admin)
  result=self.call('再申請真的充值了',action={'type':'receipt'},attachment=self.proof('new'))
  self.assertTrue(result['topup_amount_input']);self.assertIn('已保留',result['answer'])
  result=self.call('Alipay');self.assertTrue(result['topup_amount_input']);self.assertIn('Alipay',result['answer'])
  result=self.call('1200');new=result['topup_id'];self.assertNotEqual(new,old);self.assertEqual(result['topup_status'],'pending');self.assertNotIn('payment',result)
  for text in ['充值了喇','已付款','入左了','我已經用 Alipay 過數']:
   followup=self.call(text);self.assertEqual(followup['topup_id'],new);self.assertEqual(followup['topup_status'],'pending')
  with self.db.db() as db:
   rows=db.execute('SELECT * FROM topups ORDER BY created').fetchall()
   self.assertEqual(len(rows),2);self.assertEqual(rows[0]['status'],'rejected');self.assertEqual(rows[1]['method'],'alipay')
   import json
   self.assertEqual(json.loads(rows[1]['snapshot'])['paid'],120000);self.assertEqual(self.c.balance(db,self.customer),0)
   self.assertIsNone(db.execute("SELECT * FROM topup_drafts WHERE scope='test'").fetchone())
  self.assertEqual(self.catalog.calls,0)
 def test_recharge_fields_can_arrive_in_any_order(self):
  import itertools,json
  for order in itertools.permutations(['amount','method','photo']):
   with self.subTest(order=order):
    scope='-'.join(order);self.c.ensure(scope,'test',customer=self.customer);self.call('充值',scope=scope)
    for field in order:
     if field=='photo':result=self.call('入左了',scope=scope,action={'type':'receipt'},attachment=self.proof(scope))
     else:result=self.call('1200' if field=='amount' else 'Alipay',scope=scope)
    self.assertEqual(result['topup_status'],'pending')
    with self.db.db() as db:
     row=db.execute('SELECT * FROM topups WHERE id=?',(result['topup_id'],)).fetchone()
     self.assertEqual(row['method'],'alipay');self.assertEqual(json.loads(row['snapshot'])['paid'],120000)
     self.assertEqual(db.execute('SELECT COUNT(*) FROM payment_intents WHERE scope=? AND topup IS NOT NULL',(scope,)).fetchone()[0],1)
  self.assertEqual(self.catalog.calls,0)
 def test_paid_statement_without_receipt_does_not_resend_qr_or_credit(self):
  result=self.call('已付款');self.assertTrue(result['topup_amount_input'])
  self.call('500 PayMe')
  for text in ['充值了喇','已付款','我已經用 PayMe 過數']:
   result=self.call(text);self.assertIn('請傳過數截圖',result['answer']);self.assertNotIn('payment',result)
  with self.db.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM topups').fetchone()[0],0);self.assertEqual(self.c.balance(db,self.customer),0)
  from chat_inputs import recharge_input
  self.assertNotEqual((recharge_input('未付款',True) or {}).get('type'),'topup_progress')
  self.assertNotEqual((recharge_input('未充值',True) or {}).get('type'),'topup_progress')
 def test_reupload_after_rejection_preserves_old_record_and_deduplicates_pending(self):
  proof=self.proof('same-receipt');pay=self.call('充值 100 PayMe')['payment']['id']
  old=self.call(action={'type':'receipt','intent':pay},attachment=proof)['topup_id']
  self.db.review({'id':old,'decision':'rejected','reason':'補充資料後重交'},self.admin)
  self.assertEqual(self.call(action={'type':'receipt','intent':pay},attachment=proof)['topup_status'],'rejected')
  self.call(action={'type':'receipt'},attachment=proof);self.call('100');new=self.call('PayMe')['topup_id'];self.assertNotEqual(new,old)
  self.assertEqual(self.call(action={'type':'receipt'},attachment=proof)['topup_id'],new)
  self.db.review({'id':new,'decision':'approved','reason':'核對','payment_verified':True,'verified_paid':'100','verified_reference':'reapply-tx'},self.admin)
  self.assertEqual(self.call(action={'type':'receipt'},attachment=proof)['topup_status'],'approved')
  with self.db.db() as db:
   self.assertEqual(db.execute('SELECT COUNT(*) FROM topups').fetchone()[0],2);self.assertEqual(self.c.balance(db,self.customer),10000)
   self.assertEqual(db.execute("SELECT COUNT(*) FROM audit WHERE action='topup.receipt_resubmit'").fetchone()[0],1)
   self.assertEqual(db.execute('SELECT status FROM topups WHERE id=?',(old,)).fetchone()[0],'rejected')
  self.c.ensure('other','test',customer=self.customer)
  self.assertTrue(self.call(action={'type':'receipt'},scope='other',attachment=proof)['handoff'])
 def test_reapply_does_not_recover_older_abandoned_or_cancelled_payment(self):
  self.call('充值 200 PayMe');self.call('充值 100 Alipay')
  old=self.call(action={'type':'receipt'},attachment=self.proof('reject-latest'))['topup_id']
  self.db.review({'id':old,'decision':'rejected','reason':'拒絕'},self.admin)
  result=self.call(action={'type':'receipt'},attachment=self.proof('new-unknown-amount'))
  self.assertTrue(result['topup_amount_input'])
  with self.db.db() as db:
   draft=db.execute("SELECT * FROM topup_drafts WHERE scope='test'").fetchone();self.assertIsNone(draft['amount']);self.assertIsNone(draft['method'])
  self.call('取消充值');self.assertTrue(self.call('Alipay')['topup_amount_input'])
  self.call('100');self.call('重新申請');self.assertTrue(self.call('PayMe')['topup_amount_input'])
  self.assertEqual(self.catalog.calls,0)
if __name__=='__main__':unittest.main()
