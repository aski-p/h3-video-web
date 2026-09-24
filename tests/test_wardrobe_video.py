import unittest,tempfile,json
from pathlib import Path
from unittest.mock import patch
import wardrobe_video as w
import original_video as v
REPO=Path(__file__).resolve().parents[1]
class WardrobeTests(unittest.TestCase):
 def test_only_explicit_single_choice(self):
  self.assertEqual(w.normalize(None),'original')
  for x in [['dress','casual'],'mix','unknown',{},True]:
   with self.assertRaises(ValueError):w.normalize(x)
 def test_approved_h3_graph_and_all_outfits(self):
  for choice in w.CHOICES:
   g=w.motion_graph(REPO,'face.jpg','motion.mp4',choice,720,1280,107,'test')
   self.assertEqual(g['1']['inputs']['unet_name'],w.MODEL)
   self.assertEqual(g['8']['inputs']['steps'],20)
   self.assertEqual(g['5']['class_type'],'MiniMaxH3ReferenceToVideo')
   self.assertEqual(g['5']['inputs']['ref_images.ref_image_1'],['15',0])
   self.assertEqual(g['5']['inputs']['ref_videos.ref_video_1'],['17',0])
   self.assertIn(w.CHOICES[choice],g['5']['inputs']['prompt'])
   self.assertFalse(any('Lora' in n['class_type'] or 'Wan' in n['class_type'] or 'QwenImage' in n['class_type'] for n in g.values()))
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
  for bad in [{**meta,'frames':95},{**meta,'fps':30}]:
   with self.assertRaises(ValueError):w.verify(report,stats,meta,bad,'yoga')
  with self.assertRaises(ValueError):w.verify(report,{**stats,'rawSkinDeltas':[]},meta,meta,'yoga')
  with self.assertRaises(ValueError):w.verify(report,{**stats,'appliedLabDelta':[1,0,0]},meta,meta,'yoga')
 def test_legacy_request_rejected_before_generation(self):
  with self.assertRaisesRegex(ValueError,'policy_update'):v.submit({'wardrobe':'yoga','wardrobePolicy':'wardrobe-motion-v1-20260922'})
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
