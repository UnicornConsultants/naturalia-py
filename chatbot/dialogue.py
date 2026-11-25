"""Turn routing based on the last question, independently of saved payment drafts.

The router can select a workflow or ask for clarification. Wallet execution remains
in Commerce and always requires a current, explicit purchase confirmation.
"""
import base64,json,re,time
from chat_inputs import normalized,recharge_input,product_command
from purchase_inputs import purchase_input,quantity
from shopping_budget import shopping_budget

def declines_cs(text):
 return bool(re.search(r'(?:唔使|唔洗|唔好|不用|不要|不需要)(?:轉交|轉|转|搵|找|聯絡|联系)?(?:真人|客服|人工|職員|cs)',text,re.I))

def wants_cs(text):
 if re.search(r'投訴|投诉|退貨|退货|退款|破損|過敏|取消(?:已完成)?訂單|取消订单',text,re.I):return True
 if declines_cs(text):return False
 text=re.sub(r'人工智能|人工智慧|自動客服|自动客服','',text)
 return bool(re.search(r'真人|人工|客服|搵職員|找職員|找工作人员|\bcs\b',text,re.I))

def previous_dialogue(history):
 for index in range(len(history)-1,-1,-1):
  message=history[index]
  if message['role']!='assistant':continue
  p=message.get('payload') or {}
  if p.get('dialogue'):return dict(p['dialogue'])
  state={'topic':'general','prompt':'none','product_ids':[v['id'] for v in p.get('products',[])]}
  if p.get('notification'):state.update(topic='recharge',prompt='review')
  elif p.get('quote'):state.update(topic='purchase',prompt='quote',quote=p['quote']['id'])
  elif p.get('order'):state.update(topic='purchase',prompt='completed')
  elif p.get('topup_amount_input'):state.update(topic='recharge',prompt='amount')
  elif p.get('topup_methods'):state.update(topic='recharge',prompt='method')
  elif p.get('payment'):state.update(topic='recharge',prompt='receipt')
  elif p.get('topup_status'):state.update(topic='recharge',prompt='review')
  elif p.get('products'):state.update(topic='search',prompt='product')
  if p.get('budget'):state['budget']=p['budget']
  if state['topic']=='search':
   state['query']=next((m['content'] for m in reversed(history[:index]) if m['role']=='user'),'')
  return state
 return {'topic':'general','prompt':'none','product_ids':[]}

def bare_amount(text):
 q=normalized(text).lower().strip(' 。!！?？')
 q=re.sub(r'^(?:改為|改成|改做|改到|改|就|咁就)\s*','',q)
 if not re.fullmatch(r'(?:hk\$|hkd|\$)?\s*\d[\d,.]*\s*(?:蚊|元|港幣|港元)?(?:呀|啦|喇)?',q):return None
 parsed=recharge_input(q,True)
 return parsed if parsed and parsed['type']=='topup_amount' else None

