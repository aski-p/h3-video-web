import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
import wardrobe_video as w
import original_video as v
from importlib.util import module_from_spec,spec_from_file_location
REPO=Path(__file__).resolve().parents[1]
mask_spec=spec_from_file_location('hair_mask',REPO/'ops/wardrobe-h3/build_hair_mask.py')
hair_mask=module_from_spec(mask_spec);mask_spec.loader.exec_module(hair_mask)
class WardrobeTests(unittest.TestCase):
 def test_only_explicit_single_choice(self):
  self.assertEqual(w.normalize(None),'original')
  for x in [['dress','casual'],'mix','unknown',{},True]:
   with self.assertRaises(ValueError):w.normalize(x)
 def test_approved_h3_graph_and_all_outfits(self):
  for choice in w.CHOICES.keys()-{'portrait_hair','portrait_face'}:
   g=w.motion_graph(REPO,'face.jpg','motion.mp4',choice,720,1280,107,'test')
   self.assertEqual(g['1']['inputs']['unet_name'],w.MODEL)
   self.assertEqual(g['8']['inputs']['steps'],20)
   self.assertEqual(g['5']['class_type'],'MiniMaxH3ReferenceToVideo')
   self.assertEqual(g['5']['inputs']['ref_images.ref_image_1'],['15',0])
   self.assertEqual(g['5']['inputs']['ref_videos.ref_video_1'],['17',0])
   self.assertIn(w.CHOICES[choice],g['5']['inputs']['prompt'])
   self.assertFalse(any('Lora' in n['class_type'] or 'Wan' in n['class_type'] or 'QwenImage' in n['class_type'] for n in g.values()))
 def test_hair_graph_masks_source_and_preserves_audio(self):
  g=w.hair_graph(REPO,'face.jpg','motion.mp4','mask.mp4',704,1248,'test')
  self.assertEqual(g['1']['inputs']['unet_name'],w.MODEL)
  self.assertEqual(g['8']['inputs']['steps'],20)
  self.assertNotIn('ref_videos.ref_video_1',g['5']['inputs'])
  self.assertIn('ONLY the masked head and hair region',g['5']['inputs']['prompt'])
  self.assertEqual(g['10']['inputs']['latent_image'],['28',0])
  self.assertEqual(g['28']['inputs']['audio_mode'],'preserve source audio')
  self.assertEqual(g['20']['inputs']['file'],'mask.mp4')
  self.assertEqual(w.hair_size(720,1280),(704,1248))
  self.assertFalse(any('Lora' in n['class_type'] or 'Wan' in n['class_type'] or 'QwenImage' in n['class_type'] for n in g.values()))
 def test_face_only_graph_preserves_source_hair(self):
  g=w.face_graph(REPO,'face.jpg','motion.mp4','face-mask.mp4',704,1248,'test')
  self.assertEqual(g['8']['inputs']['steps'],20)
  self.assertEqual(g['20']['inputs']['file'],'face-mask.mp4')
  self.assertIn('Keep the source hairstyle',g['5']['inputs']['prompt'])
  self.assertNotIn('hair color must disappear',g['5']['inputs']['prompt'])
  face_spec=spec_from_file_location('face_mask',REPO/'ops/wardrobe-h3/build_face_mask.py')
  with patch.dict('sys.modules',{'build_hair_mask':hair_mask}):
   face_mask=module_from_spec(face_spec);face_spec.loader.exec_module(face_mask)
  self.assertGreater(face_mask.face_mask(704,1248,(200,200,110,140)).max(),250)
  self.assertEqual(face_mask.face_mask(704,1248,(200,200,110,140))[600,300],0)
 def test_hair_tracking_holds_when_face_or_mask_is_uncertain(self):
  self.assertTrue(hair_mask.duplicate_detection((197,126,298,298),(290,29,271,271)))
  self.assertTrue(hair_mask.duplicate_detection((495,336,93,93),(566,380,70,70)))
  self.assertFalse(hair_mask.duplicate_detection((100,100,100,100),(190,100,100,100)))
  box=(100,200,80,100)
  smoothed,missing=hair_mask.tracked_boxes([box]*5+[None]+[box]*5)
  self.assertEqual((len(smoothed),missing),(11,1))
  with self.assertRaisesRegex(ValueError,'hair_tracking_incomplete'):
   hair_mask.tracked_boxes([None]*8+[box]*3)
  with self.assertRaisesRegex(ValueError,'hair_tracking_gap'):
   hair_mask.tracked_boxes([box]*5+[None]*5+[box]*40)
  with self.assertRaisesRegex(ValueError,'hair_head_out_of_frame'):
   hair_mask.head_mask(384,672,(340,200,80,100))
 def test_hair_mask_repair_preserves_request_binding_and_failed_evidence(self):
  jid='orig_'+'a'*32
  with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)):
   folder=Path(tmp)/jid;folder.mkdir();(folder/'render').mkdir()
   (folder/'render'/'source.mp4').write_bytes(b'old-source')
   (folder/'hair-mask.log').write_text('ValueError: hair_multiple_faces')
   v.save(folder/'candidate.json',{'start':0,'duration':14,'sha256':'source'})
   v.save(folder/'state.json',{'id':jid,'status':'error','policy':w.POLICY,
           'wardrobe':'portrait_hair','start':0,'duration':14,
           'error':'원본 기반 품질 검사 미통과 · quality_pipeline_failed'})
   result=v.retry_wardrobe(jid,{'start':0,'duration':5.5})['job']
   self.assertEqual((result['status'],result['duration'],result['requestedDuration']),('queued',5.5,14))
   self.assertEqual((folder/'repair-attempt-1'/'source.mp4').read_bytes(),b'old-source')
   self.assertFalse((folder/'render').exists())
   self.assertEqual(v.retry_wardrobe(jid)['job']['status'],'queued')
 def test_visual_hair_retry_archives_completed_output(self):
  jid='orig_'+'b'*32
  with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)):
   folder=Path(tmp)/jid;folder.mkdir();(folder/'render').mkdir()
   (folder/'render'/'output.mp4').write_bytes(b'previous-output')
   v.save(folder/'state.json',{'id':jid,'status':'done','policy':w.POLICY,
           'wardrobe':'portrait_hair','verification':{'hairVisualReview':'required'}})
   result=v.retry_visual_hair(jid,'hair_reference_not_applied')['job']
   self.assertEqual(result['status'],'queued')
   self.assertIsNone(result['verification'])
   self.assertEqual((folder/'repair-attempt-1'/'output.mp4').read_bytes(),b'previous-output')
   self.assertEqual(result['repairHistory'][0]['action'],'regenerate_hair_reference')
 def test_face_only_retry_archives_failure_and_keeps_original_binding(self):
  jid='orig_'+'c'*32
  with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)):
   folder=Path(tmp)/jid;folder.mkdir();(folder/'render').mkdir()
   (folder/'render'/'output.mp4').write_bytes(b'failed-render')
   v.save(folder/'state.json',{'id':jid,'status':'error','policy':w.POLICY,'wardrobe':'portrait_hair'})
   result=v.retry_face_only(jid)['job']
   self.assertEqual((result['status'],result['wardrobe']),('queued','portrait_face'))
   self.assertEqual((folder/'repair-attempt-1'/'output.mp4').read_bytes(),b'failed-render')
   self.assertEqual(v.read(folder/'state.json')['requestedWardrobe'],'portrait_hair')
   self.assertEqual(v.retry_face_only(jid)['job']['status'],'queued')
 def test_face_segment_retry_keeps_source_and_original_request(self):
  jid='orig_'+'d'*32
  with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)):
   folder=Path(tmp)/jid;folder.mkdir();(folder/'render').mkdir()
   (folder/'render'/'output.mp4').write_bytes(b'failed-render')
   v.save(folder/'candidate.json',{'start':7.5,'duration':5,'sha256':'source'})
   v.save(folder/'state.json',{'id':jid,'status':'error','policy':w.POLICY,
          'wardrobe':'portrait_face','sourceSha256':'source','start':7.5,'duration':5,
          'error':'원본 기반 품질 검사 미통과 · wardrobe_face_coverage_failed',
          'repairHistory':[{'action':'regenerate_face_only'}]})
   with patch.object(v,'catalog',return_value=[{'sha256':'source','start':0,'duration':15}]):
    result=v.retry_face_segment(jid,8.5,5)['job']
   self.assertEqual((result['status'],result['start'],result['requestedStart']),('queued',8.5,7.5))
   self.assertEqual((folder/'repair-attempt-2'/'output.mp4').read_bytes(),b'failed-render')
   self.assertEqual(v.read(folder/'candidate.json')['start'],8.5)
 def test_frame_grid_covers_length_without_loop_or_speed_change(self):
  for frames,fps in [(120,30),(156,30),(178,30),(307,30),(450,30),(449,29.97)]:
   p=w.frame_plan({'frames':frames,'fps':fps})
   self.assertEqual((p['generatedFrames']-5)%17,0)
   self.assertLessEqual(p['frames']/24,frames/fps+1e-6)
   self.assertLess(frames/fps-p['frames']/24,1/24+1e-6)
   self.assertLess(p['generatedFrames']-p['frames'],17)
   self.assertGreaterEqual(p['generatedFrames'],p['frames'])
  with self.assertRaises(ValueError):w.frame_plan({'frames':480,'fps':30})
 def test_resolution_matches_approved_sample(self):
  self.assertEqual(w.size(720,1280),(720,1280));self.assertEqual(w.size(1080,1920),(720,1280))
 def test_prompt_requires_one_visible_face(self):
  prompt=w.motion_graph(REPO,'face.jpg','motion.mp4','dress',720,1280,107,'test')['5']['inputs']['prompt']
  self.assertIn('Exactly one adult woman',prompt);self.assertIn('full face remains visible',prompt)
 def test_receipt_requires_all_frame_faces_and_approved_profile(self):
  report={'output':{'referenceSimilaritySamples':[.8]*5}};meta={'width':720,'height':1280,'fps':24,'frames':96};stats={'rawSkinDeltas':[0]*96,'model':'hyperswap_1b_256','expressionFactor':0}
  receipt=w.verify(report,stats,meta,meta,'yoga')
  self.assertEqual(receipt['engine'],'minimax_h3_ref2va');self.assertFalse(receipt['nativeMotionPreserved']);self.assertFalse(receipt['publishApproved'])
  hair=w.verify(report,stats,meta,meta,'portrait_hair')
  self.assertTrue(hair['hairReferenceRequested']);self.assertEqual(hair['hairVisualReview'],'required')
  for bad in [{**meta,'frames':95},{**meta,'fps':30}]:
   with self.assertRaises(ValueError):w.verify(report,stats,meta,bad,'yoga')
  with self.assertRaises(ValueError):w.verify(report,{**stats,'rawSkinDeltas':[]},meta,meta,'yoga')
  with self.assertRaises(ValueError):w.verify(report,{**stats,'appliedLabDelta':[1,0,0]},meta,meta,'yoga')
 def test_legacy_request_rejected_before_generation(self):
  with self.assertRaisesRegex(ValueError,'policy_update'):v.submit({'wardrobe':'yoga','wardrobePolicy':'wardrobe-motion-v1-20260922'})
 def test_hair_route_rejects_untrained_short_interval(self):
  c={'sha256':'abc','sourceUrl':'https://instagram.com/reel/POST/','duration':4,'start':0}
  with patch.object(v,'catalog',return_value=[c]):
   d={'requestId':'11111111-1111-1111-1111-111111111111:1','sourceSha256':'abc','portrait':'data:image/jpeg;base64,/9j/','wardrobe':'portrait_hair','wardrobePolicy':w.POLICY}
   with self.assertRaisesRegex(ValueError,'five_seconds'):v.submit(d)
 def test_reusing_request_with_different_outfit_is_rejected(self):
  c={'sha256':'abc','sourceUrl':'https://instagram.com/reel/POST/','duration':10,'start':0}
  with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)),patch.object(v,'catalog',return_value=[c]),patch.object(v,'healthy',return_value=True):
   d={'requestId':'11111111-1111-1111-1111-111111111111:1','sourceSha256':'abc','portrait':'data:image/jpeg;base64,/9j/','wardrobe':'dress','wardrobePolicy':w.POLICY}
   self.assertEqual(v.submit(d)['policy'],w.POLICY)
   with self.assertRaisesRegex(ValueError,'conflict'):v.submit({**d,'wardrobe':'casual'})
 def test_finished_prompt_resumes_without_duplicate_submission(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);output=root/'output';output.mkdir();video=output/'clip.mp4';video.write_bytes(b'video')
   record=root/'generation.json';graph={'14':{'inputs':{'filename_prefix':'clip'}}}
   record.write_text(json.dumps({'status':'submitted','client':'client','promptId':'prompt'}))
   record.with_suffix('.graph.json').write_text(json.dumps(graph))
   history={'prompt':{'status':{'status_str':'success'},'outputs':{'14':{'images':[{'type':'output','filename':'clip.mp4','subfolder':''}]}}}}
   with patch.object(w,'OUTPUT',output),patch.object(w,'api',return_value=history) as api:
    self.assertEqual(w.render(graph,'14',record,lambda:None),video)
    self.assertEqual(record.read_text() and json.loads(record.read_text())['status'],'done')
    self.assertFalse(any(call.args[0]=='/prompt' for call in api.call_args_list))
 def test_review_output_stages_on_filesystems_without_symlinks(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);source=root/'face.mp4';source.write_bytes(b'generated-video')
   review=root/'review'
   with patch.object(Path,'symlink_to',side_effect=OSError(95,'Operation not supported')):
    staged=w.stage_review_output(source,review)
   self.assertEqual(staged.read_bytes(),source.read_bytes())
   self.assertFalse(staged.is_symlink())
   self.assertEqual(w.stage_review_output(source,review),staged)
 def test_sampler_receipt_reports_actual_steps_and_sampler_eta(self):
  rows=[{'__REALTIME_TIMESTAMP':'2000000','MESSAGE':' 50%|█████| 10/20 [6:00:00<6:08:35, 2211.56s/it]'}]
  progress=w._sampler_log(rows,20,1)
  self.assertEqual(progress['step'],10)
  self.assertEqual(progress['percent'],50)
  self.assertEqual(progress['remainingSeconds'],6*3600+8*60+35)
 def test_sampler_receipt_does_not_reuse_finished_previous_prompt(self):
  rows=[{'__REALTIME_TIMESTAMP':'2000000','MESSAGE':'100%|██████████| 20/20 [5:27:45<00:00, 983s/it]'},
        {'__REALTIME_TIMESTAMP':'3000000','MESSAGE':'Prompt executed in 05:35:02'},
        {'__REALTIME_TIMESTAMP':'4000000','MESSAGE':'  5%|▌| 1/20 [00:35:00<11:00:00, 2100s/it]'}]
  self.assertEqual(w._sampler_log(rows,20,1)['step'],1)
  self.assertIsNone(w._sampler_log(rows[:2],20,1))
 def test_websocket_receipt_tracks_only_own_sampler_and_estimates_remaining_steps(self):
  event={'type':'progress_state','data':{'prompt_id':'prompt-a','nodes':{'8':{'value':4,'max':20}}}}
  first=w._progress_event(event,'prompt-a','8',20,100)
  self.assertEqual((first['step'],first['percent']),(4,20))
  self.assertIsNone(first['remainingSeconds'])
  event['data']['nodes']['8']['value']=5
  second=w._progress_event(event,'prompt-a','8',20,140,first)
  self.assertEqual(second['remainingSeconds'],600)
  self.assertIsNone(w._progress_event(event,'prompt-other','8',20,140,first))
  self.assertIsNone(w._progress_event(event,'prompt-a','9',20,140,first))
 def test_sampler_progress_config_uses_sampler_node_not_scheduler_node(self):
  graph={'8':{'class_type':'BasicScheduler','inputs':{'steps':20}},
         '10':{'class_type':'SamplerCustomAdvanced','inputs':{'sigmas':['8',0]}}}
  self.assertEqual(w._sampler_progress_config(graph),('10',20))
  self.assertIsNone(w._sampler_progress_config({'8':graph['8']}))
if __name__=='__main__':unittest.main()
