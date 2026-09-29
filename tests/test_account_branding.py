import importlib.util
import unittest
from pathlib import Path
import numpy as np
spec=importlib.util.spec_from_file_location('branding',Path(__file__).resolve().parents[1]/'ops/face-quality/brand_account_overlay.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class BrandingTests(unittest.TestCase):
 def test_only_label_pixels_change_and_target_region_is_covered(self):
  rng=np.random.default_rng(17);frame=rng.integers(0,255,(1248,704,3),dtype=np.uint8)
  font,rects=m.label_geometry([[100,900,250,30]],704,1248,'@jobutopaki6');out=m.paint(frame,rects[0],font,'@jobutopaki6');x,y,w,h=rects[0]
  outside=np.ones(frame.shape[:2],bool);outside[y:y+h,x:x+w]=False
  self.assertTrue(np.array_equal(frame[outside],out[outside]));self.assertFalse(np.array_equal(frame[900:930,100:350],out[900:930,100:350]))
 def test_bad_or_mismatched_track_fails_closed(self):
  t={'frames':1,'fps':24,'width':704,'height':1248,'boxes':[[1,900,250,30]]}
  self.assertEqual(len(m.validate_track(t,704,1248,1,24)),1)
  with self.assertRaises(ValueError):m.validate_track(t,704,1248,2,24)
  with self.assertRaises(ValueError):m.validate_track({**t,'boxes':[[0,0,700,1200]]},704,1248,1,24)
 def test_label_size_is_stable_and_handle_cannot_inject_commands(self):
  _,r=m.label_geometry([[100,900,250,30],[110,899,248,32]],704,1248,'@jobutopaki6');self.assertEqual(r[0][2:],r[1][2:])
  with self.assertRaises(ValueError):m.label_geometry([[1,1,20,20]],704,1248,'@bad;command')
 def test_short_ocr_outliers_do_not_enlarge_label(self):
  boxes=[[100+i,900,280,43] for i in range(20)];boxes[8]=[108,860,280,122]
  _,rects=m.label_geometry(boxes,704,1248,'@jobutopaki6')
  self.assertTrue(all(r[3]<80 for r in rects));self.assertEqual(rects[8][1],rects[7][1])
 def test_branded_check_still_detects_old_account(self):
  from unittest.mock import patch
  sp=importlib.util.spec_from_file_location('account_detector',Path(__file__).resolve().parents[1]/'ops/face-quality/detect_account_overlay.py');d=importlib.util.module_from_spec(sp);sp.loader.exec_module(d)
  d.EXACT_ACCOUNT_ONLY=True
  with patch.object(d,'text_lines',return_value=[(100,900,250,30,'@jobutopaki6'),(100,800,250,30,'@artgentokyo')]):
   found=d.matching_lines(np.zeros((1248,704,3),dtype=np.uint8),'ArtGenTokyo')
  self.assertEqual(len(found),1);self.assertEqual(found[0][4],'@artgentokyo')
