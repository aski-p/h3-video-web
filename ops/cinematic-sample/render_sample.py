"""One isolated, idempotent ComfyUI sample. Does not change production defaults."""
import sys,json,time,uuid,urllib.request,urllib.parse,pathlib,subprocess
sys.path.insert(0,'/home/aski/h3-web')
import server
ROOT=pathlib.Path('/home/aski/studio-previews/cinematic-20260930');ROOT.mkdir(parents=True,exist_ok=True)
M=json.loads(pathlib.Path(__file__).with_name('20260930-rain-editorial.json').read_text())
BASE='http://127.0.0.1:8188'
def get(path):return json.load(urllib.request.urlopen(BASE+path,timeout=30))
receipt=ROOT/'receipt.json'
if receipt.exists():
 r=json.loads(receipt.read_text());pid=r['promptId']
else:
 options={'realism':{'enabled':True,'strength':M['realismStrength']},'better_motion':{'enabled':True,'strength':M['betterMotionStrength']}}
 wf=server.build_workflow(M['prompt'],M['negative'],M['width'],M['height'],124,M['steps'],M['seed'],image_name=M['referenceImage'],prefix='cinematic_sample_20260930',lora_options=options,strict_loras=True)
 ref=wf['8']['inputs']['model'];wf['cinematic']={'class_type':'LoraLoaderModelOnly','inputs':{'model':ref,'lora_name':M['localFilename'],'strength_model':M['cinematicStrength']}}
 wf['8']['inputs']['model']=['cinematic',0];wf['9']['inputs']['model']=['cinematic',0]
 assert wf['15']['inputs']['image']==M['referenceImage']
 (ROOT/'workflow.json').write_text(json.dumps(wf,indent=2))
 req=urllib.request.Request(BASE+'/prompt',data=json.dumps({'prompt':wf,'client_id':'cinematic-sample-'+str(uuid.uuid4())}).encode(),headers={'Content-Type':'application/json'})
 ack=json.load(urllib.request.urlopen(req,timeout=60));pid=ack['prompt_id'];r={'promptId':pid,'status':'submitted','manifest':M,'submittedAt':time.time(),'appliedLoras':[{**n['inputs']} for n in wf.values() if n['class_type']=='LoraLoaderModelOnly']};receipt.write_text(json.dumps(r,indent=2));print(json.dumps({'promptId':pid,'status':'submitted'}),flush=True)
start=time.time()
while True:
 h=get('/history/'+pid).get(pid)
 if h:
  if h.get('status',{}).get('status_str')=='error':
   r['status']='failed';r['engineStatus']=h.get('status');receipt.write_text(json.dumps(r,indent=2));raise RuntimeError('ComfyUI sample failed; diagnostic saved privately')
  if h.get('status',{}).get('completed'):
   (ROOT/'history.json').write_text(json.dumps(h));outputs=h.get('outputs',{});files=[v for out in outputs.values() for group in out.values() if isinstance(group,list) for v in group if isinstance(v,dict) and str(v.get('filename','')).endswith('.mp4')]
   if not files:raise RuntimeError('Completed sample has no MP4')
   f=files[0];url=BASE+'/view?'+urllib.parse.urlencode({k:f[k] for k in ['filename','subfolder','type'] if k in f});raw=ROOT/'raw.mp4';urllib.request.urlretrieve(url,raw)
   out=ROOT/'rain-editorial-5s.mp4';subprocess.run(['ffmpeg','-v','error','-y','-i',str(raw),'-t','5','-c:v','libx264','-crf','16','-pix_fmt','yuv420p','-c:a','aac','-movflags','+faststart',str(out)],check=True)
   subprocess.run(['ffmpeg','-v','error','-xerror','-i',str(out),'-f','null','-'],check=True)
   r.update(status='completed',file=str(out),finishedAt=time.time(),visualReview='required');receipt.write_text(json.dumps(r,indent=2));print(json.dumps({'status':'completed','file':str(out)}),flush=True);break
 if time.time()-start>7200:raise TimeoutError('Sample still running; receipt retained; do not resubmit')
 time.sleep(5)
