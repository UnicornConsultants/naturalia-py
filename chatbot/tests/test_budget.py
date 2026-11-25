import json,secrets,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from shopping_budget import shopping_budget,within_budget
from chat_inputs import recharge_input
from engine import Catalog
from commerce import Commerce
from admin_domain import AdminDB
from storage import Store
from whatsapp_backend import WhatsAppBackend

class BudgetTests(unittest.TestCase):
 def test_budget_phrasings_never_become_recharge_amounts(self):
  for q in ['有沒有什麼是1200以下的？','有冇1200蚊以下嘅產品','HK$1,200以內','1200以内','不超過1200','最多1200','預算1200','under HK$1,200','低於1200','1200.50以下']:
   with self.subTest(q=q):
    budget=shopping_budget(q);self.assertIsNotNone(budget);self.assertIsNone(recharge_input(q,True))
  self.assertEqual(recharge_input('1200',True)['type'],'topup_amount')
  self.assertEqual(recharge_input('充值1200',True)['type'],'topup_amount')
  self.assertEqual(recharge_input('-1200',True)['type'],'topup_invalid')
  self.assertEqual(recharge_input('1200.555',True)['type'],'topup_invalid')
 def test_integer_price_boundaries_and_unknown_prices(self):
  inclusive=shopping_budget('1200以下');strict=shopping_budget('低於1200')
  self.assertTrue(within_budget(120000,inclusive));self.assertFalse(within_budget(120000,strict));self.assertFalse(within_budget(120001,inclusive))
  self.assertEqual(shopping_budget('HK$1,200.50以內')['cents'],120050)
  for value in [None,0,True,-1]:self.assertFalse(within_budget(value,inclusive))
 def test_budget_only_search_has_results_and_never_exceeds_current_limit(self):
  catalog=Catalog()
  for query in ['有沒有什麼是1200以下的？','1200以內','預算1200','under HK$1,200']:
   with self.subTest(query=query):
    results=catalog.search(query,[{'role':'user','content':'預算5000 Delight 3 Pro專業套裝'}]);self.assertTrue(results)
    for product in results:self.assertLessEqual(float(product['price'].replace('HK$','').replace(',','')),1200)
 def test_named_product_over_budget_has_no_unrelated_replacement(self):
  result=Catalog().chat('Delight 3 Pro 1200以下',use_model=False)
  self.assertEqual(result['products'],[]);self.assertIn('暫時未找到',result['answer']);self.assertTrue(result['budget'])
 def test_override_price_and_disabled_products_are_filtered_before_model(self):
  catalog=Catalog();product=catalog.search('Delight 3 Pro')[0]
  self.assertEqual(catalog.search('Delight 3 Pro 1200以下'),[])
  discounted=catalog.search('Delight 3 Pro 1200以下',price_overrides={product['id']:120000})
  self.assertEqual(discounted[0]['price'],'HK$1,200.00')
  self.assertEqual(catalog.search('Delight 3 Pro 1200以下',price_overrides={product['id']:None}),[])
  self.assertEqual(catalog.search('Delight 3 Pro 1200以下',price_overrides={product['id']:120001}),[])
 def test_whatsapp_budget_switch_preserves_recharge_draft_and_finances(self):
  with tempfile.TemporaryDirectory() as folder:
   db=AdminDB(Path(folder)/'b2b.sqlite3');catalog=Catalog();flow=Commerce(db,catalog);adapter=WhatsAppBackend(Store(Path(folder)/'chat.sqlite3'),catalog,flow)
   base={'account':'fixture','peer':'85290000001@s.whatsapp.net'}
   def send(text):
    body={**base,'event':secrets.token_hex(8),'text':text}
    with patch('kimi.configured',return_value=False):adapter.observe(body);return adapter.handle(body)
   send('Hi 我想購買 delight 3 Pro專業套裝')
   scope=flow.snapshot({'role':'admin'})['conversations'][0]['id']
   with db.db() as c:code=c.execute("SELECT code FROM product_codes WHERE product='691879818842a6000ac34afc'").fetchone()[0]
   self.assertTrue(send('買'+code)['insufficient'])
   with db.db() as c:
    draft=tuple(c.execute('SELECT * FROM topup_drafts WHERE scope=?',(scope,)).fetchone())
    tables=['topups','ledger','lots','wallet_orders','payment_intents'];before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
   result=send('有沒有什麼是1200以下的？')
   self.assertTrue(result['products']);self.assertNotIn('topup_amount_input',result);self.assertFalse(result.get('handoff'));self.assertEqual(result['budget']['cents'],120000)
   for p in result['products']:self.assertLessEqual(float(p['price'].replace('HK$','').replace(',','')),1200);self.assertTrue(p['choice']);self.assertTrue(p['image'])
   empty=send('有冇1蚊以下嘅產品？');self.assertEqual(empty['products'],[]);self.assertFalse(empty.get('handoff'));self.assertIn('暫時未找到',empty['answer'])
   with db.db() as c:
    self.assertEqual(tuple(c.execute('SELECT * FROM topup_drafts WHERE scope=?',(scope,)).fetchone()),draft)
    self.assertEqual(before,{t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables})
 def test_final_card_price_recheck_cannot_show_over_budget_item(self):
  with tempfile.TemporaryDirectory() as folder:
   db=AdminDB(Path(folder)/'db');flow=Commerce(db,Catalog());product=flow.catalog.search('1200以下')[0]
   flow.price({'product':product['id'],'price':'1300','active':True},{'id':'fixture','role':'admin'})
   result=flow.add_shortcuts('test',{'answer':'候選商品','products':[product],'budget':shopping_budget('1200以下')})
   self.assertEqual(result['products'],[]);self.assertIn('售價已更新',result['answer'])

if __name__=='__main__':unittest.main()
