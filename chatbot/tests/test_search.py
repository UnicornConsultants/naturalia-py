import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import Catalog
class SearchTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):cls.c=Catalog()
 def test_missing_word(self):
  target='6897bb8a55a99e00012ceb95'
  for q in ['我想找美白肌膚保養套裝','我想找肌膚保養套裝','我想找美白肌膚保養套莊','想搵提亮膚色嘅護膚套裝']:
   with self.subTest(q=q):self.assertIn(target,[p['id'] for p in self.c.search(q)])
 def test_exclusion(self):
  found=self.c.search('唔要美白，想搵保濕套裝')
  self.assertNotIn('6897bb8a55a99e00012ceb95',[p['id'] for p in found])
 def test_context(self):
  self.assertEqual(self.c.search('有冇套裝？',[{'role':'user','content':'我想美白'}])[0]['id'],self.c.search('我想美白 有冇套裝？')[0]['id'])
 def test_unknown(self):self.assertEqual(self.c.search('xyzabcdefgh'),[])
 def test_brand_and_form(self):
  results=self.c.search('Shirley Price 面霜')
  self.assertTrue(results)
  for p in results:self.assertIn('霜',p['title'])
 def test_budget(self):
  results=self.c.search('保濕面霜 低於500')
  self.assertTrue(results)
  for p in results:self.assertLessEqual(float(p['price'].replace('HK$','').replace(',','')),500)
 def test_unrelated(self):self.assertEqual(self.c.search('我想買電腦'),[])
 def test_public_only(self):
  self.assertEqual(len(self.c.items),188)
  for p in self.c.search('保濕'):
   self.assertTrue(p['url'].startswith('https://www.naturalia-group.com/'))
   self.assertNotIn('_text',p)
 def test_named_product_does_not_fill_remaining_recommendation_slots(self):
  target='691879818842a6000ac34afc'
  for q in ['Hi 我想購買 delight 3 Pro專業套裝','DELIGHT 3 PRO 專業套裝','Delight 3 Pro','I want Delight 3 Pro','Naturalia Sintesi - Delight 3 Pro Professional Kit']:
   with self.subTest(q=q):self.assertEqual([p['id'] for p in self.c.search(q)],[target])
 def test_explicit_name_overrides_previous_broad_search(self):
  results=self.c.search('想買 Delight 3 Pro專業套裝',[{'role':'user','content':'有冇平啲香薰套裝'}])
  self.assertEqual(len(results),1);self.assertEqual(results[0]['sku'],'6346')
 def test_same_name_variants_require_clarification(self):
  result=self.c.chat('我想購買活膚面霜',use_model=False)
  self.assertEqual({p['sku'] for p in result['products']},{'CA1052','CAT5619'})
  self.assertIn('確認你想選邊款',result['answer']);self.assertIn('CA1052',result['answer'])
 def test_every_exact_catalog_title_remains_findable(self):
  for product in self.c.items:
   with self.subTest(title=product['title']):self.assertIn(product['id'],[p['id'] for p in self.c.search(product['title'])])
 def test_named_matching_does_not_turn_generic_or_variant_queries_into_exact_identity(self):
  from product_identity import named_products
  for q in ['oil','Joyous10','Delight 3 ProMax','類似 Delight 3 Pro 的其他套裝','唔要 Delight 3 Pro']:
   with self.subTest(q=q):self.assertEqual(named_products(q,self.c.items),[])
  self.assertGreater(len(self.c.search('我想找肌膚保養套裝')),1)
if __name__=='__main__':unittest.main()
