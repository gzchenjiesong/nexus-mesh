# ==============================================================================
#  setup-work-node.ps1 —— 在 Windows 公司电脑上准备 Nexus Mesh 执行者节点
# ==============================================================================
#
#  与 macOS 版的差别（不是简单翻译，是平台性的）：
#
#    ┌──────────┬─────────────────────┬──────────────────────────────┐
#    │          │ macOS               │ Windows                      │
#    ├──────────┼─────────────────────┼──────────────────────────────┤
#    │ 常驻     │ launchd (plist)     │ 计划任务 (ScheduledTask)     │
#    │ 无窗口   │ 天然后台            │ 必须用 pythonw.exe           │
#    │ CA 路径  │ ~/nexus-mesh/ca.pem │ 同，但要给 PowerShell 正确转义│
#    │ 权限     │ chmod 600           │ icacls（语义不同，见下）      │
#    └──────────┴─────────────────────┴──────────────────────────────┘
#
#  用法（PowerShell，建议以普通用户身份，不一定要管理员）：
#
#      powershell -ExecutionPolicy Bypass -File setup-work-node.ps1 -Check
#      powershell -ExecutionPolicy Bypass -File setup-work-node.ps1
#
#  ⚠ 如果你的终端显示中文乱码，先执行：
#      chcp 65001
#
# ==============================================================================

[CmdletBinding()]
param(
    [switch]$Check,                 # 只检查，不改动
    [switch]$InstallTask,           # 顺便创建计划任务（常驻）
    [string]$NodeId = $(if ($env:NXS_NODE_ID) { $env:NXS_NODE_ID } else { "work-desktop" }),
    [string]$Server = $(if ($env:NXS_SERVER) { $env:NXS_SERVER } else { "43.138.222.206:18800" })
)

$ErrorActionPreference = "Stop"
$ConfDir   = if ($env:NXS_ACTOR_DIR) { $env:NXS_ACTOR_DIR } else { Join-Path $HOME ".nexus-mesh" }
$AgentDir  = Join-Path $ConfDir "agents"
$CaFile    = Join-Path $ConfDir "ca.pem"
$CredFile  = Join-Path $AgentDir "$NodeId.env"
$ServerUrl = "https://$Server/nxs"
$RepoDir   = Split-Path -Parent $PSScriptRoot

function Say($msg) { Write-Host $msg }
function Ok($msg)  { Write-Host "  OK   $msg" -ForegroundColor Green }
function Bad($msg) { Write-Host "  FAIL $msg" -ForegroundColor Red }
function Warn($msg){ Write-Host "  WARN $msg" -ForegroundColor Yellow }

Say "-> 环境"
Say "   节点 ID   : $NodeId"
Say "   协调节点  : $ServerUrl"
Say "   凭据目录  : $AgentDir"

# ------------------------------------------------------------------ Python
# ⚠ 这是 Windows 上最可能的阻塞点：公司电脑常常没有 Python。
Say ""
Say "-> Python（桥接是 Python 程序，必须 >= 3.11）"
$Py = $null
$PyVer = $null

# ⚠ Windows 上探测 Python 有两个真实的坑，都必须处理：
#
# ① **Microsoft Store 存根**：系统自带的 `python.exe` 可能是个重解析点
#    （在 %LOCALAPPDATA%\Microsoft\WindowsApps\ 下），跑它会**弹出应用商店**。
#    那不是 Python，却会占住 `python` 这个命令名 —— 看起来像"装了但用不了"。
#
# ② **卡住**：某些环境下跑存根或 py 启动器会长时间无响应。脚本如果直接
#    `& python -c ...`，就会静默卡死 —— 用户只看到输出停在某一行，
#    既没有报错也没有进展。
#
# 所以：跳过 WindowsApps、带硬超时、并且**先打印再执行**（卡住时能定位）。

