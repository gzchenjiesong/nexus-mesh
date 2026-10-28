# ==============================================================================
#  diag-python.ps1 —— 只查 Python，5 秒出结果
# ==============================================================================
#  用途：setup 脚本停住时，先跑这个确认 Python 到底什么情况。
#  它不做任何改动，只读。
#
#  用法：powershell -ExecutionPolicy Bypass -File deploy\diag-python.ps1
# ==============================================================================

Write-Host "=== 谁占着 python 这个名字 ===" -ForegroundColor Cyan
foreach ($name in @("python", "python3", "py")) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $c) { Write-Host "  $name : 找不到"; continue }
    $isStub = $c.Source -like "*\WindowsApps\*"
    $tag = if ($isStub) { "  ← Microsoft Store 存根（是假的，会弹商店）" } else { "" }
    $color = if ($isStub) { "Yellow" } else { "Green" }
    Write-Host "  $name : $($c.Source)$tag" -ForegroundColor $color
}

Write-Host ""
Write-Host "=== PATH 里所有 python.exe ===" -ForegroundColor Cyan
$found = $false
foreach ($dir in $env:PATH -split ";") {
    if (-not $dir) { continue }
    foreach ($f in @("python.exe", "python3.exe")) {
        $p = Join-Path $dir $f
        if (Test-Path $p) {
            $found = $true
            $isStub = $p -like "*\WindowsApps\*"
            $tag = if ($isStub) { "  ← 存根" } else { "" }
            Write-Host "  $p$tag"
        }
    }
}
if (-not $found) { Write-Host "  （PATH 里一个都没有）" -ForegroundColor Yellow }

Write-Host ""
Write-Host "=== 实际跑一次（8 秒超时）===" -ForegroundColor Cyan
$probe = Join-Path $env:TEMP "nxs_diag_ver.py"
$out   = Join-Path $env:TEMP "nxs_diag_out.txt"
$err   = Join-Path $env:TEMP "nxs_diag_err.txt"
Set-Content -Path $probe -Encoding ASCII -Value 'import sys; print("%d.%d.%d" % sys.version_info[:3])'

$anyOk = $false
foreach ($name in @("python", "python3", "py")) {
    $c = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $c) { continue }
    if ($c.Source -like "*\WindowsApps\*") {
        Write-Host "  $name : 跳过（存根）" -ForegroundColor Yellow
        continue
    }
    Write-Host "  试 $name ... " -NoNewline
    $proc = $null
    try {
        $proc = Start-Process -FilePath $c.Source -ArgumentList @($probe) `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $out -RedirectStandardError $err
        if (-not $proc.WaitForExit(8000)) {
            try { $proc.Kill() } catch { }
            Write-Host "超时 8 秒无响应 ✗" -ForegroundColor Red
            continue
        }
        $ver = if (Test-Path $out) { (Get-Content $out -Raw) } else { "" }
        if ($ver -and $ver.Trim()) {
            Write-Host "版本 $($ver.Trim()) ✓" -ForegroundColor Green
            $anyOk = $true
        } else {
            $e = if (Test-Path $err) { (Get-Content $err -Raw) } else { "" }
            $first = if ($e) { ($e.Trim() -split "`n")[0] } else { "无输出" }
            Write-Host "无输出（${first}）✗" -ForegroundColor Red
        }
    } catch {
        Write-Host "跑不起来：$($_.Exception.Message) ✗" -ForegroundColor Red
    } finally {
        if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
    }
}
foreach ($f in @($probe, $out, $err)) {
    try { if (Test-Path $f) { Remove-Item $f -Force } } catch { }
}

Write-Host ""
if ($anyOk) {
    Write-Host "结论：Python 可用。" -ForegroundColor Green
    Write-Host "  但注意版本要 >= 3.11。上面显示的版本若低于 3.11，需要升级。"
} else {
    Write-Host "结论：没有可用的 Python。" -ForegroundColor Red
    Write-Host ""
    Write-Host "  装法（PowerShell 里跑，装完重开一个窗口）："
    Write-Host "    winget install Python.Python.3.12"
    Write-Host ""
    Write-Host "  ⚠ 装的时候**勾上 Add python.exe to PATH**，否则还是找不到。"
    Write-Host '  ⚠ 别点系统弹的「应用商店」里那个 —— 那是 Microsoft 的转发，'
    Write-Host "    不是真的 Python。要用 python.org 或 winget 装的。"
}
