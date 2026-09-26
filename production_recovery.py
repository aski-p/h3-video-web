"""Cause-specific recovery; immutable evidence, unchanged quality and cancellation."""
import time

TRANSIENT = {'URLError','TimeoutError','ConnectionError','ConnectionResetError','RemoteDisconnected','HTTPError',
             'wardrobe_timeout','render_timeout','wardrobe_submission_retries_exhausted'}
QUALITY = {'wardrobe_identity_failed','wardrobe_face_coverage_failed','wardrobe_generation_failed',
           'wardrobe_h3_generation_mismatch','wardrobe_output_mismatch','wardrobe_account_overlay_remains',
           'wardrobe_account_overlay_review_required','hair_reference_color_mismatch',
           'hair_scene_preservation_failed'}

def plan(state, code, now=None):
    now=time.time() if now is None else now
    history=state.get('recoveryHistory',[])
    tried={x.get('strategy') for x in history}
    if code=='cancelled' or state.get('sourceReleasedAt') or state.get('status')=='cancelled':return None
    attempt=state.get('recoveryCount',len(history))+1
    result={'attempt':attempt,'reason':code,'nextAttemptAt':now+min(900,15*2**min(attempt-1,6))}
    # Every new synthesis gets one local post-processing repair first.
    generation=state.get('generationAttempt',0)
    tracked=any(x.get('strategy')=='tracked_overlay' and x.get('generationAttempt',0)==generation for x in history)
    if code in ('wardrobe_account_overlay_review_required','wardrobe_account_overlay_remains') and not tracked:
        return result|{'strategy':'tracked_overlay','stage':'계정명 위치 추적·복원·재검수','generationAttempt':generation}
    if code in TRANSIENT:
        return result|{'strategy':'resume_checkpoint','stage':'서버 연결 복구 후 저장 지점에서 재개'}
    if code in QUALITY:
        return result|{'strategy':'regenerate_quality','stage':'품질 미달 원인 반영 · 새 시드로 재생성','generationAttempt':generation+1}
    if code in ('hair_tracking_gap','hair_tracking_incomplete') and 'multiscale_face_tracking' not in tried and state.get('wardrobe')=='portrait_face':
        return result|{'strategy':'multiscale_face_tracking','stage':'고해상도 얼굴 재검출·연속 추적'}
    # Missing inputs, content filters, ambiguous identities and integrity failures
    # need new evidence. Repeating unchanged input cannot repair these failures.
    return None
