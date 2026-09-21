import copy
import unittest
import tempfile,json
from pathlib import Path
from unittest.mock import patch
import original_video as v

class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.meta={'frames':120,'width':720,'height':1280,'fps':30}
        self.workflow={'timingVerified':True,'source':self.meta,'output':self.meta}
        self.stats={'model':'hyperswap_1b_256','expressionFactor':0,'appliedLabDelta':None,'rawSkinDeltas':[[0,0,0]]*120}
        expression={'eyeAspectRatio':.3,'mouthAspectRatio':.1}
        self.report={'source':{'expressionSamples':[dict(expression) for _ in range(5)]},'output':{'referenceSimilaritySamples':[.8]*5,'lowerBodyMAE':3,'expressionSamples':[dict(expression) for _ in range(5)]}}
    def test_success_is_not_publication_approval(self):
        self.assertFalse(v.quality_gate(self.report,self.workflow,self.stats)['publishApproved'])
    def test_missing_face_frames_blocks(self):
        self.stats['rawSkinDeltas'].pop()
        with self.assertRaisesRegex(ValueError,'coverage'):v.quality_gate(self.report,self.workflow,self.stats)
    def test_weak_identity_blocks(self):
        self.report['output']['referenceSimilaritySamples'][0]=.3
        with self.assertRaisesRegex(ValueError,'identity'):v.quality_gate(self.report,self.workflow,self.stats)
    def test_changed_model_blocks(self):
        self.stats['model']='ghost_1_256'
        with self.assertRaisesRegex(ValueError,'profile'):v.quality_gate(self.report,self.workflow,self.stats)
    def test_timing_mismatch_blocks(self):
        self.workflow['output']={**self.meta,'frames':119}
        with self.assertRaisesRegex(ValueError,'timing'):v.quality_gate(self.report,self.workflow,self.stats)
    def test_expression_drift_blocks(self):
        self.report['output']['expressionSamples'][1]['eyeAspectRatio']=.4
        with self.assertRaisesRegex(ValueError,'expression'):v.quality_gate(self.report,self.workflow,self.stats)
    def test_path_traversal_blocks(self):
        with self.assertRaises(ValueError):v.folder('../secret')
class NoveltyTests(unittest.TestCase):
    def test_used_hash_and_post_survive_card_deletion(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)):
            f=Path(tmp)/('orig_'+'a'*32);f.mkdir()
            (f/'state.json').write_text(json.dumps({'status':'done','sourceSha256':'old'}))
            (f/'candidate.json').write_text(json.dumps({'sourceUrl':'https://instagram.com/user/reel/POST/'}))
            self.assertTrue(v.used_source({'sha256':'old','sourceUrl':'other'}))
            self.assertTrue(v.used_source({'sha256':'new','sourceUrl':'https://instagram.com/reel/POST/?x=1'}))
            self.assertFalse(v.used_source({'sha256':'new','sourceUrl':'https://instagram.com/reel/NEW/'}))
    def test_segment_cannot_exceed_reviewed_bounds(self):
        c={'start':2,'duration':12}
        self.assertEqual(v.requested_segment(c,{'start':2,'duration':10})['duration'],10)
        for d in [{'start':1,'duration':10},{'start':2,'duration':13},{'duration':float('nan')}]:
            with self.assertRaises(ValueError):v.requested_segment(c,d)
    def test_same_request_idempotent_but_other_request_cannot_reuse(self):
        c={'sha256':'abc','sourceUrl':'https://instagram.com/reel/POST/','duration':10,'start':0}
        with tempfile.TemporaryDirectory() as tmp,patch.object(v,'ROOT',Path(tmp)),patch.object(v,'catalog',return_value=[c]),patch.object(v,'healthy',return_value=True):
            d={'requestId':'11111111-1111-1111-1111-111111111111:1','sourceSha256':'abc','portrait':'data:image/jpeg;base64,/9j/'}
            first=v.submit(d);self.assertEqual(v.submit(d)['id'],first['id'])
            with self.assertRaisesRegex(ValueError,'already_used'):v.submit({**d,'requestId':'22222222-2222-2222-2222-222222222222:1'})
            with self.assertRaisesRegex(ValueError,'conflict'):v.submit({**d,'duration':8})
    def test_long_clip_needs_more_than_five_samples(self):
        base=QualityGateTests();base.setUp();base.meta['frames']=300;base.stats['rawSkinDeltas']=[[0,0,0]]*300
        with self.assertRaisesRegex(ValueError,'identity'):v.quality_gate(base.report,base.workflow,base.stats)
        for key in ('source','output'):base.report[key]['expressionSamples']*=2
        base.report['output']['referenceSimilaritySamples']*=2
        self.assertTrue(v.quality_gate(base.report,base.workflow,base.stats)['expressionVerified'])

if __name__=='__main__':unittest.main()
