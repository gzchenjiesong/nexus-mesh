#!/usr/bin/env bash
# ==============================================================================
#  gen-tls.sh —— 生成 Nexus Mesh 的 CA 与服务端证书
# ==============================================================================
#
#  ⚠ 为什么必须用脚本，不能手敲命令
#
#  这套证书最初是我在服务器上手工跑 openssl 生成的，结果 **CA 漏了 keyUsage
#  扩展**。后果：
#
#    · Python 3.13（OpenSSL 3.5）默认开 VERIFY_X509_STRICT
#      → 「CA cert does not include key usage extension」，连不上
#    · Windows 的 curl（schannel 后端）会做吊销检查
#      → 「the revocation status is unknown」，也连不上
#
#  同一个缺陷被两个不同的验证器以两种措辞报出来，都看不出根因。
#  手敲命令的必然结果就是漏一两行 —— 所以写死在这里。
#
#  ==============================================================================
#  ⚠⚠ 重新生成 CA 会让**所有现有证书失效**
#  ==============================================================================
#
#  CA 换了之后，服务端证书必须重签，且所有节点都要拿新 CA。
#  所以本脚本默认**只检查不生成**，加 --force 才会真的覆盖。
#
#  用法：
#      bash deploy/gen-tls.sh --check            # 只看现状（默认）
#      bash deploy/gen-tls.sh --force --dir DIR  # 真的生成（会覆盖）
#      bash deploy/gen-tls.sh --force --server-ip 1.2.3.4 --dir /etc/nexus-mesh/tls
#
#  生成物：
#      <dir>/ca.pem        自签 CA（公开）
#      <dir>/ca.key        CA 私钥（**绝不外传**）
#      <dir>/server.pem    服务端证书（公开）
#      <dir>/server.key    服务端私钥（**绝不外传**）
# ==============================================================================

set -euo pipefail

DIR="/etc/nexus-mesh/tls"
SERVER_IP="${NXS_SERVER_IP:-43.138.222.206}"
DAYS_CA="${DAYS_CA:-3650}"
DAYS_SERVER="${DAYS_SERVER:-825}"
#: 服务运行组。证书要让它可读 —— 否则服务起不来（见下方说明）。
SERVICE_GROUP="${SERVICE_GROUP:-nxs}"
FORCE=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir)       DIR="$2"; shift 2 ;;
    --server-ip) SERVER_IP="$2"; shift 2 ;;
    --force)     FORCE=1; shift ;;
    --check)     FORCE=0; shift ;;
    -h|--help)   sed -n '2,36p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 2 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
bad()  { printf '  ✗ %s\n' "$*"; }
warn() { printf '  ⚠ %s\n' "$*"; }

CA_PEM="$DIR/ca.pem"
CA_KEY="$DIR/ca.key"
SRV_PEM="$DIR/server.pem"
SRV_KEY="$DIR/server.key"

# ------------------------------------------------------------------ 检查
say "→ 目标目录: $DIR"
say "  服务端 IP: $SERVER_IP"

if [[ -f "$CA_PEM" ]]; then
  say ""
  say "→ 现有 CA"
  openssl x509 -in "$CA_PEM" -noout -subject -dates 2>/dev/null | sed 's/^/    /'

  # ★ 核心检查：有没有 keyUsage
  #
  # 这是本脚本存在的理由。缺这条扩展时：
  #   Python 3.13+  → CERTIFICATE_VERIFY_FAILED（缺 keyUsage）
  #   Windows curl  → schannel revocation unknown
  # 两种报错都看不出是这里的问题。
  if openssl x509 -in "$CA_PEM" -noout -text 2>/dev/null | grep -q "X509v3 Key Usage"; then
    ok "CA 有 Key Usage 扩展（合规）"
    openssl x509 -in "$CA_PEM" -noout -text 2>/dev/null \
      | grep -A1 "X509v3 Key Usage" | tail -1 | sed 's/^/      /'
  else
    bad "CA **缺少 Key Usage 扩展** —— 严格验证器会拒绝它"
    say ""
    say "      受影响："
    say "        · Python 3.13+（默认 VERIFY_X509_STRICT）"
    say "        · Windows 的 curl（schannel 后端）"
    say ""
    say "      修法：加 --force 重新生成（会让所有现有证书失效）"
  fi

  if openssl x509 -in "$CA_PEM" -noout -text 2>/dev/null | grep -q "CA:TRUE"; then
    ok "CA 有 Basic Constraints CA:TRUE"
  else
    bad "CA 缺少 Basic Constraints CA:TRUE"
  fi
