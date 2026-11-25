"""Conservative text parsing for purchase codes and custom recharge amounts."""
import re,unicodedata
from shopping_budget import shopping_budget

def normalized(value):return unicodedata.normalize('NFKC',value).strip()

def product_command(value):
 q=normalized(value)
 # Accept conversational wrappers, without fuzzy-matching one product code to another.
 m=re.fullmatch(r'(?:唔該\s*|麻煩\s*|我想\s*|我要\s*|想\s*|要\s*)?(?:(買|买|購買|以充值額購買|詳情|详情|有意購買)\s*)?[「『“"\']?([A-Za-z][A-Za-z0-9]{3})[」』”"\']?\s*(?:呀|啊|啦|喇|呢|吖|唔該|謝謝|谢谢)?[!！。,.，？?\s]*',q)
 return (m[1] or '買',m[2]) if m else None

def recharge_input(value,awaiting=False):
 if shopping_budget(value):return None
 q=normalized(value).lower().strip(' 。!！')
 if q in ['取消充值','取消入錢','取消入钱']:return {'type':'cancel_topup'}
 if re.fullmatch(r'(?:重新|再次|再)(?:申請|申请|充值|入錢|入钱)[呀啊啦喇]*',q):return {'type':'restart_topup'}
 method=None
 for pattern,name in [(r'pay\s*me','payme'),(r'alipay|支付寶|支付宝','alipay')]:
  if re.search(pattern,q):
   if method:return {'type':'topup_invalid'}
   method=name;q=re.sub(pattern,'',q).strip()
 if method and re.fullmatch(r'(?:用|用咗|已用|我用|我想用)?\s*(?:付款|支付|過數|过数)?[呀啊啦喇\s]*',q):return {'type':'topup_method','method':method}
 q=re.sub(r'\s+','',q) if re.search(r'已|咗|左|了|完成|成功',q) else q
 if re.fullmatch(r'(?:我)?(?:已經|已经|已)(?:用)?(?:充值|付款|支付|過數|过数|入數|入数|入錢|入钱)(?:咗|左|了|完成|成功)?[了喇啦呀啊吖囉咯嘞\s]*',q):return {'type':'topup_progress','method':method}
 if re.fullmatch(r'(?:我)?(?:已經|已经|已|真係|真的)?(?:充值|付款|支付|過數|过数|入數|入数|入錢|入钱|入)(?:咗|左|了|完成|成功)(?:了|喇|啦|呀|啊|吖|囉|咯|嘞|\s)*',q):return {'type':'topup_progress','method':method}
 explicit=bool(re.search(r'充值|增值|入錢|入钱',q) or re.match(r'^(?:唔該|麻煩)?\s*(?:我想|我要|想|要)?\s*(?:入|充)(?=\s*(?:hk\$|hkd|\$|\d|$))',q))
 if not awaiting and not explicit and not method:return None
 q=re.sub(r'^(?:唔該|麻煩)?\s*(?:我想|我要|想|要)?\s*(?:充值|增值|入錢|入钱|入|充)?\s*','',q)
 q=re.sub(r'^(?:hk\$|hkd|\$)\s*','',q)
 q=re.sub(r'\s*(?:港元|港幣|港币|元|蚊)?\s*(?:呀|啊|啦|喇|唔該|謝謝)?$','',q).strip()
 if explicit and not q:return {'type':'plans'}
 if re.fullmatch(r'(?:\d{1,9}|\d{1,3}(?:,\d{3}){1,2})(?:\.\d{1,2})?',q):return {'type':'topup_amount','amount':q.replace(',',''),'method':method}
 # A product name containing a number (e.g. Delight 3 Pro) is not a failed amount.
 if (explicit or method or awaiting) and re.fullmatch(r'[+\-\d\s.,$]+',q):return {'type':'topup_invalid'}
 return None
