import concurrent.futures,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_domain import AdminDB
from commerce import Commerce
from review_notifications import ReviewNotifier
class Catalog:items=[]
class NoticeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=AdminDB(Path(self.tmp.name)/'db');self.c=Commerce(self.db,Catalog());self.user={'id':'finance','role':'finance'}
  self.c.ensure('scope','whatsapp','account','85290000000@s.whatsapp.net');self.customer=self.c.ensure('scope')['customer']
  self.plan=self.db.create_plan({'name':'Fixture','paid':'100','principal':'100','bonus':'20'},self.user)['id']
  self.topup=self.db.create_topup({'request_key':'fixture','customer':self.customer,'plan':self.plan,'method':'payme','reference':'unique','evidence':'fixture'},self.user)['id']
  with self.db.db(True) as db:
   plan=dict(db.execute('SELECT * FROM plans WHERE id=?',(self.plan,)).fetchone())
   db.execute('INSERT INTO payment_intents VALUES (?,?,?,?,?,?,?,?)',('intent','scope',self.customer,self.plan,'payme',json.dumps(plan),self.topup,1))
 def tearDown(self):self.tmp.cleanup()
 def review(self,decision='approved'):
  return self.db.review({'id':self.topup,'decision':decision,'reason':'未能核對收款','payment_verified':True,'verified_paid':'100'},self.user)
 def test_approved_persists_job_and_delivers_once_after_restart(self):
  self.review();sent=[];notifier=ReviewNotifier(self.c,lambda row:sent.append(row) or 'sent')
  notifier.process();self.review();notifier.process()
  self.assertEqual(len(sent),1);self.assertEqual(sent[0]['peer'],'85290000000@s.whatsapp.net');self.assertIn('充值成功',sent[0]['message']);self.assertIn('HK$120.00',sent[0]['message'])
  self.assertEqual(len(self.c.history('scope')['messages']),1);self.assertEqual(self.c.history('scope')['messages'][0]['payload']['delivery'],'sent')
  with self.db.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM ledger').fetchone()[0],1)
 def test_rejected_notifies_reason_without_credits_or_changing_human_state(self):
  self.c.state({'scope':'scope','status':'human'},{'id':'cs','role':'support'});self.review('rejected');sent=[]
  ReviewNotifier(self.c,lambda row:sent.append(row) or 'sent').process()
  self.assertIn('未獲批准',sent[0]['message']);self.assertIn('未能核對收款',sent[0]['message']);self.assertEqual(self.c.history('scope')['status'],'human')
  with self.db.db() as db:self.assertEqual(self.c.balance(db,self.customer),0)
 def test_disconnected_retries_same_id_and_uncertain_is_not_resent(self):
  self.review();attempts=[]
  def send(row):attempts.append(row['id']);raise OSError('offline')
  n=ReviewNotifier(self.c,send);n.process()
  with self.db.db(True) as db:
   self.assertEqual(db.execute('SELECT status FROM review_notifications').fetchone()[0],'pending');db.execute('UPDATE review_notifications SET next_attempt=0')
  n.send=lambda row:attempts.append(row['id']) or 'uncertain';n.process();n.process()
  self.assertEqual(len(attempts),2);self.assertEqual(attempts[0],attempts[1]);self.assertEqual(len(self.c.history('scope')['messages']),1)
  self.assertEqual(self.c.history('scope')['messages'][0]['payload']['delivery'],'uncertain')
 def test_two_workers_share_atomic_claim_and_web_uses_history_only(self):
  self.review();sent=[]
  def worker(_):ReviewNotifier(self.c,lambda row:sent.append(row['id']) or 'sent').process()
  with concurrent.futures.ThreadPoolExecutor() as pool:list(pool.map(worker,range(4)))
  self.assertEqual(len(sent),1)
  # No migration backfill of earlier historical reviews without an explicit job.
  with self.db.db(True) as db:
   db.execute("UPDATE conversations SET channel='web' WHERE id='scope'");db.execute('DELETE FROM review_notifications');db.execute('UPDATE review_notice_jobs SET processed=0')
  sent.clear();ReviewNotifier(self.c,lambda row:sent.append(row) or 'sent').process();self.assertEqual(sent,[])
