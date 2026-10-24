# -*- coding: utf-8 -*-
"""V4.9 打包脚本"""
import base64, gzip, os, subprocess, sys
HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "热榜助手V6.0"

def step_embed():
    with open(os.path.join(HERE, "ui2.html"), "rb") as f:
        b64 = base64.b64encode(gzip.compress(f.read(), 9)).decode("ascii")
    with open(os.path.join(HERE, "ui_embed.py"), "w", encoding="utf-8") as f:
        f.write('# 自动生成\n# -*- coding: utf-8 -*-\nHTML_B64 = "%s"\n' % b64)
    print("ui_embed.py ok")

def step_icon():
    subprocess.check_call([sys.executable, os.path.join(HERE, "make_icon.py")])

def step_build():
    subprocess.check_call([sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole",
        "--clean", "--noconfirm", "--name", NAME,
        "--icon", os.path.join(HERE, "icon.ico"),
        "--add-data", os.path.join(HERE, "icon.ico") + ";.",
        "--hidden-import", "tray",
        "--hidden-import", "win32com.client", "--hidden-import", "pythoncom",
        "--hidden-import", "pywintypes", "--collect-all", "edge_tts",
        "--collect-all", "aiohttp", "--collect-all", "certifi",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", HERE, os.path.join(HERE, "app2.py")])

if __name__ == "__main__":
    step_icon(); step_embed(); step_build()
    out = os.path.join(HERE, "dist", NAME + ".exe")
    print("BUILD OK:", out, os.path.getsize(out)//1024, "KB" if os.path.exists(out) else "MISSING")
