import unittest, tempfile,json
from pathlib import Path
from unittest.mock import patch
import wardrobe_video as w
import original_video as v
class WardrobeTests(unittest.TestCase):
 def test_only_explicit_single_choice(self):
  self.assertEqual(w.normalize(None),'original')
  for x in [['dress','casual'],'mix','unknown',{},True]:
   with self.assertRaises(ValueError):w.normalize(x)
 def test_new_outfits_have_distinct_anchor_prompts(self):
  prompts=[]
  for choice in ['bikini','swimsuit','yoga']:
   self.assertEqual(w.normalize(choice),choice)
   prompt=w.anchor_graph('adult.png',choice,432,768,'test')['5']['inputs']['prompt']
   self.assertIn(w.CHOICES[choice],prompt);self.assertIn('adult woman',prompt);prompts.append(prompt)
  self.assertEqual(len(set(prompts)),3)
 def test_chunks_cover_full_length_without_repeated_motion(self):
  for frames in [61,81,82,120,156,300,450,900]:
   parts=list(w.chunks(frames));self.assertEqual(sum(x['take'] for x in parts),frames)
   done=0
   for part in parts:
    self.assertEqual(part['offset'],done) # Comfy itself subtracts continuation overlap.
    self.assertLessEqual(part['length'],81);self.assertEqual(part['length']%4,1)
    self.assertGreaterEqual(part['length']-part['overlap'],part['take']);done+=part['take']
 def test_output_size_is_honest_preview_resolution(self):self.assertEqual(w.size(720,1280),(432,768))
 def test_receipt_cannot_claim_original_pixels_or_publish(self):
  report={'output':{'referenceSimilaritySamples':[.8]*5}};meta={'width':432,'height':768,'fps':30,'frames':120};stats={'rawSkinDeltas':[0]*120,'model':'hyperswap_1b_256','expressionFactor':0}
  receipt=w.verify(report,stats,meta,meta,'dress');self.assertFalse(receipt['originalPixelsPreserved']);self.assertFalse(receipt['publishApproved'])
  with self.assertRaises(ValueError):w.verify(report,stats,meta,{**meta,'frames':119},'dress')
  with self.assertRaises(ValueError):w.verify(report,{**stats,'rawSkinDeltas':[]},meta,meta,'dress')
 def test_reusing_request_with_different_outfit_is_rejected(self):
  c={'sha256':'abc','sourceUrl':'https://instagram.com/reel/POST/','duration':10,'start':0}
  with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)),patch.object(v,'catalog',return_value=[c]),patch.object(v,'healthy',return_value=True):
   d={'requestId':'11111111-1111-1111-1111-111111111111:1','sourceSha256':'abc','portrait':'data:image/jpeg;base64,/9j/','wardrobe':'dress'}
   self.assertEqual(v.submit(d)['policy'],w.POLICY)
   with self.assertRaisesRegex(ValueError,'conflict'):v.submit({**d,'wardrobe':'casual'})
 def test_continuation_is_trimmed_and_fps_retained(self):
  g=w.motion_graph(Path(__file__).resolve().parents[1],'face.png','source.mp4','last.png',432,768,24,{'length':81,'offset':81,'take':80,'overlap':1},'test')
  self.assertEqual(g['15']['inputs']['continue_motion'],['22',0]);self.assertEqual(g['18']['inputs']['batch_index'],1);self.assertEqual(g['19']['inputs']['fps'],24)
if __name__=='__main__':unittest.main()
