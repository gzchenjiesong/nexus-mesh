<#
=============================================================================
 diag-net.ps1 —— 分清「网络不通」和「证书不认」
=============================================================================
 用户遇到的情况：上一轮检查 HTTP 200，建了 CA 文件后再跑变 FAIL。
 这说明网络本身没问题，**大概率是证书校验失败被误报成"不可达"**。

 这两件事的修法完全不同：
   网络不通  → 查防火墙 / 代理 / 安全组
   证书不认  → 查 CA 文件内容与路径

 本脚本把两者分开测，并给出 curl 的精确退出码。

 用法：
   powershell -ExecutionPolicy Bypass -File deploy\diag-net.ps1
=============================================================================
#>

param(
    [string]$Server = $(if ($env:NXS_SERVER) { $env:NXS_SERVER } else { "43.138.222.206:18800" })
)

$ConfDir = if ($env:NXS_ACTOR_DIR) { $env:NXS_ACTOR_DIR } else { Join-Path $HOME ".nexus-mesh" }
$CaFile  = Join-Path $ConfDir "ca.pem"
$ServerUrl = "https://$Server/nxs"
$healthUrl = "$ServerUrl/health"

$host0 = $Server.Split(':')[0]
$port0 = $Server.Split(':')[1]

function Line { Write-Host ("-" * 60) -ForegroundColor DarkGray }

Write-Host "=== 目标 ===" -ForegroundColor Cyan
Write-Host "  健康检查 : $healthUrl"
Write-Host "  CA 文件  : $CaFile"

# ------------------------------------------------------------------ ① TCP
Line
Write-Host "① TCP 能连上吗（这一层与证书无关）" -ForegroundColor Cyan
try {
    $tcp = Test-NetConnection -ComputerName $host0 -Port $port0 -InformationLevel Quiet -WarningAction SilentlyContinue
} catch {
    $tcp = $false
}
if ($tcp) {
    Write-Host "  OK   TCP $host0`:$port0 可连接" -ForegroundColor Green
} else {
    Write-Host "  FAIL TCP $host0`:$port0 连不上" -ForegroundColor Red
    Write-Host ""
    Write-Host "  → 这是**网络层**问题，与证书无关。按顺序查：" -ForegroundColor Yellow
    Write-Host "     1. 公司防火墙是否拦出站 $port0"
    Write-Host "     2. 是否有 HTTP/SOCKS 代理：echo `$env:HTTPS_PROXY"
    Write-Host "     3. 云服务器安全组是否放行 $port0"
    Write-Host ""
    Write-Host "  不再往下测（后续都会失败，只会产生噪音）。"
    exit 1
}

# ------------------------------------------------------------------ ② 不验证书
Line
Write-Host "② 跳过证书校验（-k）—— 验证 TLS 之上的 HTTP 是否正常" -ForegroundColor Cyan
$outK = & curl.exe -sS -k --max-time 10 -o NUL -w "%{http_code}" $healthUrl 2>&1
$codeK = $LASTEXITCODE
if ($codeK -eq 0 -and $outK -eq "200") {
    Write-Host "  OK   HTTP 200（-k）" -ForegroundColor Green
    Write-Host "       → 网络与 TLS 握手都正常，问题**只在证书校验**"
} else {
    Write-Host "  FAIL curl 退出码 ${codeK}，输出：$outK" -ForegroundColor Red
    Write-Host ""
    switch ($codeK) {
        6  { Write-Host "  → 域名解析失败（这里用的是 IP，不该出现）" -ForegroundColor Yellow }
        7  { Write-Host "  → 连不上（TCP 层刚才是通的？可能有代理干扰）" -ForegroundColor Yellow }
        28 { Write-Host "  → 超时（网络慢或被丢包）" -ForegroundColor Yellow }
        35 { Write-Host "  → TLS 握手失败（可能是企业中间人代理）" -ForegroundColor Yellow }
        default { Write-Host "  → 未预期的错误，把上面输出发我" -ForegroundColor Yellow }
    }
}

