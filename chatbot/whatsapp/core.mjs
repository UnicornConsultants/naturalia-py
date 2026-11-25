import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
export function incoming(message,type='notify',since=0){
 const key=message?.key||{},peer=key.remoteJid||'';
 if(type!=='notify'||key.fromMe||!key.id||!/^\d+(?::\d+)?@(s\.whatsapp\.net|lid)$/.test(peer))return null;
 const timestamp=Number(message.messageTimestamp||0);
 if(!timestamp||timestamp<since-5)return null;
 let body=message.message;
 for(let i=0;i<3;i++)body=body?.ephemeralMessage?.message||body?.viewOnceMessage?.message||body;
 const media=body?.imageMessage;
 if(media&&['image/jpeg','image/png'].includes(media.mimetype)&&Number(media.fileLength)>0&&Number(media.fileLength)<=5*1024*1024)return {peer,id:key.id,text:media.caption?.slice(0,1000)||'圖片',timestamp,media:true};
 const text=body?.conversation||body?.extendedTextMessage?.text;
 if(typeof text!=='string'||!text.trim()||text.length>1000)return null;
 return {peer,id:key.id,text:text.trim(),timestamp};
}
function publicProducts(result){return (result.products||[]).slice(0,3).filter(p=>String(p.url).startsWith('https://www.naturalia-group.com/'));}
export function productCaption(p){
 const code=/^[A-Z][A-Z0-9]{3}$/.test(String(p.choice))?p.choice:null;
 const buy=code?'買 '+code:'購買編號 '+p.id;
 return `${code?'商品 '+code+'｜':''}${p.title}\n${p.price}（${p.as_of} 存檔參考）\n\n有意購買／查看詳情：\n${p.url}\n\n以充值額購買：回覆「${buy}」\n買多件可回覆「${code||p.id} 2個」。選購後會先確認商品、數量及總額，確認後才扣款。`;
}
export function formatReply(result){
 const lines=[result.answer];
 if(result.warning)lines.push(result.warning);
 for(const p of publicProducts(result))lines.push(productCaption(p));
 for(const plan of result.topup_plans||[])for(const method of ['payme','alipay'])lines.push(`${plan.choice?'方案 '+plan.choice+'｜':''}${plan.name}：HK$${(plan.paid/100).toFixed(2)}，本金 HK$${(plan.principal/100).toFixed(2)} + 贈額 HK$${(plan.bonus/100).toFixed(2)}\n回覆：${plan.choice?'充值 '+plan.choice+' '+(method==='payme'?'PayMe':'Alipay'):'充值方案 '+plan.id+' '+method}`);
 if(result.quote&&!result.answer?.includes('回覆「確認」'))lines.push('確認扣款請回覆「確認」。改數量可回覆「買 2 個」，或回覆「取消購買」。');
 for(const source of result.sources||[])if(String(source.url).startsWith('https://www.naturalia-group.com/'))lines.push(`${source.title}\n${source.url}`);
 return lines.join('\n\n').slice(0,6000);
}
export async function sendRichReply(peer,result,{sendMessage,loadImage,canSend}){
 const check=()=>{if(!canSend())throw Error('Conversation paused or connection changed');};
 const products=publicProducts(result);
 check();await sendMessage(peer,{text:formatReply({...result,products:[]})});
 for(const product of products){
  check();let image=null;
  // A missing file can fall back to text. A failed send is uncertain and must never be retried here.
  try{image=await loadImage(product.id)}catch{}
  check();const caption=productCaption(product);
  await sendMessage(peer,image?{image:image.data,mimetype:image.mime,caption}:{text:caption+'\n\n圖片暫時未能載入，請按上方連結查看。'});
 }
}
export async function sendPaymentReply(peer,result,{sendMessage,loadQR,canSend}){
 const check=()=>{if(!canSend())throw Error('Conversation paused or connection changed');};
 const options=result.payment?[result.payment]:(result.topup_methods||[]).filter(p=>p.qr);
 // Load both known local QR images before sending instructions. Never substitute a different payee.
 const images=[];
 for(const p of options){if(!['payme','alipay'].includes(p.method))throw Error('Invalid payment method');const image=await loadQR(p.method);if(!image?.data?.length)throw Error('Payment QR unavailable');images.push({option:p,image});}
 check();await sendMessage(peer,{text:formatReply(result)});
 for(const {option:p,image} of images){
  check();if(!image?.data?.length)throw Error('Payment QR unavailable');
  const label=p.method==='payme'?'PayMe':'Alipay';
  await sendMessage(peer,{image:image.data,mimetype:image.mime,caption:label+' 收款 QR Code · HK$'+(p.amount/100).toFixed(2)+'\n請核對收款人及金額。付款後傳過數截圖，核款後才入帳。'});
 }
}
export class Inbox {
 constructor(path,{reply,send,connected,observe,presence}){
  this.typing=new Map();this.presence=presence;
  this.observe=observe;this.db=new DatabaseSync(path);this.reply=reply;this.send=send;this.connected=connected;this.mode='paused';this.tail=Promise.resolve();
  this.db.exec(`CREATE TABLE IF NOT EXISTS inbox (id TEXT PRIMARY KEY,account TEXT,peer TEXT,external_id TEXT,question TEXT,answer TEXT,status TEXT,error TEXT,created INTEGER);CREATE TABLE IF NOT EXISTS contacts(account TEXT,peer TEXT,paused INTEGER DEFAULT 0,PRIMARY KEY(account,peer));`);
  this.db.exec('CREATE TABLE IF NOT EXISTS incoming_contacts(id TEXT PRIMARY KEY,phone TEXT);CREATE TABLE IF NOT EXISTS incoming_attachments(id TEXT PRIMARY KEY,payload TEXT);CREATE TABLE IF NOT EXISTS outbound_payloads(id TEXT PRIMARY KEY,payload TEXT)');
  this.db.exec("UPDATE inbox SET status='uncertain',error='程序中斷，請核對手機送出記錄' WHERE status='sending'; UPDATE inbox SET status='failed',error='程序中斷，可手動重新產生草稿' WHERE status IN ('received','generating')");
 }
 get mode(){return this._mode;}
 set mode(value){this._mode=value;if(value!=='auto')this.stopAllTyping();}
 presenceUpdate(entry,type){Promise.resolve().then(()=>this.presence?.(entry.account,entry.peer,type)).catch(()=>{});}
 startTyping(id,account,peer){
  if(!this.presence||!this.connected(account))return;
  const key=JSON.stringify([account,peer]);let entry=this.typing.get(key);
  if(entry){entry.ids.add(id);return;}
  entry={account,peer,ids:new Set([id])};this.typing.set(key,entry);
  this.presenceUpdate(entry,'composing');
  entry.timer=setInterval(()=>{
   if(this.mode!=='auto'||this.paused(account,peer)||!this.connected(account)){this.stopTypingContact(account,peer);return;}
   this.presenceUpdate(entry,'composing');
  },5000);entry.timer.unref?.();
 }
 stopTypingContact(account,peer){const key=JSON.stringify([account,peer]),entry=this.typing.get(key);if(!entry)return;clearInterval(entry.timer);this.typing.delete(key);this.presenceUpdate(entry,'paused');}
 stopTyping(id){for(const entry of this.typing.values())if(entry.ids.delete(id)&&!entry.ids.size)this.stopTypingContact(entry.account,entry.peer);}
 stopAllTyping(){for(const entry of this.typing.values())this.stopTypingContact(entry.account,entry.peer);}
 paused(account,peer){return !!this.db.prepare('SELECT paused FROM contacts WHERE account=? AND peer=?').get(account,peer)?.paused;}
 pause(account,peer,paused){this.db.prepare('INSERT INTO contacts VALUES (?,?,?) ON CONFLICT(account,peer) DO UPDATE SET paused=excluded.paused').run(account,peer,paused?1:0);if(paused)this.stopTypingContact(account,peer);}
 list(account){return this.db.prepare('SELECT i.*,COALESCE(c.paused,0) AS contact_paused FROM inbox i LEFT JOIN contacts c ON c.account=i.account AND c.peer=i.peer WHERE i.account=? ORDER BY i.created DESC,i.rowid DESC LIMIT 100').all(account);}
 get(id){const row=this.db.prepare('SELECT * FROM inbox WHERE id=?').get(id);if(row){row.phone=this.db.prepare('SELECT phone FROM incoming_contacts WHERE id=?').get(id)?.phone||'';const a=this.db.prepare('SELECT payload FROM incoming_attachments WHERE id=?').get(id);if(a)row.attachment=JSON.parse(a.payload)}return row;}
 set(id,status,error=null,answer=null){this.db.prepare('UPDATE inbox SET status=?,error=?,answer=COALESCE(?,answer) WHERE id=?').run(status,error,answer,id);}
 accept(account,msg,{simulate=false}={}){
  const id=createHash('sha256').update(JSON.stringify([account,msg.peer,msg.id])).digest('hex');
  const paused=this.mode==='paused'||this.paused(account,msg.peer);
  const result=this.db.prepare('INSERT OR IGNORE INTO inbox VALUES (?,?,?,?,?,?,?,?,?)').run(id,account,msg.peer,msg.id,msg.text,null,paused&&!simulate?'paused':'received',null,Date.now());
  if(!result.changes)return {id,duplicate:true};
  if(msg.phone)this.db.prepare('INSERT OR REPLACE INTO incoming_contacts VALUES (?,?)').run(id,msg.phone);
  if(msg.attachment)this.db.prepare('INSERT OR REPLACE INTO incoming_attachments VALUES (?,?)').run(id,JSON.stringify(msg.attachment));
  const autoAtReceipt=this.mode==='auto';
  if(autoAtReceipt&&!paused&&!simulate)this.startTyping(id,account,msg.peer);
  this.tail=this.tail.then(async()=>{
   // Persist every inbound message in CS before generation, including failures and handover races.
   if(paused&&!simulate){if(this.observe)await this.observe(this.get(id));}
   else await this.generate(id,simulate,autoAtReceipt);
  }).catch(()=>this.set(id,'failed','CS 訊息同步失敗，請核對後重試')).finally(()=>this.stopTyping(id));
  return {id,duplicate:false};
 }
 async generate(id,simulate=false,allowAuto=false){
  const row=this.get(id);if(!row||!['received','failed','paused'].includes(row.status))return;
  if(!simulate&&this.observe){try{await this.observe(row)}catch{this.set(id,'failed','CS 訊息同步失敗，請核對後重試');return;}}
  if(!simulate&&(this.mode==='paused'||this.paused(row.account,row.peer))){this.set(id,'paused');return;}
  this.set(id,'generating');
  try{
   const result=await this.reply(row,simulate);const answer=formatReply(result);
   if(result.suppress_delivery){this.set(id,'paused');return;}
   this.db.prepare('INSERT OR REPLACE INTO outbound_payloads VALUES (?,?)').run(id,JSON.stringify(result));
   this.set(id,simulate?'test':'draft',null,answer);
   if(allowAuto&&!simulate&&this.mode==='auto'&&!this.paused(row.account,row.peer))await this.deliver(id);
  }catch{this.set(id,'failed','生成失敗；未送出訊息');}
 }
 async deliver(id){
  const row=this.get(id);
  if(!row||row.status!=='draft'||!row.answer)throw Error('只有草稿可以送出');
  if(!this.connected(row.account))throw Error('WhatsApp 尚未連線或帳戶不同');
  if(this.paused(row.account,row.peer))throw Error('此對話已交由人工接手');
  this.set(id,'sending');
  try{await this.send(row.peer,row.answer,JSON.parse(this.db.prepare('SELECT payload FROM outbound_payloads WHERE id=?').get(id)?.payload||'{}'),row.account);this.set(id,'sent');}
  catch{this.set(id,'uncertain','未能確認送出結果；請先核對 WhatsApp，避免重複發送');}
 }
 close(){this.stopAllTyping();this.db.close();}
}

