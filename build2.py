# -*- coding: utf-8 -*-
"""热榜助手 · 一键打包 Windows exe

用法（在本目录下执行）：
    python build2.py                 # 按 VERSION 里的版本号打包，输出 dist/热榜助手V<版本>.exe
    python build2.py --no-embed      # 跳过「由 ui2.html 重生成 ui_embed.py」这一步（不推荐）

依赖：pip install pyinstaller          （离线翻译功能另需 ctranslate2 / sentencepiece / numpy）
说明：
  1. 界面在 ui2.html，打包时会被压缩成 base64 写进 ui_embed.py，脚本里必须能 import 到它；
     所以**每次改完 ui2.html 都要重新打包**，否则 exe 里还是旧页面。
  2. 图标用 ic_launcher.ico（与安卓端同一张图，16~256 多尺寸）。
  3. 想指定输出目录可用环境变量 REBANG_DIST，例如 REBANG_DIST=D:/out。
"""
import base64
import gzip
import io
import os
import subprocess
import sys
import textwrap

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)

HTML = "ui2.html"
ICON = "ic_launcher.ico"


def embed_ui():
    """把 ui2.html 压成 gzip+base64 存进 ui_embed.py（app2.load_page 优先用它）"""
    raw = open(HTML, "rb").read()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(raw)
    b64 = base64.b64encode(buf.getvalue()).decode()
    body = "\n".join('    "%s"' % c for c in textwrap.wrap(b64, 120))
    open("ui_embed.py", "w", encoding="utf-8").write(
        "# -*- coding: utf-8 -*-\n"
        "# 内嵌界面（gzip + base64），由 %s 自动生成，请勿手改\n"
        "HTML_B64 = (\n%s\n)\n" % (HTML, body))
    print("内嵌页面 %d 字节 -> ui_embed.py（%d 字节）" % (len(raw), os.path.getsize("ui_embed.py")))


def version():
    try:
        v = open("VERSION", encoding="utf-8").read().strip()
    except OSError:
        v = ""
    return v or "0.0.0"


def hidden_imports():
    """能用才带上（离线翻译链），缺了也不影响主功能"""
    out = []
    for m in ("ctranslate2", "sentencepiece", "numpy"):
        try:
            __import__(m)
            out += ["--hidden-import", m]
        except Exception:
            print("提示：没装 %s，跳过（离线翻译不可用，其它功能正常）" % m)
    return out


def main():
    ver = version()
    name = "热榜助手V%s" % ver
    dist = os.environ.get("REBANG_DIST") or "dist"

    if "--no-embed" not in sys.argv:
        embed_ui()

    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
           "--onefile", "--noconsole",
           "--name", name,
           "--distpath", dist,
           "--workpath", os.path.join("_build", "pyinstaller"),
           "--specpath", os.path.join("_build", "spec")]
    icon = os.path.join(BASE, ICON)          # 必须绝对路径：--specpath 会把相对路径带跑偏
    if os.path.exists(icon):
        cmd += ["--icon", icon]
    cmd += hidden_imports() + ["app2.py"]

    print("\n$", " ".join(cmd))
    if subprocess.run(cmd).returncode != 0:
        raise SystemExit("打包失败")

    exe = os.path.join(dist, name + ".exe")
    print("\n=== 完成 %s  %.1f MB ===" % (exe, os.path.getsize(exe) / 1048576.0))


if __name__ == "__main__":
    main()
