import base64,json,secrets,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_domain import AdminDB
from commerce import Commerce
from engine import Catalog
from storage import Store
from whatsapp_backend import WhatsAppBackend
import kimi

class DialogueTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=AdminDB(Path(self.tmp.name)/'db');self.catalog=Catalog();self.c=Commerce(self.db,self.catalog);self.staff={'id':'fixture','role':'admin'}
  self.customer=self.db.create_customer({'company':'Fixture','contact':'Fixture','phone':'90000001'},self.staff)['id']
  self.c.ensure('test','test',customer=self.customer)
  self.product=next(p for p in self.catalog.items if p['title']=='維他命原B5植物深層修護髪膜');self.expensive=next(p for p in self.catalog.items if p['title']=='Delight 3 Pro專業套裝')
  self.c.add_shortcuts('test',self.c.result('商品',products=[self.product,self.expensive]))
  with self.db.db() as c:self.code=c.execute('SELECT code FROM product_codes WHERE product=?',(self.product['id'],)).fetchone()[0]
  plan=self.db.create_plan({'name':'fixture','paid':'1200','principal':'1200','bonus':'0'},self.staff)['id']
  topup=self.db.create_topup({'request_key':'fund','customer':self.customer,'plan':plan,'method':'payme','reference':'fixture-tx','evidence':'fixture'},self.staff)
  self.db.review({'id':topup['id'],'decision':'approved','reason':'fixture','payment_verified':True,'verified_paid':'1200'},self.staff)
 def tearDown(self):self.tmp.cleanup()
 def call(self,text,**kwargs):return self.c.respond('test',text,secrets.token_hex(12),use_model=kwargs.pop('use_model',False),**kwargs)
 def balance(self):
  with self.db.db() as c:return self.c.balance(c,self.customer)
 def draft(self):
  with self.db.db() as c:
   row=c.execute("SELECT * FROM topup_drafts WHERE scope='test'").fetchone();return dict(row) if row else None
 def proof(self):return {'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\n'+secrets.token_bytes(12)).decode()}
 def test_recharge_does_not_reuse_purchase_confirmation(self):
  old=self.call(self.code)['quote'];self.call('充值')
  for text in ['好','OK','確認','明白','然後呢']:
   result=self.call(text);self.assertNotIn('order',result);self.assertEqual(result['dialogue']['topic'],'recharge')
  self.assertEqual(self.balance(),120000)
  self.assertNotIn('order',self.call('按舊按鈕',action={'type':'confirm','quote':old['id']}))
 def test_browse_after_recharge_does_not_consume_numbers_as_amount(self):
  self.call('充值500');before=self.draft();self.call('保濕面霜')
  result=self.call('900');self.assertTrue(result['clarification']);self.assertNotIn('payment',result);self.assertEqual(before,self.draft())
  self.call('繼續充值');self.assertEqual(self.call('700')['topup_methods'][0]['amount'],70000)
 def test_budget_short_followup_and_more_keep_cap_and_skip_previous_cards(self):
  self.call('充值500');draft=self.draft()
  a=self.call('1200以下');b=self.call('500');self.assertEqual(b['budget']['cents'],50000)
  second=self.call('其他款');self.assertTrue(second['products']);self.assertEqual(second['budget']['cents'],50000)
  self.assertFalse({p['id'] for p in b['products']} & {p['id'] for p in second['products']})
  for p in second['products']:self.assertLessEqual(float(p['price'].replace('HK$','').replace(',','')),500)
  self.assertEqual(draft,self.draft());self.assertEqual(self.balance(),120000)
 def test_cheaper_after_expensive_product_really_changes_candidates(self):
  self.call('Delight 3 Pro');result=self.call('平啲')
  self.assertNotIn(self.expensive['id'],[p['id'] for p in result['products']]);self.assertTrue(result['budget']);self.assertLess(result['budget']['cents'],660000)
 def test_ordinals_use_visible_card_order(self):
  result=self.call('保濕面霜');self.assertGreaterEqual(len(result['products']),2);expected=result['products'][1]['id']
  selected=self.call('買第二款');self.assertEqual(selected['products'][0]['id'],expected);self.assertEqual(selected['selection']['product'],expected)
  self.assertEqual(self.balance(),120000)
 def test_ambiguous_references_and_multi_product_requests_never_charge(self):
  cards=self.call('保濕面霜')['products']
  for text in ['買呢個','都要','第一同第二','好']:
   result=self.call(text);self.assertTrue(result['clarification']);self.assertEqual([p['id'] for p in result['products']],[p['id'] for p in cards]);self.assertNotIn('order',result)
  self.assertEqual(self.balance(),120000)
 def test_single_product_reference_price_and_buy(self):
  self.call(self.product['title']);price=self.call('呢個幾錢')
  self.assertIn('HK$230.00',price['answer']);self.assertEqual(price['products'][0]['id'],self.product['id'])
  quote=self.call('買呢個')['quote'];self.assertEqual(quote['product'],self.product['id']);self.assertEqual(self.call('確認')['order']['amount'],23000)
 def test_quantity_correction_pause_and_cancel_are_scoped(self):
  self.call(self.code);self.assertEqual(self.call('改為兩')['quote']['quantity'],2)
  self.call('等陣先');self.assertNotIn('order',self.call('OK'));self.assertEqual(self.balance(),120000)
  self.assertIn('quote',self.call('繼續購買'));self.call('取消');self.assertNotIn('order',self.call('確認'))
  self.call('充值500');self.call('取消');self.assertIsNone(self.draft());self.assertEqual(self.balance(),120000)
 def test_wallet_interrupt_requires_new_quote(self):
  self.call(self.code);self.assertIn('HK$1,200.00',self.call('仲有幾多錢')['answer'])
  result=self.call('確認');self.assertNotIn('order',result);self.assertIn('quote',result);self.assertEqual(self.balance(),120000)
 def test_topup_method_edit_and_help_do_not_lose_receipt(self):
  self.call('充值');self.call('1200');self.call('PayMe');self.assertEqual(self.call('改用Alipay')['payment']['method'],'alipay')
  result=self.call('PayMe定Alipay');self.assertIn('PayMe',result['answer']);self.assertNotIn('topup_id',result)
  self.assertEqual(self.draft()['method'],'alipay');self.assertEqual(self.draft()['amount'],120000)
  self.assertEqual(self.call('已付款')['dialogue']['topic'],'recharge')
  submitted=self.call('入左了',attachment=self.proof());self.assertEqual(submitted['topup_status'],'pending')
  self.assertEqual(self.call('收到未')['topup_id'],submitted['topup_id']);self.assertEqual(self.balance(),120000)
 def test_pending_and_approved_topup_never_make_acknowledgement_buy(self):
  self.call('買貴貨',action={'type':'buy','product':self.expensive['id']});self.call('充值500 PayMe')
  topup=self.call('過數截圖',attachment=self.proof())['topup_id']
  self.assertNotIn('order',self.call('好'));self.assertEqual(self.call('核款進度')['topup_status'],'pending')
  self.db.review({'id':topup,'decision':'approved','reason':'fixture','payment_verified':True,'verified_paid':'500','verified_reference':'new-tx'},self.staff);self.c.notify_review(topup)
  self.assertNotIn('order',self.call('確認'));self.assertEqual(self.balance(),170000)
  result=self.call('繼續購買');self.assertTrue(result['insufficient']);self.assertEqual(result['selection']['product'],self.expensive['id'])
 def test_general_picture_is_not_automatically_a_payment_proof(self):
  result=self.call('圖片',attachment=self.proof());self.assertTrue(result['handoff']);self.assertIsNone(self.draft())
  user=next(m for m in self.c.history('test')['messages'] if m['role']=='user');self.assertTrue(user['payload']['evidence_id'])
  self.assertTrue(self.call('產品圖片')['handoff'])
  with self.db.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM topups').fetchone()[0],1)
 def test_explicit_receipt_before_amount_does_not_require_reupload(self):
  result=self.call('係過數截圖',attachment=self.proof());self.assertTrue(result['topup_amount_input']);self.assertTrue(self.draft()['evidence'])
  self.call('500');submitted=self.call('Alipay');self.assertEqual(submitted['topup_status'],'pending')
 def test_cancel_completed_order_goes_to_cs(self):
  self.call(self.code);self.call('確認');self.assertTrue(self.call('取消訂單')['handoff']);self.assertEqual(self.balance(),97000)
 def test_negative_cs_request_and_explicit_handover(self):
  self.assertFalse(self.call('唔使搵真人')['answer'].startswith('呢個問題需要 CS'))
  result=self.call('搵職員');self.assertTrue(result['handoff']);self.assertTrue(self.call('繼續購買')['handoff'])
 def test_state_is_per_conversation_and_survives_service_reload(self):
  self.call('充值500');self.c=Commerce(self.db,self.catalog);self.assertEqual(self.call('Alipay')['payment']['amount'],50000)
  self.c.ensure('other','test',customer=self.customer)
  result=self.c.respond('other','500','one',use_model=False);self.assertNotIn('payment',result);self.assertIsNone(self.c.purchase_context('other'))
 def test_semantic_interpretation_is_read_only_and_fails_to_clarification(self):
  self.call(self.code)
  with patch('kimi.configured',return_value=True),patch('kimi.interpret',return_value='clarify') as interpret:
   result=self.call('幫我處理埋啦',use_model=True);self.assertTrue(result['clarification']);self.assertNotIn('order',result);interpret.assert_called_once()
  self.assertEqual(self.balance(),120000)
  with patch('kimi.configured',return_value=True),patch('kimi.interpret',side_effect=kimi.ModelError('fixture')):
   self.assertTrue(self.call('唔係呢個意思喎',use_model=True)['clarification'])
 def test_semantic_cheaper_routes_to_valid_price_filtered_search(self):
  self.call('Delight 3 Pro')
  with patch('kimi.configured',return_value=True),patch('kimi.interpret',return_value='cheaper'),patch('kimi.generate',side_effect=kimi.ModelError('fixture')):
   result=self.call('嗰個價我接受唔到',use_model=True)
  self.assertTrue(result.get('budget'));self.assertNotIn('order',result);self.assertEqual(self.balance(),120000)
 def test_acknowledgement_after_ambiguous_purchase_reply_requires_new_quote(self):
  self.call(self.code)
  with patch('kimi.configured',return_value=True),patch('kimi.interpret',return_value='clarify'):
   self.assertTrue(self.call('幫我處理埋啦',use_model=True)['clarification'])
  result=self.call('好');self.assertNotIn('order',result);self.assertEqual(self.balance(),120000)
 def test_payment_policy_question_is_not_an_invalid_amount(self):
  self.call('充值');result=self.call('充值有手續費嗎？');self.assertTrue(result['handoff']);self.assertNotIn('請提供一個大於',result['answer'])
 def test_router_model_rejects_money_operations_and_unknown_json(self):
  for value in ['confirm','charge','topup_amount','approve',None]:
   response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps({'choices':[{'message':{'content':json.dumps({'intent':value})}}]}).encode()
   with patch.dict('os.environ',{'KIMI_API_BASE':'https://example.invalid/v1','KIMI_API_KEY':'fixture','KIMI_MODEL':'fixture'}),patch('urllib.request.urlopen',return_value=response):
    with self.assertRaises(kimi.ModelError):kimi.interpret('anything',{'topic':'purchase'})
 def test_quantity_slots_named_product_delta_and_reorder(self):
  result=self.call('我想要'+self.product['title']+'兩個唔該');self.assertEqual(result['quote']['quantity'],2)
  self.assertEqual(self.call('加多1個')['quote']['quantity'],3)
  self.assertEqual(self.call('減少2個')['quote']['quantity'],1)
  self.assertNotIn('quote',self.call('減少1個'));self.assertNotIn('order',self.call('確認'))
  self.call('買2個');self.call('確認');self.assertEqual(self.balance(),74000)
  result=self.call('再買2個');self.assertEqual(result['quote']['quantity'],2);self.assertEqual(self.balance(),74000)
 def test_category_quantity_is_carried_until_selection(self):
  result=self.call('我想買2個面霜');self.assertTrue(result['products']);self.assertNotIn('quote',result)
  product=result['products'][0]['id'];selected=self.call('第一款')
  self.assertEqual(selected['selection'],{'product':product,'quantity':2});self.assertEqual(self.balance(),120000)
 def test_ordinal_and_quantity_in_same_sentence(self):
  products=self.call('面霜')['products'];result=self.call('第二款要兩個')
  self.assertEqual(result['selection'],{'product':products[1]['id'],'quantity':2});self.assertEqual(self.balance(),120000)
 def test_invalid_quantity_embedded_in_sentence_never_uses_partial_number(self):
  self.call(self.code)
  for q in ['我要呢個1.5個唔該','我想要呢款-1個呀','加多100個']:
   result=self.call(q);self.assertNotIn('quote',result);self.assertNotIn('order',result);self.assertEqual(self.balance(),120000)
 def test_no_more_alternatives_does_not_handover_or_repeat_cards(self):
  self.call('面霜')
  with patch.object(self.catalog,'chat',return_value={'answer':'沒有','products':[],'mode':'handoff'}):result=self.call('其他款')
  self.assertFalse(result.get('handoff'));self.assertEqual(result['products'],[]);self.assertIn('調整預算',result['answer'])
 def test_product_picture_during_recharge_does_not_submit_payment(self):
  self.call('充值500 PayMe');draft=self.draft();result=self.call('呢個產品幾錢',attachment=self.proof())
  self.assertTrue(result['handoff']);self.assertEqual(draft,self.draft())
  with self.db.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM topups').fetchone()[0],1)
 def test_status_query_does_not_submit_saved_evidence(self):
  self.call('充值500 PayMe')
  proof=self.db.save_evidence('fixture','image/png',base64.b64decode(self.proof()['data']),self.staff)
  self.c.draft('test',evidence=proof['id']);draft=self.draft();result=self.call('核款進度')
  self.assertNotIn('topup_id',result);self.assertEqual(draft,self.draft())
  with self.db.db() as c:self.assertEqual(c.execute('SELECT COUNT(*) FROM topups').fetchone()[0],1)
 def test_whatsapp_observe_and_reply_keep_one_user_turn_and_context(self):
  store=Store(Path(self.tmp.name)/'wa');backend=WhatsAppBackend(store,self.catalog,self.c)
  def send(q,attachment=None):
   data={'account':'fixture','peer':'fixture@lid','phone':'85290000001','event':secrets.token_hex(12),'text':q}
   if attachment:data['attachment']=attachment
   backend.observe(data)
   with patch('kimi.configured',return_value=False):result=backend.handle(data);self.assertEqual(json.loads(json.dumps(result)),backend.handle(data))
   return result
  send('充值');self.assertIn('topup_methods',send('500'))
  search=send('1200以下');self.assertEqual(search['dialogue']['topic'],'search')
  result=send('圖片',self.proof());self.assertTrue(result['handoff']);self.assertEqual(result['dialogue']['topic'],'support')
  with self.db.db() as c:
   scope=c.execute("SELECT id FROM conversations WHERE peer='fixture@lid'").fetchone()[0]
   self.assertEqual(c.execute("SELECT COUNT(*) FROM conversation_messages WHERE scope=? AND role='user'",(scope,)).fetchone()[0],4)
 def test_unclear_turns_and_ai_identity_do_not_force_handover(self):
  self.call('充值500')
  for text in ['你係咪人工智能','唔使搵真人','唔使CS','唔好轉人工','不用找客服','仲未明','唔係呢個意思','你講咩呀','都唔係']:
   result=self.call(text);self.assertFalse(result.get('handoff'),text)
  self.assertEqual(self.c.history('test')['status'],'ai');self.assertIn('payment',self.call('Alipay'))
 def test_handover_then_topic_changes_cannot_mutate_wallet_or_topup(self):
  quote=self.call(self.code)['quote'];self.call('充值500');draft=self.draft();self.call('產品圖片',attachment=self.proof())
  for status in ['waiting','human']:
   if status=='human':self.c.state({'scope':'test','status':'human'},self.staff)
   for q,action in [('500以下',None),('繼續購買',None),('確認',None),('唔使CS，買兩個',None),('充值700',None),('查餘額',{'type':'balance'}),('舊確認',{'type':'confirm','quote':quote['id']})]:
    result=self.call(q,action=action);self.assertTrue(result['handoff']);self.assertEqual(result['dialogue']['topic'],'support');self.assertNotIn('quote',result);self.assertNotIn('order',result)
   self.assertEqual(self.c.history('test')['status'],status);self.assertEqual(self.balance(),120000);self.assertEqual(self.draft(),draft)
  self.c.state({'scope':'test','status':'ai'},self.staff)
  result=self.call('繼續購買');self.assertIn('quote',result);self.assertNotIn('order',result);self.assertEqual(self.balance(),120000)
 def test_human_takeover_during_model_interpretation_blocks_operation(self):
  self.call(self.code)
  def takeover(*a):self.c.state({'scope':'test','status':'human'},self.staff);return 'balance'
  with patch('kimi.configured',return_value=True),patch('kimi.interpret',side_effect=takeover):result=self.call('戶口幫我睇睇',use_model=True)
  self.assertTrue(result['handoff']);self.assertNotIn('1,200',result['answer']);self.assertEqual(self.c.history('test')['status'],'human')
 def test_cached_quote_is_suppressed_after_handover(self):
  result=self.c.respond('test',self.code,'same-event',use_model=False);self.assertIn('quote',result)
  self.call('搵真人');retry=self.c.respond('test',self.code,'same-event',use_model=False,channel='whatsapp')
  self.assertTrue(retry['suppress_delivery']);self.assertNotIn('quote',retry);self.assertEqual(self.balance(),120000)
 def test_handover_does_not_demote_assigned_cs(self):
  self.c.state({'scope':'test','status':'human'},self.staff);self.c.handover('test','late model handoff')
  self.assertEqual(self.c.history('test')['status'],'human')
 def test_explicit_non_payment_image_during_recharge_goes_to_cs(self):
  self.call('充值500 PayMe');before=self.draft();result=self.call('唔係付款截圖，幫我睇下',attachment=self.proof())
  self.assertTrue(result['handoff']);self.assertEqual(before,self.draft())

if __name__=='__main__':unittest.main()
