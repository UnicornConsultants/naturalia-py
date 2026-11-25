"""Recognize product price limits separately from recharge amounts."""
import re,unicodedata
from decimal import Decimal

def shopping_budget(value):
 q=unicodedata.normalize('NFKC',value).lower()
 if re.search(r'充值|增值|入錢|入钱|過數|过数',q) and not re.search(r'產品|产品|商品|買|买|搵|找|推薦|推荐',q):return None
 amount=r'(?:hk\$|hkd|\$)?\s*(\d{1,9}(?:,\d{3})*(?:\.\d{1,2})?)\s*(?:港幣|港币|港元|元|蚊)?'
 patterns=[r'(低於|低于|少於|少于|不超過|不超过|最多|預算|预算|under|below|budget|<=|≤|<)\s*'+amount,
           amount+r'\s*(以下|以內|以内|內|内)']
 found=[]
 for index,pattern in enumerate(patterns):
  for match in re.finditer(pattern,q):
   operator,number=match.groups() if index==0 else match.groups()[::-1]
   found.append((match.start(),{'cents':int(Decimal(number.replace(',',''))*100),'inclusive':operator not in ['低於','低于','少於','少于','under','below','<'],'span':match.span()}))
 return max(found,key=lambda item:item[0])[1] if found else None

def within_budget(cents,budget):
 return isinstance(cents,int) and not isinstance(cents,bool) and cents>0 and (cents<=budget['cents'] if budget['inclusive'] else cents<budget['cents'])

def budget_only(value,budget):
 q=unicodedata.normalize('NFKC',value).lower();start,end=budget['span'];q=q[:start]+q[end:]
 q=re.sub(r'有沒有|有没有|有冇|什麼|什么|有咩|產品|产品|商品|推薦|推荐|請問|请问|我想|想搵|想找|有沒有|可以|平啲|便宜|預算|预算|please|products?|anything|something|recommend|show|me|find|within|for','',q)
 q=re.sub(r'[我想要買买找搵有的是嘅啲些嗎吗呢咩幾几]','',q)
 return not re.search(r'[a-z\u3400-\u9fff]',q)
