import sys,tempfile,unittest,json,os
from pathlib import Path
from unittest.mock import patch,MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage import Store
from engine import Catalog
import kimi
class Integrations(unittest.TestCase):
 def test_session_isolation_and_delete(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d)/'test.db');a=s.create();b=s.create();s.append(a,'Hello',{'answer':'World'})
   self.assertEqual(len(s.history(a)),2);self.assertEqual(s.history(b),[]);s.delete(a);self.assertFalse(s.valid(a));self.assertTrue(s.valid(b))
 def test_policy_sources(self):
  r=Catalog().chat('運送政策');self.assertEqual(r['mode'],'knowledge');self.assertIn('delivery-policy',r['sources'][0]['url']);self.assertFalse(r['products'])
 def test_transactions_unavailable(self):
  self.assertEqual(Catalog().chat('查我的訂單')['mode'],'unavailable')
 def test_kimi_evidence(self):
  product={'id':'test','title':'產品','description':'這是有來源的保濕產品描述。'}
  for quote,success in [('有來源的保濕產品描述',True),('可以治療所有皮膚問題',False)]:
   reply={'choices':[{'message':{'content':json.dumps({'recommendations':[{'id':'test','quote':quote}]})}}]}
   response=MagicMock();response.__enter__.return_value.read.return_value=json.dumps(reply).encode()
   with patch.dict(os.environ,{'KIMI_API_KEY':'fake','KIMI_MODEL':'fake','KIMI_API_BASE':'https://example.invalid/v1'}),patch('urllib.request.urlopen',return_value=response):
    if success:self.assertEqual(kimi.generate('保濕',[],[product])[0][0]['id'],'test')
    else:
     with self.assertRaises(kimi.ModelError):kimi.generate('保濕',[],[product])
 def test_provider_error_safe(self):
  with patch.dict(os.environ,{'KIMI_API_KEY':'fake','KIMI_MODEL':'fake','KIMI_API_BASE':'https://example.invalid/v1'}),patch('urllib.request.urlopen',side_effect=RuntimeError('secret')):
   with self.assertRaisesRegex(kimi.ModelError,'provider_unavailable'):kimi.generate('test',[],[])
 def test_model_selected_cards_match_text_in_same_order(self):
  catalog=Catalog();products=catalog.search('保濕套裝');self.assertGreaterEqual(len(products),3)
  chosen=[products[2],products[0]];selected=[{'id':p['id'],'title':p['title'],'quote':'測試資料引文'} for p in chosen]
  with patch('kimi.configured',return_value=True),patch('kimi.generate',return_value=(selected,'無')):
   result=catalog.chat('保濕套裝')
  self.assertEqual([p['id'] for p in result['products']],[p['id'] for p in chosen])
  self.assertNotIn(products[1]['title'],result['answer'])
 def test_named_product_limits_model_candidates_and_fallback(self):
  catalog=Catalog();query='Hi 我想購買 delight 3 Pro專業套裝';product=catalog.search(query)[0]
  selected=[{'id':product['id'],'title':product['title'],'quote':'測試資料引文'}]
  with patch('kimi.configured',return_value=True),patch('kimi.generate',return_value=(selected,'品牌')) as generate:
   result=catalog.chat(query)
   self.assertEqual(len(generate.call_args.args[2]),1)
  self.assertEqual([p['id'] for p in result['products']],[product['id']]);self.assertNotIn('偏好的品牌',result['answer'])
  with patch('kimi.configured',return_value=True),patch('kimi.generate',side_effect=kimi.ModelError('fixture')):
   fallback=catalog.chat(query)
  self.assertEqual([p['id'] for p in fallback['products']],[product['id']])
if __name__=='__main__':unittest.main()
