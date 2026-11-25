"""Resolve explicitly named products before broader semantic recommendations."""
import re,unicodedata
from functools import lru_cache

def name_key(value):
 return re.sub(r'[^a-z0-9\u3400-\u9fff]','',unicodedata.normalize('NFKC',value).casefold())

def product_names(translations):
 names=set()
 for title in translations.values():
  if not isinstance(title,str):continue
  names.add(name_key(title))
  if ' - ' in title:names.add(name_key(title.split(' - ',1)[1]))
  # A distinctive named range such as Delight 3 Pro is also a product name.
  for part in re.findall(r'[A-Za-z][A-Za-z0-9 .+\-/]*',title):
   key=name_key(part)
   if len(key)>=8 and re.search(r'\d',key):names.add(key)
 return sorted(n for n in names if len(n)>=(5 if n.isascii() else 3))

@lru_cache(maxsize=2048)
def name_pattern(name):
 return re.compile((r'(?<![a-z0-9])' if name[0].isascii() else '')+r'[\W_]*'.join(re.escape(c) for c in name)+(r'(?![a-z0-9])' if name[-1].isascii() else ''))

def named_products(query,items):
 # Asking for alternatives/exclusions remains a recommendation query.
 if re.search(r'唔要|不要|唔想|不想|排除|除咗|除了|類似|类似|替代|代替|其他|另外|similar|alternative|instead|except|without',query,re.I):return []
 key=unicodedata.normalize('NFKC',query).casefold()
 matches=[]
 for item in items:
  aliases=[name for name in item['_names'] if name_pattern(name).search(key)]
  if aliases:matches.append((item,max(aliases,key=len)))
 # A full, longer name outranks another product whose name is only nested inside it.
 return [item for item,alias in matches if not any(alias!=other and alias in other for _,other in matches)]
