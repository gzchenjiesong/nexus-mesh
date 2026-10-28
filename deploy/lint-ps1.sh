#!/usr/bin/env bash
# ==============================================================================
#  lint-ps1.sh —— 用**真实的 PowerShell 解析器**检查所有 .ps1
# ==============================================================================
#
#  ⚠ 为什么需要这个
#
#  我之前用 Python 写了几百行静态检查（BOM、引号配对、变量赋值、括号嵌套），
#  测试全绿 —— 但里面一直藏着三个真正的语法/运行错误：
#
#    · param 块里漏了一个逗号        → 语法错误，脚本根本解析不了
#    · $env:TEMP 为 null 时 Join-Path → 运行时崩
#    · '\\b(...)'  在单引号里不是单词边界 → 正则永远匹配不到（静默误报）
#
#  静态检查抓不到这些。**能抓到它们的只有解析器本身。**
#
#  而一旦有了真实解析器，跑一遍只要 2 秒 —— 没有任何理由不做。
#
#  用法：
#      bash deploy/lint-ps1.sh            # 检查（没有 pwsh 会自动下载）
#      bash deploy/lint-ps1.sh --no-fetch # 只检查，不下载
#
#  退出码：0 = 全部通过；1 = 有语法错误
# ==============================================================================

set -uo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PWSH_CACHE="${PWSH_CACHE:-/tmp/nxs-pwsh}"
NO_FETCH=0

for arg in "$@"; do
  case "$arg" in
    --no-fetch) NO_FETCH=1 ;;
    -h|--help) sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
bad()  { printf '  ✗ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }

# ------------------------------------------------------------------ 找 pwsh
find_pwsh() {
  if command -v pwsh >/dev/null 2>&1; then
    command -v pwsh
    return 0
  fi
  if [ -x "$PWSH_CACHE/pwsh" ]; then
    echo "$PWSH_CACHE/pwsh"
    return 0
  fi
  return 1
}

PWSH="$(find_pwsh || true)"

if [ -z "$PWSH" ]; then
  if [ "$NO_FETCH" = "1" ]; then
    warn "找不到 pwsh，且指定了 --no-fetch"
    say "     跳过真实语法检查（只做了文件存在性检查）"
    exit 0
  fi

  say "→ 本机没有 pwsh，尝试下载（约 68 MB，只下一次）"
  mkdir -p "$PWSH_CACHE"

  # 选架构
  case "$(uname -m)" in
    arm64|aarch64) ARCH="arm64" ;;
    x86_64|amd64)  ARCH="x64" ;;
    *) warn "未知架构 $(uname -m)，跳过"; exit 0 ;;
  esac

  case "$(uname -s)" in
    Darwin) OS="osx" ;;
    Linux)  OS="linux" ;;
    *) warn "未知系统 $(uname -s)，跳过"; exit 0 ;;
  esac

  # ⚠ 不用 GitHub API 查最新版 —— 它有速率限制，未认证时经常 403。
  #   试几个已知版本，哪个能下就用哪个。
  VER=""
  for V in 7.5.4 7.4.7 7.4.6; do
    URL="https://github.com/PowerShell/PowerShell/releases/download/v${V}/powershell-${V}-${OS}-${ARCH}.tar.gz"
    CODE=$(curl -sIL -o /dev/null -w "%{http_code}" --max-time 20 "$URL" 2>/dev/null || echo "000")
    if [ "$CODE" = "200" ]; then VER="$V"; break; fi
  done

  if [ -z "$VER" ]; then
    warn "下载失败（网络受限？）。跳过真实语法检查。"
    say "     你也可以自己装：brew install powershell"
    exit 0
  fi

  URL="https://github.com/PowerShell/PowerShell/releases/download/v${VER}/powershell-${VER}-${OS}-${ARCH}.tar.gz"
  say "     下载 PowerShell ${VER} (${OS}-${ARCH})"
  if ! curl -sL --max-time 300 -o "$PWSH_CACHE/pwsh.tar.gz" "$URL"; then
    warn "下载中断，跳过"
    exit 0
  fi
  if ! tar xzf "$PWSH_CACHE/pwsh.tar.gz" -C "$PWSH_CACHE" 2>/dev/null; then
    warn "解压失败，跳过"
    exit 0
  fi
  chmod +x "$PWSH_CACHE/pwsh" 2>/dev/null || true
  PWSH="$PWSH_CACHE/pwsh"

  if [ ! -x "$PWSH" ]; then
    warn "下载后仍不可执行，跳过"
    exit 0
  fi
  ok "$($PWSH --version 2>&1 | head -1)"
fi

say "→ 解析器：$PWSH"
say ""

# ------------------------------------------------------------------ 检查
# 用 PowerShell 自己的 Parser.ParseFile —— 与 IDE 里报的错完全一致。
#
# ⚠ 不用 `pwsh -File` 执行脚本：那会真的跑起来（有副作用，比如改环境变量）。
#   Parser 只解析不执行。
# ⚠ 路径通过**环境变量**传，不用 `$args[0]`。
#   实测：`pwsh -Command '<script>' arg1` 里 $args 是空的 ——
#   于是 Get-ChildItem 收到 null 目录，报
#     Cannot bind argument to parameter 'Path' because it is null
#   而脚本接着还打了"全部通过" —— **假通过**。
#   一个用来查错的工具自己犯了它要查的错，这必须修。
export NXS_LINT_DIR="$REPO_DIR"

"$PWSH" -NoProfile -Command '
$repo = $env:NXS_LINT_DIR
if (-not $repo) {
    Write-Host "  ✗ 内部错误：NXS_LINT_DIR 未传进来"
    exit 2
}
$deployDir = Join-Path $repo "deploy"
if (-not (Test-Path $deployDir)) {
    Write-Host "  ✗ 找不到目录：$deployDir"
    exit 2
}
$files = Get-ChildItem -Path $deployDir -Filter "*.ps1" -File | Sort-Object Name
if (-not $files) { Write-Host "  （deploy/ 下没有 .ps1）"; exit 0 }

$fail = 0
foreach ($f in $files) {
    $tokens = $null
    $errors = $null
    [System.Management.Automation.Language.Parser]::ParseFile(
        $f.FullName, [ref]$tokens, [ref]$errors) | Out-Null

    if ($errors.Count -eq 0) {
        Write-Host ("  ✓ {0}  ({1} tokens)" -f $f.Name, $tokens.Count)
    } else {
        $fail++
        Write-Host ("  ✗ {0}  ({1} 个错误)" -f $f.Name, $errors.Count)
        foreach ($e in $errors) {
            Write-Host ("      行 {0} 列 {1}: {2}" -f `
                $e.Extent.StartLineNumber, $e.Extent.StartColumnNumber, $e.Message)
        }
    }
}

Write-Host ""
if ($fail -gt 0) {
    Write-Host ("  共 {0} 个文件有语法错误" -f $fail)
    exit 1
} else {
    Write-Host ("  全部通过（{0} 个文件）" -f $files.Count)
}
'
rc=$?

if [ $rc -ne 0 ]; then
  bad "有语法错误 —— 上面列了行号，直接改"
  exit 1
fi
ok "所有 .ps1 语法正确"
