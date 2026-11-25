import concurrent.futures,json,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_domain import AdminDB,AdminError,money
class AdminTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.db=AdminDB(Path(self.tmp.name)/'b2b.db')
  self.admin={'id':'admin','role':'admin'};self.support={'id':'support','role':'support'}
  self.customer=self.db.create_customer({'company':'測試公司','contact':'測試聯絡人'},self.admin)['id']
  self.plan=self.db.create_plan({'name':'測試方案','paid':'100.00','principal':'100','bonus':'40','principal_days':'365','bonus_days':'30'},self.admin)['id']
 def tearDown(self):self.tmp.cleanup()
 def request(self,**extra):
  body={'customer':self.customer,'plan':self.plan,'request_key':'key','method':'bank','reference':'TEST-001','evidence':'測試核款說明'};body.update(extra);return self.db.create_topup(body,self.support)['id']
 def approve(self,uid,**extra):
  body={'id':uid,'decision':'approved','reason':'測試已核實','payment_verified':True,'verified_paid':'100'};body.update(extra);return self.db.review(body,self.admin)
 def test_exact_cents(self):
  self.assertEqual(money('0.29'),29)
  for invalid in ['1.001','-1','NaN','1e3',True,1.2]:
   with self.assertRaises(AdminError):money(invalid)
 def test_approval_creates_single_ledger_and_separate_lots(self):
  uid=self.request();self.approve(uid);self.approve(uid)
  snapshot=self.db.snapshot(self.admin);self.assertEqual(snapshot['customers'][0]['balance'],14000);self.assertEqual(len(snapshot['ledger']),1)
  with self.db.db() as db:
   lots=db.execute('SELECT * FROM lots').fetchall();self.assertEqual(len(lots),2);self.assertNotEqual(lots[0]['expires'],lots[1]['expires'])
 def test_concurrent_approval(self):
  uid=self.request()
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda _:self.approve(uid),range(2)))
  self.assertEqual(len(self.db.snapshot(self.admin)['ledger']),1)
 def test_support_cannot_approve(self):
  uid=self.request()
  with self.assertRaises(AdminError) as e:self.db.review({'id':uid},self.support)
  self.assertEqual(e.exception.status,403)
 def test_payment_confirmation_and_amount_required(self):
  uid=self.request()
  for extra in [{'payment_verified':False},{'verified_paid':'99'}]:
   with self.assertRaises(AdminError):self.approve(uid,**extra)
  self.assertEqual(self.db.snapshot(self.admin)['customers'][0]['balance'],0)
 def test_request_idempotent_and_reference_collision(self):
  uid=self.request();self.assertEqual(self.request(),uid)
  with self.assertRaises(AdminError):self.request(request_key='other')
  with self.assertRaises(AdminError):self.request(evidence='changed')
 def test_rejection_never_credits(self):
  uid=self.request();self.db.review({'id':uid,'decision':'rejected','reason':'未找到付款'},self.admin)
  with self.assertRaises(AdminError):self.approve(uid)
  self.assertEqual(self.db.snapshot(self.admin)['customers'][0]['balance'],0)
 def test_plan_versions_and_disabled_plan(self):
  uid=self.request();self.db.deactivate_plan({'id':self.plan},self.admin);self.approve(uid)
  with self.assertRaises(AdminError):self.request(request_key='new',reference='new')
  self.assertEqual(self.db.snapshot(self.admin)['topups'][0]['snapshot']['bonus'],4000)
 def test_edit_delete_restore_preserve_topup_snapshot(self):
  uid=self.request()
  change={'id':self.plan,'version':1,'name':'修改方案','paid':'200','principal':'200','bonus':'50','principal_days':'90','bonus_days':'10'}
  self.db.edit_plan(change,self.admin)
  with self.assertRaises(AdminError):self.db.edit_plan(change,self.admin)
  with self.assertRaises(AdminError):self.db.edit_plan(dict(change,version=2),self.support)
  self.db.plan_state({'id':self.plan,'action':'delete'},self.admin)
  self.approve(uid)
  snapshot=self.db.snapshot(self.admin);self.assertEqual(snapshot['topups'][0]['snapshot']['paid'],10000);self.assertEqual(snapshot['customers'][0]['balance'],14000)
  self.assertTrue(snapshot['plans'][0]['archived'])
  with self.assertRaises(AdminError):self.request(request_key='deleted',reference='deleted')
  self.db.plan_state({'id':self.plan,'action':'restore'},self.admin)
  self.assertFalse(self.db.snapshot(self.admin)['plans'][0]['active'])
  self.db.plan_state({'id':self.plan,'action':'activate'},self.admin)
  new=self.request(request_key='new',reference='new')
  self.assertEqual(next(t for t in self.db.snapshot(self.admin)['topups'] if t['id']==new)['snapshot']['paid'],20000)
 def test_expired_lots_excluded(self):
  uid=self.request();self.approve(uid)
  with self.db.db(True) as db:db.execute('UPDATE lots SET expires=? WHERE kind=?',(time.time()-1,'bonus'))
  self.assertEqual(self.db.snapshot(self.admin)['customers'][0]['balance'],10000)
 def test_immutable_ledger_and_audit(self):
  uid=self.request();self.approve(uid)
  for table in ['ledger','audit']:
   with self.assertRaises(sqlite3.IntegrityError):
    with self.db.db(True) as db:db.execute('DELETE FROM '+table)
 def test_csv_injection(self):
  with self.db.db(True) as db:db.execute('UPDATE customers SET company=?',('=HYPERLINK("bad")',))
  self.approve(self.request());self.assertIn("'=HYPERLINK",self.db.export_csv(self.admin).decode())
  with self.assertRaises(AdminError):self.db.export_csv(self.support)
 def test_staff_setup_login_expiry_logout(self):
  credentials={'name':'測試管理員','username':'owner','password':'test1234'}
  self.assertTrue(self.db.needs_setup())
  with self.assertRaises(AdminError):self.db.create_staff(dict(credentials,password='test123'),setup=True)
  self.db.create_staff(credentials,setup=True)
  with self.assertRaises(AdminError):self.db.create_staff(credentials,setup=True)
  token=self.db.login(credentials);self.assertEqual(self.db.session(token)['role'],'admin');self.assertIsNone(self.db.session('bad'))
  self.db.logout(token);self.assertIsNone(self.db.session(token))
 def test_receipt_validation(self):
  uid=self.db.save_evidence('test.pdf','application/pdf',b'%PDF-1.7 test',self.support)['id'];self.assertEqual(self.db.get_evidence(uid,self.admin)['mime'],'application/pdf')
  with self.assertRaises(AdminError):self.db.save_evidence('evil.html','image/png',b'<script>x</script>',self.support)
if __name__=='__main__':unittest.main()
