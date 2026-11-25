"""Conversation-scoped checkout context and recoverable checkout prompts."""
import json,time
from purchase_inputs import purchase_input

class PurchaseFlow:
 def purchase_context(self,scope):
  with self.db.db(True) as c:
   row=c.execute('SELECT * FROM purchase_context WHERE scope=?',(scope,)).fetchone()
   if row:return dict(row)
   # Upgrade existing conversations without replaying any financial action.
   row=c.execute("SELECT q.* FROM purchase_quotes q JOIN chat_choices v ON v.target=q.id AND v.scope=q.scope AND v.kind='quote' WHERE q.scope=? AND q.expires>0 AND v.expires>? ORDER BY q.rowid DESC LIMIT 1",(scope,time.time())).fetchone()
   if not row:return None
   phase='completed' if row['order_id'] else 'selected'
   c.execute('INSERT INTO purchase_context VALUES (?,?,?,?,?,?)',(scope,row['product'],row['quantity'],row['id'],phase,time.time()))
   return dict(c.execute('SELECT * FROM purchase_context WHERE scope=?',(scope,)).fetchone())
 def selected_products(self,scope,product=None):
  if product:return [{k:v for k,v in p.items() if not k.startswith('_')} for p in self.catalog.items if p['id']==product]
  with self.db.db() as c:
   rows=c.execute("SELECT payload FROM conversation_messages WHERE scope=? AND role='assistant' AND payload IS NOT NULL ORDER BY id DESC LIMIT 30",(scope,)).fetchall()
  for row in rows:
   products=json.loads(row['payload']).get('products',[])
   if products:return [{k:v for k,v in p.items() if not k.startswith('_')} for p in self.catalog.items if p['id'] in {v['id'] for v in products}][:3]
  return []
 def purchase_help(self,scope,message='我未能確認你想點樣購買，今次未有扣款。',product=None):
  ctx=self.purchase_context(scope)
  if not product and ctx and ctx['phase'] not in ['cancelled','browsing']:product=ctx['product']
  products=self.selected_products(scope,product)
  suffix='請回覆下方商品碼選購；買兩件可在商品碼後加「2個」。'
  if product:suffix='已保留你選擇的商品。可回覆商品碼重新報價，或回覆「買 2 個」更改數量；確認報價後才會扣款。'
  result=self.result(message+'\n'+suffix,products=products)
  if ctx and product==ctx['product']:result['selection']={'product':product,'quantity':ctx['quantity']}
  return result
 def checkout_input(self,scope,question):
  parsed=purchase_input(question)
  if not parsed:return None,None
  ctx=self.purchase_context(scope);kind=parsed['type']
  if kind=='cancel_purchase':
   if not ctx:return {},self.result('目前沒有待確認購買，未有扣款。')
   with self.db.db(True) as c:
    c.execute("UPDATE purchase_context SET phase='cancelled',updated=? WHERE scope=?",(time.time(),scope))
    c.execute('UPDATE purchase_quotes SET expires=0 WHERE scope=? AND order_id IS NULL',(scope,))
   return {},self.result('已取消今次未完成的購買，未有扣款。已完成的訂單不受影響。',products=self.selected_products(scope,ctx['product']))
  if kind=='quantity':
   if parsed['quantity'] is None:
    with self.db.db(True) as c:c.execute("UPDATE purchase_context SET phase='selected' WHERE scope=? AND phase='quote'",(scope,))
    return {},self.purchase_help(scope,'數量請用 1 至 99 的整數，例如「買 2 個」。未有扣款。')
   product=None
   if parsed['code']:
    with self.db.db() as c:
     row=c.execute("SELECT v.target FROM chat_choices v JOIN product_codes p ON p.product=v.target WHERE v.scope=? AND v.kind='product' AND p.code=? AND v.expires>?",(scope,parsed['code'],time.time())).fetchone()
    if row:product=row['target']
   elif ctx and ctx['phase'] in ['selected','quote']:product=ctx['product']
   elif not ctx or ctx['phase']=='browsing':
    candidates=self.selected_products(scope)
    if len(candidates)==1:product=candidates[0]['id']
   if not product:return {},self.purchase_help(scope,'想買 '+str(parsed['quantity'])+' 件邊款商品？請連商品碼一齊回覆；今次未有扣款。')
   return {'type':'buy','product':product,'quantity':parsed['quantity']},None
  if kind=='confirm_number':
   with self.db.db() as c:row=c.execute("SELECT target FROM chat_choices WHERE scope=? AND kind='quote' AND number=? AND expires>?",(scope,parsed['number'],time.time())).fetchone()
   if not row:return {},self.purchase_help(scope,'呢個確認編號不存在或已過期，未有扣款。如果想買兩件，請回覆「買 2 個」。')
   return {'type':'confirm','quote':row['target']},None
  if kind=='confirm_current':
   if ctx and ctx['phase']=='completed':return {'type':'confirm','quote':ctx['quote']},None
   if ctx and ctx['phase']=='quote' and ctx['quote']:return {'type':'confirm','quote':ctx['quote']},None
   if ctx and ctx['phase']=='selected':return {'type':'buy','product':ctx['product'],'quantity':ctx['quantity']},None
   return {},self.purchase_help(scope,'目前沒有可直接確認的報價，未有扣款。請先選擇商品及數量。')
  return {},self.purchase_help(scope)
 def confirm_purchase(self,scope,customer,quote):
  ctx=self.purchase_context(scope)
  with self.db.db() as c:row=c.execute('SELECT * FROM purchase_quotes WHERE id=? AND scope=? AND customer=?',(quote,scope,customer)).fetchone()
  if not row:return self.purchase_help(scope,'搵唔到呢個購買確認，未有扣款。')
  if row['order_id']:
   with self.db.db() as c:order=dict(c.execute('SELECT * FROM wallet_orders WHERE id=?',(row['order_id'],)).fetchone())
   result=self.order_result(order);result['answer']='此購買已完成，沒有再次扣款。\n'+result['answer'];return result
  if ctx and (ctx['quote']!=quote or ctx['phase']!='quote'):
   return self.purchase_help(scope,'呢個確認已失效，或商品／數量已改動，未有扣款。請按目前選擇重新報價。')
  try:return self.purchase(scope,customer,quote)
  except Exception as error:
   from admin_domain import AdminError
   if not isinstance(error,AdminError):raise
   # A fresh price/expiry quote must be shown and confirmed again, never auto-charged.
   with self.db.db() as c:state=c.execute('SELECT status FROM conversations WHERE id=?',(scope,)).fetchone()
   if state and state['status']=='ai':
    result=self.quote(scope,customer,row['product'],row['quantity']);result['answer']=str(error)+'。今次未有扣款。\n'+result['answer'];return result
   return self.purchase_help(scope,str(error)+'。今次未有扣款。')
