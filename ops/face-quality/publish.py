"""Package a reviewed winner and the prior result without replacing source files."""
import argparse,json,shutil,subprocess,html
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--folder',type=Path,required=True);p.add_argument('--source',type=Path,required=True);p.add_argument('--winner',required=True);a=p.parse_args()
if not a.winner.replace('_','').isalnum():p.error('invalid winner name')
r=a.folder;winner=r/(a.winner+'.mp4')
if not winner.is_file():p.error('winner missing')
shutil.copyfile(a.source,r/'source.mp4');shutil.copyfile(winner,r/'best.mp4')
subprocess.run(['ffmpeg','-v','error','-i',str(r/'source.mp4'),'-i',str(r/'previous.mp4'),'-i',str(r/'best.mp4'),'-filter_complex',"[0:v]scale=360:640,drawtext=text='ORIGINAL':x=12:y=20:fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6[a];[1:v]scale=360:640,drawtext=text='PREVIOUS':x=12:y=20:fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6[b];[2:v]scale=360:640,drawtext=text='SELECTED':x=12:y=20:fontcolor=white:fontsize=22:box=1:boxcolor=black@0.6[c];[a][b][c]hstack=inputs=3[v]",'-map','[v]','-map','0:a?','-c:v','libx264','-crf','18','-pix_fmt','yuv420p','-c:a','copy','-movflags','+faststart','-y',str(r/'comparison.mp4')],check=True)
metrics=json.loads((r/'metrics.json').read_text());rows=''.join('<tr><td>'+html.escape(name)+'</td><td>'+format(v['referenceSimilarityMean'],'.3f')+'</td><td>'+str(v['frames'])+'</td></tr>' for name,v in metrics.items())
page='''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>얼굴 교체 개선 비교</title><style>body{background:#111319;color:#eff1f5;font:16px system-ui;max-width:1100px;margin:30px auto;padding:16px}video{width:100%;border-radius:12px}a{color:#aecbff}td,th{padding:10px;text-align:left;border-bottom:1px solid #343840}p{line-height:1.7;color:#c2c7d0}</style><h1>원본 · 이전 결과 · 개선 결과</h1><p>왼쪽부터 원본 / 이전 GHOST / 선택한 개선본. 원본 자체는 AI 콘텐츠입니다.<br>동일한 4초 원본, 고정 얼굴, 120프레임으로 비교했습니다.</p><video controls playsinline preload="metadata" src="comparison.mp4"></video><p>선택 모델: WINNER<br><a href="best.mp4">개선본 단독 영상</a> · <a href="source.mp4">원본</a> · <a href="previous.mp4">이전 결과</a> · <a href="face-comparison.jpg">얼굴 확대 비교</a></p><h2>측정 결과</h2><p>얼굴 유사도는 표본 5개 프레임의 임베딩 코사인 유사도이며, 성공률이나 정확도 퍼센트가 아닙니다. 수치와 시각 검토를 함께 사용했습니다.</p><table><tr><th>결과</th><th>참조 얼굴 유사도</th><th>프레임</th></tr>ROWS</table></html>'''.replace('WINNER',html.escape(a.winner)).replace('ROWS',rows)
(r/'comparison.html').write_text(page)
print(r/'comparison.mp4')
