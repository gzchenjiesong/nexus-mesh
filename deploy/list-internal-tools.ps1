<#
=============================================================================
 list-internal-tools.ps1 —— 找出这台机器上可能用于访问内部系统的 CLI
=============================================================================
 用途：填 CODEBUDDY_ALLOWED_TOOLS 之前，先搞清楚有哪些命令行工具可用。
       **只读**，不执行任何被发现的程序。

 背景：我给的示例里写了 `projectctl`，那是我编的假名字 —— 它看起来像真的，
       很容易被照抄。所以不给例子了，直接扫这台机器实际的。

 用法：
   powershell -ExecutionPolicy Bypass -File deploy\list-internal-tools.ps1

   只看最可能的（默认）：
   powershell -ExecutionPolicy Bypass -File deploy\list-internal-tools.ps1

   列出 PATH 里所有非常见可执行文件（较吵，用于兜底）：
   powershell -ExecutionPolicy Bypass -File deploy\list-internal-tools.ps1 -All
=============================================================================
#>

[CmdletBinding()]
param(
    [switch]$All
)

$ErrorActionPreference = "SilentlyContinue"

function Head($t) { Write-Host ""; Write-Host "=== $t ===" -ForegroundColor Cyan }
function Line { Write-Host ("-" * 66) -ForegroundColor DarkGray }

# ---------------------------------------------------------------- 扫描
# 这些是系统/开发环境自带的，不是"内部业务工具"，默认不列
$commonNames = @(
    # Windows 自带
    "cmd","powershell","pwsh","wscript","cscript","mshta","reg","regedit","whoami",
    "where","find","findstr","sort","more","tree","xcopy","robocopy","attrib","icacls",
    "cmdkey","certutil","bitsadmin","schtasks","sc","net","netstat","ipconfig","ping",
    "tracert","nslookup","tasklist","taskkill","systeminfo","ver","set","path","type",
    "copy","move","del","ren","dir","md","rd","cd","echo","cls","exit","title","color",
    "curl","tar","expand","compact","cipher","diskpart","format","chkdsk","sfc","wmic",
    "msiexec","rundll32","regsvr32","control","explorer","notepad","calc","mspaint",
    # 开发工具
    "git","node","npm","npx","yarn","pnpm","bun","deno","python","python3","py","pip",
    "pip3","uv","poetry","conda","java","javac","mvn","gradle","go","rustc","cargo",
    "dotnet","ruby","gem","php","perl","lua","Rscript","cmake","make","ninja","gcc",
    "g++","clang","cl","msbuild","nuget","docker","docker-compose","kubectl","helm",
    "terraform","ansible","vagrant","ssh","scp","sftp","ftp","telnet","openssl",
    "codebuddy","code","code-insiders","buddy","buddycn","hermes","openclaw",
    "jq","yq","grep","sed","awk","rg","fd","bat","less","vim","nvim","nano","emacs",
    "7z","zip","unzip","rar","gzip","which","gh","glab","svn","hg","sqlite3","psql",
    "mysql","mongo","redis-cli","aws","az","gcloud","aliyun","tccli","ffmpeg"
)

#: 名字里出现这些词，大概率是内部业务工具
$hints = @(
    "project","task","meeting","doc","wiki","oa","erp","crm","hr","pm","issue",
    "jira","tapd","zentao","confluence","feishu","lark","wecom","wework","dingtalk",
    "gitlab","github","git","repo","code","review","pipeline","ci","build","deploy",
    "report","stat","data","query","search","index","kibana","grafana","monitor",
    "asset","cmdb","ticket","approval","flow","workflow","salary","budget","finance"
)

$candidates = @()
foreach ($dir in ($env:PATH -split ";")) {
    if (-not $dir -or -not (Test-Path -LiteralPath $dir)) { continue }
    foreach ($f in (Get-ChildItem -LiteralPath $dir -File -Filter "*.exe" -ErrorAction SilentlyContinue)) {
        $base = [System.IO.Path]::GetFileNameWithoutExtension($f.Name).ToLower()
        if ($commonNames -contains $base) { continue }
        if ($base -match '^(unins|setup|install|update|vc_redist|api-ms-win|ucrtbase|msvcp\d)') { continue }

        $hit = $false
        foreach ($h in $hints) {
            if ($base -like "*$h*") { $hit = $true; break }
        }
        if ($hit -or $All) {
            $candidates += [pscustomobject]@{
                Name = $base
                Path = $f.FullName
                Dir  = $dir
                Hit  = $hit
            }
        }
    }
}

# ---------------------------------------------------------------- 分组输出
Head "① 名字像业务工具的可执行文件"

