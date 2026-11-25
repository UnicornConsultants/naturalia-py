"""Public catalog retrieval; optional server-side model gateway. No member data."""
import json, math, os, re, unicodedata, urllib.request, difflib
from knowledge import Knowledge
from pathlib import Path
from product_media import ProductMedia
from collections import Counter
from product_identity import product_names,named_products
from shopping_budget import shopping_budget,within_budget,budget_only
ROOT = Path(__file__).resolve().parent.parent
GROUPS = {
 '美白': ['美白','亮白','嫩白','提亮','亮膚','whitening','brightening'],
 '保濕': ['保濕','補水','水潤','hydrating','hydration','moisturizing'],
 '套裝': ['套裝','套莊','套装','set','kit'],
 '護膚': ['護膚','肌膚保養','皮膚保養','skincare','skin care'],
 '抗老': ['抗老','抗皺','逆齡','細紋','anti-aging','anti ageing'],
}
def normalize(s):
 s=unicodedata.normalize('NFKC',s).lower()
 for canonical, words in GROUPS.items():
  for word in sorted(words,key=len,reverse=True):
   s=re.sub(r'\b'+re.escape(word)+r'\b',canonical,s) if word.isascii() else s.replace(word,canonical)
 return s

def tokens(s):
 s=normalize(s)
 return Counter(re.findall(r'[a-z0-9]+',s)+[s[i:i+2] for i in range(len(s)-1) if all('\u3400'<=c<='\u9fff' for c in s[i:i+2])])

def gateway(route,payload):
 base=os.environ.get('MODEL_API_BASE','').rstrip('/')
 if not base: raise RuntimeError('model not configured')
 headers={'Content-Type':'application/json'}
 if os.environ.get('MODEL_API_KEY'): headers['Authorization']='Bearer '+os.environ['MODEL_API_KEY']
 req=urllib.request.Request(base+'/'+route,data=json.dumps(payload).encode(),headers=headers)
 with urllib.request.urlopen(req,timeout=35) as r: return json.load(r)

