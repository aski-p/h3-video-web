import ast
import math
import os
from pathlib import Path
import sys
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