$likely = $candidates | Where-Object { $_.Hit } | Sort-Object Name -Unique
if ($likely) {
    foreach ($c in $likely) {
        Write-Host ("   {0}" -f $c.Name) -ForegroundColor Green
        Write-Host ("      {0}" -f $c.Path) -ForegroundColor DarkGray
    }
    Write-Host ""
    Write-Host "   ↑ 这些是**候选**。逐个确认它真的能查你要的数据：" -ForegroundColor Yellow
    foreach ($c in $likely) {
        Write-Host ("     & '{0}' --help" -f $c.Path) -ForegroundColor Yellow
    }
} else {
    Write-Host "   没有找到名字像业务工具的可执行文件。" -ForegroundColor Yellow
    Write-Host "   可能是：内部系统只提供网页/HTTP API，没有 CLI。" -ForegroundColor Yellow
}

if ($All) {
    Head "② PATH 里所有非常见可执行文件（-All）"
    $others = $candidates | Where-Object { -not $_.Hit } | Sort-Object Name -Unique
    if ($others) {
        foreach ($c in $others) {
            Write-Host ("   {0}" -f $c.Name) -ForegroundColor Gray
        }
    } else {
        Write-Host "   （无）"
    }
}

# ---------------------------------------------------------------- 环境线索
Head "③ 环境变量里的线索"
$envHits = @()
foreach ($e in (Get-ChildItem Env:)) {
    $n = $e.Name
    if ($n -match '(?i)(project|task|meeting|jira|tapd|zentao|feishu|lark|wecom|dingtalk|oa_|erp|crm|gitlab|token|api_?key)') {
        # 值可能含密钥，只显示名字和长度
        $envHits += ("{0}  (长度 {1})" -f $n, ([string]$e.Value).Length)
    }
}
if ($envHits) {
    $envHits | Sort-Object -Unique | ForEach-Object { Write-Host "   $_" -ForegroundColor Green }
    Write-Host ""
    Write-Host "   ⚠ 变量名里有 token/api_key 的，值可能含凭据。" -ForegroundColor Yellow
    Write-Host "     不要把它们贴到任何地方 —— 包括这个脚本的输出。" -ForegroundColor Yellow
} else {
    Write-Host "   没有明显相关的环境变量。"
}

# ---------------------------------------------------------------- 已装的应用
Head "④ 已安装程序里的线索（可能带 CLI）"
$appDirs = @(
    "$env:ProgramFiles",
    "${env:ProgramFiles(x86)}",
    "$env:LOCALAPPDATA\Programs"
)
$appHits = @()
foreach ($d in $appDirs) {
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
    foreach ($sub in (Get-ChildItem -LiteralPath $d -Directory -ErrorAction SilentlyContinue)) {
        $n = $sub.Name.ToLower()
        foreach ($h in $hints) {
            if ($n -like "*$h*") { $appHits += $sub.FullName; break }
        }
    }
}
if ($appHits) {
    $appHits | Sort-Object -Unique | ForEach-Object { Write-Host "   $_" -ForegroundColor Green }
} else {
    Write-Host "   没有明显的业务系统客户端。"
}

# ---------------------------------------------------------------- 结论
Head "结论"
if ($likely.Count -gt 0) {
    Write-Host "  找到了候选 CLI。下一步：" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   1. 逐个试：& '<路径>' --help  （看清它能不能查项目/会议数据）"
    Write-Host "   2. 确认后设白名单（只放真正要用的那一个）："
    Write-Host ""
    $first = ($likely | Select-Object -First 1)
    Write-Host ("      `$env:CODEBUDDY_ALLOWED_TOOLS = `"Bash({0}:*),Read,Grep`"" -f $first.Name) -ForegroundColor Yellow
    Write-Host ""
    Write-Host "   3. 再跑 setup-work-node.ps1 -Check 确认校验通过"
} else {
    Write-Host "  没有找到现成的业务 CLI。三条路：" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "   A) 内部系统有 HTTP API → 自己写一个小查询脚本"
    Write-Host "      放在固定路径，白名单写："
    Write-Host ""
    Write-Host "        `$env:CODEBUDDY_ALLOWED_TOOLS = \"Bash(python:D:/nexus-mesh/internal/query.py:*),Read\""
    Write-Host ""
    Write-Host "      ⚠ 不要写 Bash(python:*) —— 那等于允许执行任意 Python 代码。"
    Write-Host "        要限定到具体脚本路径。"
    Write-Host ""
    Write-Host "   B) 只有网页界面 → 需要走浏览器自动化，工作量大得多"
    Write-Host "      而且要处理登录态，不建议作为第一步"
    Write-Host ""
    Write-Host "   C) 有数据库只读账号 → 写 SQL 查询脚本，白名单同上限定到脚本"
}

Line
Write-Host "本脚本只读，没有执行任何被发现的程序。" -ForegroundColor DarkGray
