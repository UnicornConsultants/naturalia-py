"""Offline model evaluation; never invokes Kimi."""
import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from engine import Catalog
c=Catalog();c.prepare_vectors();assert c.vectors is not None
cases=[('我想找美白肌膚保養套裝','6897bb8a55a99e00012ceb95'),('我想找肌膚保養套裝','6897bb8a55a99e00012ceb95'),('想搵提亮膚色嘅護膚套裝','6897bb8a55a99e00012ceb95'),('我想找美白肌膚保養套莊','6897bb8a55a99e00012ceb95'),('brightening skincare set','6897bb8a55a99e00012ceb95')]
results=[]
for query,target in cases:
 start=time.time();found=c.search(query);ids=[p['id'] for p in found];results.append({'query':query,'expected':target,'pass':target in ids,'ids':ids,'seconds':round(time.time()-start,3)})
for q in ['xyzabcdefgh','我想買電腦','火箭發射時間']:
 results.append({'query':q,'pass':not c.search(q),'expected':'no results'})
print(json.dumps(results,ensure_ascii=False,indent=2))
out=Path(__file__).resolve().parents[1]/'runtime/semantic-evaluation.json';out.write_text(json.dumps(results,ensure_ascii=False,indent=2));assert all(x['pass'] for x in results)
