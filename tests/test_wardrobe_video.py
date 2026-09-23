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
if __name__=='__main__':unittest.main()
