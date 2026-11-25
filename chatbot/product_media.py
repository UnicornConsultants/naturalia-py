"""Only archived public catalog images may be served; no arbitrary URL/path fetches."""
import json,re
from pathlib import Path

class ProductMedia:
 def __init__(self,root):
  root=Path(root);assets=(root/'assets').resolve();self.files={}
  downloads=json.loads((root/'indexes/assets-download.json').read_text())
  by_clip={a.get('image_clip_id'):a for a in downloads.values() if a.get('local_path')}
  for product in json.loads((root/'products.json').read_text()):
   item=by_clip.get((product.get('media') or [{}])[0].get('_id'));uid=product['id']
   if not item or not re.fullmatch(r'[a-f0-9]{24}',uid):continue
   path=(root/item['local_path']).resolve()
   if assets not in path.parents or not path.is_file() or path.stat().st_size>5*1024*1024:continue
   with path.open('rb') as f:head=f.read(12)
   mime='image/png' if head.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg' if head.startswith(b'\xff\xd8\xff') else None
   if mime:self.files[uid]=(path,mime)
 def get(self,uid):return self.files.get(uid)
