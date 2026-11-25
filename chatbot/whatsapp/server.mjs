import express from 'express';
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomBytes,timingSafeEqual} from 'node:crypto';
import makeWASocket,{useMultiFileAuthState,DisconnectReason,downloadMediaMessage} from '@whiskeysockets/baileys';
import pino from 'pino';
import QRCode from 'qrcode';
import {Inbox,incoming,sendRichReply,sendPaymentReply,ReviewDelivery,guardedSend} from './core.mjs';
const root=path.dirname(fileURLToPath(import.meta.url));const runtime=path.join(root,'runtime');fs.mkdirSync(runtime,{recursive:true,mode:0o700});process.umask(0o077);
const port=Number(process.env.WA_PORT||8767);const backend='http://127.0.0.1:'+Number(process.env.CHATBOT_PORT||8765);
let sock=null,reconnectTimer=null,generation=0,since=0,wanted=false,attempts=0;
const state={connection:'disconnected',qr:null,account:'',error:null,lastDisconnectCode:null};
const inbox=new Inbox(path.join(runtime,'inbox.sqlite3'),{
 reply:async(row,test)=>{
  const secret=fs.readFileSync(path.join(root,'../runtime/whatsapp-service.key'),'utf8').trim();
  const r=await fetch(backend+'/internal/whatsapp/reply',{method:'POST',headers:{'Content-Type':'application/json','X-WA-Secret':secret},body:JSON.stringify({account:row.account,peer:row.peer,event:row.external_id,text:row.question,phone:row.phone,attachment:row.attachment,test}),signal:AbortSignal.timeout(65000)});
  if(!r.ok)throw Error('backend unavailable');return r.json();
 },
 observe:async row=>{const secret=fs.readFileSync(path.join(root,'../runtime/whatsapp-service.key'),'utf8').trim();const r=await fetch(backend+'/internal/whatsapp/observe',{method:'POST',headers:{'Content-Type':'application/json','X-WA-Secret':secret},body:JSON.stringify({account:row.account,peer:row.peer,event:row.external_id,text:row.question,phone:row.phone,attachment:row.attachment}),signal:AbortSignal.timeout(20000)});if(!r.ok)throw Error('CS sync failed')},
 connected:account=>state.connection==='connected'&&state.account===account&&!!sock,
 presence:async(account,peer,type)=>{if(state.connection==='connected'&&state.account===account&&sock)await sock.sendPresenceUpdate(type,peer);},
 send:async(peer,text,result,account)=>{
  const sendMessage=guardedSend(async()=>{
   if(!inbox.connected(account)||inbox.paused(account,peer)||inbox.mode==='paused')return false;
   const secret=fs.readFileSync(path.join(root,'../runtime/whatsapp-service.key'),'utf8').trim();
   const response=await fetch(backend+'/internal/whatsapp/delivery',{method:'POST',headers:{'Content-Type':'application/json','X-WA-Secret':secret},body:JSON.stringify({account,peer,handoff:result?.handoff===true}),signal:AbortSignal.timeout(10000)});
   if(!response.ok)throw Error('Conversation state unavailable');return (await response.json()).allowed===true;
  },(jid,body,options)=>sock.sendMessage(jid,body,options));
  if(result?.payment||result?.topup_methods?.some(p=>p.qr))return sendPaymentReply(peer,result,{
   canSend:()=>inbox.connected(account)&&!inbox.paused(account,peer)&&inbox.mode!=='paused',
   sendMessage,
   loadQR:async method=>({data:fs.readFileSync(path.join(root,'../../info',method==='payme'?'PayMe.jpeg':'alipay.jpeg')),mime:'image/jpeg'})
  });
  if(!result?.products?.length)return sendMessage(peer,{text},{linkPreview:undefined});
  return sendRichReply(peer,result,{
   canSend:()=>inbox.connected(account)&&!inbox.paused(account,peer)&&inbox.mode!=='paused',
   sendMessage,
   loadImage:async id=>{
    if(!/^[a-f0-9]{24}$/.test(id))return null;
    const response=await fetch(backend+'/product-images/'+id,{signal:AbortSignal.timeout(10000)});
    if(!response.ok)return null;const mime=response.headers.get('content-type');
    if(!['image/png','image/jpeg'].includes(mime))return null;
    const data=Buffer.from(await response.arrayBuffer());if(data.length>5*1024*1024)return null;
    return {data,mime};
   }
  });
 }
});
const modePath=path.join(runtime,'reply-mode.json');
try{const saved=JSON.parse(fs.readFileSync(modePath,'utf8'));if(['paused','draft','auto'].includes(saved.mode))inbox.mode=saved.mode}catch{}
function setMode(mode){fs.writeFileSync(modePath+'.tmp',JSON.stringify({mode}),{mode:0o600});fs.renameSync(modePath+'.tmp',modePath);inbox.mode=mode;}
function pauseMode(){inbox.mode='paused';try{setMode('paused')}catch{state.error='暫停模式未能儲存，請檢查本機檔案權限';}}
async function connect(){
 if(sock||state.connection==='connecting')return;
 wanted=true;const own=++generation;state.connection='connecting';state.error=null;since=Math.floor(Date.now()/1000);
 try{
  const {state:auth,saveCreds}=await useMultiFileAuthState(path.join(runtime,'auth'));
  if(own!==generation)return;
  const current=makeWASocket({auth,logger:pino({level:'silent'}),markOnlineOnConnect:false,syncFullHistory:false,shouldSyncHistoryMessage:()=>false});sock=current;
  let credentialWrites=Promise.resolve();
  current.ev.on('creds.update',()=>{if(own===generation)credentialWrites=credentialWrites.then(()=>saveCreds()).catch(()=>{state.error='登入狀態儲存失敗';pauseMode();});});
  current.ev.on('connection.update',async update=>{
   if(own!==generation)return;
   if(update.qr){const qr=await QRCode.toDataURL(update.qr);if(own===generation){state.qr=qr;state.connection='qr';}}
   if(update.connection==='open'){
    state.connection='connected';state.qr=null;state.error=null;state.lastDisconnectCode=null;attempts=0;console.log('[connection] open');
    state.account=String(current.user?.id||'').replace(/:\d+@/,'@');
   }
   if(update.connection==='close'){
    sock=null;state.qr=null;state.connection='disconnected';
    const code=update.lastDisconnect?.error?.output?.statusCode;state.lastDisconnectCode=code||null;
    console.log('[connection] close code='+String(code||'unknown')+' attempt='+attempts);
    await credentialWrites;if(own!==generation)return;
    if(code===DisconnectReason.connectionReplaced){wanted=false;pauseMode();state.error='此連結被另一個連線取代，請確認沒有另一個程序使用同一裝置登入。';return;}
    if(code===DisconnectReason.forbidden||code===DisconnectReason.badSession){wanted=false;pauseMode();state.error='WhatsApp 拒絕此裝置連線（'+code+'），請先核對手機已連結裝置。';return;}
    if(code===DisconnectReason.loggedOut){wanted=false;pauseMode();state.error='連結已失效，請在手機解除此裝置，再重設本機連線。';return;}
    if(wanted&&attempts<5){reconnectTimer=setTimeout(()=>connect(),Math.min(30000,1000*2**attempts++));state.error=code===DisconnectReason.restartRequired?'掃碼資料已收到，正在完成裝置登入…':'連線中斷（'+String(code||'未知')+'），正在重試';}
    else{pauseMode();state.error='裝置連線未完成（'+String(code||'未知')+'），請按重新連接；手機登入與此裝置連線是分開的';}
   }
  });
  current.ev.on('messages.upsert',async event=>{
   if(own!==generation||state.connection!=='connected')return;
   for(const message of event.messages||[]){const msg=incoming(message,event.type,since);if(!msg)continue;
    let phoneJid=msg.peer.endsWith('@s.whatsapp.net')?msg.peer:message.key?.remoteJidAlt;
    if(!phoneJid?.endsWith('@s.whatsapp.net')&&msg.peer.endsWith('@lid')){try{phoneJid=await current.signalRepository.lidMapping.getPNForLID(msg.peer)}catch{}}
    if(typeof phoneJid==='string'&&/^\d+(?::\d+)?@s\.whatsapp\.net$/.test(phoneJid))msg.phone=phoneJid.split('@')[0].split(':')[0];
    if(msg.media){try{const stream=await downloadMediaMessage(message,'stream',{});let length=0;const chunks=[];for await(const chunk of stream){length+=chunk.length;if(length>5*1024*1024){stream.destroy();throw Error('too large')}chunks.push(chunk)}const raw=Buffer.concat(chunks);msg.attachment={mime:raw[0]===0x89?'image/png':'image/jpeg',data:raw.toString('base64')}}catch{msg.text='圖片未能讀取，請 CS 跟進';}}
    if(own===generation&&state.connection==='connected')inbox.accept(state.account,msg);
   }
  });
 }catch{sock=null;state.connection='disconnected';state.error='連線初始化失敗';}
}
function disconnect(persist=true){wanted=false;generation++;clearTimeout(reconnectTimer);sock?.end(undefined);sock=null;state.connection='disconnected';state.qr=null;if(persist)pauseMode();else inbox.mode='paused';}
const app=express();app.disable('x-powered-by');
app.use((req,res,next)=>{
 if(!['127.0.0.1:'+port,'localhost:'+port].includes(req.headers.host))return res.status(403).json({error:'Invalid host'});
 res.set({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY'});next();
});
app.use(express.json({limit:'16kb'}));
app.get('/',(_req,res)=>res.redirect(backend+'/admin#whatsapp'));
app.get('/admin.js',(_req,res)=>res.sendFile(path.join(root,'admin.js')));
app.use('/api',(req,res,next)=>{
 const origin=req.headers.origin;
 if(origin&&!['http://127.0.0.1:'+port,'http://localhost:'+port].includes(origin))return res.status(403).json({error:'Invalid origin'});
 let secret;try{secret=fs.readFileSync(path.join(root,'../runtime/whatsapp-service.key'),'utf8').trim();}catch{return res.status(503).json({error:'Backend not ready'});}
 const key=Buffer.from(req.get('X-Backend-Key')||'');const expected=Buffer.from(secret);
 if(key.length!==expected.length||!timingSafeEqual(key,expected))return res.status(401).json({error:'Unauthorized'});next();
});
app.get('/api/status',(req,res)=>{const messages=[...(state.account?inbox.list(state.account):[]),...(req.query.test==='1'?inbox.list('simulation'):[])].sort((a,b)=>b.created-a.created);res.json({...state,mode:inbox.mode,messages})});
app.post('/api/connect',(_req,res)=>{if(!sock&&state.connection!=='connecting'){clearTimeout(reconnectTimer);attempts=0;}void connect();res.json({ok:true});});
app.post('/api/disconnect',(_req,res)=>{disconnect();res.json({ok:true});});
app.post('/api/reset-connection',(_req,res)=>{disconnect();const auth=path.join(runtime,'auth');if(fs.existsSync(auth))fs.renameSync(auth,path.join(runtime,'auth-archive-'+Date.now()));state.account='';state.error=null;res.json({ok:true});});
app.post('/api/mode',(req,res)=>{if(!['paused','draft','auto'].includes(req.body.mode))return res.status(400).json({error:'模式無效'});setMode(req.body.mode);res.json({ok:true});});
app.post('/api/contact-scope',(req,res)=>{const {account,peer,paused}=req.body;if(typeof account!=='string'||!/^\d+@(s\.whatsapp\.net|lid)$/.test(peer))return res.status(400).json({error:'Invalid peer'});inbox.pause(account,peer,paused===true);res.json({ok:true})});
app.post('/api/contact',(req,res)=>{
 const row=inbox.get(req.body.id);if(!row)return res.status(404).json({error:'找不到對話'});
 inbox.pause(row.account,row.peer,req.body.paused===true);res.json({ok:true});
});
app.post('/api/approve',async(req,res)=>{try{await inbox.deliver(req.body.id);res.json({ok:true});}catch(e){res.status(409).json({error:e.message});}});
app.post('/api/retry',(req,res)=>{
 const row=inbox.get(req.body.id);if(!row||!['failed','paused'].includes(row.status))return res.status(409).json({error:'此訊息不可重試'});
 if(row.account!=='simulation'&&inbox.mode==='paused')return res.status(409).json({error:'請先啟用草稿模式'});
 inbox.tail=inbox.tail.then(()=>inbox.generate(row.id,row.account==='simulation')).catch(()=>{});res.json({ok:true});
});
inbox.db.exec('CREATE TABLE IF NOT EXISTS manual_sends(id TEXT PRIMARY KEY,account TEXT,peer TEXT,text TEXT,status TEXT)');
inbox.db.exec("UPDATE manual_sends SET status='uncertain' WHERE status='sending'");
const reviewDelivery=new ReviewDelivery(inbox.db,{connected:account=>inbox.connected(account),send:(peer,body)=>sock.sendMessage(peer,body)});
app.post('/api/notify',async(req,res)=>{try{res.json(await reviewDelivery.deliver(req.body))}catch(error){res.status(error.status||503).json({error:error.message})}});
app.post('/api/manual',async(req,res)=>{
 const {account,peer,text,request_id:id}=req.body;
 if(typeof id!=='string'||id.length>100||typeof text!=='string'||!text.trim()||text.length>3000||!/^\d+@(s\.whatsapp\.net|lid)$/.test(peer))return res.status(400).json({error:'Invalid reply'});
 const old=inbox.db.prepare('SELECT * FROM manual_sends WHERE id=?').get(id);
 if(old){if(old.account!==account||old.peer!==peer||old.text!==text)return res.status(409).json({error:'Duplicate key'});return res.json({status:old.status})}
 if(!inbox.connected(account))return res.status(409).json({error:'WhatsApp not connected'});
 inbox.pause(account,peer,true);inbox.db.prepare('INSERT INTO manual_sends VALUES (?,?,?,?,?)').run(id,account,peer,text,'sending');
 let status='sent';try{await sock.sendMessage(peer,{text})}catch{status='uncertain'}inbox.db.prepare('UPDATE manual_sends SET status=? WHERE id=?').run(status,id);res.json({status});
});
app.post('/api/simulate',(req,res)=>{
 const {text}=req.body;if(typeof text!=='string'||!text.trim()||text.length>1000)return res.status(400).json({error:'請輸入 1 至 1000 字'});
 const result=inbox.accept('simulation',{id:randomBytes(12).toString('hex'),peer:'test-user',text},{simulate:true});res.json(result);
});
app.use((err,_req,res,_next)=>res.status(400).json({error:'請求格式無效'}));
const server=app.listen(port,'127.0.0.1',()=>console.log(`Naturalia WhatsApp: http://127.0.0.1:${port} (${inbox.mode}; no WhatsApp connection until you click connect)`));
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>{disconnect(false);server.close(()=>process.exit(0));});