fi

if [[ -f "$SRV_PEM" ]]; then
  say ""
  say "→ 现有服务端证书"
  openssl x509 -in "$SRV_PEM" -noout -subject -dates 2>/dev/null | sed 's/^/    /'
  san=$(openssl x509 -in "$SRV_PEM" -noout -ext subjectAltName 2>/dev/null | tail -1 | xargs || true)
  if [[ -n "$san" ]]; then ok "SAN: $san"; else bad "缺 SAN（客户端会校验主机名失败）"; fi
  if openssl x509 -in "$SRV_PEM" -noout -text 2>/dev/null | grep -q "TLS Web Server Authentication"; then
    ok "有 EKU serverAuth"
  else
    bad "缺 EKU serverAuth"
  fi
fi

if [[ "$FORCE" == "0" ]]; then
  say ""
  say "检查完毕（未改动）。要重新生成加 --force —— 那会让所有现有证书失效。"
  exit 0
fi

# ------------------------------------------------------------------ 生成
mkdir -p "$DIR"
# ⚠ 目录必须是 755，不能是 700。
#
# 实测踩过：初版写 chmod 700，结果**服务起不来** ——
# 协调节点以 User=root / Group=nxs 运行，700 目录它进不去，报
#   FAIL 网关启动失败：TLS 证书加载失败: [Errno 13] Permission denied
# 而错误信息里没说是哪个文件、哪种权限。
chmod 755 "$DIR"

STAMP=$(date +%Y%m%d-%H%M%S)
for f in "$CA_PEM" "$CA_KEY" "$SRV_PEM" "$SRV_KEY"; do
  [[ -f "$f" ]] && cp "$f" "$f.bak-$STAMP" && say "  已备份 $f.bak-$STAMP"
done

CFG=$(mktemp)
trap 'rm -f "$CFG"' EXIT

# ★ 扩展配置必须写死在这里
#
# CA 侧最容易漏的就是 keyUsage。RFC 5280 里它算"RECOMMENDED"而非 MUST，
# 所以 openssl 不写也能出证书 —— 但现代验证器都要求它。
#
#   keyCertSign  必须 —— 没它就不能签下级证书（严格模式直接拒）
#   cRLSign      有 CRL 时需要
#   critical     必须标 —— 让不认识这条扩展的验证器直接拒绝，而不是忽略
cat > "$CFG" <<EOF
[ req ]
distinguished_name = dn
prompt             = no

[ dn ]
CN = Nexus Mesh Internal CA
O  = nexus-mesh

[ v3_ca ]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints       = critical, CA:true
keyUsage               = critical, digitalSignature, cRLSign, keyCertSign

[ v3_server ]
subjectKeyIdentifier   = hash
authorityKeyIdentifier = keyid,issuer
basicConstraints       = critical, CA:false
keyUsage               = critical, digitalSignature, keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt

[ alt ]
IP.1  = $SERVER_IP
IP.2  = 127.0.0.1
DNS.1 = localhost
EOF

say ""
say "→ 生成 CA"
openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$CA_KEY" -out "$CA_PEM" \
  -days "$DAYS_CA" -config "$CFG" -extensions v3_ca 2>/dev/null
chmod 600 "$CA_KEY"
chmod 644 "$CA_PEM"
ok "ca.pem / ca.key"