# ------------------------------------------------------------------ ③ 用 CA
Line
Write-Host "③ 用 CA 校验（脚本实际用的方式）" -ForegroundColor Cyan
if (-not (Test-Path $CaFile)) {
    Write-Host "  SKIP CA 文件不存在：$CaFile" -ForegroundColor Yellow
    Write-Host "       → 脚本此时会用 -k 兜底，所以不该失败。若仍失败，看 ② 的结果。"
} else {
    $size = (Get-Item $CaFile).Length
    Write-Host "       CA 文件大小：$size 字节"

    # ⚠ 两个最常见的 PEM 损坏方式，先查这两个
    $bytes = [System.IO.File]::ReadAllBytes($CaFile)
    $hasBom = ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF)
    if ($hasBom) {
        Write-Host "  ⚠ 检测到 UTF-8 BOM！OpenSSL 会认不出 BEGIN 行" -ForegroundColor Yellow
    }
    $firstLine = ([System.IO.File]::ReadAllLines($CaFile))[0]
    $firstLineClean = $firstLine.TrimStart([char]0xFEFF)
    if ($firstLineClean -ne "-----BEGIN CERTIFICATE-----") {
        Write-Host "  ⚠ 首行不是 '-----BEGIN CERTIFICATE-----'" -ForegroundColor Yellow
        Write-Host "     实际首行：[$firstLine]" -ForegroundColor Yellow
    } else {
        Write-Host "  OK   首行正确（BEGIN 行）" -ForegroundColor Green
    }
    if ($size -lt 800) {
        Write-Host "  ⚠ 文件只有 $size 字节，正常的 CA 证书约 1200 字节 —— 可能粘贴不完整" -ForegroundColor Yellow
    }

    $outCa = & curl.exe -sS --cacert $CaFile --max-time 10 -o NUL -w "%{http_code}" $healthUrl 2>&1
    $codeCa = $LASTEXITCODE
    if ($codeCa -eq 0 -and $outCa -eq "200") {
        Write-Host "  OK   HTTP 200（用 CA 校验）" -ForegroundColor Green
    } else {
        Write-Host "  FAIL curl 退出码 $codeCa" -ForegroundColor Red
        Write-Host "       输出：$outCa"
        if ($codeCa -eq 60) {
            Write-Host ""
            Write-Host "  → 退出码 60 = **证书校验失败**。这不是网络问题。" -ForegroundColor Yellow
            Write-Host "     常见原因（按概率）：" -ForegroundColor Yellow
            Write-Host "       a) CA 文件被记事本存成了 UTF-8 BOM 或 .pem.txt"
            Write-Host "       b) 粘贴时首尾行缺失/多了空格"
            Write-Host "       c) 用的是服务器的另一份 CA（本项目只有一份，指纹应为："
            Write-Host "          26:45:31:A6:A9:AA:CC:3C:C3:F4:68:E6:C1:F4:60:17:"
            Write-Host "          BB:DD:59:01:F6:F7:7E:65:B2:06:8B:33:0B:A7:29:62)"
            Write-Host ""
            Write-Host "     最快的验证：② 里 -k 是 200，这里 60 —— 那一定是 CA 的锅。"
        }
    }
}

# ------------------------------------------------------------------ ④ 代理
Line
Write-Host "④ 代理环境变量（企业网络常见干扰）" -ForegroundColor Cyan
$proxies = @{
    "HTTPS_PROXY" = $env:HTTPS_PROXY
    "HTTP_PROXY"  = $env:HTTP_PROXY
    "ALL_PROXY"   = $env:ALL_PROXY
    "NO_PROXY"    = $env:NO_PROXY
}
$anyProxy = $false
foreach ($k in $proxies.Keys) {
    if ($proxies[$k]) {
        $anyProxy = $true
        Write-Host "  $k = $($proxies[$k])"
    }
}
if (-not $anyProxy) {
    Write-Host "  未设置（直连）" -ForegroundColor Green
} else {
    Write-Host "  ⚠ 存在代理设置 —— 若代理做 TLS 中间人，自签 CA 会失败" -ForegroundColor Yellow
    Write-Host "     可以把目标加进 NO_PROXY 试试："
    Write-Host "       `$env:NO_PROXY = `"$host0`""
}

Line
Write-Host "结论" -ForegroundColor Cyan
Write-Host "  以上四项里，② 成功而 ③ 失败 → CA 文件问题（不是网络）"
Write-Host "  ① 失败                          → 网络问题（与证书无关）"
Write-Host "  ②③ 都成功                       → 网络与证书都正常，可继续注册"