# 把探测脚本写成临时文件，而不是内联 `-c "..."`。
# 理由：内联要处理引号转义与括号嵌套，一旦编码或转义出问题就崩在解析阶段，
# 而报错位置指向别处。写成文件后完全没有这层风险。
$probeFile = Join-Path $env:TEMP "nxs_pyver_probe.py"
$outFile   = Join-Path $env:TEMP "nxs_pyver_out.txt"
$errFile   = Join-Path $env:TEMP "nxs_pyver_err.txt"
Set-Content -Path $probeFile -Encoding ASCII `
    -Value 'import sys; print("%d.%d" % sys.version_info[:2])'

foreach ($cand in @("python", "python3", "py")) {
    $exe = Get-Command $cand -ErrorAction SilentlyContinue
    if (-not $exe) { continue }

    # ① 跳过 Microsoft Store 存根
    if ($exe.Source -like "*\WindowsApps\*") {
        Warn "$cand 是 Microsoft Store 存根（不是真的 Python），跳过"
        continue
    }

    Say "   试 $cand -> $($exe.Source)"

    # ② 带硬超时地跑一次。Start-Process 而不是 `&`：
    #    `&` 没有超时，卡住就永远卡住。
    $raw = $null
    $proc = $null
    try {
        $proc = Start-Process -FilePath $exe.Source -ArgumentList @($probeFile) `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $outFile -RedirectStandardError $errFile
        if (-not $proc.WaitForExit(8000)) {
            try { $proc.Kill() } catch { }
            Warn "$cand 超过 8 秒无响应，跳过"
            continue
        }
        if (Test-Path $outFile) { $raw = (Get-Content $outFile -Raw) }
    } catch {
        Warn "$cand 跑不起来：$($_.Exception.Message)"
        continue
    } finally {
        if ($proc -and -not $proc.HasExited) { try { $proc.Kill() } catch { } }
    }

    if (-not $raw) {
        # 常见于 py 启动器没装 Python、或存根返回空
        $errText = ""
        if (Test-Path $errFile) { $errText = (Get-Content $errFile -Raw) }
        if ($errText -and $errText.Trim()) {
            Warn "$cand 无输出（$($errText.Trim().Split([char]10)[0])）"
        } else {
            Warn "$cand 无输出，跳过"
        }
        continue
    }

    $verText = ([string]$raw).Trim()

    # 不用 [version] 强转 —— 它遇多余空白或后缀就抛异常，
    # 而异常会被 catch 吞掉，看起来像"版本不够"。分段比较更直白。
    $parts = $verText.Split('.')
    if ($parts.Length -lt 2) { Warn "$cand 版本格式异常：$verText"; continue }
    $major = 0; $minor = 0
    if (-not [int]::TryParse($parts[0], [ref]$major)) { continue }
    if (-not [int]::TryParse($parts[1], [ref]$minor)) { continue }

    $versionOk = ($major -gt 3) -or (($major -eq 3) -and ($minor -ge 11))
    if ($versionOk) {
        # 存**完整路径**而不是命令名 —— 计划任务启动时 PATH 可能与终端不同，
        # 存命令名会导致"手工能跑、任务报找不到 python"。
        $Py = $exe.Source
        $PyVer = $verText
        Ok "$cand -> ${PyVer}（$($exe.Source)）"
        break
    }
    Warn "$cand -> ${verText}（需要 3.11+）"
}

# 清掉临时文件（失败也不管，不影响流程）
foreach ($f in @($probeFile, $outFile, $errFile)) {
    try { if (Test-Path $f) { Remove-Item $f -Force } } catch { }
}

if (-not $Py) {
    Bad "没有可用的 Python 3.11+"
    Say ""
    Say "   装法（任选）："
    Say "     winget install Python.Python.3.12"
    Say "     或从 https://www.python.org/downloads/windows/ 下载"
    Say ""
    Say "   装的时候**勾上 Add python.exe to PATH**。"
    Say "   装完重开一个 PowerShell 窗口再跑本脚本。"
    if ($Check) { exit 1 }
    Say ""
    Say "   （继续做其余检查，但节点无法启动）"
}

# ------------------------------------------------------------------ CA 准备
# ⚠ **必须在网络检查之前。**
#
#   初版把 CA 复制放在后面的"准备本机文件"一节，于是网络检查跑的时候
#   CA 还不存在 —— 检查用的是系统 CA 库，自签证书必然验不过。
#   也就是说：**这项检查在修好之前永远会失败**，而失败原因是脚本自己的
#   顺序问题，不是环境问题。
Say ""
Say "-> CA 证书"

