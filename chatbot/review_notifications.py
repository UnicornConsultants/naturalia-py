"""Durable review-result delivery, separate from AI replies and human takeover."""
import json,os,threading,time,urllib.request
from pathlib import Path

class ReviewNotifier:
 def __init__(self,commerce,send=None):self.commerce=commerce;self.db=commerce.db;self.send=send or self.send_whatsapp
 def send_whatsapp(self,row):
  key=(Path(__file__).parent/'runtime/whatsapp-service.key').read_text().strip()
  req=urllib.request.Request('http://127.0.0.1:'+os.getenv('WA_PORT','8767')+'/api/notify',data=json.dumps({'request_id':row['id'],'account':row['account'],'peer':row['peer'],'text':row['message']}).encode(),headers={'X-Backend-Key':key,'Content-Type':'application/json'})
  with urllib.request.urlopen(req,timeout=12) as response:return json.load(response)['status']
 def process(self,topup=None):
  with self.db.db() as c:
   jobs=[r['topup'] for r in c.execute('SELECT topup FROM review_notice_jobs WHERE processed=0'+(' AND topup=?' if topup else '')+' LIMIT 20',(topup,) if topup else ())]
  for job in jobs:self.commerce.notify_review(job)
  # Claim one batch once, so a failed connection does not cause a tight retry loop.
  now=time.time()
  with self.db.db(True) as c:
   rows=[dict(r) for r in c.execute("SELECT * FROM review_notifications WHERE status IN ('pending','sending') AND next_attempt<=?"+(' AND topup=?' if topup else '')+' ORDER BY created LIMIT 10',(now,topup) if topup else (now,))]
   for row in rows:c.execute("UPDATE review_notifications SET status='sending',attempts=attempts+1,next_attempt=?,updated=? WHERE id=?",(now+60,now,row['id']))
  for row in rows:
   try:
    status=self.send(row)
    if status not in ['sent','uncertain']:status='pending'
   except Exception:status='pending' # Retry the SAME request id; bridge deduplication prevents repeated sends.
   now=time.time()
   with self.db.db(True) as c:
    c.execute('UPDATE review_notifications SET status=?,next_attempt=?,updated=? WHERE id=?',(status,now+min(60,5*2**min(row['attempts'],4)),now,row['id']))
    message=c.execute('SELECT payload FROM conversation_messages WHERE id=?',(row['message_id'],)).fetchone()
    if message:
     payload=json.loads(message['payload']);payload['delivery']=status
     c.execute('UPDATE conversation_messages SET payload=? WHERE id=?',(json.dumps(payload,ensure_ascii=False),row['message_id']))
    if status in ['sent','uncertain']:self.db.audit(c,'review-notifier','topup.notification.'+status,row['topup'],{'scope':row['scope']})
 def start(self):
  self.stop=threading.Event()
  def run():
   while not self.stop.is_set():
    try:self.process()
    except Exception:pass # Persisted jobs remain available for the next poll.
    self.stop.wait(5)
  threading.Thread(target=run,name='review-notifications',daemon=True).start()
