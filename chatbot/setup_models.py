from pathlib import Path
from huggingface_hub import snapshot_download
from local_models import EMBED_MODEL,RUNTIME
for name,repo in [('embedding',EMBED_MODEL)]:
 print('Downloading',repo,flush=True)
 snapshot_download(repo_id=repo,local_dir=RUNTIME/'models'/name,allow_patterns=['*.json','*.safetensors','*.txt','*.model','1_Pooling/*'],ignore_patterns=['onnx/*','openvino/*'])
print('Local models ready.')
