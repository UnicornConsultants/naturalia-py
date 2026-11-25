import json,re
from html.parser import HTMLParser
from pathlib import Path
class TextParser(HTMLParser):
 def __init__(self):super().__init__();self.parts=[];self.hidden=0
 def handle_starttag(self,tag,attrs):
  if tag in ['script','style']:self.hidden+=1
  if tag in ['p','li','h1','h2','h3','br']:self.parts.append('\n')
 def handle_endtag(self,tag):
  if tag in ['script','style']:self.hidden=max(0,self.hidden-1)
 def handle_data(self,data):
  if not self.hidden:self.parts.append(data)
class Knowledge:
 def __init__(self,root):
  self.pages=[]
  for p in json.loads((root/'pages.json').read_text()):
   if p.get('language')!='zh-hant' or p.get('kind') not in ['about','pages']:continue
   parser=TextParser();parser.feed((root/p['content_html_path']).read_text())
   lines=[re.sub(r'\s+',' ',s).strip() for s in ''.join(parser.parts).splitlines()]
   text='\n'.join(s for s in lines if s)
   self.pages.append({'id':Path(p['content_html_path']).stem,'title':p['title'],'description':text,'url':p['url'],'as_of':p.get('fetched_at','')[:10]})
 def answer(self,q):
  topics=[(r'退貨|換貨|退換|退款|return|refund','退換貨政策'),(r'運費|送貨|運送|配送|shipping|delivery','運送政策'),(r'私隱|隱私|privacy','隱私政策'),(r'公司|品牌介紹|商店介紹','商店介紹')]
  title=next((title for pattern,title in topics if re.search(pattern,q,re.I)),None)
  if not title:return None
  page=next((p for p in self.pages if p['title']==title),None)
  if not page:return None
  return {'answer':f'以下是 {page["as_of"]} 存檔的「{title}」內容，最新安排請以來源頁面為準。\n\n'+page['description'][:1800], 'sources':[page], 'products':[], 'mode':'knowledge','warning':None}
