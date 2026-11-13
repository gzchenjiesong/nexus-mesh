"""生产验证：半开连接不得冻结网关。

在服务器上跑（对 127.0.0.1:18800）。这是那场 13 天事故的现场复验。

用法：python3 /root/nexus-mesh-deploy/verify-tls-resilience.py
"""

import socket
import ssl
import time
import urllib.request

PORT = 18800
URL = "https://127.0.0.1:18800/nxs/health"


def health(timeout: float = 5.0):
    """请求 /health，返回 (状态码, 耗时毫秒)。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(URL)
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.status, (time.time() - started) * 1000


def main() -> int:
    print("  ① 基线")
    status, ms = health()
    print(f"     {status} · {ms:.0f}ms")

    print("  ② 建 3 个半开连接（TCP 通、不发 ClientHello）")
    silent = []
    for _ in range(3):
        silent.append(socket.create_connection(("127.0.0.1", PORT), timeout=5))
    time.sleep(0.5)

    results = []
    for i in (1, 2):
        try:
            status, ms = health(timeout=10)
            fast = ms < 2000
            print(f"  ③.{i} 攻击期间请求: {status} · {ms:.0f}ms"
                  f"  {'✓' if fast else '← 慢（在等握手超时）'}")
            results.append(status == 200 and fast)
        except Exception as exc:                       # noqa: BLE001
            print(f"  ③.{i} 攻击期间请求: 失败 {type(exc).__name__}: {exc}")
            results.append(False)

    for sock in silent:
        sock.close()
    time.sleep(1)

    status, ms = health()
    print(f"  ④ 释放后: {status} · {ms:.0f}ms")

    print()
    if all(results):
        print("  ✓ 修复生效：半开连接不阻塞其他请求")
        return 0
    print("  ✗ 仍被阻塞 —— 修复未生效")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
