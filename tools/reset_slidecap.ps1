<#
SlideCap 결과를 지우고 처음부터 다시 만들기 위한 정리 스크립트 (한 번만 쓰는 용도)

지우는 것 (모두 휴지통으로 보냄, 되돌릴 수 있음):
  1. 영상 폴더: 영상과 "같은 이름"의 .pptx  (SlideCap이 만든 것만. 강의록 PPT는 그대로)
  2. 영상 폴더: slidecap_region.json, slidecap_region.jpg  (예전 영역 설정)
  3. PDF 폴더: 영상과 "같은 이름"의 .pdf

사용법 (reset_slidecap.bat 더블클릭, 또는):
  powershell -ExecutionPolicy Bypass -File reset_slidecap.ps1 -VideoRoot "E:\01_Radiology\연수강좌 원본" -PdfRoot "E:\01_Radiology\02_lecture"
먼저 지울 목록을 보여 주고, "예"라고 입력해야 지웁니다.
#>
param(
    [string]$VideoRoot = "E:\01_Radiology\연수강좌 원본",
    [string]$PdfRoot = "E:\01_Radiology\02_lecture"
)

Add-Type -AssemblyName Microsoft.VisualBasic
$videoExt = @(".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".ts", ".mts", ".m2ts", ".mpg", ".mpeg", ".3gp", ".asf", ".vob")

if (-not (Test-Path -LiteralPath $VideoRoot)) { Write-Host "영상 폴더가 없습니다: $VideoRoot" -ForegroundColor Red; exit 1 }

$videos = Get-ChildItem -LiteralPath $VideoRoot -Recurse -File | Where-Object { $videoExt -contains $_.Extension.ToLower() }
$names = @{}
foreach ($v in $videos) { $names[$v.BaseName.ToLower()] = $true }

$targets = New-Object System.Collections.Generic.List[string]
foreach ($v in $videos) {
    $ppt = Join-Path $v.DirectoryName ($v.BaseName + ".pptx")
    if (Test-Path -LiteralPath $ppt) { $targets.Add($ppt) }
}
Get-ChildItem -LiteralPath $VideoRoot -Recurse -File -Include "slidecap_region.json", "slidecap_region.jpg" |
    ForEach-Object { $targets.Add($_.FullName) }
if (Test-Path -LiteralPath $PdfRoot) {
    Get-ChildItem -LiteralPath $PdfRoot -Recurse -File -Filter *.pdf |
        Where-Object { $names.ContainsKey($_.BaseName.ToLower()) } |
        ForEach-Object { $targets.Add($_.FullName) }
} else {
    Write-Host "PDF 폴더가 없습니다 (건너뜀): $PdfRoot" -ForegroundColor Yellow
}

$targets = $targets | Sort-Object -Unique
Write-Host ""
Write-Host "영상 $($videos.Count)개를 찾았습니다. 지울 파일 $(@($targets).Count)개:" -ForegroundColor Cyan
$targets | ForEach-Object { Write-Host "  $_" }
if (@($targets).Count -eq 0) { Write-Host "지울 파일이 없습니다."; exit 0 }

Write-Host ""
$ans = Read-Host "위 파일을 휴지통으로 보낼까요? (예/아니오)"
if ($ans -ne "예" -and $ans -ne "y" -and $ans -ne "yes") { Write-Host "취소했습니다. 아무것도 지우지 않았습니다."; exit 0 }

$n = 0
foreach ($f in $targets) {
    try {
        [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile($f, 'OnlyErrorDialogs', 'SendToRecycleBin')
        $n++
    } catch { Write-Host "  실패: $f ($_)" -ForegroundColor Red }
}
Write-Host "$n 개를 휴지통으로 보냈습니다. 이제 평소 쓰던 .bat을 실행하면 새로 만듭니다." -ForegroundColor Green
