"""Compare timing, outside-face pixels, and sampled reference-face similarity.
Similarity is only a relative diagnostic, never a pass/fail identity guarantee.
"""
import argparse,json,os,sys
from pathlib import Path
os.environ.setdefault('OMP_NUM_THREADS','1')
p=argparse.ArgumentParser();p.add_argument('--engine',type=Path,required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--portrait',type=Path,required=True);p.add_argument('--folder',type=Path,required=True);a=p.parse_args()
a.source=a.source.resolve();a.portrait=a.portrait.resolve();a.folder=a.folder.resolve();sys.path.insert(0,str(a.engine.resolve()));os.chdir(a.engine)
import cv2,numpy as np
from trial import configure_cpu
configure_cpu()
from facefusion.program import create_program
from facefusion.args import apply_args
from facefusion import state_manager
from facefusion.face_analyser import get_many_faces
from facefusion.face_selector import sort_and_filter_faces
args=vars(create_program().parse_args(['headless-run','-s',str(a.portrait),'-t',str(a.source),'-o','/tmp/unused-face-quality.mp4','--face-detector-model','retinaface','--face-landmarker-model','2dfan4','--face-selector-gender','female','--execution-providers','cpu','--execution-thread-count','4']))
apply_args(args,state_manager.init_item)
source_face=sort_and_filter_faces(get_many_faces([cv2.imread(str(a.portrait))]))[0]
def read(path):
 cap=cv2.VideoCapture(str(path));fps=cap.get(cv2.CAP_PROP_FPS);out=[]
 while True:
  ok,f=cap.read()
  if not ok:break
  out.append(f)
 cap.release();return out,fps
original,fps=read(a.source);indices=[0,30,60,90,len(original)-1];report={};columns=[]
paths=[a.source]+[x for x in sorted(a.folder.glob('*.mp4')) if x.stem not in ('comparison','best')]
for path in paths:
 frames,rate=read(path)
 if len(frames)!=len(original) or rate!=fps or frames[0].shape!=original[0].shape:raise ValueError('timing or dimensions differ: '+path.name)
 scores=[];tiles=[];expressions=[]
 for idx in indices:
  faces=sort_and_filter_faces(get_many_faces([frames[idx]]))
  # The trial contains one visible adult female subject. Review all sampled crops.
  if not faces: scores.append(None);expressions.append(None);tiles.append(np.zeros((280,240,3),np.uint8));continue
  f=faces[0];points=f.landmark_set['68'];ear=[]
  for offset in (36,42):
   eye=points[offset:offset+6];ear.append(float((np.linalg.norm(eye[1]-eye[5])+np.linalg.norm(eye[2]-eye[4]))/(2*max(np.linalg.norm(eye[0]-eye[3]),1e-6))))
  expressions.append({'eyeAspectRatio':float(np.mean(ear)),'mouthAspectRatio':float(np.linalg.norm(points[62]-points[66])/max(np.linalg.norm(points[60]-points[64]),1e-6))})
  scores.append(float(np.dot(f.embedding_norm,source_face.embedding_norm)))
  x1,y1,x2,y2=np.rint(f.bounding_box).astype(int);pad=30;h,w=frames[idx].shape[:2]
  crop=frames[idx][max(0,y1-pad):min(h,y2+pad),max(0,x1-pad):min(w,x2+pad)]
  tile=cv2.resize(crop,(240,280));cv2.putText(tile,f'{idx/30:.1f}s / {scores[-1]:.2f}',(8,22),cv2.FONT_HERSHEY_SIMPLEX,.5,(255,255,255),1);tiles.append(tile)
 header=np.zeros((48,240,3),np.uint8);cv2.putText(header,path.stem.replace('hyperswap_','hyper_')[:25],(5,28),cv2.FONT_HERSHEY_SIMPLEX,.48,(255,255,255),1);columns.append(np.concatenate([header,*tiles],axis=0))
 outside=[float(np.abs(x[650:].astype(float)-y[650:].astype(float)).mean()) for x,y in zip(original,frames)]
 stats=path.with_suffix('.stats.json');data=json.loads(stats.read_text()) if stats.exists() else {}
 report[path.stem]={'frames':len(frames),'fps':rate,'size':[frames[0].shape[1],frames[0].shape[0]],'expressionSamples':expressions,'referenceSimilaritySamples':scores,'referenceSimilarityMean':float(np.mean([s for s in scores if s is not None])),'lowerBodyMAE':float(np.mean(outside)),'skinDeltaSamples':len(data.get('rawSkinDeltas',[])),'recommendedLabDelta':data.get('recommendedLabDelta'),'appliedLabDelta':data.get('appliedLabDelta')}
 print(path.stem,json.dumps(report[path.stem]),flush=True)
cv2.imwrite(str(a.folder/'face-comparison.jpg'),np.concatenate(columns,axis=1))
(a.folder/'metrics.json').write_text(json.dumps(report,indent=2))
