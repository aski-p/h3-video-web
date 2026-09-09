# ASKI MiniMax H3 RTX 5080 Worker

이 패키지는 Windows RTX 5080 PC에서 로컬 ComfyUI를 사용하고, coordinator에는 outbound HTTPS만 연결합니다.

## 설치

1. 배포 담당자가 전달한 비공개 ZIP을 풉니다.
2. `Install-H3Worker.ps1`을 PowerShell에서 실행합니다. 관리자 권한은 필요하지 않습니다.
3. 설치 프로그램이 RTX 5080, ComfyUI, 8개 exact-H3 모델의 크기·SHA-256을 검증하고 768×432·1초·4-step 로컬 H3 영상을 실제 생성·decode합니다.
4. 실제 생성 검증 marker가 만들어진 뒤에만 현재 사용자 로그온 자동실행을 등록하고 worker를 시작합니다.

`worker-token.txt`는 비밀 파일이며 Git에 포함하지 않습니다. 설치 후 ZIP을 삭제하세요.

## 동작 계약

- OFF 작업은 PGX, ON 작업은 RTX 5080 한 곳에만 제출됩니다.
- PGX와 RTX 큐는 독립 실행됩니다.
- 모델/ComfyUI/heartbeat가 준비되지 않으면 웹 토글은 활성화되지 않습니다.
- Wan 등 다른 모델로 fallback하지 않습니다.
- ComfyUI는 `127.0.0.1:8188`에서만 사용합니다.

## 제거

`Uninstall-H3Worker.ps1`을 실행하면 자동실행과 worker 프로세스, 설치 폴더를 제거합니다. ComfyUI와 모델은 삭제하지 않습니다.
