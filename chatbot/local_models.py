"""Local CPU inference. Models are downloaded by setup_models.py, never by a request."""
import hashlib,json,os,threading
from pathlib import Path
BASE=Path(__file__).resolve().parent
RUNTIME=BASE/'runtime'
EMBED_MODEL='sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
class LocalModels:
 def __init__(self):
  self.encoder=None;self.lock=threading.Lock()
  if (RUNTIME/'models/embedding').exists():
   from sentence_transformers import SentenceTransformer
   self.encoder=SentenceTransformer(str(RUNTIME/'models/embedding'),device='cpu',local_files_only=True)
 def encode(self,texts):
  with self.lock:return self.encoder.encode(texts,normalize_embeddings=True,show_progress_bar=False).tolist()
 def index(self,texts):
  digest=hashlib.sha256(json.dumps([EMBED_MODEL,texts],ensure_ascii=False).encode()).hexdigest()
  path=RUNTIME/'vectors.json'
  if path.exists():
   old=json.loads(path.read_text())
   if old.get('digest')==digest:return old['vectors']
  vectors=self.encode(texts);RUNTIME.mkdir(exist_ok=True)
  temp=path.with_suffix('.tmp');temp.write_text(json.dumps({'digest':digest,'vectors':vectors}));temp.replace(path)
  return vectors