# CA —— **优先从仓库复制，不要求手工粘贴**
#
# ⚠ 手工粘贴 PEM 是个可靠的出错方式，实测过两种：
#   · 记事本存成 UTF-8 BOM → curl 退出码 77（"CA 文件加载不了"）
#   · 粘贴被截断          → 同样 77
#   而 77 的提示语里看不出是编码问题，很容易被误判成网络故障。
#
# CA 是**公开证书**（不含私钥），本来就该随代码走 —— 放仓库里
# deploy/ca.pem，脚本直接复制，一次都不用粘。
$repoCa = Join-Path $PSScriptRoot "ca.pem"
if (Test-Path $CaFile) {
    Ok "CA 已存在: $CaFile"
    $sz = (Get-Item $CaFile).Length
    if ($sz -lt 800) {
        Warn "但文件只有 $sz 字节（正常约 1200）—— 可能粘贴不完整"
        if (Test-Path $repoCa) {
            Copy-Item $repoCa $CaFile -Force
            Ok "已用仓库里的版本覆盖（$((Get-Item $CaFile).Length) 字节）"
        }
    } else {
        # 检查有没有 BOM —— 有的话 curl 会直接报 77
        $bytes = [System.IO.File]::ReadAllBytes($CaFile)
        if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
            Warn "检测到 UTF-8 BOM —— curl 会因此加载失败（退出码 77）"
            if (Test-Path $repoCa) {
                Copy-Item $repoCa $CaFile -Force
                Ok "已用仓库里的版本覆盖（去掉了 BOM）"
            }
        }
    }
} elseif (Test-Path $repoCa) {
    Copy-Item $repoCa $CaFile -Force
    Ok "CA 已从仓库复制: $CaFile"
} else {
    Bad "找不到 CA 证书"
    Say ""
    Say "   期望位置：$(Join-Path $PSScriptRoot 'ca.pem')"
    Say "   也检查过：$CaFile"
    Say ""
    Say "   ⚠ 如果仓库里应该有一份但你没有，最常见的原因是被 .gitignore 拦了。"
    Say "     .gitignore 里有一条 *.pem（防私钥泄露，是对的），"
    Say "     而 deploy/ca.pem 需要一条 !deploy/ca.pem 例外。"
    Say ""
    Say "     验证：git ls-files deploy/ca.pem"
    Say "       有输出 → 文件在仓库里，可能是 clone 不完整"
    Say "       无输出 → 文件没入库，需要 git add -f deploy/ca.pem 或修 .gitignore"
    Say ""
    Say "   或者从服务器取："
    Say "     scp root@$($Server.Split(':')[0]):/etc/nexus-mesh/tls/ca.pem `"$CaFile`""
}

# ------------------------------------------------------------------ 网络
Say ""
Say "-> 网络与证书"

# ⚠⚠ **健康检查必须用 Python，不能用 curl.exe**
#
#   Windows 上 curl.exe 用的是**系统 TLS 栈（schannel）**，而桥接（Python）
#   用的是**内置 OpenSSL**。两者对同一张证书的判断可能不同：
#
#     schannel  → 会做吊销检查（CRL/OCSP）。自签 CA 没发布吊销信息，
#                 于是报「the revocation status is unknown」并拒绝。
#     OpenSSL   → 默认不查吊销；但 3.13+ 默认开 VERIFY_X509_STRICT，
#                 会要求 CA 有 keyUsage 扩展。
#
#   实测：同一个 CA，macOS 的 curl 通过，Windows 的 curl 失败，
#   而这跟桥接能不能连**毫无关系**。用 curl 做判据只会制造噪音。
#
#   所以这里跑一段 Python，用与 HttpTransport._ssl_context 完全相同的方式
#   建上下文 —— 测的就是生产代码实际会做的事。

$pyForCheck = $Py
if (-not $pyForCheck) {
    # Python 都没装时退回 curl（带 --ssl-no-revoke 规避 schannel 吊销检查）
    Warn "没有 Python，退回 curl 检查（结果仅供参考）"
    $curlOut = & curl.exe -sS -k --ssl-no-revoke --max-time 10 -o NUL `
                   -w "%{http_code}" "$ServerUrl/health" 2>&1
    if ($LASTEXITCODE -eq 0 -and "$curlOut" -eq "200") {
        Ok "协调节点可达（HTTP 200，未验证书）"
    } else {
        Bad "协调节点不可达（curl 退出码 ${LASTEXITCODE}）"
        if ($Check) { exit 1 }
    }
} else {
    # 把检查逻辑写成临时 .py —— 不用内联 -c，避免引号/括号转义问题。
    #
    # ⚠ 参数通过**环境变量**传，不走 -ArgumentList。
    #
    #   实测踩过：`-ArgumentList @($file, $url, $chkCa)` 在 $chkCa 为空时
    #   PowerShell 直接抛
    #     「无法对参数 ArgumentList 执行参数验证。参数为 Null 或空」
    #   而那句话完全没提是数组里哪个元素空的。
    #
    #   环境变量没有这个问题：空值就是空串，不会被参数验证拦下。
    #   而且子进程默认继承父进程环境，不需要额外传参。
    $chkFile = Join-Path $env:TEMP "nxs_health_check.py"
    $chkOut  = Join-Path $env:TEMP "nxs_health_out.txt"
    $chkErr  = Join-Path $env:TEMP "nxs_health_err.txt"

    $chkCode = @'
import json, os, ssl, urllib.error, urllib.request

url = os.environ.get("NXS_CHK_URL", "")
ca  = os.environ.get("NXS_CHK_CA", "")
out = {"ok": False, "url": url}
try:
    ctx = None
    if ca:
        ctx = ssl.create_default_context(cafile=ca)
        out["strict"] = bool(ctx.verify_flags & ssl.VERIFY_X509_STRICT)
    else:
        out["note"] = "no-ca"
    with urllib.request.urlopen(url, context=ctx, timeout=10) as r:
        out["ok"] = (r.status == 200)
        out["status"] = r.status
except urllib.error.URLError as e:
    # ⚠ **证书错误会被 urllib 包进 URLError。**
    #
    #   所以不能写成 `except ssl.SSLCertVerificationError` 在前 ——
    #   urlopen 抛出的是 URLError(reason=SSLCertVerificationError)，
    #   那个分支永远不触发，证书问题会被误判成网络问题。
    #   而这正是我一直在修的那类混淆，不能自己再犯一次。
    reason = e.reason
    if isinstance(reason, ssl.SSLCertVerificationError):
        out["err"] = "cert: " + str(reason)
        out["verify_code"] = getattr(reason, "verify_code", None)
    else:
        out["err"] = "net: " + str(reason)
except ssl.SSLCertVerificationError as e:
    # 兜底：某些路径可能直接抛，不经 URLError 包装
    out["err"] = "cert: " + str(e)
    out["verify_code"] = getattr(e, "verify_code", None)
except Exception as e:
    out["err"] = type(e).__name__ + ": " + str(e)
print(json.dumps(out))
'@
    Set-Content -Path $chkFile -Value $chkCode -Encoding UTF8

    # 传参走环境变量（见上），ArgumentList 只给文件路径 —— 单元素，不可能为空
    # ⚠ 这里必须**现查**，不能依赖外部变量。
    #   之前写了个 $hasCa 但从来没赋值 —— PowerShell 里未定义变量是 $null，
    #   于是 if ($null) 恒假，CA 永远不被使用。而脚本不会报错。
    $chkHasCa = Test-Path $CaFile
    $env:NXS_CHK_URL = "$ServerUrl/health"
    $env:NXS_CHK_CA  = if ($chkHasCa) { $CaFile } else { "" }

    $pyOut = $null
    $pyProc = $null
    $runErr = $null
    try {
        $pyProc = Start-Process -FilePath $pyForCheck -ArgumentList $chkFile `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $chkOut -RedirectStandardError $chkErr
        if (-not $pyProc.WaitForExit(25000)) {
            try { $pyProc.Kill() } catch { }
            $runErr = "health check timed out after 25s"
        } elseif (Test-Path $chkOut) {
            $pyOut = (Get-Content $chkOut -Raw)
        }
    } catch {
        $runErr = $_.Exception.Message
    } finally {
        Remove-Item Env:\NXS_CHK_URL -ErrorAction SilentlyContinue
        Remove-Item Env:\NXS_CHK_CA  -ErrorAction SilentlyContinue
    }

    $res = $null
    if ($pyOut) {
        try { $res = $pyOut | ConvertFrom-Json } catch { $res = $null }
    }
    if ($runErr) {
        # 检查本身跑不起来 —— 说清楚，别让人以为是网络问题
        Warn "健康检查没跑成：$runErr"
        if (Test-Path $chkErr) {
            $e = (Get-Content $chkErr -Raw)
            if ($e -and $e.Trim()) { Say ("       " + ($e.Trim() -split "`n")[0]) }
        }
    }

    # 清理临时文件
    foreach ($f in @($chkFile, $chkOut, $chkErr)) {
        try { if (Test-Path $f) { Remove-Item $f -Force } } catch { }
    }

    if ($res -and $res.ok) {
        $strictNote = if ($res.strict) { "，严格模式" } else { "" }
        Ok "协调节点可达（HTTP 200${strictNote}）"
    } elseif ($res -and $res.err) {
        $errText = [string]$res.err
        if ($errText.StartsWith("cert:")) {
            # ★ 证书问题 —— 与网络无关
            Bad "网络通，但**证书校验失败**"
            Say ""
            Say "   $errText"
            Say ""
            if ($errText -match "key usage") {
                Say "   → CA 证书**缺少 Key Usage 扩展**。这不是你的文件坏了，"
                Say "     是服务端那张 CA 本身不合规 —— 生成时漏了扩展。"
                Say ""
                Say "     受影响的客户端："
                Say "       · Python 3.13+（默认 VERIFY_X509_STRICT）"
                Say "       · Windows 的 curl（schannel 会报 revocation unknown）"
                Say ""
                Say "     修法：服务端重新生成 CA（deploy/gen-tls.sh），"
                Say "           然后把新的 ca.pem 同步到本机。"
            } else {
                Say "   → 按顺序查："
                Say "     a) CA 文件与服务器上的是否同一份（比对 SHA256 指纹）"
                Say "     b) 文件是否被记事本改成 UTF-8 BOM 或 .pem.txt"
                Say "     c) 内容是否被截断（正常约 1200 字节）"
            }
        } else {
            Bad "协调节点不可达"
            Say ""
            Say "   $errText"
            Say ""
            Say "   → 这是网络层问题，按顺序查："
            Say "     1. Test-NetConnection $($Server.Split(':')[0]) -Port $($Server.Split(':')[1])"
            Say "     2. echo `$env:HTTPS_PROXY      ← 有代理试试设 NO_PROXY"
            Say "     3. 云服务器安全组是否放行"
        }
        if ($Check) { exit 1 }
    } else {
        Warn "健康检查没拿到结果 —— 继续做后续检查"
    }
}

