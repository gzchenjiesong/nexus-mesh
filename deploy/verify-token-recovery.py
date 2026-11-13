"""本机端到端：旧 cookie 撞 401 → 自动恢复页面 → 新 token 进得去。

验证的是**用户真实走的那条路**，不是单元测试里的抽象分支：
  ① 服务重启后旧标签带着过期 cookie 进来 → 401 页面必须告诉他能自恢复
  ② 恢复目标（hub enter）会拿 `?t=<新token>` 回来 → 必须换到 cookie
  ③ 换了 cookie 必须真的能取数据
  ④ SSH 隧道场景不该被跳去公网 hub
"""

import urllib.error
import urllib.request

from nxs.ui import UiServer

SECRET = b"ui-test-secret-0123456789abcdef"
HUB = "https://hub.example.com/api/nexus-enter"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):  # noqa: D102
        return None


def get(url: str, cookie: str = "", host: str = ""):
    req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    if host:
        req.add_header("Host", host)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def main() -> int:
    srv = UiServer("http://127.0.0.1:1/nxs", SECRET, port=0,
                   recover_url=HUB).start()
    try:
        print("① 服务重启后的旧标签（带过期 cookie，经 hub 域名访问）")
        code, _, body = get(f"{srv.base_url}/",
                            cookie="nxs_ui=stale-token-from-before-restart",
                            host="nexus.i-strategy-compass.cn")
        page = body.decode()
        print(f"   HTTP {code}（应 401）")
        print(f"   说清是什么事: {'凭据已失效' in page}")
        print(f"   给自动恢复按钮: {'立即重新进入' in page}")
        print(f"   恢复目标正确: {HUB in page}")
        print(f"   有倒计时反馈: {'正在自动重新进入' in page}")
        print(f"   有防循环保护: {'GUARD_MS' in page and 'sessionStorage' in page}")

        print("\n② hub enter 会带 ?t=<新token> 回来，这里模拟那一步")
        code, headers, _ = get(f"{srv.base_url}/?t={srv.token}",
                              host="nexus.i-strategy-compass.cn")
        cookie = headers.get("Set-Cookie", "")
        print(f"   HTTP {code}（应 303）  Location={headers.get('Location')}")
        print(f"   Set-Cookie: {cookie}")
        print(f"   带 Max-Age: {'Max-Age=' in cookie}")

        print("\n③ 用换到的 cookie 取数据")
        code, _, body = get(f"{srv.base_url}/api/state",
                            cookie=f"nxs_ui={srv.token}",
                            host="nexus.i-strategy-compass.cn")
        print(f"   HTTP {code}（应 200）  —— 恢复后确实进得去")

        print("\n④ SSH 隧道场景（Host=127.0.0.1）不该被跳去公网 hub")
        code, _, body = get(f"{srv.base_url}/", cookie="nxs_ui=stale",
                            host="127.0.0.1:18080")
        page = body.decode()
        print(f"   HTTP {code}（应 401）")
        print(f"   无自动恢复按钮: {'立即重新进入' not in page}")
        print(f"   保留手动命令: {'journalctl' in page}")
        return 0
    finally:
        srv.stop()


if __name__ == "__main__":
    raise SystemExit(main())
