import concurrent.futures,json,secrets,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_domain import AdminDB,AdminError
from commerce import Commerce

class Catalog:
 def __init__(self):
  self.calls=0
  self.items=[{'id':'p1','title':'護髮膜','price':'HK$230','_price_cents':23000,'_tokens':{'髮'},'url':'https://www.naturalia-group.com/products/mask','image':'/product-images/'+'a'*24},
              {'id':'p2','title':'面霜','price':'HK$100','_price_cents':10000,'url':'https://www.naturalia-group.com/products/cream'}]
 def chat(self,q,*a,**k):
  self.calls+=1
  return {'answer':'商品','mode':'retrieval','products':[{k:v for k,v in p.items() if not k.startswith('_')} for p in self.items if q=='全部' or q in p['title']]}

class CheckoutTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=AdminDB(Path(self.tmp.name)/'db');self.catalog=Catalog();self.flow=Commerce(self.db,self.catalog);self.staff={'id':'fixture','role':'admin'}
  self.customer=self.db.create_customer({'company':'Fixture','contact':'Test','phone':'90000001'},self.staff)['id']
  plan=self.db.create_plan({'name':'Fixture','paid':'1200','principal':'1200','bonus':'0'},self.staff)['id']
  topup=self.db.create_topup({'request_key':'fund','customer':self.customer,'plan':plan,'method':'payme','reference':'fixture-tx','evidence':'fixture'},self.staff)
  self.db.review({'id':topup['id'],'decision':'approved','reason':'Fixture','payment_verified':True,'verified_paid':'1200'},self.staff)
  with self.db.db(True) as c:c.execute("INSERT INTO product_codes VALUES ('p1','JWU3')");c.execute("INSERT INTO product_codes VALUES ('p2','ABCD')")
  self.flow.ensure('chat','test',customer=self.customer);self.call('全部')
 def tearDown(self):self.tmp.cleanup()
 def call(self,text='操作',scope='chat',**kwargs):return self.flow.respond(scope,text,secrets.token_hex(12),use_model=False,**kwargs)
 def balance(self):
  with self.db.db() as c:return self.flow.balance(c,self.customer)
 def test_natural_confirmations_use_current_quote_without_search(self):
  self.catalog.items[0]['_price_cents']=1000
  before_calls=self.catalog.calls
  for phrase in ['確認','確認購買','我確認','確定','OK','okay','yes please','好呀','係喇','可以','好啊買啦','照落單']:
   with self.subTest(phrase=phrase):
    quote=self.call('買 JWU3')['quote'];result=self.call(phrase)
    self.assertEqual(result['order']['amount'],1000);self.assertEqual(result['order']['quantity'],1)
    self.assertEqual(self.call(phrase)['order']['id'],result['order']['id'])
  self.assertEqual(self.catalog.calls,before_calls)
 def test_quantity_words_and_code_variants_requote_without_debit(self):
  self.call('JWU3')
  for phrase in ['買2個','買 2 個','改兩件','我要兩個','我想買兩個','JWU3 × 2','買 JWU3 2個','買兩件 JWU3','JWU3 x2','數量改為2','確認買2個','買兩個點算？','唔係，我要2個','呢個兩件']:
   with self.subTest(phrase=phrase):
    result=self.call(phrase);self.assertEqual(result['quote']['quantity'],2);self.assertEqual(result['quote']['amount'],46000);self.assertEqual(result['quote']['unit_price'],23000)
    self.assertEqual(result['products'][0]['id'],'p1');self.assertEqual(self.balance(),120000)
  result=self.call('確認');self.assertEqual(result['order']['quantity'],2);self.assertEqual(result['order']['amount'],46000);self.assertEqual(self.balance(),74000)
  self.assertIn('× 2',result['answer']);self.assertEqual(self.flow.snapshot(self.staff)['orders'][0]['unit_price'],23000)
 def test_changed_quantity_invalidates_old_buttons_and_numbers(self):
  old=self.call('JWU3')['quote'];new=self.call('買2個')['quote']
  for kwargs in [{'text':'確認購買 '+str(old['choice'])},{'action':{'type':'confirm','quote':old['id']}}]:
   result=self.call(**kwargs);self.assertNotIn('order',result);self.assertEqual(result['products'][0]['id'],'p1')
  self.assertEqual(self.balance(),120000);self.assertEqual(self.call('確認')['order']['quantity'],2)
 def test_missing_number_question_and_typo_keep_selected_product(self):
  self.call('JWU3');calls=self.catalog.calls
  for phrase in ['確認購買 999','確認？','確認可以嗎','確認得唔得','確任','confirm?','買多啲','數量點改']:
   result=self.call(phrase);self.assertEqual(result['products'][0]['id'],'p1');self.assertNotIn('order',result);self.assertFalse(result.get('handoff'))
  self.assertEqual(self.catalog.calls,calls);self.assertEqual(self.balance(),120000)
 def test_cancel_and_negative_reply_never_debit_or_reuse_old_quote(self):
  for phrase in ['取消購買','唔買住','唔好扣款','唔確認','不确认']:
   q=self.call('JWU3')['quote'];self.call(phrase)
   self.assertNotIn('order',self.call('確認'));self.assertNotIn('order',self.call(action={'type':'confirm','quote':q['id']}));self.assertEqual(self.balance(),120000)
 def test_expired_quote_and_changed_unit_price_require_new_confirmation(self):
  old=self.call('JWU3 2個')['quote']
  with self.db.db(True) as c:c.execute('UPDATE purchase_quotes SET expires=? WHERE id=?',(time.time()-1,old['id']))
  fresh=self.call('確認');self.assertNotIn('order',fresh);self.assertEqual(fresh['quote']['quantity'],2);self.assertEqual(self.balance(),120000)
  self.catalog.items[0]['_price_cents']=24000
  fresh=self.call('確認');self.assertNotIn('order',fresh);self.assertEqual(fresh['quote']['amount'],48000);self.assertEqual(fresh['products'][0]['price'],'HK$240.00')
  self.assertEqual(self.call('確認')['order']['amount'],48000)
 def test_invalid_quantities_never_change_order_or_call_model(self):
  self.call('JWU3');calls=self.catalog.calls
  for phrase in ['買0個','買-1個','買1.5個','買100個','JWU3 100個']:
   result=self.call(phrase);self.assertNotIn('quote',result);self.assertNotIn('order',result);self.assertTrue(result['products'])
  for qty in [0,-1,100,True,2.5,None]:
   result=self.call(action={'type':'buy','product':'p1','quantity':qty});self.assertNotIn('quote',result)
  self.assertEqual(self.balance(),120000);self.assertEqual(self.catalog.calls,calls)
  self.assertNotIn('order',self.call('確認')) # Invalid quantity needs a fresh displayed quote.
 def test_quantity_without_unambiguous_product_shows_candidates(self):
  result=self.call('買2個');self.assertNotIn('quote',result);self.assertEqual(len(result['products']),2)
  self.call('JWU3');self.call('全部');result=self.call('買2個');self.assertNotIn('quote',result);self.assertEqual(len(result['products']),2)
  self.call('有意購買 ABCD');self.assertEqual(self.call('買2個')['quote']['product'],'p2')
 def test_unrelated_search_suspends_confirmation_and_preserves_candidates(self):
  self.call('JWU3');self.call('面霜');result=self.call('OK');self.assertNotIn('order',result);self.assertEqual(result['products'][0]['id'],'p2');self.assertEqual(self.balance(),120000)
 def test_insufficient_total_retains_product_and_quantity(self):
  result=self.call('JWU3 6個');self.assertTrue(result['insufficient']);self.assertEqual(result['shortfall'],18000);self.assertEqual(result['selection']['quantity'],6);self.assertEqual(result['products'][0]['id'],'p1')
  result=self.call('改兩件');self.assertEqual(result['quote']['amount'],46000)
 def test_parallel_quantity_purchases_cannot_overdraw(self):
  self.catalog.items[0]['_price_cents']=40000
  self.flow.ensure('other','test',customer=self.customer)
  a=self.call(action={'type':'buy','product':'p1','quantity':2})['quote'];b=self.call(scope='other',action={'type':'buy','product':'p1','quantity':2})['quote']
  with concurrent.futures.ThreadPoolExecutor() as pool:
   results=list(pool.map(lambda args:self.flow.purchase(args[0],self.customer,args[1]),[('chat',a['id']),('other',b['id'])]))
  self.assertEqual(sum('order' in r for r in results),1);self.assertEqual(self.balance(),40000)
  failed=next(r for r in results if 'order' not in r);self.assertEqual(failed['selection']['quantity'],2);self.assertTrue(failed['products'])
 def test_existing_pre_upgrade_quote_is_reshown_not_silently_charged(self):
  self.call('JWU3')
  with self.db.db(True) as c:c.execute('DELETE FROM purchase_context')
  result=self.call('確認');self.assertIn('quote',result);self.assertNotIn('order',result);self.assertEqual(result['products'][0]['id'],'p1');self.assertEqual(self.balance(),120000)
 def test_cross_scope_confirm_and_human_takeover_cannot_charge(self):
  q=self.call('JWU3')['quote'];self.flow.ensure('other','test',customer=self.customer)
  self.assertNotIn('order',self.call(scope='other',action={'type':'confirm','quote':q['id']}))
  self.flow.state({'scope':'chat','status':'human'},self.staff);self.assertTrue(self.call('確認')['handoff'])
  self.flow.state({'scope':'chat','status':'ai'},self.staff);self.assertIn('quote',self.call('確認'));self.assertEqual(self.balance(),120000)
 def test_testchat_reset_expires_purchase_context(self):
  from admin_chat import AdminChat
  scope='test:fixture:retrieval:'+self.customer
  self.flow.ensure(scope,'test',customer=self.customer);q=self.call(scope=scope,action={'type':'buy','product':'p1'})['quote']
  AdminChat(self.db,self.catalog,commerce=self.flow).handle(self.staff,'reset',{'customer':self.customer})
  self.assertNotIn('order',self.call('確認',scope=scope));self.assertNotIn('order',self.call(scope=scope,action={'type':'confirm','quote':q['id']}));self.assertEqual(self.balance(),120000)

if __name__=='__main__':unittest.main()
