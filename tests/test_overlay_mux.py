"""The restoration mux must keep every video frame when source audio is shorter."""
import ast,json,subprocess,tempfile,unittest
from pathlib import Path
class OverlayMuxTest(unittest.TestCase):
 def test_short_audio_does_not_trim_restored_video(self):
  script=Path(__file__).resolve().parents[1]/'ops/face-quality/remove_overlay.py'
  tree=ast.parse(script.read_text())
  mux=next(n for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute) and n.func.attr=='run' and isinstance(n.args[0],ast.List) and any(isinstance(x,ast.Constant) and x.value=='-movflags' for x in n.args[0].elts))
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);raw=root/'video.mp4';source=root/'source.m4a';output=root/'output.mp4'
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=s=64x64:r=24:d=2','-an','-c:v','libx264',str(raw)],check=True)
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=duration=1.5','-c:a','aac',str(source)],check=True)
   from types import SimpleNamespace
   command=eval(compile(ast.Expression(mux.args[0]),str(script),'eval'),{'raw':raw,'a':SimpleNamespace(source=source,output=output),'str':str})
   subprocess.run(command,check=True)
   streams=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(output)]))['streams']
   self.assertEqual(next(s['nb_frames'] for s in streams if s['codec_type']=='video'),'48')
   self.assertTrue(any(s['codec_type']=='audio' for s in streams))
