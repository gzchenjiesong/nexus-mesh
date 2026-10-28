# ==============================================================================
#  setup-work-laptop.ps1 —— 在 Windows 公司电脑上准备 Nexus Mesh 执行者节点
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
#      powershell -ExecutionPolicy Bypass -File setup-work-laptop.ps1 -Check
#      powershell -ExecutionPolicy Bypass -File setup-work-laptop.ps1
#
#  ⚠ 如果你的终端显示中文乱码，先执行：
#      chcp 65001
#
# ==============================================================================

[CmdletBinding()]
param(
    [switch]$Check,                 # 只检查，不改动
    [switch]$InstallTask,           # 顺便创建计划任务（常驻）
    [string]$NodeId = $(if ($env:NXS_NODE_ID) { $env:NXS_NODE_ID } else { "work-laptop" }),
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

# ------------------------------------------------------------------ 网络
Say ""
Say "-> 网络（公司网络可能拦出站）"
$healthOk = $false
try {
    if (Test-Path $CaFile) {
        # PowerShell 5.1 的 Invoke-WebRequest 不能直接用自签 CA，走 curl.exe
        $code = & curl.exe -sS --cacert $CaFile --max-time 10 `
            -o NUL -w "%{http_code}" "$ServerUrl/health" 2>$null
    } else {
        $code = & curl.exe -sS -k --max-time 10 `
            -o NUL -w "%{http_code}" "$ServerUrl/health" 2>$null
    }
    $healthOk = ($code -eq "200")
} catch { $healthOk = $false }

if ($healthOk) {
    Ok "协调节点可达（HTTP 200）"
} else {
    Bad "协调节点不可达"
    Say ""
    Say "   排查顺序："
    Say "     1. Test-NetConnection $($Server.Split(':')[0]) -Port $($Server.Split(':')[1])"
    Say "     2. 公司是否有 HTTP 代理：echo `$env:HTTPS_PROXY"
    Say "     3. 企业 TLS 中间人会换证书 —— 那自签 CA 一定验不过，见文档"
    if ($Check) { exit 1 }
}

# ------------------------------------------------------------------ codebuddy
Say ""
Say "-> CodeBuddy CLI（执行者靠它，与 IDE 是两个独立软件）"
$CbPath = $null
$cand = Get-Command codebuddy -ErrorAction SilentlyContinue
if ($cand) { $CbPath = $cand.Source }
if (-not $CbPath) {
    # 原生二进制安装位置
    foreach ($p in @(
        (Join-Path $env:LOCALAPPDATA "codebuddy\bin\codebuddy.exe"),
        (Join-Path $HOME "AppData\Roaming\npm\codebuddy.cmd")
    )) {
        if (Test-Path $p) { $CbPath = $p; break }
    }
}
if ($CbPath) {
    Ok "codebuddy -> $CbPath"
    try { & $CbPath --version 2>$null | ForEach-Object { Say "        $_" } } catch { }
} else {
    Bad "没装 codebuddy（只有 IDE 不够）"
    Say ""
    Say "   二选一（PowerShell 里跑）："
    Say "     irm https://www.codebuddy.cn/cli/install.ps1 | iex"
    Say "     npm install -g @tencent-ai/codebuddy-code"
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

# CA
if (-not (Test-Path $CaFile)) {
    $localCa = Join-Path $HOME "nexus-mesh-deploy\ca.pem"
    if (Test-Path $localCa) {
        Copy-Item $localCa $CaFile
        Ok "CA 已复制（来自 ~\nexus-mesh-deploy）"
    } else {
        Bad "找不到 ca.pem"
        Say "     从服务器取："
        Say "       scp root@$($Server.Split(':')[0]):/etc/nexus-mesh/tls/ca.pem `"$CaFile`""
    }
} else {
    Ok "CA 已存在: $CaFile"
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
