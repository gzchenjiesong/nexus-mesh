"""把 401 页面导出成静态文件，供浏览器实测自动跳转。"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from nxs.ui.page import unauthorized_page

out = pathlib.Path("/tmp/nxs-401")
out.mkdir(parents=True, exist_ok=True)
(out / "page.html").write_text(
    unauthorized_page("https://hub.example.com/api/nexus-enter"),
    encoding="utf-8")
print("已写出", out / "page.html")
