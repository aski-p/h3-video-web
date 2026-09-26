import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import original_video as v
import wardrobe_video as w
from production_recovery import plan

class RecoveryTests(unittest.TestCase):
 def test_fresh_job_does_not_gain_incomplete_recovery_state(self):
  with tempfile.TemporaryDirectory() as tmp:
   state={'status':'queued'};v.prepare_recovery(Path(tmp),state)
   self.assertNotIn('recovery',state)
 def test_quality_uses_local_repair_then_new_generation_without_lowering_gates(self):
  state={'wardrobe':'portrait_face','duration':15,'generationAttempt':0}
  first=plan(state,'wardrobe_account_overlay_review_required',100)
  self.assertEqual(first['strategy'],'tracked_overlay')
  state.update(recoveryHistory=[first],recoveryCount=1)
  second=plan(state,'wardrobe_account_overlay_review_required',120)
  self.assertEqual(second['strategy'],'regenerate_quality')
  self.assertEqual(second['generationAttempt'],1)
  self.assertEqual(state['duration'],15)
  state.update(generationAttempt=1,recoveryCount=2,recoveryHistory=[first,second])
  self.assertEqual(plan(state,'wardrobe_account_overlay_remains',140)['strategy'],'tracked_overlay')
 def test_transient_has_no_arbitrary_terminal_limit_but_backoff(self):
  state={'recoveryCount':10000}
  result=plan(state,'URLError',100)
  self.assertEqual(result['nextAttemptAt'],1000)
  self.assertEqual(result['strategy'],'resume_checkpoint')
 def test_hard_blocks_and_cancellation_do_not_retry(self):
  for code in ('content_blocked','source_integrity_failed','portrait_integrity_failed','hair_multiple_faces','cancelled'):
   self.assertIsNone(plan({},code))
  self.assertIsNone(plan({'sourceReleasedAt':1},'wardrobe_identity_failed'))
  self.assertIsNone(plan({'status':'cancelled'},'URLError'))
 def test_recovery_archives_once_and_preserves_binding(self):
  with tempfile.TemporaryDirectory() as tmp:
   f=Path(tmp);(f/'render').mkdir();(f/'render'/'output.mp4').write_bytes(b'evidence')
   state={'status':'error','sourceSha256':'abc','portraitSha256':'def','duration':15,'wardrobe':'portrait_face'}
   recovery=plan(state,'wardrobe_identity_failed',100)
   v.schedule_recovery(f,state,recovery)
   v.prepare_recovery(f,state)
   self.assertEqual((f/'recovery-1'/'output.mp4').read_bytes(),b'evidence')
   (f/'render').mkdir();(f/'render'/'output.mp4').write_bytes(b'new')
   v.prepare_recovery(f,v.read(f/'state.json'))
   self.assertEqual((f/'render'/'output.mp4').read_bytes(),b'new')
   saved=v.read(f/'state.json')
   self.assertEqual((saved['sourceSha256'],saved['portraitSha256'],saved['duration']),('abc','def',15))
 def test_checkpoint_refuses_modified_video_before_any_child(self):
  with tempfile.TemporaryDirectory() as tmp:
   f=Path(tmp);work=f/'render/wardrobe';work.mkdir(parents=True)
   state={'sourceSha256':'abc','portraitSha256':'def','duration':15,'wardrobe':'portrait_face'}
   v.save(f/'state.json',state);(f/'render/output.mp4').write_bytes(b'verified')
   w.save_post_checkpoint(f,{}, {},{},704,1248)
   (work/'post-face.mp4').write_bytes(b'changed')
   with self.assertRaisesRegex(ValueError,'post_checkpoint_binding_mismatch'):
    w.finish_post(f,f,{},'portrait_face',None,None)

if __name__=='__main__':unittest.main()

class WorkerRecoveryTests(unittest.TestCase):
 def test_worker_quality_failure_becomes_queued_without_changing_duration(self):
  with tempfile.TemporaryDirectory() as tmp:
   f=Path(tmp)/'job';f.mkdir();config=Path(tmp)/'config';config.mkdir()
   portrait=f/'portrait.jpg';portrait.write_bytes(b'portrait')
   manifest=Path(tmp)/'manifest.json';v.save(manifest,{'assets':[{'sha256':'source'}]})
   v.save(config/'workflow.json',{'nasRoot':tmp,'engine':tmp})
   v.save(f/'candidate.json',{'manifest':'manifest.json','duration':15})
   v.save(f/'state.json',{'id':'job','status':'queued','policy':w.POLICY,'wardrobe':'portrait_face','portraitSha256':v.sha(portrait),'sourceSha256':'source','duration':15})
   (f/'render').mkdir();(f/'render/source.mp4').write_bytes(b'source')
   with patch.object(v,'CONFIG',config),patch.object(w,'process',side_effect=ValueError('wardrobe_identity_failed')):
    v.process(f,Path(tmp))
   result=v.read(f/'state.json')
   self.assertEqual(result['status'],'queued');self.assertEqual(result['duration'],15)
   self.assertEqual(result['recovery']['strategy'],'regenerate_quality')

class SubmissionRecoveryTests(unittest.TestCase):
 def test_lost_acknowledgement_reattaches_prompt_without_duplicate(self):
  import urllib.error
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);record=root/'generation.json';output=root/'result.mp4';output.write_bytes(b'output')
   graph={'14':{'inputs':{'filename_prefix':'test'}}};calls=[]
   def initial(path,data=None):
    calls.append(path)
    if path=='/prompt':raise urllib.error.URLError('lost acknowledgement')
    raise AssertionError(path)
   with patch.object(w,'api',side_effect=initial),patch.object(w,'_sampler_progress_config',return_value=None):
    with self.assertRaises(urllib.error.URLError):w.render(graph,'14',record,lambda:None)
   state=json.loads(record.read_text());self.assertTrue(state['submissionPending'])
   def resume(path,data=None):
    calls.append(path)
    if path=='/queue':return {'queue_running':[[0,'same-prompt',graph,{'client_id':state['client']}]],'queue_pending':[]}
    if path=='/history':return {}
    if path=='/history/same-prompt':return {'same-prompt':{'status':{'status_str':'success'},'outputs':{'14':{'images':[{'type':'output','filename':'result.mp4'}]}}}}
    raise AssertionError(path)
   with patch.object(w,'OUTPUT',root),patch.object(w,'api',side_effect=resume),patch.object(w,'_sampler_progress_config',return_value=None):
    self.assertEqual(w.render(graph,'14',record,lambda:None),output)
   self.assertEqual(calls.count('/prompt'),1)