# ------------------------------------------------------------------ codebuddy
Say ""
Say "-> CodeBuddy CLI（执行者靠它，与 IDE 是两个独立软件）"

# ⚠ 检测顺序必须与桥接（examples/codebuddy_node.py 的 find_codebuddy）
#   **完全一致** —— 否则这里显示的路径不是桥接实际会用的那个。
#
# 优先级：.exe > .cmd > .ps1
#   .exe  原生二进制，参数不经 shell，中文 prompt 无乱码风险（最佳）
#   .cmd  npm shim，经 cmd.exe 中转，中文可能因代码页（936/GBK）乱码
#   .ps1  npm shim，经 PowerShell 中转，还受执行策略限制
#
# 为什么不能直接用 `Get-Command codebuddy`：
#   它按 PATHEXT 匹配，**PowerShell 自己的 .PS1 优先级最高**，
#   所以永远返回 .ps1 —— 而桥接（Python 的 shutil.which）因为
#   PATHEXT 默认值里没有 .PS1，返回的是 .cmd。
#   两边看到的不是同一个文件，会误导判断。

$CbPath = $null
$CbCandidates = @(
    (Join-Path $env:LOCALAPPDATA "codebuddy\bin\codebuddy.exe"),
    (Join-Path $env:APPDATA "npm\codebuddy.exe"),
    (Join-Path $env:APPDATA "npm\codebuddy.cmd"),
    (Join-Path $env:APPDATA "npm\codebuddy.ps1")
)
foreach ($cand in $CbCandidates) {
    if ($cand -and (Test-Path $cand)) { $CbPath = $cand; break }
}
if (-not $CbPath) {
    # 退回 PATH 查找
    $c = Get-Command codebuddy -ErrorAction SilentlyContinue
    if ($c) { $CbPath = $c.Source }
}