say "→ 生成服务端证书"
openssl req -newkey rsa:2048 -nodes \
  -keyout "$SRV_KEY" -out "$DIR/server.csr" \
  -subj "/CN=$SERVER_IP/O=nexus-mesh" 2>/dev/null
chmod 600 "$SRV_KEY"

openssl x509 -req -in "$DIR/server.csr" \
  -CA "$CA_PEM" -CAkey "$CA_KEY" -CAcreateserial \
  -out "$SRV_PEM" -days "$DAYS_SERVER" \
  -extfile "$CFG" -extensions v3_server 2>/dev/null
rm -f "$DIR/server.csr" "$DIR/ca.srl"
chmod 644 "$SRV_PEM"
ok "server.pem / server.key"

# ------------------------------------------------------------------ 自检
say ""
say "→ 自检（用 OpenSSL 自己验一遍）"
if openssl verify -CAfile "$CA_PEM" "$SRV_PEM" >/dev/null 2>&1; then
  ok "服务端证书能被 CA 验证"
else
  bad "证书链验证失败 —— 生成过程有问题，别急着部署"
fi

say ""
say "→ 关键扩展确认"
openssl x509 -in "$CA_PEM" -noout -text | grep -A1 "X509v3 Key Usage" | tail -1 | sed 's/^/    CA keyUsage: /'
openssl x509 -in "$SRV_PEM" -noout -text | grep -A1 "X509v3 Key Usage" | tail -1 | sed 's/^/    SRV keyUsage: /'
openssl x509 -in "$SRV_PEM" -noout -ext subjectAltName 2>/dev/null | tail -1 | sed 's/^/    SAN: /'

# ------------------------------------------------------------------ 权限
#
# ⚠ **这一步错了服务就起不来**，而且报错只有一句
#   "TLS 证书加载失败: [Errno 13] Permission denied"，不说是哪个文件。
#
# 规则：
#   ca.pem      644 root:<服务组>   公开，服务与节点都要读
#   server.pem  644 root:<服务组>   公开
#   server.key  640 root:<服务组>   **服务进程要读** → 组必须可读
#   ca.key      600 root            签名用的，服务不需要，绝不能给组
#
# 初版把 server.key 设成 600（只有 root 能读），而服务是 root:nxs 运行、
# 靠组权限读文件 —— 于是拒绝。用 640 才对。
say ""
say "→ 设置权限（服务组: ${SERVICE_GROUP}）"
if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
  chown root:"$SERVICE_GROUP" "$CA_PEM" "$SRV_PEM" "$SRV_KEY"
  chmod 644 "$CA_PEM" "$SRV_PEM"
  chmod 640 "$SRV_KEY"
  chmod 600 "$CA_KEY"
  ok "ca.pem 644 · server.pem 644 · server.key 640 · ca.key 600"
  ok "属组 root:$SERVICE_GROUP"
else
  warn "系统里没有 $SERVICE_GROUP 组 —— 跳过属组设置"
  warn "若服务以别的用户/组运行，手工 chown 后再启动"
  chmod 644 "$CA_PEM" "$SRV_PEM"
  chmod 640 "$SRV_KEY"
  chmod 600 "$CA_KEY"
fi

SHA=$(openssl x509 -in "$CA_PEM" -noout -fingerprint -sha256 2>/dev/null | sed 's/.*=//')
say ""
say "→ CA 指纹（分发给各节点时核对用）"
say "    $SHA"
say ""
say "→ 接下来（会中断服务，按顺序做）"
say "    1. 重启协调节点：systemctl restart nexus-mesh"
say "    2. 重启各桥接：  systemctl restart nexus-mesh-hermes nexus-mesh-taskflow"
say "    3. 把新 ca.pem 复制回仓库：cp $CA_PEM <repo>/deploy/ca.pem"
say "    4. 各工作节点重新拉取仓库（或用部署脚本自动复制）"
say "    5. 验证：nxs ops nodes  应能看到全部节点 alive"
