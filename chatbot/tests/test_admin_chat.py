import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from admin_chat import AdminChat
from admin_domain import AdminDB,AdminError
class FakeCatalog:
 def __init__(self):self.calls=[]
 def chat(self,q,history,use_model):
  self.calls.append((q,history,use_model));return {'answer':'測試回答','products':[],'mode':'fallback'}
class ChatTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.catalog=FakeCatalog();self.chat=AdminChat(AdminDB(Path(self.tmp.name)/'db'),self.catalog);self.user={'id':'one','role':'support'}
 def tearDown(self):self.tmp.cleanup()
 def test_history_mode_isolation_and_reset(self):
  self.chat.handle(self.user,'message',{'message':'套裝'})
  self.chat.handle(self.user,'message',{'message':'平啲'})
  self.assertFalse(self.catalog.calls[-1][2]);self.assertEqual(self.catalog.calls[-1][1][0]['content'],'套裝')
  self.assertEqual(self.chat.handle({'id':'two','role':'support'},'history',{})['messages'],[])
  self.assertEqual(self.chat.handle(self.user,'history',{'mode':'kimi'})['messages'],[])
  self.chat.handle(self.user,'message',{'message':'套裝','mode':'kimi'});self.assertTrue(self.catalog.calls[-1][2])
  self.chat.handle(self.user,'reset',{})
  self.assertEqual(self.chat.handle(self.user,'history',{})['messages'],[])
  self.assertEqual(len(self.chat.handle(self.user,'history',{'mode':'kimi'})['messages']),2)
 def test_permissions_validation_and_busy(self):
  with self.assertRaises(AdminError):self.chat.handle({'id':'finance','role':'finance'},'history',{})
  for body in [{'message':''},{'message':'x'*1001},{'message':'test','mode':'invalid'}]:
   with self.assertRaises(AdminError):self.chat.handle(self.user,'message',body)
  self.chat.lock.acquire()
  try:
   with self.assertRaises(AdminError) as error:self.chat.handle(self.user,'message',{'message':'test'})
   self.assertEqual(error.exception.status,429)
  finally:self.chat.lock.release()
  self.assertEqual(self.catalog.calls,[])
