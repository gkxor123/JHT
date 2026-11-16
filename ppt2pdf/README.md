# PPT2PDF — PPT 여러 개를 한 번에 PDF로

PPT/PPTX 파일(폴더째도 가능)을 넣으면 한꺼번에 PDF로 바꿔 줍니다. 설치는 필요 없습니다.

## 다운로드
GitHub 저장소의 **Releases → `PPT2PDF (최신)` → `PPT2PDF.exe`** 를 받습니다.

## 사용법 (Windows)
- **가장 빠른 방법**: PPT 파일이나 폴더를 `PPT2PDF.exe` 아이콘에 **끌어다 놓기** → 창이 뜨고 바로 변환을 시작합니다.
- 또는 exe 실행 → 파일·폴더를 목록에 끌어다 놓거나 **파일 추가 / 폴더 추가** → **▶ PDF로 변환**
- 폴더는 하위 폴더까지 모두 찾습니다.
- PDF는 기본적으로 **원본과 같은 폴더에 같은 이름**(`강의.pptx` → `강의.pdf`)으로 저장됩니다.

## 옵션
| 옵션 | 설명 |
|---|---|
| 엔진 | `auto`: PowerPoint가 있으면 PowerPoint, 없으면 LibreOffice |
| 원본 화질 / 작은 용량 | 작은 용량은 그림 해상도를 낮춥니다. CT·MRI 같은 영상 그림은 **원본 화질** 권장 |
| 이미 변환된 파일 건너뛰기 | PPT보다 새 PDF가 이미 있으면 건너뜀 (다시 돌려도 빠름) |
| 원본과 같은 폴더에 저장 | 끄면 PDF를 지정한 폴더 한 곳에 모읍니다 |

## 속도를 위해 한 일
- **PowerPoint**: PowerPoint를 한 번만 띄워(창 없이) 모든 파일에 재사용합니다. 파일마다 PowerPoint를 켜고 끄는 시간이 없습니다.
  PowerPoint는 한 PC에서 하나만 돌 수 있어서 파일은 차례로 처리합니다.
- **LibreOffice**: CPU 코어 수만큼(최대 6개) 동시에 변환합니다.
- 이미 변환된 파일은 건너뜁니다.

## 참고
- PowerPoint(Office) 또는 [LibreOffice](https://www.libreoffice.org/) 중 하나가 설치되어 있어야 합니다. PowerPoint가 가장 정확합니다.
- 변환하는 동안 원래 쓰던 PowerPoint 창은 그대로 쓸 수 있고, 프로그램이 끝나도 닫지 않습니다.
- 암호가 걸린 PPTX는 "실패 - 암호가 걸린 파일"로 표시하고 건너뜁니다(암호 입력창 때문에 멈추지 않도록).
- `~$`로 시작하는 Office 임시 파일은 무시합니다.
- 같은 폴더에 `a.ppt`와 `a.pptx`가 함께 있으면 `a.pdf`, `a (ppt).pdf`로 따로 저장합니다.

## 명령줄 (.bat 용)
```
PPT2PDF.exe --cli "D:\강의" "E:\자료\1강.pptx" [--out 폴더] [--engine auto|powerpoint|libreoffice] [--quality print|screen] [--overwrite]
```
결과는 `%USERPROFILE%\ppt2pdf_cli.log`에 남습니다. 종료 코드: 0 성공, 1 일부 실패, 2 파일 없음, 3 엔진 없음.

## 개발
```
cd ppt2pdf
pip install -r requirements.txt
python ppt2pdf_main.py                 # GUI
python tests/test_convert.py           # 변환 테스트 (한글 경로, 하위 폴더, 이름 충돌, 암호 파일)
pyinstaller PPT2PDF.spec               # dist/PPT2PDF.exe
```
`ppt2pdf/` 아래를 push하면 GitHub Actions(`Build PPT2PDF exe`)가 Windows에서 테스트·빌드하고 Release를 갱신합니다.
