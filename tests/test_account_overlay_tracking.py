import unittest
import importlib.util
from pathlib import Path
from unittest.mock import patch
import numpy as np
spec=importlib.util.spec_from_file_location('overlay',Path(__file__).resolve().parents[1]/'ops/face-quality/detect_account_overlay.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class OverlayTrackingTests(unittest.TestCase):
 def test_bounds_include_omitted_edge_glyphs_but_never_large_regions(self):
  box=m.complete_label_bounds([[83,964,176,35]],704,1248)[0]
  self.assertLessEqual(box[0],68);self.assertGreaterEqual(box[0]+box[2],269)
  with self.assertRaisesRegex(ValueError,'account_overlay_location_uncertain'):
   m.complete_label_bounds([[0,0,400,40]],704,1248)
 def test_local_ocr_is_checked_against_frame_dimensions_and_exact_account(self):
  crop=np.zeros((70,320,3),np.uint8)
  with patch.object(m,'text_lines',return_value=[(40,20,210,25,'@artgentokyo')]):
   result=m.matching_lines(crop,'artgentokyo',adaptive=True,psm=7,full_size=(704,1248))
  self.assertEqual(len(result),1)
  with patch.object(m,'text_lines',return_value=[(40,20,210,25,'@unrelated')]):
   self.assertEqual(m.matching_lines(crop,'artgentokyo',adaptive=True,full_size=(704,1248)),[])
 def test_large_marks_and_ambiguous_pixel_tracking_remain_rejected(self):
  frame=np.zeros((1248,704,3),np.uint8)
  with patch.object(m,'text_lines',return_value=[(0,0,600,200,'@artgentokyo')]):
   with self.assertRaisesRegex(ValueError,'account_overlay_too_large'):m.matching_lines(frame,'artgentokyo',adaptive=True)
  rng=np.random.default_rng(7);frames=[rng.integers(0,255,(90,90),dtype=np.uint8) for _ in range(5)]
  boxes=[[10,10,20,20],None,None,None,[50,50,20,20]]
  self.assertEqual(m.bridge_text_gaps(frames,boxes),boxes)
