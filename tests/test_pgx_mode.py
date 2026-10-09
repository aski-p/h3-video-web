import ast
import math
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pgx_mode import PgxMode, ModeError


class ModeTests(unittest.TestCase):
    def make(self):
        with patch.dict(os.environ, {'H3_PGX_MODE_ENABLED':'1'}):
            c=PgxMode()
        self.states={'video':'active','qwen':'inactive'}
        self.calls=[]
        def ctl(verb, mode):
            self.calls.append((verb,mode))
            if verb=='is-active': return self.states[mode]
            self.states[mode]='active' if verb=='start' else 'inactive'
        c._ctl=ctl
        c.healthy=lambda mode:self.states[mode]=='active'
        c._wait=lambda mode:None
        return c

    def test_unconfigured_no_service_commands(self):
        with patch.dict(os.environ, {'H3_PGX_MODE_ENABLED':'0'}): c=PgxMode()
        self.assertFalse(c.status()['configured'])
        with self.assertRaises(ModeError): c.request('qwen')

    def test_busy_does_not_stop_jobs(self):
        c=self.make()
        with self.assertRaises(ModeError): c.request('qwen',busy=True)
        self.assertFalse(any(v=='stop' for v,m in self.calls))

    def test_busy_http_probe_preserves_video_admission_not_readiness(self):
        c=self.make();c.healthy=lambda mode:False
        status=c.status()
        self.assertEqual(status['mode'],'loading')
        self.assertEqual(status['selected_mode'],'video')
        self.assertFalse(status['ready'])
        self.assertTrue(c.video_allowed())
        self.assertEqual(c.request('video',busy=True)['mode'],'loading')
        self.assertFalse(any(v in ('stop','start') for v,m in self.calls))

    def test_unhealthy_qwen_never_admits_video(self):
        c=self.make();self.states={'video':'inactive','qwen':'active'}
        c.healthy=lambda mode:False
        self.assertEqual(c.status()['selected_mode'],'qwen')
        self.assertFalse(c.video_allowed())

    def test_uncertain_service_states_fail_closed(self):
        for states in ({'video':'active','qwen':'active'},
                       {'video':'activating','qwen':'inactive'},
                       {'video':'active','qwen':'deactivating'},
                       {'video':'inactive','qwen':'inactive'}):
            c=self.make();self.states=states
            self.assertFalse(c.video_allowed())
        c._ctl=lambda *args: (_ for _ in ()).throw(ModeError('unavailable'))
        self.assertFalse(c.video_allowed())

    def test_video_admission_does_not_make_http_probe(self):
        c=self.make()
        c.healthy=lambda mode: (_ for _ in ()).throw(AssertionError('HTTP probe'))
        self.assertTrue(c.video_allowed())

    def test_loading_switch_failure_restores_selected_video(self):
        c=self.make();c.healthy=lambda mode:False
        def wait(mode):
            if mode=='qwen':raise ModeError('not ready')
        c._wait=wait;c.switching=True;c._switch('qwen','video')
        self.assertTrue(c.video_allowed())
        self.assertIn(('start','video'),self.calls)

    def test_actual_admission_queues_busy_video_and_rejects_qwen(self):
        c=self.make();c.healthy=lambda mode:False
        source=(Path(__file__).resolve().parents[1]/'server.py').read_text()
        tree=ast.parse(source)
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='admit_generation_job')
        scope=dict(PGX_MODE=c,GENERATION_UNUSED=None,
                   GENERATE_ADMISSION_LOCK=threading.RLock(),LOCK=threading.RLock(),
                   QUEUE_LOCK=threading.RLock(),QUEUE_RESERVATIONS={'pgx':0},
                   JOBS={},QUEUE=[],MAX_PENDING_JOBS=5,
                   queued_jobs_for_target=lambda *args:0)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'admission','exec'),scope)
        job={'id':'isolated-test','cfg':{'worker_target':'pgx'}}
        self.assertEqual(scope['admit_generation_job'](job)[0],'accepted')
        self.assertIs(scope['JOBS'][job['id']],job)
        self.assertEqual(scope['QUEUE_RESERVATIONS']['pgx'],1)
        self.states={'video':'inactive','qwen':'active'}
        blocked={'id':'blocked-test','cfg':{'worker_target':'pgx'}}
        self.assertEqual(scope['admit_generation_job'](blocked)[0],'pgx_mode')
        self.assertNotIn(blocked['id'],scope['JOBS'])
        self.assertEqual(scope['QUEUE_RESERVATIONS']['pgx'],1)
        worker=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='ensure_comfyui')
        self.assertIn('ready_within_deadline()',ast.unparse(worker))

    def test_invalid_mode_and_duplicate(self):
        c=self.make()
        with self.assertRaises(ValueError):c.request('qwen; reboot')
        c.switching=True
        with self.assertRaises(ModeError):c.request('video')
        self.assertFalse(c.video_allowed())

    def test_stop_before_start_both_directions(self):
        c=self.make();c.switching=True;c._switch('qwen','video')
        self.assertEqual(c.status()['mode'],'qwen')
        self.assertFalse(c.video_allowed())
        self.assertLess(self.calls.index(('stop','video')),self.calls.index(('start','qwen')))
        c.switching=True;c._switch('video','qwen')
        self.assertTrue(c.video_allowed())

    def test_timeout_restores_previous(self):
        c=self.make()
        def wait(mode):
            if mode=='qwen':raise ModeError('not ready')
        c._wait=wait;c.switching=True;c._switch('qwen','video')
        self.assertEqual(c.status()['mode'],'video');self.assertIn('not ready',c.error)

    def test_failed_stop_never_starts_qwen(self):
        c=self.make();original=c._ctl
        def ctl(verb,mode):
            if verb=='stop' and mode=='video':raise ModeError('denied')
            return original(verb,mode)
        c._ctl=ctl;c.switching=True;c._switch('qwen','video')
        self.assertNotIn(('start','qwen'),self.calls)

    def test_qwen_status_exposes_exact_flash_route_and_context(self):
        c=self.make();self.states={'video':'inactive','qwen':'active'}
        status=c.status()
        self.assertEqual(status['mode'],'qwen')
        self.assertEqual(status['model'],'Qwen3.8-Flash-Next-EXL3')
        self.assertEqual(status['base_url'],'http://127.0.0.1:8899/v1')
        self.assertEqual(status['context_length'],262144)

    def test_duration_keeps_long_i2v_single_take(self):
        source=Path(__file__).resolve().parents[1]/'server.py'
        tree=ast.parse(source.read_text())
        functions={'normalize_generation_seconds','normalize_worker_strategy','snap_len','segment_frame_plan','worker_segment_frame_plan'}
        nodes=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in functions]
        scope=dict(math=math,I2V_REFERENCE_MAX_SECONDS=15,MAX_SECONDS=60,RTX5080_MAX_SINGLE_SECONDS=15,RTX5080_SAFE_SEG_SECONDS=4,STRATEGY_SPLIT='split',STRATEGY_SINGLE='single')
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'duration','exec'),scope)
        for seconds in (5,8,10,15):
            self.assertEqual(scope['normalize_generation_seconds'](seconds,'i2v'),seconds)
            self.assertEqual(scope['normalize_worker_strategy']('rtx5080',seconds,'single',4),('single',4))
            self.assertEqual(len(scope['worker_segment_frame_plan']('rtx5080',seconds,4,'single')),1)
        with self.assertRaises(ValueError):scope['normalize_generation_seconds'](16,'i2v')

if __name__=='__main__':unittest.main()