if ($CbPath) {
    Ok "codebuddy -> $CbPath"

    # 说清这个形态的代价 —— 中文 prompt 是主要用途，
    # 编码问题一旦出现很难联想，所以提前讲明白
    $low = $CbPath.ToLower()
    if ($low.EndsWith(".exe")) {
        Say "       形态：原生二进制 —— 参数不经 shell，中文无乱码风险（最佳）"
    } elseif ($low.EndsWith(".cmd")) {
        Warn "形态：npm shim（.cmd）—— 经 cmd.exe 中转"
        Say "       中文 prompt 可能因控制台代码页（936/GBK）乱码。"
        Say "       建议改装原生二进制：irm https://www.codebuddy.cn/cli/install.ps1 | iex"
    } elseif ($low.EndsWith(".ps1")) {
        Warn "形态：npm shim（.ps1）—— 经 PowerShell 中转，受执行策略限制"
        Say "       桥接会用 powershell -ExecutionPolicy Bypass -File 包装它。"
        Say "       建议改装原生二进制：irm https://www.codebuddy.cn/cli/install.ps1 | iex"
    }

    try { & $CbPath --version 2>$null | ForEach-Object { Say "       版本 $_" } } catch { }
} else {
    Bad "没装 codebuddy（只有 IDE 不够）"
    Say ""
    Say "   二选一（PowerShell 里跑）："
    Say "     irm https://www.codebuddy.cn/cli/install.ps1 | iex      ← 推荐，产出 .exe"
    Say "     npm install -g @tencent-ai/codebuddy-code               ← 产出 .cmd/.ps1"
    Say ""
    Say "   装完先跑一次交互式 codebuddy 完成登录，再回来。"
    if ($Check) { exit 1 }
}

