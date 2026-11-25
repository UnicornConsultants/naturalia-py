import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from storage import Store
from commerce import Commerce
from admin_domain import AdminDB
from whatsapp_backend import WhatsAppBackend
class FakeCatalog:
 items=[]
 def __init__(self):self.calls=[]
 def chat(self,text,history,use_model=True):
  self.calls.append((text,history,use_model));return {'answer':'測試回答','products':[]}
class WhatsAppTests(unittest.TestCase):
 def test_identity_idempotency_and_test_mode(self):
  with tempfile.TemporaryDirectory() as d:
   store=Store(Path(d)/'db');catalog=FakeCatalog();b=WhatsAppBackend(store,catalog)
   body={'account':'a','peer':'person','event':'m1','text':'hello','test':True}
   b.handle(body);b.handle(body);self.assertEqual(len(catalog.calls),1);self.assertFalse(catalog.calls[0][2])
   b.handle(dict(body,event='m2'));self.assertEqual(len(catalog.calls[-1][1]),2)
   b.handle(dict(body,account='b'));self.assertEqual(catalog.calls[-1][1],[])
   self.assertFalse(b.authorized('bad'));self.assertTrue(b.authorized(b.secret))
 def test_paused_channel_records_without_handover(self):
  with tempfile.TemporaryDirectory() as d:
   catalog=FakeCatalog();db=AdminDB(Path(d)/'admin.db');commerce=Commerce(db,catalog);b=WhatsAppBackend(Store(Path(d)/'chat.db'),catalog,commerce)
   body={'account':'a','peer':'85290000001@s.whatsapp.net','event':'hi-1','text':'hi'}
   b.observe(body);b.observe(body)
   snapshot=commerce.snapshot({'role':'admin'});conv=snapshot['conversations'][0]
   self.assertEqual(conv['status'],'ai');self.assertIsNone(conv['reason']);self.assertEqual(snapshot['waiting_cs'],0)
   self.assertEqual(len(commerce.history(conv['id'])['messages']),1);self.assertEqual(catalog.calls,[])
   b.observe(dict(body,event='cs-2',text='我要找真人'))
   self.assertEqual(commerce.snapshot({'role':'admin'})['waiting_cs'],1)
   commerce.state({'scope':conv['id'],'status':'human'},{'id':'staff','role':'admin'})
   b.observe(dict(body,event='hi-3'))
   self.assertEqual(commerce.history(conv['id'])['status'],'human')
 def test_validation(self):
  with tempfile.TemporaryDirectory() as d:
   b=WhatsAppBackend(Store(Path(d)/'db'),FakeCatalog())
   with self.assertRaises(ValueError):b.handle({'text':'a'})
 def test_observe_then_reply_and_retry_do_not_duplicate_customer_message(self):
  with tempfile.TemporaryDirectory() as d:
   catalog=FakeCatalog();db=AdminDB(Path(d)/'admin.db');commerce=Commerce(db,catalog);b=WhatsAppBackend(Store(Path(d)/'chat.db'),catalog,commerce)
   body={'account':'a','peer':'85290000001@s.whatsapp.net','event':'welcome','text':'hi'}
   b.observe(body);result=b.handle(body);b.observe(body);self.assertEqual(b.handle(body),result)
   conv=commerce.snapshot({'id':'staff','role':'admin'})['conversations'][0]
   self.assertEqual([m['role'] for m in commerce.history(conv['id'])['messages']],['user','assistant'])
   self.assertEqual(catalog.calls,[])
   commerce.state({'scope':conv['id'],'status':'human'},{'id':'staff','role':'admin'})
   body=dict(body,event='manual-followup',text='客服，退貨點處理？')
   b.observe(body);result=b.handle(body)
   self.assertTrue(result['suppress_delivery']);self.assertEqual(commerce.history(conv['id'])['status'],'human')
   self.assertEqual([m['role'] for m in commerce.history(conv['id'])['messages']],['user','assistant','user'])
 def test_observe_after_legacy_reply_does_not_duplicate(self):
  with tempfile.TemporaryDirectory() as d:
   catalog=FakeCatalog();db=AdminDB(Path(d)/'admin.db');commerce=Commerce(db,catalog);b=WhatsAppBackend(Store(Path(d)/'chat.db'),catalog,commerce)
   body={'account':'a','peer':'85290000001@s.whatsapp.net','event':'welcome','text':'hi'}
   b.handle(body);b.observe(body)
   conv=commerce.snapshot({'id':'staff','role':'admin'})['conversations'][0]
   self.assertEqual(len(commerce.history(conv['id'])['messages']),2)
 def test_photo_first_reapplication_survives_observe_reply_and_retries(self):
  import base64,json
  with tempfile.TemporaryDirectory() as d:
   catalog=FakeCatalog();db=AdminDB(Path(d)/'admin.db');commerce=Commerce(db,catalog);b=WhatsAppBackend(Store(Path(d)/'chat.db'),catalog,commerce)
   staff={'id':'fixture','role':'admin'};counter=0
   def send(text,photo=None):
    nonlocal counter
    counter+=1;body={'account':'fixture','peer':'85290000001@s.whatsapp.net','event':str(counter),'text':text}
    if photo:body['attachment']={'mime':'image/png','data':base64.b64encode(b'\x89PNG\r\n\x1a\n'+photo.encode()).decode()}
    b.observe(body);result=b.handle(body);b.observe(body);self.assertEqual(b.handle(body),result);return result
   send('充值 1200 Alipay');old=send('入左了','first')['topup_id']
   db.review({'id':old,'decision':'rejected','reason':'不是付款憑證'},staff)
   self.assertTrue(send('再申請真的充值了','second')['topup_amount_input'])
   self.assertTrue(send('Alipay')['topup_amount_input']);new=send('1200')['topup_id']
   self.assertEqual(send('充值了喇')['topup_id'],new);self.assertNotEqual(old,new)
   conv=commerce.snapshot(staff)['conversations'][0];messages=commerce.history(conv['id'])['messages']
   self.assertEqual(len(messages),12);self.assertEqual(sum(m['role']=='user' for m in messages),6)
   photos=[m for m in messages if m['role']=='user' and (m.get('payload') or {}).get('evidence_id')]
   self.assertEqual(len(photos),2);self.assertEqual(catalog.calls,[])
   with db.db() as c:
    row=c.execute('SELECT * FROM topups WHERE id=?',(new,)).fetchone();self.assertEqual(row['method'],'alipay');self.assertEqual(json.loads(row['snapshot'])['paid'],120000)
    self.assertEqual(c.execute('SELECT COUNT(*) FROM topups').fetchone()[0],2);self.assertEqual(commerce.balance(c,conv['customer']),0)
 def test_handoff_notice_once_then_topic_changes_record_without_ai_reply(self):
  with tempfile.TemporaryDirectory() as d:
   catalog=FakeCatalog();db=AdminDB(Path(d)/'admin.db');commerce=Commerce(db,catalog);b=WhatsAppBackend(Store(Path(d)/'chat.db'),catalog,commerce)
   staff={'id':'fixture','role':'admin'}
   def send(text,event):
    data={'account':'a','peer':'85290000001@s.whatsapp.net','event':event,'text':text}
    b.observe(data);return b.handle(data)
   send('hi','welcome');first=send('我要找真人','cs');self.assertTrue(first['handoff']);self.assertFalse(first.get('suppress_delivery'))
   conv=commerce.snapshot(staff)['conversations'][0];reason=conv['reason']
   for i,text in enumerate(['搵CS','有冇1200以下','充值500','確認','唔使CS','圖片']):
    self.assertTrue(send(text,'followup-'+str(i))['suppress_delivery'])
   history=commerce.history(conv['id']);self.assertEqual(history['status'],'waiting')
   self.assertEqual(sum(m['role']=='assistant' for m in history['messages']),2);self.assertEqual(sum(m['role']=='user' for m in history['messages']),8)
   self.assertEqual(commerce.snapshot(staff)['conversations'][0]['reason'],reason);self.assertEqual(catalog.calls,[])
   # A replay of the old welcome cannot bypass the active handover through the adapter cache.
   self.assertTrue(send('hi','welcome')['suppress_delivery'])
   commerce.state({'scope':conv['id'],'status':'human'},staff);self.assertTrue(send('退貨','assigned')['suppress_delivery']);self.assertEqual(commerce.history(conv['id'])['status'],'human')
   commerce.state({'scope':conv['id'],'status':'ai'},staff);self.assertFalse(send('hi','resume').get('handoff'))
 def test_delivery_gate_blocks_stale_replies_and_all_assigned_cs_messages(self):
  with tempfile.TemporaryDirectory() as d:
   catalog=FakeCatalog();db=AdminDB(Path(d)/'admin.db');commerce=Commerce(db,catalog);b=WhatsAppBackend(Store(Path(d)/'chat.db'),catalog,commerce)
   data={'account':'a','peer':'85290000001@s.whatsapp.net','event':'one','text':'hi'};staff={'id':'staff','role':'admin'}
   self.assertFalse(b.delivery_allowed(data)['allowed']);b.observe(data);self.assertTrue(b.delivery_allowed(data)['allowed'])
   conv=commerce.snapshot(staff)['conversations'][0];commerce.handover(conv['id'],'image')
   self.assertFalse(b.delivery_allowed(data)['allowed']);self.assertTrue(b.delivery_allowed(dict(data,handoff=True))['allowed'])
   self.assertFalse(b.delivery_allowed(dict(data,handoff='true'))['allowed']);self.assertFalse(b.delivery_allowed(dict(data,account='other',handoff=True))['allowed'])
   commerce.state({'scope':conv['id'],'status':'human'},staff);self.assertFalse(b.delivery_allowed(dict(data,handoff=True))['allowed'])
   commerce.state({'scope':conv['id'],'status':'ai'},staff);self.assertTrue(b.delivery_allowed(data)['allowed'])
if __name__=='__main__':unittest.main()
