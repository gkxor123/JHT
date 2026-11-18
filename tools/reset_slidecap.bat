@echo off
chcp 65001 >nul
rem SlideCap 결과(PPT, PDF, 영역 설정)를 휴지통으로 보내고 다시 만들 준비. 폴더가 다르면 아래 두 경로를 고치세요.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0reset_slidecap.ps1" -VideoRoot "E:\01_Radiology\연수강좌 원본" -PdfRoot "E:\01_Radiology\02_lecture"
pause