# ------------------------------------------------------------------ 白名单
Say ""
Say "-> 工具白名单（决定这个节点能做什么）"
if ($env:CODEBUDDY_ALLOWED_TOOLS) {
    Ok "已配置: $($env:CODEBUDDY_ALLOWED_TOOLS)"
} else {
    Bad "未配置 CODEBUDDY_ALLOWED_TOOLS"
    Say ""
    Say "   这一步必须你自己填 —— 我不知道你公司内网的 CLI 叫什么。"
    Say "   PowerShell 里设（当前窗口临时生效）："
    Say ""
    Say '     $env:CODEBUDDY_ALLOWED_TOOLS = "Bash(projectctl:*),Bash(meetingctl:*),Read,Grep"'
    Say ""
    Say "   永久生效（用户级）："
    Say ""
    Say '     [Environment]::SetEnvironmentVariable("CODEBUDDY_ALLOWED_TOOLS",'
    Say '         "Bash(projectctl:*),Read,Grep", "User")'
}

if ($Check) {
    Say ""
    Say "检查完毕（-Check 模式，未做改动）"
    exit 0
}

# ------------------------------------------------------------------ 目录与文件
Say ""
Say "-> 准备本机文件"
New-Item -ItemType Directory -Force -Path $AgentDir | Out-Null
Ok "凭据目录: $AgentDir"

# ⚠ Windows 的权限语义与 POSIX 不同：没有 chmod 600 这种"只属主可读"。
#   最接近的做法是移除继承并用 icacls 只授予当前用户。
#   但这不是强隔离（管理员仍可读）。所以**真正的保护是不要放明文到磁盘**——
#   而这里必须放，所以额外靠"不加入任何同步目录"来控制。
try {
    & icacls $AgentDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F" 2>$null | Out-Null
    Ok "已限制凭据目录访问（仅当前用户）"
} catch {
    Warn "设置目录权限失败，请手工确认 $AgentDir 不是共享/同步目录"
}

# 凭据
if (Test-Path $CredFile) {
    Ok "凭据已存在: ${CredFile}（不覆盖）"
} else {
    Bad "缺少凭据: $CredFile"
    $envName = "NXS_SECRET_" + ($NodeId.ToUpper() -replace "-", "_")
    Say ""
    Say "   密钥在服务器上（env 里叫 ${envName}）。"
    Say "   在服务器上取："
    Say ""
    Say "     grep $envName /etc/nexus-mesh/nxs.env"
    Say ""
    Say "   然后在本机建 ${CredFile}，内容（四行）："
    Say ""
    Say "     NXS_NODE_ID=$NodeId"
    Say "     NXS_COORDINATOR_URL=$ServerUrl"
    Say "     NXS_CA_FILE=$CaFile"
    Say "     NXS_SECRET=<从服务器取到的值>"
    Say ""
    Say "   ⚠ 不在这里生成新密钥 —— 两边不一致的症状是 AU-SIG-01，"
    Say "     而那看起来像「密钥写错」，实际是「两边不是同一把」。"
}

