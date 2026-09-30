import hashlib
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import studio_audio

class StudioAudioTest(unittest.TestCase):
    def test_script_validation(self):
        self.assertEqual(studio_audio.validate(studio_audio.POLICY, '오늘은 쉬어 갈게요.', 15), '오늘은 쉬어 갈게요.')
        for text in ['Hello', '오늘 3번', '가'*49]:
            with self.assertRaises(ValueError):studio_audio.validate(studio_audio.POLICY,text,15)
        with self.assertRaises(ValueError):studio_audio.validate('unknown','안녕.',15)

    def test_silent_output_removes_native_audio_preserves_frames(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,'test.mp4')
            subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=size=64x64:rate=24:duration=2','-f','lavfi','-i','sine=frequency=2000:duration=2','-c:v','libx264','-c:a','aac',path],check=True)
            def frames():return subprocess.check_output(['ffmpeg','-v','error','-i',path,'-map','0:v:0','-f','hash','-hash','sha256','-'])
            before=frames()
            self.assertIsNone(studio_audio.apply(path,{'seconds':2}))
            receipt=studio_audio.apply(path,{'audio_policy':studio_audio.POLICY,'dialogue_ko':'','seconds':2})
            self.assertEqual(before,frames())
            self.assertEqual(receipt['mode'],'silent')
            self.assertTrue(receipt['generated_audio_removed'])

    def test_failed_tts_keeps_source_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path=os.path.join(folder,'source.mp4')
            with open(path,'wb') as f:f.write(b'original')
            with patch('studio_audio._duration',return_value=15),patch('studio_audio._speak',side_effect=RuntimeError('offline')):
                with self.assertRaises(RuntimeError):studio_audio.apply(path,{'audio_policy':studio_audio.POLICY,'dialogue_ko':'안녕하세요.','seconds':15})
            with open(path,'rb') as f:self.assertEqual(f.read(),b'original')

if __name__=='__main__':unittest.main()
