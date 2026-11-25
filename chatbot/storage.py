import hashlib,json,secrets,sqlite3,time
from pathlib import Path
class Store:
 def __init__(self,path):
  self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
  with self.connect() as db:
   db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, created REAL)')
   db.execute('CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY, session TEXT, role TEXT, content TEXT, payload TEXT)')
 def connect(self):return sqlite3.connect(self.path,timeout=20)
 def create(self):
  token=secrets.token_urlsafe(32)
  with self.connect() as db:db.execute('INSERT INTO sessions VALUES (?,?)',(self.key(token),time.time()))
  return token
 def key(self,token):return hashlib.sha256(token.encode()).hexdigest()
 def valid(self,token):
  if not token:return False
  with self.connect() as db:return bool(db.execute('SELECT 1 FROM sessions WHERE id=? AND created>?',(self.key(token),time.time()-86400*7)).fetchone())
 def history(self,token):
  with self.connect() as db:
   rows=db.execute('SELECT role,content,payload FROM messages WHERE session=? ORDER BY id DESC LIMIT 24',(self.key(token),)).fetchall()
   return [{'role':r,'content':c,'payload':json.loads(p) if p else None} for r,c,p in reversed(rows)]
 def append(self,token,question,result):
  with self.connect() as db:
   db.execute('INSERT INTO messages(session,role,content,payload) VALUES (?,?,?,?)',(self.key(token),'user',question,None))
   db.execute('INSERT INTO messages(session,role,content,payload) VALUES (?,?,?,?)',(self.key(token),'assistant',result['answer'],json.dumps(result,ensure_ascii=False)))
 def delete(self,token):
  with self.connect() as db:
   db.execute('DELETE FROM messages WHERE session=?',(self.key(token),));db.execute('DELETE FROM sessions WHERE id=?',(self.key(token),))
 def purge(self):
  with self.connect() as db:
   db.execute('DELETE FROM messages WHERE session IN (SELECT id FROM sessions WHERE created<?)',(time.time()-86400*7,));db.execute('DELETE FROM sessions WHERE created<?',(time.time()-86400*7,))
