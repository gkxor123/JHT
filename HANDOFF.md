# SlideCap 인수인계 (다음 작업 세션용)

이 문서는 사용자의 PC(`E:\01_Radiology`)에서 실행되는 Claude Code 세션이
이어서 작업할 수 있도록 남긴 메모입니다.

## 지금까지 한 일
- `SlideCap` 완성. 강의 영상에서 PPT 영역만 슬라이드가 바뀔 때마다 캡처해 .pptx를 만듭니다.
  (`README.md` 참고)
- 사용자 요구사항 (확정):
  1. 마우스 포인터 이동이나 포인터가 나타나고 사라지는 것은 캡처하지 않음 → 구현 완료
  2. 글씨나 그림이 빈 바탕에 하나씩 추가되는 슬라이드는 **마지막 장면만** 저장 → 구현 완료.
     angiogram처럼 그림이 바뀌는 장면은 각각 저장.
  3. GIF나 동영상의 움직임은 캡처하지 않음 → 구현 완료
  4. PPT는 **영상 옆**에 같은 이름으로 저장
- 자동 모드: `SlideCap.exe --auto "<폴더>"`
  - 하위 폴더까지 뒤져서 PPT가 없는 영상만 처리합니다.
  - 영역은 폴더마다 한 번만 묻고, 그 폴더의 `slidecap_region.json`에 저장합니다.
- Windows exe는 GitHub Actions(`Build Windows exe`)가 빌드합니다. 결과는 Artifacts의 `SlideCap-windows`입니다.
- 테스트: `python tests/test_engine.py` (합성 영상 5종). 현재 모두 통과합니다.

## 남은 일 (이 PC에서)
1. `E:\01_Radiology` 폴더의 `CLAUDE` 파일과 기존 `.bat` 파일을 먼저 읽기.
2. 사용자의 작업 방식: "연수강좌 원본" 아래 해당 분과 폴더에 강좌 폴더(예: `2026년 11월 15일 00연수강좌`)를
   올리고 `.bat`을 실행하면, 그 강좌 영상들의 PPT가 영상 옆에 자동으로 생성되어야 함.
   → 기존 `.bat`에 `SlideCap.exe --auto "<연수강좌 원본 경로>"` 호출을 추가.
   - .bat에 한글 경로가 있으면 `chcp 65001` 및 파일 인코딩(UTF-8)에 주의.
   - SlideCap.exe는 GUI(창 모드) exe이므로, 끝날 때까지 기다리려면 `start "" /wait`로 실행.
3. `CLAUDE` 파일에 추가한 기능 설명을 기록 (다른 PC에서 볼 수 있도록).
4. **실제 강의 영상(30분 내외)으로 테스트**하고 결과 PPT를 사용자와 함께 확인. 필요하면 민감도를 조정.
   - 문제가 생기면 `python slidecap_main.py --cli <영상> --roi X,Y,W,H`로 재현하고,
     `detect_slides(..., debug=print)`로 판단 근거를 확인.
5. 사용자가 "완벽하다"고 하면 그때 SlideCap.exe를 `01_Radiology`에 배치 (사용자 요청).
