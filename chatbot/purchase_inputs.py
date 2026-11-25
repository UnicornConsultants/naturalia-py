"""Checkout language parsing; amounts and authorization always stay server-side."""
import re
from chat_inputs import normalized

def quantity(value):
 if isinstance(value,bool):return None
 if isinstance(value,int):return value if 1<=value<=99 else None
 if not isinstance(value,str):return None
 value=value.replace('兩','二').replace('两','二')
 digits='零一二三四五六七八九'
 if value in digits:value=str(digits.index(value))
 elif re.fullmatch('[一二三四五六七八九]?十[一二三四五六七八九]?',value):
  tens,ones=value.split('十');value=str((digits.index(tens) if tens else 1)*10+(digits.index(ones) if ones else 0))
 return int(value) if re.fullmatch(r'\d{1,2}',value) and 1<=int(value)<=99 else None

def purchase_input(value):
 q=normalized(value).lower().strip(' 「」『』“”"\'')
 if re.search(r'充值(?!額購買)|增值|pay\s*me|alipay|支付寶|支付宝',q):return None
 question=bool(re.search(r'[?？]|點樣|點買|點算|怎樣|如何|嗎|吗|得唔得|可唔可以',q))
 q=q.strip(' 。,.，!！?？')
 q=re.sub(r'(?:點算|可以嗎|可以吗|得唔得)$','',q).strip()
 if re.fullmatch(r'(?:暫時|而家|先)?(?:唔買|唔要|不要買|不買|不买|取消購買|取消购买|取消訂單|取消订单|唔好扣款|唔確認|不確認|不确认|取消)(?:住|了|啦|喇|呀)?',q):return {'type':'cancel_purchase'}
 # A number with a unit means quantity. A legacy confirmation number remains a quote reference.
 number=r'[-+]?\d+(?:\.\d+)?|[零一二三四五六七八九十兩两百]+'
 unit=r'(?:個|个|件|支|盒|套|瓶|罐|包|樽|份)'
 code=r'[a-z][a-z0-9]{3}'
 prefix=r'(?:(?:唔係|不是)[,，]?\s*)?(?:(?:唔該|麻煩)\s*)?(?:(?:我想|我要|想|要|改為|改做|改成|改|確認|确认)\s*)?(?:(?:購買|购买|買|买)\s*)?(?:(?:呢個|呢款|這個|这个)\s*)?'
 suffix=r'\s*(?:呀|啊|啦|喇|吖|唔該|謝謝|谢谢)?'
 patterns=[prefix+r'(?P<code>'+code+r')\s*(?:[x×*]\s*)?(?P<qty>'+number+r')\s*'+unit+r'?'+suffix,
           prefix+r'(?P<qty>'+number+r')\s*'+unit+r'\s*(?P<code>'+code+r')?'+suffix,
           r'(?:數量|数量)\s*(?:改為|改成|改做|係|是|:)?\s*(?P<qty>'+number+r')\s*'+unit+r'?'+suffix]
 for pattern in patterns:
  match=re.fullmatch(pattern,q)
  if match:return {'type':'quantity','quantity':quantity(match['qty']),'code':(match.groupdict().get('code') or '').upper()}
 confirm=re.fullmatch(r'(?:確認購買|确认购买|確認|确认|確定購買|确定购买)\s*(\d{1,6})'+suffix,q)
 if confirm:return {'type':'confirm_number','number':int(confirm[1])} if not question else {'type':'purchase_help'}
 if re.fullmatch(r'(?:唔該\s*)?(?:我)?(?:確認(?:購買|扣款|落單)?|确认(?:购买|扣款|下单)?|確定(?:購買|扣款|要)?|确定(?:购买|扣款|要)?|confirm(?: purchase)?|yes(?: please)?|ok(?:ay)?|好|好嘅|好的|好呀買|好啊買|係|是|可以|得|冇問題|沒問題|就買呢個|就要呢個|照買|照落單|買|落單|下單)(?:呀|啊|啦|喇|吖|嘅|了|唔該|謝謝|谢谢|\s)*',q):
  return {'type':'purchase_help' if question else 'confirm_current'}
 if re.search(r'確認|确认|確定|确定|確任|確忍|confirm|落單|下單|數量|数量|買多|买多|買\s*\d|买\s*\d',q) or re.search('(?:'+number+r')\s*'+unit,q):return {'type':'purchase_help'}
 return None
