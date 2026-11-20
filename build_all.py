# -*- coding: utf-8 -*-
"""一键出包：升版本号 -> 内嵌页面 -> 打包 exe -> 打包 APK

用法：
    python build_all.py            # 正常流程（版本号自动 +1）
    python build_all.py --no-bump  # 不改版本，只重新打包
    python build_all.py --only-apk # 只出 APK
    python build_all.py --only-exe # 只出 exe（仍会升版本）
"""
import base64
import glob
import gzip
import io
import os
import subprocess
import sys
import textwrap

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)


def sh(cmd):
    print("\n$", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise SystemExit("失败：%s" % " ".join(cmd))


def embed_ui():
    """V8.0.7 教训：直接调 PyInstaller 打包必须先跑 embed_ui()，否则 EXE 内嵌的还是旧 UI（表现为版本号停在旧版）。"""
    raw = open("ui.html", "rb").read()
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=9, mtime=0) as f:
        f.write(raw)
    b = base64.b64encode(buf.getvalue()).decode()
    lines = "\n".join('    "%s"' % c for c in textwrap.wrap(b, 120))
    open("ui_embed.py", "w", encoding="utf-8").write(
        "# -*- coding: utf-8 -*-\n# 内嵌 UI 页面（gzip+base64），由 ui.html 生成，请勿手改\nHTML_B64 = (\n" + lines + "\n)\n")
    print("内嵌页面 %d 字节 -> ui_embed.py" % len(raw))


def spec_file():
    cands = sorted(glob.glob("热榜助手V*.spec"), key=os.path.getmtime)
    if not cands:
        raise SystemExit("找不到 热榜助手V*.spec")
    return cands[-1]


def main():
    args = sys.argv[1:]
    no_bump = "--no-bump" in args
    only_apk = "--only-apk" in args
    only_exe = "--only-exe" in args

    if not no_bump:
        sh([sys.executable, "bump.py"])

    if not only_apk:
        embed_ui()
        sh([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", spec_file()])

    if not only_exe:
        sh([sys.executable, "build_apk.py"])

    ver = open("VERSION", encoding="utf-8").read().strip() if os.path.exists("VERSION") else "?"
    print("\n=== 完成 V%s ===" % ver)
    for p in sorted(glob.glob(os.path.join("dist", "热榜助手V%s.*" % ver))):
        print("  %s  %.1f MB" % (p, os.path.getsize(p) / 1024.0 / 1024.0))


if __name__ == "__main__":
    main()
