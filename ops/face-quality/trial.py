"""Reproducible local face-only trial; does not alter the production engine.
Run using FaceFusion's Python environment. Engine checks and filters remain active.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import threading

os.environ.setdefault('OMP_NUM_THREADS','1')

def configure_cpu():
    import cv2
    import onnxruntime as ort
    from facefusion import inference_manager
    cv2.setNumThreads(1)
    original=ort.InferenceSession
    def bounded_session(*args,**kwargs):
        options=ort.SessionOptions();options.intra_op_num_threads=2;options.inter_op_num_threads=1
        options.add_session_config_entry('session.intra_op.allow_spinning','0')
        options.add_session_config_entry('session.inter_op.allow_spinning','0')
        kwargs['sess_options']=options
        return original(*args,**kwargs)
    inference_manager.InferenceSession=bounded_session

def robust_delta(values):
    import numpy as np
    if not values:return [0.,0.,0.]
    return np.clip(np.median(np.asarray(values),axis=0),[-8,-6,-6],[8,6,6]).tolist()

def apply_tone(crop,skin,delta,strength=.7):
    import cv2
    import numpy as np
    # Constant shot-level correction avoids independently changing color each frame.
    # Only skin inside the eventual face mask is changed; eyes/lips are excluded.
    crop=np.rint(crop).clip(0,255).astype(np.uint8)
    lab=cv2.cvtColor(crop,cv2.COLOR_BGR2LAB).astype(np.float32)
    corrected=cv2.cvtColor(np.clip(lab+np.asarray(delta,dtype=np.float32)*strength,0,255).astype(np.uint8),cv2.COLOR_LAB2BGR)
    alpha=np.clip(skin,0,1)[...,None]
    result=np.rint(crop.astype(np.float32)*(1-alpha)+corrected.astype(np.float32)*alpha).clip(0,255).astype(np.uint8)
    result[skin<=0]=crop[skin<=0]
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--engine',type=Path,required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--portrait',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--model',choices=['ghost_1_256','hyperswap_1a_256','hyperswap_1b_256','hyperswap_1c_256'],required=True);p.add_argument('--tone-from',type=Path);p.add_argument('--expression',type=int,default=0);a=p.parse_args()
    for path in (a.source,a.portrait):
        if not path.is_file():p.error('input missing')
    a.output=a.output.resolve();a.source=a.source.resolve();a.portrait=a.portrait.resolve();a.engine=a.engine.resolve();a.output.parent.mkdir(parents=True,exist_ok=True)
    sys.path.insert(0,str(a.engine));os.chdir(a.engine)
    import cv2
    import numpy as np
    from facefusion import conda,core
    configure_cpu()
    from facefusion.processors.modules.face_swapper import core as swapper
    from facefusion.face_masker import create_region_mask
    tone=robust_delta(json.loads(a.tone_from.read_text())['rawSkinDeltas']) if a.tone_from else None
    original=swapper.paste_back;stats=[];lock=threading.Lock()
    def paste(frame,crop,mask,affine):
        target=cv2.warpAffine(frame,affine,(crop.shape[1],crop.shape[0]),borderMode=cv2.BORDER_REPLICATE)
        skin=np.minimum(create_region_mask(target,['skin']),create_region_mask(crop,['skin']))*mask
        valid=skin>.8
        if np.count_nonzero(valid)>100:
            target_lab=cv2.cvtColor(target,cv2.COLOR_BGR2LAB).astype(float)
            crop_lab=cv2.cvtColor(np.rint(crop).clip(0,255).astype(np.uint8),cv2.COLOR_BGR2LAB).astype(float)
            delta=np.median(target_lab[valid]-crop_lab[valid],axis=0).tolist()
            with lock:stats.append(delta)
        if tone is not None:crop=apply_tone(crop,skin,tone)
        return original(frame,crop,mask,affine)
    swapper.paste_back=paste
    processors=['face_swapper']+(['expression_restorer'] if a.expression else [])
    sys.argv=['facefusion.py','headless-run','-s',str(a.portrait),'-t',str(a.source),'-o',str(a.output),'--processors',*processors,
      '--face-swapper-model',a.model,'--face-swapper-weight','0.5','--face-swapper-pixel-boost','512x512',
      '--face-selector-mode','reference','--reference-frame-number','30','--reference-face-position','0','--reference-face-distance','0.3','--face-selector-gender','female',
      '--face-mask-types','box','occlusion','region','--face-occluder-model','xseg_1','--face-parser-model','bisenet_resnet_34','--face-mask-blur','0.3',
      '--face-detector-model','retinaface','--face-landmarker-model','2dfan4','--output-video-scale','1','--output-video-fps','30','--output-video-quality','95','--output-video-preset','fast',
      '--execution-providers','cpu','--execution-thread-count','4','--temp-path',str(a.output.parent/'temp'/a.output.stem),'--jobs-path',str(a.output.parent/'jobs'/a.output.stem),'--log-level','info']
    if a.expression:sys.argv+=['--expression-restorer-model','live_portrait','--expression-restorer-factor',str(a.expression)]
    conda.setup()
    try:core.cli()
    finally:
        a.output.with_suffix('.stats.json').write_text(json.dumps({'model':a.model,'rawSkinDeltas':stats,'recommendedLabDelta':robust_delta(stats),'appliedLabDelta':tone,'expressionFactor':a.expression,'command':sys.argv},indent=2))
if __name__=='__main__':main()