class Catalog:
 def __init__(self):
  self.media=ProductMedia(ROOT/'data/naturalia-group')
  self.items=[]
  for p in json.loads((ROOT/'data/naturalia-group/products.json').read_text()):
   pages=p.get('pages',[]); page=next((x for x in pages if x.get('language')=='zh-hant'),pages[0] if pages else {})
   desc=p.get('description_by_language',{}).get('zh-hant',{}).get('text','')
   title=p['title_translations'].get('zh-hant') or p['title_translations'].get('en','')
   text=' '.join([title,p['title_translations'].get('en',''),desc,p.get('sku','')])
   item={'id':p['id'],'sku':p.get('sku',''),'title':title,'description':desc,'url':page.get('url',''),'as_of':page.get('fetched_at','')[:10], 'price':(p.get('sale_price') or p.get('price') or {}).get('label','價格請查看商品頁'),'image':(p.get('media') or [{}])[0].get('default_image_url','')}
   price=p.get('sale_price') or p.get('price') or {}
   item['_price_cents']=price.get('cents') if price.get('currency_iso')=='HKD' else None
   item['_names']=product_names(p['title_translations'])
   if self.media.get(p['id']):item['image']='/product-images/'+p['id']
   item['_text']=normalize(text); item['_tokens']=tokens(text); self.items.append(item)
  self.df=Counter(t for p in self.items for t in p['_tokens'])
  self.vectors=None;self.local=None;self.knowledge=Knowledge(ROOT/'data/naturalia-group')
 def prepare_vectors(self):
  from local_models import LocalModels
  self.local=LocalModels()
  if self.local.encoder is not None:
   self.vectors=self.local.index([p['_text'][:3000] for p in self.items]);return
  if os.environ.get('EMBEDDING_MODEL') and os.environ.get('MODEL_API_BASE'):
   result=gateway('embeddings',{'model':os.environ['EMBEDDING_MODEL'],'input':[p['_text'][:6000] for p in self.items]})
   self.vectors=[x['embedding'] for x in sorted(result['data'],key=lambda x:x['index'])]
   if len(self.vectors)!=len(self.items): raise ValueError('embedding count mismatch')
 def search(self,query,history=None,price_overrides=None,exclude_ids=None):
  excluded_ids=set(exclude_ids or [])
  budget=shopping_budget(query);price_overrides=price_overrides or {}
  only_budget=budget and budget_only(query,budget)
  def public(p):
   item={k:v for k,v in p.items() if not k.startswith('_')}
   if p['id'] in price_overrides and price_overrides[p['id']] is not None:item['price']='HK$'+format(price_overrides[p['id']]/100,',.2f')
   return item
  named=named_products(query,self.items)
  if named:return [public(p) for p in named if p['id'] not in excluded_ids and (not budget or within_budget(price_overrides.get(p['id'],p['_price_cents']),budget))]
  history=history or []
  # Only short follow-ups inherit prior user intent; explicit new requirements replace it.
  if not budget and re.search(r'有冇套|有沒有套|其他呢|仲有呢|平啲|低於|少於|預算',query) and history and not any(x in query for x in ['改為','改搵','重新']):
   previous=' '.join(m['content'] for m in history[-8:] if m.get('role')=='user')
   query=previous+' '+query
  if budget:
   raw=unicodedata.normalize('NFKC',query).lower();start,end=budget['span'];q=normalize(raw[:start]+' '+raw[end:])
  else:q=normalize(query)
  excluded=[]
  if re.search(r'電腦|手機|火箭|股票|天氣|新聞',q):return []
  english=re.findall(r'[a-z]{3,}',q)
  vocabulary=set(t for p in self.items for t in p['_tokens'] if t.isascii())
  if not only_budget and english and not re.search(r'[\u3400-\u9fff]',q) and not any(w in vocabulary for w in english):
   corrections=[difflib.get_close_matches(w,vocabulary,n=1,cutoff=0.82) for w in english]
   if not any(corrections):return []
   for w,c in zip(english,corrections):
    if c:q=q.replace(w,c[0])
  for term in GROUPS:
   if re.search(r'(唔要|不要|不想|排除|唔想)[^，,。]{0,3}'+term,q):
    excluded.append(term);q=q.replace(term,'')
  qt=tokens(re.sub(r'我想找|我想搵|我想買|我想要|想搵|有冇|有沒有|請問|我想','',q)); wants_set='套裝' in q
  brands=[b for b in ['shirley price','naturalia sintesi','bycure','stemtonic','absonature'] if b in q]
  forms=[f for f in ['面霜','眼霜','精華','面膜','洗髮','香薰'] if f in q]
  qvec=None
  if self.vectors is not None and not only_budget:
   qvec=self.local.encode([q])[0] if self.local and self.local.encoder is not None else gateway('embeddings',{'model':os.environ['EMBEDDING_MODEL'],'input':[q]})['data'][0]['embedding']
  scored=[]
  for i,p in enumerate(self.items):
   if p['id'] in excluded_ids:continue
   text=p['_text']
   if brands and not all(b in text for b in brands):continue
   if forms and not all(f in normalize(p['title']) or (f=='面霜' and 'cream' in text and '霜' in p['title']) for f in forms):continue
   if '購物金' in p['title'] and '購物金' not in q:continue
   if any(x in text for x in excluded): continue
   if wants_set and '套裝' not in normalize(p['title']): continue
   if budget:
    if not within_budget(price_overrides.get(p['id'],p['_price_cents']),budget):continue
   score=sum(min(n,p['_tokens'].get(t,0))*math.log(1+len(self.items)/(1+self.df[t])) for t,n in qt.items())
   if qvec:
    v=self.vectors[i];cos=sum(a*b for a,b in zip(v,qvec))/(math.sqrt(sum(a*a for a in v))*math.sqrt(sum(a*a for a in qvec)) or 1)
    score+=max(0,cos)*12
   for term in GROUPS:
    if term in q and term in text: score+=5
    if term in q and term in normalize(p['title']):score+=6
   if '護膚' in q and any(x in text for x in ['肌膚','皮膚','面霜','精華']): score+=5
   if only_budget:score=0
   if only_budget or (score>1 and (not qvec or cos>=0.25 or score>15)):scored.append((score,p))
  scored.sort(key=lambda x:x[0],reverse=True)
  return [public(p) for _,p in scored[:6]]
 def chat(self,query,history=None,use_model=True,price_overrides=None,exclude_ids=None):
  budget=shopping_budget(query)
  policy=self.knowledge.answer(query)
  if policy:return dict(policy,semantic_enabled=self.vectors is not None)
  if not budget and re.search(r'充值|餘額|訂單|登入|會員|優惠|付款|扣款|真人|客服聯絡',query):
   return {'answer':'本機測試版尚未連接會員及交易服務，無法查詢帳戶或執行付款。最新優惠、充值及會員服務請到 Naturalia 網站聯絡店方確認。','products':[],'sources':[{'title':'Naturalia 官方網站','url':'https://www.naturalia-group.com/'}],'mode':'unavailable','warning':None,'semantic_enabled':self.vectors is not None}
  products=self.search(query,history,price_overrides,exclude_ids)
  named=named_products(query,self.items)
  mode='retrieval'; warning=None
  answer='找到以下相關商品，可以查看來源及商品詳情。價格為存檔參考，以網站現價為準。' if products else '暫時未找到符合條件的商品，可以提供品牌、功效或產品類型再試。'
  if named and products:
   answer='你指定的商品係「'+products[0]['title']+'」，以下有圖片、價格及購買入口。' if len(named)==1 else '找到以下符合名稱的商品，請按商品碼確認你想選邊款：\n'+'\n'.join(p['title']+'｜貨號 '+(p.get('sku') or '未提供')+'｜'+p['price'] for p in products)
  if not named and products and '套裝' in normalize(query) and not any(t in normalize(query) for t in ['美白','保濕','抗老']):answer+=' 你較想改善保濕、提亮，還是細紋？'
  import kimi
  if use_model and kimi.configured() and products and not (named and len(named)>1):
   try:
    selected,follow=kimi.generate(query,history or [],products)
    by_id={p['id']:p for p in products}
    products=[by_id[p['id']] for p in selected]
    answer=('以下係你指定的商品資料（價格及供應以網站為準）：' if named else '以下商品資料與你的需求相關（價格及供應以網站為準）：')+'\n\n'+'\n\n'.join('「'+p['title']+'」\n'+p['quote']+'\n來源：['+p['id']+']' for p in selected)
    if not named and follow in ['功效','預算','品牌']:answer+='\n\n你有偏好的'+follow+'嗎？'
    mode='kimi_rag'
   except kimi.ModelError:warning='Kimi 暫時未提供可核對的回答，現顯示檢索結果。'
  if budget:
   limit='HK$'+format(budget['cents']/100,',.2f')+(' 或以下' if budget['inclusive'] else ' 以下（不包括此金額）')
   answer=('以下商品符合 '+limit+' 嘅預算。\n'+answer) if products else '暫時未找到符合 '+limit+' 及你其他條件的商品。你想提高預算，定改搵其他產品類型？'
  return {'answer':answer,'products':products,'mode':mode,'warning':warning,'semantic_enabled':self.vectors is not None,**({'budget':budget} if budget else {})}