# ------------------------------------------------------------------ skill
Say ""
Say "-> 安装 skill"
$WbSkills = Join-Path $HOME ".workbuddy\skills\agent-network\nexus-mesh"
$skillSrc = Join-Path $RepoDir "nexus-mesh-skill.md"
if (Test-Path $skillSrc) {
    New-Item -ItemType Directory -Force -Path $WbSkills | Out-Null
    Copy-Item $skillSrc (Join-Path $WbSkills "SKILL.md") -Force
    Ok "已放 WorkBuddy skill: $WbSkills\SKILL.md"
    # CodeBuddy 的 skill 目录是另一个位置，也放一份（两边共用同一份内容）
    $CbSkills = Join-Path $HOME ".codebuddy\skills\nexus-mesh"
    New-Item -ItemType Directory -Force -Path $CbSkills | Out-Null
    Copy-Item $skillSrc (Join-Path $CbSkills "SKILL.md") -Force
    Ok "已放 CodeBuddy skill: $CbSkills\SKILL.md"
} else {
    Bad "找不到 nexus-mesh-skill.md（应在 ${RepoDir}）"
}

# ------------------------------------------------------------------ 计划任务
Say ""
Say "-> 常驻（Windows 计划任务）"
if ($InstallTask) {
    if (-not $Py) { Bad "没有 Python，无法创建任务"; exit 1 }
    $nodeScript = Join-Path $RepoDir "examples\codebuddy_node.py"
    if (-not (Test-Path $nodeScript)) { Bad "找不到 $nodeScript"; exit 1 }

    # pythonw.exe：无控制台窗口。找不到就退回 python.exe（会有黑窗）
    $pyw = $null
    try {
        $pyDir = Split-Path -Parent $Py
        $pywCand = Join-Path $pyDir "pythonw.exe"
        if (Test-Path $pywCand) { $pyw = $pywCand }
    } catch { }
    if (-not $pyw) { $pyw = $Py; Warn "找不到 pythonw.exe，将使用 python.exe（会有黑窗）" }

    $taskName = "NexusMesh-$NodeId"

    # 已存在就先删 —— 否则 Register 会报错，而错误信息不直观
    if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
        Say "   已移除旧任务"
    }

    $action = New-ScheduledTaskAction -Execute $pyw `
        -Argument "`"$nodeScript`"" -WorkingDirectory $RepoDir

    # ⚠ 用 AtLogOn 而不是 AtStartup：
    #   AtStartup 要"无论用户是否登录都运行"，那需要存储账户密码或用 SYSTEM。
    #   公司电脑上两者都不合适（改系统权限 / 存密码）。
    #   代价：**用户注销后进程会停** —— 对"7x24 开机且保持登录"的机器没问题。
    $trigger = New-ScheduledTaskTrigger -AtLogOn

    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -RestartCount 999 `
        -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)     # 0 = 不限时长

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Description "Nexus Mesh 内网执行者节点（${NodeId}）" | Out-Null

    Ok "计划任务已创建: $taskName"
    Say "       启动: Start-ScheduledTask -TaskName $taskName"
    Say "       状态: Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo"
    Say "       日志: 日志写 stderr，计划任务不捕获 —— 用 Start-Process 手工跑一次看输出"
} else {
    Say "   跳过（加 -InstallTask 才会创建）"
    Say ""
    Say "   手工创建的话，用这条："
    Say "     schtasks /create /tn NexusMesh-$NodeId /tr `"`"$pyw`" `"$RepoDir\examples\codebuddy_node.py`"`" /sc onlogon /rl highest"
}

# ------------------------------------------------------------------ 收尾
Say ""
Say "-> 接下来"
Say ""
Say "  1) 设环境变量（永久，用户级）："
Say ""
Say '     [Environment]::SetEnvironmentVariable("NXS_ACTOR_DIR", "' + $AgentDir + '", "User")'
Say '     [Environment]::SetEnvironmentVariable("NXS_COORDINATOR_URL", "' + $ServerUrl + '", "User")'
Say '     [Environment]::SetEnvironmentVariable("NXS_CA_FILE", "' + $CaFile + '", "User")'
Say ""
Say "  2) 确认 CodeBuddy 能用（会消耗一点额度）："
Say ""
Say "     cd $RepoDir"
Say "     & `"$Py`" examples\codebuddy_node.py --check-tools"
Say ""
Say "  3) 注册节点："
Say ""
Say "     & `"$Py`" examples\codebuddy_node.py --dry-run"
Say ""
Say "  4) 在服务器上批准："
Say ""
Say "     nxs ops approve $NodeId --url $ServerUrl --ca-file <服务器上的 ca.pem>"
Say ""
Say "  5) 常驻：重跑本脚本加 -InstallTask"
Say ""
