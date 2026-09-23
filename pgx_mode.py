"""Exclusive PGX services; no browser-supplied commands or unit names."""
import json
import os
import re
import subprocess
import threading
import time
import urllib.request


class ModeError(RuntimeError):
    pass


class PgxMode:
    def __init__(self, run=subprocess.run, healthy=None):
        self.enabled = os.environ.get('H3_PGX_MODE_ENABLED') == '1'
        self.units = {'video': 'comfyui-minimax-h3.service', 'qwen': 'qwen38-exl3.service'}
        self.qwen_model = os.environ.get('H3_QWEN_MODEL_NAME', 'Qwen3.8-Flash-Next-EXL3')
        try:
            self.qwen_context = int(os.environ.get('H3_QWEN_CONTEXT_LENGTH', '262144'))
        except ValueError:
            self.qwen_context = 262144
        self.qwen_base_url = os.environ.get('H3_QWEN_BASE_URL', 'http://127.0.0.1:8899/v1').rstrip('/')
        self.run = run
        self.healthy = healthy or self._healthy
        self.lock = threading.RLock()
        self.switching = False
        self.target = None
        self.error = None

    def _ctl(self, verb, mode):
        result = self.run(['/usr/bin/systemctl', verb, self.units[mode]],
                          capture_output=True, text=True, timeout=90)
        if verb == 'is-active':
            value = result.stdout.strip()
            if value not in ('active', 'inactive', 'failed', 'activating', 'deactivating'):
                raise ModeError('PGX 서비스 상태를 확인할 수 없습니다.')
            return value
        if result.returncode:
            raise ModeError('PGX 서비스 제어 실패 · 서버 서비스/권한을 확인해 주세요.')

    def _healthy(self, mode):
        url = f'{self.qwen_base_url}/models' if mode == 'qwen' else 'http://127.0.0.1:8188/system_stats'
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status != 200:
                    return False
                if mode != 'qwen':
                    return True
                payload = json.loads(r.read().decode('utf-8'))
                return self.qwen_model in {str(item.get('id')) for item in payload.get('data', [])}
        except Exception:
            return False

    def status(self):
        with self.lock:
            if not self.enabled:
                return {'configured': False, 'mode': 'unconfigured', 'switching': False,
                        'message': 'PGX 모드 전환 서버 설치가 필요합니다.'}
            if self.switching:
                return {'configured': True, 'mode': 'switching', 'target': self.target,
                        'switching': True, 'message': '모델 전환 중 · 준비 상태 확인 중'}
            try:
                video, qwen = self._ctl('is-active', 'video'), self._ctl('is-active', 'qwen')
                mode = 'video' if video == 'active' and qwen in ('inactive','failed') else 'qwen' if qwen == 'active' and video in ('inactive','failed') else 'unknown'
                if mode != 'unknown' and not self.healthy(mode):
                    mode = 'loading'
                result = {'configured': True, 'mode': mode, 'switching': False,
                          'message': self.error or {'video':'영상 생성 모드 준비', 'qwen':'Qwen 3.8 Flash Next EXL3 · 262K 컨텍스트 준비', 'loading':'모델 로딩 중', 'unknown':'서버 상태 확인 필요'}.get(mode)}
                if mode == 'qwen':
                    result.update(model=self.qwen_model, context_length=self.qwen_context,
                                  base_url=self.qwen_base_url)
                return result
            except Exception:
                return {'configured': True, 'mode':'unknown', 'switching':False, 'message':'PGX 서비스 상태를 확인할 수 없습니다.'}

    def video_allowed(self):
        return not self.enabled or self.status()['mode'] == 'video'

    def request(self, target, busy=False):
        with self.lock:
            if not self.enabled:
                raise ModeError('PGX 모드 전환 서버 설치가 필요합니다.')
            if target not in self.units:
                raise ValueError('지원하지 않는 모드입니다.')
            if self.switching:
                raise ModeError('이미 모델을 전환하고 있습니다.')
            previous = self.status()['mode']
            if target == previous:
                return self.status()
            if busy:
                raise ModeError('PGX 생성·대기 작업이 있습니다. 작업을 완료하거나 취소한 뒤 전환해 주세요.')
            self.switching, self.target, self.error = True, target, None
            threading.Thread(target=self._switch, args=(target, previous), daemon=True).start()
            return self.status()

    def _wait(self, target):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            if self.healthy(target):
                return
            time.sleep(3)
        raise ModeError('모델 준비 시간 초과 · 기존 모드 복구를 시도합니다.')

    def _switch(self, target, previous):
        other = 'video' if target == 'qwen' else 'qwen'
        try:
            self._ctl('stop', other)
            if self._ctl('is-active', other) not in ('inactive','failed') or self.healthy(other):
                raise ModeError('기존 모델이 종료되지 않아 전환을 중단했습니다.')
            self._ctl('start', target)
            self._wait(target)
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, ModeError) else '모델 전환 실패 · 서버 로그를 확인해 주세요.'
            try:
                self._ctl('stop', target)
                if previous in self.units and self._ctl('is-active', target) in ('inactive','failed') and not self.healthy(target):
                    self._ctl('start', previous)
                    self._wait(previous)
            except Exception:
                self.error += ' 기존 모드 자동 복구도 실패했습니다.'
        finally:
            with self.lock:
                self.switching = False


controller = PgxMode()