export class ReviewDelivery {
 constructor(db,{connected,send}){
  this.db=db;this.connected=connected;this.send=send;
  db.exec("CREATE TABLE IF NOT EXISTS notification_sends(id TEXT PRIMARY KEY,account TEXT,peer TEXT,text TEXT,status TEXT); UPDATE notification_sends SET status='uncertain' WHERE status='sending'");
 }
 async deliver({request_id:id,account,peer,text}){
  const fail=(message,status)=>{const error=new Error(message);error.status=status;throw error};
  if(typeof id!=='string'||!id||id.length>100||typeof account!=='string'||!account||typeof text!=='string'||!text.trim()||text.length>3000||!/^\d+@(s\.whatsapp\.net|lid)$/.test(peer))fail('Invalid notification',400);
  const old=this.db.prepare('SELECT * FROM notification_sends WHERE id=?').get(id);
  if(old){if(old.account!==account||old.peer!==peer||old.text!==text)fail('Duplicate key',409);return {status:old.status};}
  if(!this.connected(account))fail('WhatsApp not connected',409);
  this.db.prepare('INSERT INTO notification_sends VALUES (?,?,?,?,?)').run(id,account,peer,text,'sending');
  let status='sent';try{await this.send(peer,{text})}catch{status='uncertain'}
  this.db.prepare('UPDATE notification_sends SET status=? WHERE id=?').run(status,id);return {status};
 }
}
