import copy
import unittest
import original_video as v

class QualityGateTests(unittest.TestCase):
    def setUp(self):
        self.meta={'frames':120,'width':720,'height':1280,'fps':30}
        self.workflow={'timingVerified':True,'source':self.meta,'output':self.meta}
        self.stats={'model':'hyperswap_1b_256','expressionFactor':0,'appliedLabDelta':None,'rawSkinDeltas':[[0,0,0]]*120}
        self.report={'output':{'referenceSimilaritySamples':[.8]*5,'lowerBodyMAE':3}}
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
    def test_path_traversal_blocks(self):
        with self.assertRaises(ValueError):v.folder('../secret')
if __name__=='__main__':unittest.main()
