"""Staff-owned test conversations, isolated from customers and WhatsApp."""
import json,threading,time
from admin_domain import AdminError
class AdminChat:
 def __init__(self,db,catalog,lock=None,commerce=None):
  self.commerce=commerce;self.db=db;self.catalog=catalog;self.lock=lock or threading.Lock()
  with db.db(True) as conn:conn.execute('CREATE TABLE IF NOT EXISTS test_chats(staff TEXT, mode TEXT, messages TEXT, updated REAL, PRIMARY KEY(staff,mode))')
 def handle(self,user,action,body):
  self.db.require(user,'admin','support')
  mode=body.get('mode','retrieval')
  if mode not in ['retrieval','kimi']:raise AdminError('測試模式無效')
  if not self.lock.acquire(blocking=False):raise AdminError('助手正在處理訊息，請稍後再試',429)
  try:
   if self.commerce:
    import secrets
    customer=body.get('customer') or None
    if customer:
     with self.db.db() as conn:
      if not conn.execute('SELECT 1 FROM customers WHERE id=?',(customer,)).fetchone():raise AdminError('測試客戶不存在')
    scope='test:'+user['id']+':'+mode+':'+(customer or 'guest')
    if action=='reset':
     with self.db.db(True) as conn:
      conn.execute('DELETE FROM conversation_messages WHERE scope=?',(scope,));conn.execute('DELETE FROM conversation_events WHERE scope=?',(scope,));conn.execute("UPDATE conversations SET status='ai' WHERE id=?",(scope,))
      conn.execute('DELETE FROM purchase_context WHERE scope=?',(scope,));conn.execute('UPDATE purchase_quotes SET expires=0 WHERE scope=? AND order_id IS NULL',(scope,));conn.execute("UPDATE chat_choices SET expires=0 WHERE scope=? AND kind='quote'",(scope,))
     return self.commerce.history(scope)
    self.commerce.ensure(scope,'test',customer=customer)
    if action=='message':
     question=body.get('message')
     if not isinstance(question,str) or not question.strip() or len(question)>1000:raise AdminError('請輸入 1 至 1000 字訊息')
     self.commerce.respond(scope,question,body.get('event') or secrets.token_hex(16),action=body.get('action'),attachment=body.get('attachment'),use_model=mode=='kimi',channel='test',customer=customer)
    return self.commerce.history(scope)
   with self.db.db(True) as conn:
    conn.execute('DELETE FROM test_chats WHERE updated<?',(time.time()-7*86400,))
    row=conn.execute('SELECT messages FROM test_chats WHERE staff=? AND mode=?',(user['id'],mode)).fetchone()
   messages=json.loads(row['messages']) if row else []
   if action=='reset':
    with self.db.db(True) as conn:conn.execute('DELETE FROM test_chats WHERE staff=? AND mode=?',(user['id'],mode))
    return {'messages':[]}
   if action=='message':
    question=body.get('message')
    if not isinstance(question,str) or not question.strip() or len(question)>1000:raise AdminError('請輸入 1 至 1000 字訊息')
    history=[{'role':m['role'],'content':m['content'][:2000]} for m in messages][-10:]
    result=self.catalog.chat(question.strip(),history,use_model=mode=='kimi')
    messages.extend([{'role':'user','content':question.strip()},{'role':'assistant','content':result['answer'],'payload':result}]);messages=messages[-24:]
    with self.db.db(True) as conn:conn.execute('INSERT OR REPLACE INTO test_chats VALUES (?,?,?,?)',(user['id'],mode,json.dumps(messages,ensure_ascii=False),time.time()))
   return {'messages':messages}
  finally:self.lock.release()