class DialogueFlow:
 def dialogue_products(self,state,scope):
  by_id={p['id']:p for p in self.catalog.items}
  return [{k:v for k,v in by_id[pid].items() if not k.startswith('_')} for pid in state.get('product_ids',[]) if pid in by_id][:3]
 def clarify_turn(self,scope,state,answer=None):
  if state['topic']=='recharge':
   prompt=state['prompt']
   messages={'amount':'你想充值幾多港幣？請直接回覆金額，例如「500」。',
             'method':'你今次用 PayMe 定 Alipay 過數？請回覆其中一種付款方式。',
             'receipt':'如果已過數，請傳付款截圖；收到後會提交後台核款。',
             'review':'你可以回覆「核款進度」查詢申請，或回覆「繼續購買」返回選購。'}
   result=self.result(answer or messages.get(prompt,'想繼續充值可回覆「繼續充值」，或者直接講想搵咩產品。'))
   if prompt=='amount':result['topup_amount_input']=True
  elif state['topic'] in ['search','purchase']:
   result=self.result(answer or '你想選邊款？可回覆商品碼或「第二款」；想比較價錢可講「平啲」，想換一批可講「其他款」。',products=self.dialogue_products(state,scope))
  else:result=self.result(answer or '你想搵產品、充值，定查餘額？可以直接講你想做嘅事。')
  result['dialogue']=dict(state);result['clarification']=True
  return result
 def read_topup_status(self,scope):
  with self.db.db() as c:
   row=c.execute('SELECT t.* FROM payment_intents i JOIN topups t ON t.id=i.topup WHERE i.scope=? ORDER BY i.created DESC LIMIT 1',(scope,)).fetchone()
   draft=c.execute('SELECT * FROM topup_drafts WHERE scope=? AND updated>?',(scope,time.time()-86400,)).fetchone()
  if draft:
   result=self.result('你而家有一筆尚未提交核款的充值；可以回覆「繼續充值」補齊資料。',dialogue={'topic':'recharge','prompt':'resume','product_ids':[]})
   if row:result['answer']+='\n最近已提交的申請：'+self.topup_status(row)['answer']
   return result
  if row:return self.topup_status(row)
  return self.result('暫時未有已提交的充值申請。若已過數，請傳截圖；想接住之前的充值可回覆「繼續充值」。',dialogue={'topic':'recharge','prompt':'receipt','product_ids':[]})
 def routed_search(self,query,state=None,exclude=None):
  return {'action':{'type':'search'},'query':query,'exclude_ids':list(exclude or []),'contextualized':True,'result':None}
 def reference_product(self,scope,state):
  products=self.dialogue_products(state,scope)
  if len(products)==1:return products[0]['id']
  if state['topic']=='purchase':
   ctx=self.purchase_context(scope)
   if ctx and ctx['phase'] in ['quote','selected']:return ctx['product']
  return None
 def search_basis(self,scope,state):
  query=state.get('query','');products=self.dialogue_products(state,scope)
  # Asking for alternatives to a named item must not keep that exact name as a filter.
  from product_identity import named_products
  if not query or named_products(query,[p for p in self.catalog.items if '_names' in p]):
   text=' '.join(p['title'] for p in products)
   query=next((word for word in ['套裝','髪膜','髮膜','洗髮','面霜','眼霜','精華','面膜','香薰'] if word in text),'護膚')
  budget=shopping_budget(query)
  if budget:
   q=normalized(query).lower();start,end=budget['span'];query=q[:start]+q[end:]
  return query.strip() or '護膚'
 def quantity_slots(self,scope,question,state,ctx):
  """Extract product and quantity slots without requiring an exact command sentence."""
  q=normalized(question).lower()
  if re.search(r'唔好|不要|不買|不买|唔買|第',q):return None
  matches=list(re.finditer(r'([-+]?\d+(?:\.\d+)?|[零一二三四五六七八九十兩两百]+)\s*(?:個|个|件|支|套|盒|瓶|樽|份)',q))
  if len(matches)!=1 or not re.search(r'買|买|要|改|加|減|减|呢個|呢款|這個|这个|幾錢|多少钱',q):return None
  n=quantity(matches[0][1])
  if n is None:
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
   return {'action':{},'result':self.purchase_help(scope,'數量請用 1 至 99 的整數，例如「買 2 個」。未有扣款。'),'query':question}
  codes=re.findall(r'(?<![a-z0-9])([a-z][a-z0-9]{3})(?![a-z0-9])',q,re.I);product=None
  if codes:
   with self.db.db() as c:
    choices=[r['target'] for code in codes for r in c.execute("SELECT v.target FROM chat_choices v JOIN product_codes p ON p.product=v.target WHERE v.scope=? AND v.kind='product' AND v.expires>? AND p.code=?",(scope,time.time(),code.upper()))]
   if len(set(choices))!=1:return None
   product=choices[0]
  if not product:
   from product_identity import named_products
   named=named_products(q,[p for p in self.catalog.items if '_names' in p])
   if len(named)==1:product=named[0]['id']
  if not product:
   category=next((s for s in ['面霜','眼霜','面膜','精華','髪膜','髮膜','洗髮','套裝','香薰'] if s in q),None)
   if category:
    route=self.routed_search(category);route['pending_quantity']=n;return route
   product=self.reference_product(scope,state)
   if not product and ctx and state['topic']=='recharge' and ctx['phase']=='selected':product=ctx['product']
   if not product and ctx and q.startswith('再買') and state['topic']=='purchase':product=ctx['product']
  if not product:return {'action':{},'result':self.clarify_turn(scope,state,'你想買 '+str(n)+' 件邊款？請選商品碼或講產品名稱。'),'query':question}
  delta=re.search(r'(加多|再加|加|減少|减少|減|减|少)\s*'+re.escape(matches[0][0]),q)
  if delta and ctx and ctx['product']==product and ctx['phase'] in ['selected','quote']:
   n=ctx['quantity']+(-n if delta[1] in ['減少','减少','減','减','少'] else n)
  if not 1<=n<=99:
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
   return {'action':{},'result':self.purchase_help(scope,'修改後數量要介乎 1 至 99 件。想取消可回覆「取消購買」。'),'query':question}
  return {'action':{'type':'buy','product':product,'quantity':n},'result':None,'query':question}
 def route_turn(self,scope,question,history,attachment=None):
  state=previous_dialogue(history);q=normalized(question).strip(' 。!！?？').lower();ctx=self.purchase_context(scope)
  def decision(action=None,result=None,**kwargs):return {'action':action or {},'result':result,'query':question,**kwargs}
  if wants_cs(question):return decision({'type':'cs'})
  if re.fullmatch(r'(?:你|您)(?:係|是)(?:咪|不是)?(?:AI|ai|人工智能|機械人|机器人)(?:嗎|吗|呀)?',q):return decision(result=self.clarify_turn(scope,state,'我係 Naturalia AI 商品助手，可以幫你搵產品、充值及購物；需要真人時可以講「聯絡 CS」。'))
  if re.search(r'充值|付款|過數|充值額',q) and re.search(r'手續費|手续费|最低|上限|有效期|到期|幾耐|多久|保證|保证',q):return decision({'type':'cs'})
  if declines_cs(q):return decision(result=self.clarify_turn(scope,state,'好，繼續由商品助手協助。你可以講想搵嘅產品或下一步。'))
  legacy=re.fullmatch(r'充值(?:方案)?\s*(\d{1,6})\s*(?:payme|alipay|支付寶|支付宝)',q,re.I)
  if legacy:
   with self.db.db() as c:old=c.execute("SELECT 1 FROM chat_choices WHERE scope=? AND kind='plan' AND number=?",(scope,int(legacy[1]))).fetchone()
   if old or '方案' in q:
    action,result=self.shortcuts(scope,question);return decision(action,result)
  if attachment:
   if re.search(r'(?:唔係|不是|並非|并非).*(?:過數|过数|付款|憑證|凭证)',question):return decision({'type':'cs','reason':'客人提供非付款圖片，需要 CS 查看'})
   if re.search(r'產品|产品|商品|幾錢|多少錢|點用|功效',question) and not re.search(r'過數|付款|充值|憑證',question):return decision({'type':'cs'})
   if state['topic']=='recharge' or re.search(r'過數|过数|付款|充值|入左|入咗|憑證|凭证|入數紙',question):return decision({'type':'receipt'},attachment=attachment)
   return decision({'type':'cs','reason':'客人提供圖片，需要 CS 查看'})
  if state.get('pending_evidence') and state['prompt']=='image_kind':
   if re.search(r'過數|过数|付款|充值|入數|入数|憑證|凭证',q):
    with self.db.db() as c:proof=c.execute('SELECT mime,body FROM evidence WHERE id=?',(state['pending_evidence'],)).fetchone()
    if proof:return decision({'type':'receipt'},attachment={'mime':proof['mime'],'data':base64.b64encode(proof['body']).decode()})
   if re.search(r'產品|产品|商品|幾錢|什么|邊款',q):return decision({'type':'cs'},result=None)
  # Explicit topic changes take precedence over suspended purchase/topup data.
  if shopping_budget(question):return self.routed_search(question)
  budget_statement=re.search(r'(?:預算(?:係|是)?|我(?:只)?(?:得|有)|只(?:得|有))\s*(\d+(?:\.\d{1,2})?)\s*(?:蚊|元|港幣)?',q)
  if budget_statement and not re.search(r'充值|付款|過數|入錢',q):return self.routed_search('預算'+budget_statement[1])
  if re.search(r'買得起|按(?:我)?餘額|用(?:我)?餘額.*(?:推薦|搵|買咩)|餘額可以買',q):
   with self.db.db() as c:
    conv=c.execute('SELECT customer FROM conversations WHERE id=?',(scope,)).fetchone();available=self.balance(c,conv['customer']) if conv and conv['customer'] else None
   if available is None:return decision(result=self.clarify_turn(scope,{'topic':'search','prompt':'budget','product_ids':[]},'未有已核實的帳戶餘額。你想用幾多港幣做預算？'))
   return self.routed_search('預算'+str(available/100))
  if re.search(r'餘額|余额|仲有幾多錢|還有多少錢|戶口有幾多',q):return decision({'type':'balance'})
  if re.fullmatch(r'(?:再|繼續|继续|返去|返回)?(?:充值|增值|入錢|入钱)(?:先|呀|啦)?',q):return decision({'type':'plans'})
  if re.search(r'核款|入帳未|入账|批[咗左了]?未|審核進度|审核进度',q) or (state['topic']=='recharge' and re.fullmatch(r'收到未|收咗未|得未|搞掂未|成功未|仲未到|有消息未',q)):return decision({'type':'review_status'})
  if re.fullmatch(r'(?:再|重新|繼續|继续|買返|返回|返去)(?:購買|购买|買|买|選購|选购|頭先嗰件|頭先嗰款)(?:呀|啦)?',q):
   if ctx:return decision({'type':'buy','product':ctx['product'],'quantity':ctx['quantity']})
   return decision(result=self.clarify_turn(scope,state,'想繼續買邊款？請回覆商品碼或產品名稱。'))
  if re.fullmatch(r'(?:取消|算啦|算了|唔要住|唔使住|不要了|暫時唔使)',q):
   if state['topic']=='recharge':
    if state['prompt']=='review':return decision({'type':'cs'})
    return decision({'type':'cancel_topup'})
   if state['topic']=='purchase':return decision({'type':'cancel_purchase'})
   return decision(result=self.clarify_turn(scope,{'topic':'general','prompt':'none','product_ids':[]},'好，暫停今次選購。想再開始可以直接講產品、預算或「充值」。'))
  # Payment help is informational, never an implicit submission of a stored receipt.
  payment_question=re.search(r'點(?:樣)?(?:充值|付款|過數)|如何充值|怎樣充值|用咩(?:付款|充值)|(?:pay\s*me|alipay|支付寶).*(?:點用|可以|得唔得|嗎)|可唔可以.*(?:payme|alipay)|payme.*(?:定|還是).*alipay',q)
  if payment_question:
   next_state=state if state['topic']=='recharge' else {'topic':'recharge','prompt':'amount','product_ids':[]}
   return decision(result=self.clarify_turn(scope,next_state,'可以用 PayMe 或 Alipay。先提供充值金額取得收款 QR Code，過數後傳截圖，工作人員核款後才入帳。'+('你想充值幾多港幣？' if next_state['prompt']=='amount' else '已填資料會保留，可以接住上一步辦理。')))
  if re.search(r'(?:再|重新|重發|重发|畀|俾|給|给|要).*(?:qr|二維碼|二维码|收款碼|收款码)',q):
   if state['topic']=='recharge' and state['prompt']=='review':return decision({'type':'review_status'})
   return decision({'type':'plans'})
  amount=bare_amount(question)
  if amount:
   if state['topic']=='recharge' and state['prompt'] in ['amount','method','receipt']:return decision(amount)
   if state['topic']=='search' and (state.get('budget') or state['prompt']=='budget'):return self.routed_search(self.search_basis(scope,state)+' 預算'+amount['amount'])
  if state['topic']=='recharge':
   adjusted=re.sub(r'^(?:改用|轉用|转用|改為|改成|改做|改入|改|就用)\s*','',q)
   parsed=recharge_input(adjusted,True)
   if parsed:return decision(parsed)
  else:
   parsed=recharge_input(question,False)
   if parsed:return decision(parsed)
  # Relative search uses the current topic and visible cards, not every number in history.
  cheaper=bool(re.search(r'平[啲d]|便宜(?:一點|一点|些|點|点)?|太貴|太贵|貴咗|買唔起',q,re.I))
  more=bool(re.fullmatch(r'(?:(?:仲有|有冇|有沒有|还有)?(?:其他|另外|別的|别的)|仲有|換|换|換一|换一)(?:呢|款|產品|产品|商品|選擇|选择|一批|啲|d)?(?:呢|嗎|吗)?',q,re.I))
  unlimited=bool(re.search(r'唔(?:使|洗)(?:理|限)?(?:價錢|價|預算)|不限(?:價|预算|預算)|不設預算',q))
  if cheaper or more or unlimited:
   products=self.dialogue_products(state,scope);base=self.search_basis(scope,state);budget=state.get('budget');limit=budget['cents'] if budget else None
   if cheaper:
    with self.db.db() as c:prices=[self.effective_price(c,p['id']) for p in products]
    prices=[p['cents'] for p in prices if p]
    if prices:limit=min([min(prices)-1]+([limit] if limit is not None else []))
    if limit is None:return decision(result=self.clarify_turn(scope,{**state,'topic':'search','prompt':'budget'},'想搵平啲嘅，可以講你嘅預算，例如「500以下」。'))
   if unlimited:limit=None
   query=base+(' 預算'+str(max(0,limit)/100) if limit is not None else '')
   excluded=list(dict.fromkeys(state.get('seen',[])+[p['id'] for p in products]))[-60:] if not unlimited else []
   return self.routed_search(query,exclude=excluded)
  # Ordinals refer to the cards actually shown, never global historical choice numbers.
  ordinal_quantity=re.fullmatch(r'(?:我想|我要|想|要|買|买|揀|選|选)?\s*第([一二三123])(?:個|个|款|件)?\s*(?:買|要)?\s*([一二三四五六七八九十兩两\d]+)(?:個|个|件|支|套|盒)(?:呀|啦)?',q)
  if ordinal_quantity:
   index=quantity(ordinal_quantity[1]);n=quantity(ordinal_quantity[2]);products=self.dialogue_products(state,scope)
   if n and index<=len(products):return decision({'type':'buy','product':products[index-1]['id'],'quantity':n})
   return decision(result=self.clarify_turn(scope,state,'請選下方商品，數量需為 1 至 99 件。'))
  ordinal=re.fullmatch(r'(?:我想|我要|想|要|買|买|揀|選|选)?\s*第?([一二三123])(?:個|个|款|件)(?:呀|啦)?',q)
  if ordinal and ('第' in q or re.search(r'揀|選|选',q) or q in ['第一個','第二個','第三個']):
   index=quantity(ordinal[1]);products=self.dialogue_products(state,scope)
   if index and index<=len(products):return decision({'type':'buy','product':products[index-1]['id'],'quantity':state.get('pending_quantity',1)})
   return decision(result=self.clarify_turn(scope,state,'呢個位置未有商品，請選下方商品碼。'))
  if state['topic']=='search' and not state.get('budget') and re.fullmatch('[123]',q):
   products=self.dialogue_products(state,scope)
   if int(q)<=len(products):return decision({'type':'buy','product':products[int(q)-1]['id'],'quantity':state.get('pending_quantity',1)})
  if re.search(r'都要|兩款都|两款都|全部(?:買|要)|第一.*第二|第一.*第三',q):return decision(result=self.clarify_turn(scope,state,'可以逐款確認購買。你想先買邊款？請回覆商品碼，件數可一齊講。'))
  slots=self.quantity_slots(scope,question,state,ctx)
  if slots:return slots
  if re.fullmatch(r'(?:就|我想|我要|想|要|買|买|想買|想买|就要|就買)?\s*(?:呢個|呢款|這個|这个|這款|这款)(?:呀|啦|喇)?',q):
   product=self.reference_product(scope,state)
   if product:return decision({'type':'buy','product':product,'quantity':state.get('pending_quantity',1)})
   return decision(result=self.clarify_turn(scope,state,'你指邊款？請回覆商品碼或「第二款」。'))
  if re.fullmatch(r'(?:呢個|呢款|佢|這個|这个)?(?:幾錢|幾多錢|多少錢|多少錢一件|單價|价钱|價格)(?:呀|呢)?',q):
   product=self.reference_product(scope,state)
   if product:return decision({'type':'product_price','product':product})
   return decision(result=self.clarify_turn(scope,state,'想問邊款嘅價錢？請回覆商品碼或產品名稱。'))
  if re.search(r'^(?:呢個|呢款|佢|這個|这个)?(?:有咩功效|功效|點用|用法|成分|適合咩|適合什麼|介紹)',q):
   product=self.reference_product(scope,state)
   if product:
    title=next(p['title'] for p in self.catalog.items if p['id']==product);return self.routed_search(title+' '+question)
   return decision(result=self.clarify_turn(scope,state,'想了解邊款產品？請先選商品，我再按資料回答。'))
  if re.fullmatch(r'等陣(?:先)?|遲啲(?:先)?|我再諗諗|再諗下|我諗下|稍後|稍后|待會|待会',q):
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
   return decision(result=self.clarify_turn(scope,{**state,'prompt':'paused'},'好，資料會保留，暫時未有新操作。想接住做可回覆「繼續購買」或「繼續充值」。'))
  # A short acknowledgement answers only the last prompt, not an old wallet draft.
  affirmative=purchase_input(question)
  if affirmative and affirmative['type']=='confirm_current':
   if state['prompt']=='paused':return decision(result=self.clarify_turn(scope,state,'想接住做可回覆「繼續購買」或「繼續充值」。'))
   if state['topic']=='recharge':
    if ctx and re.search(r'購買|购买|落單|下單',q):return decision({'type':'buy','product':ctx['product'],'quantity':ctx['quantity']})
    return decision(result=self.clarify_turn(scope,state))
   if state['topic']=='search':return decision(result=self.clarify_turn(scope,state,'想買邊款？請回覆商品碼或「買呢個」（只顯示一款時適用）。'))
   if ctx and ctx['phase']=='quote' and not (state['topic']=='purchase' and state['prompt']=='quote'):
    with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=?",(scope,))
    if re.search(r'確認|确认|確定|确定|買|买|落單|下單',q):return decision({'type':'buy','product':ctx['product'],'quantity':ctx['quantity']})
    return decision(result=self.clarify_turn(scope,state))
   if state['topic'] not in ['purchase'] and not ctx:return decision(result=self.clarify_turn(scope,state))
  if re.fullmatch(r'多謝(?:你)?|唔該(?:晒)?|謝謝|谢谢|thanks?|thx|收到(?:啦|喇)?|明白(?:了|啦)?|👍|🙏',q,re.I):
   return decision(result=self.clarify_turn(scope,state,'收到。'+('有需要可以繼續講想搵嘅產品或下一步。' if state['topic']=='general' else '你可以繼續上一步，或者直接講想轉做咩。')))
  if re.fullmatch(r'再買\s*([一二三四五六七八九十兩两\d]+)(?:個|件|套)',q) and ctx:
   n=quantity(re.search(r'再買\s*(.+?)(?:個|件|套)$',q)[1])
   if n:return decision({'type':'buy','product':ctx['product'],'quantity':n})
  if re.fullmatch(r'(?:改為|改做|改成|改|要)\s*[一二三四五六七八九十兩两\d]+',q) and state['topic']=='purchase':
   n=quantity(re.sub(r'^(?:改為|改做|改成|改|要)\s*','',q))
   if n and ctx:return decision({'type':'buy','product':ctx['product'],'quantity':n})
  action,result=self.shortcuts(scope,question)
  if action.get('type')=='buy' and 'quantity' not in action and state.get('pending_quantity'):action['quantity']=state['pending_quantity']
  if action or result:return decision(action,result)
  if re.fullmatch(r'\d+(?:\.\d+)?',q):
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
   return decision(result=self.clarify_turn(scope,{**state,'prompt':'clarify'},'呢個數字係預算、購買數量，定充值金額？可以講「500以下」、「買2個」或「充值500」。'))
  # Questions about an in-progress step get a focused prompt instead of unrelated products.
  if state['topic'] in ['recharge','purchase'] and re.fullmatch(r'點做|然後呢|之後呢|下一步|咩意思|什麼意思|點解|不明白|唔明|嗯+|哦+|吓|咁點',q):return decision(result=self.clarify_turn(scope,state))
  if re.fullmatch(r'(?:繼續|继续)',q):
   if state['topic']=='recharge':return decision({'type':'plans'} if state['prompt']!='review' else {'type':'review_status'})
   return decision(result=self.clarify_turn(scope,state))
  return decision()
 def language_fallback(self,scope,question,history,use_model):
  """Optional semantic interpretation can only read, search or clarify; never confirm money."""
  state=previous_dialogue(history);q=normalized(question)
  if state['topic'] not in ['recharge','purchase','search']:return None
  from product_identity import named_products
  items=[p for p in self.catalog.items if '_names' in p]
  if not items:return None
  explicit_search=bool(re.search(r'全部|搵|找|搜尋|搜索|推薦|推荐|有冇|有沒有|面霜|精華|面膜|套裝|髮膜|髪膜|洗髮|香薰|護膚',q))
  if explicit_search or (items and named_products(q,items)):return None
  # Existing policy answers are handled by the knowledge source, without a router call.
  if hasattr(self.catalog,'knowledge') and self.catalog.knowledge.answer(q):return None
  intent=None
  if use_model and items:
   import kimi
   if kimi.configured():
    try:intent=kimi.interpret(q,{'topic':state['topic'],'prompt':state['prompt'],'products':[p['title'] for p in self.dialogue_products(state,scope)]})
    except kimi.ModelError:pass
  mapping={'cheaper':'平啲','alternatives':'其他款','product_price':'幾錢'}
  if intent in mapping:return self.route_turn(scope,mapping[intent],history)
  if intent=='product_info':
   product=self.reference_product(scope,state)
   if product:return self.routed_search(next(p['title'] for p in self.catalog.items if p['id']==product)+' '+question)
   return {'action':{},'result':self.clarify_turn(scope,state,'想問邊款產品資料？請選商品碼。'),'query':question}
  if intent=='balance':return {'action':{'type':'balance'},'result':None,'query':question}
  if intent=='topup_status':return {'action':{'type':'review_status'},'result':None,'query':question}
  if intent=='topup_help':
   result=self.clarify_turn(scope,{'topic':'recharge','prompt':'amount','product_ids':[]},'充值可用 PayMe 或 Alipay，過數後傳截圖，待工作人員核款入帳。你想充值幾多港幣？')
   return {'action':{},'result':result,'query':question}
  if intent=='search':return None
  if intent=='human':return {'action':{'type':'cs'},'result':None,'query':question}
  if intent=='clarify' or (intent is None and state['topic'] in ['recharge','purchase'] and len(q)<=30):
   attempts=state.get('clarifications',0)+1
   result=self.clarify_turn(scope,{**state,'clarifications':attempts,**({'prompt':'clarify'} if state['topic']=='purchase' else {})},'我未確定你想做邊一步。'+('想繼續充值、查核款進度，定轉返搵產品？' if state['topic']=='recharge' else '想改數量、問產品資料，定重新選一款？'))
   return {'action':{},'result':result,'query':question}
  return None
 def finish_dialogue(self,scope,result,question,route,history,kind):
  if result.get('dialogue'):
   if result['dialogue']['topic'] in ['recharge','image','general'] or result['dialogue'].get('prompt')=='clarify':
    with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
   return result
  old=previous_dialogue(history);state={'topic':'general','prompt':'none','product_ids':[p['id'] for p in result.get('products',[])]}
  if result.get('handoff'):state.update(topic='support')
  elif result.get('quote'):state.update(topic='purchase',prompt='quote',quote=result['quote']['id'])
  elif result.get('order'):state.update(topic='purchase',prompt='completed')
  elif result.get('topup_amount_input'):state.update(topic='recharge',prompt='amount')
  elif result.get('topup_methods'):state.update(topic='recharge',prompt='method')
  elif result.get('payment') or (kind=='topup_progress' and not result.get('topup_status')):state.update(topic='recharge',prompt='receipt')
  elif result.get('topup_status'):state.update(topic='recharge',prompt='review')
  elif result.get('products') or result.get('budget'):
   ctx=self.purchase_context(scope)
   if result.get('selection') or (ctx and ctx['phase']=='quote' and result.get('mode')=='workflow'):
    state.update(topic='purchase',prompt='quote' if ctx and ctx['phase']=='quote' else 'product')
   else:state.update(topic='search',prompt='product')
  if kind=='balance' and not result.get('handoff'):
   state={**old,'prompt':'none'}
   if old['topic']=='purchase':
    with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
    result['answer']+='\n想繼續買頭先的商品，可回覆「繼續購買」重新確認報價。'
  if state['topic']=='recharge':
   with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
  if state['topic']=='search':
   state['query']=old.get('query',question) if kind=='product_price' else route.get('query',question)
   if result.get('budget'):state['budget']=result['budget']
   state['seen']=list(dict.fromkeys(route.get('exclude_ids',[])+state['product_ids']))[-60:]
   if route.get('pending_quantity'):state['pending_quantity']=route['pending_quantity']
  result['dialogue']=state
  return result
