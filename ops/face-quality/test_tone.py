import unittest
import numpy as np
from trial import apply_tone,robust_delta
class ToneTests(unittest.TestCase):
 def test_outside_skin_unchanged(self):
  crop=np.full((32,32,3),[90,140,180],dtype=np.uint8);skin=np.zeros((32,32),np.float32);skin[8:24,8:24]=1
  result=apply_tone(crop,skin,[8,-6,6]);self.assertTrue(np.array_equal(result[skin==0],crop[skin==0]));self.assertTrue(np.any(result[skin>0]!=crop[skin>0]))
 def test_engine_float_crop_supported(self):
  crop=np.full((16,16,3),120.,dtype=np.float64);result=apply_tone(crop,np.zeros((16,16)),[1,2,3]);self.assertTrue(np.array_equal(result,crop.astype(np.uint8)))
 def test_outliers_cannot_drive_tone(self):
  self.assertEqual(robust_delta([[2,3,4],[2,3,4],[200,-200,200]]),[2.,3.,4.]);self.assertEqual(robust_delta([[100,100,-100]]),[8.,6.,-6.]);self.assertEqual(robust_delta([]),[0.,0.,0.])
if __name__=='__main__':unittest.main()
