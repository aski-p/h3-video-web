import json,tempfile,unittest
from pathlib import Path
import wardrobe_video as w
class ReviewedOverlayTests(unittest.TestCase):
 def test_bound_multi_regions_and_invalid_evidence(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)/'review.json';cp={'outputSha256':'verified'}
   data={'outputSha256':'verified','frames':309,'width':704,'height':1248,'regions':[[440,30,204,45],[250,1044,200,50]]}
   p.write_text(json.dumps(data));self.assertEqual(w.reviewed_overlay_regions(p,cp,704,1248,309)['regions'],data['regions'])
   with self.assertRaisesRegex(ValueError,'binding_mismatch'):w.reviewed_overlay_regions(p,{'outputSha256':'other'},704,1248,309)
   with self.assertRaisesRegex(ValueError,'binding_mismatch'):w.reviewed_overlay_regions(p,cp,704,1248,300)
   for box in ([0,0,704,1248],[-1,0,20,20],[0,0,20.5,20]):
    data['regions']=[box];p.write_text(json.dumps(data))
    with self.assertRaisesRegex(ValueError,'regions_invalid'):w.reviewed_overlay_regions(p,cp,704,1248,309)
