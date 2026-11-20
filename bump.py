# -*- coding: utf-8 -*-
"""版本号自动递增工具（热榜助手）

用法：
    python bump.py            # 递增修订号：7.9 -> 7.9.1   （每次改完跑一次）
    python bump.py minor      # 递增次版本：7.9.1 -> 7.10
    python bump.py major      # 递增主版本：7.9.1 -> 8.0
    python bump.py show       # 只显示当前版本

会同步修改：
    VERSION                      版本号唯一来源
    app2.py                      APP_VER / PRESET_VER（内置源清单版本，随之升级）
    ui.html                      <title> / 侧栏版本徽标 / APP_VER 常量 / 关于页
    apk/index.html               ver 字段 / 关于页
    apk/build.gradle             versionName / versionCode
    apk/AndroidManifest.xml      （无版本字段，跳过）
    热榜助手V*.spec              文件名与 name=（打包产物名随之变化）
"""
import os
import re
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
VER_FILE = os.path.join(BASE, "VERSION")


def read_ver():
    if os.path.exists(VER_FILE):
        with open(VER_FILE, encoding="utf-8") as f:
            v = f.read().strip()
        if v:
            return v
    # 兜底：从 app2.py 读
    with open(os.path.join(BASE, "app2.py"), encoding="utf-8") as f:
        m = re.search(r'^APP_VER\s*=\s*"([\d.]+)"', f.read(), re.M)
    return m.group(1) if m else "7.8"


def bump(v, part="patch"):
    nums = [int(x) for x in v.split(".") if x.isdigit()]
    while len(nums) < 3:
        nums.append(0)
    if part == "major":
        nums = [nums[0] + 1, 0, 0]
    elif part == "minor":
        nums = [nums[0], nums[1] + 1, 0]
    else:
        nums[2] += 1
    # 去掉多余的 .0 尾巴，保持 7.9 / 7.9.1 这种写法人性化
    out = [nums[0], nums[1], nums[2]]
    while len(out) > 2 and out[-1] == 0:
        out.pop()
    return ".".join(str(n) for n in out)


def vcode(v):
    nums = [int(x) for x in v.split(".") if x.isdigit()]
    while len(nums) < 3:
        nums.append(0)
    return nums[0] * 10000 + nums[1] * 100 + nums[2]


def sub_file(path, pattern, repl, count=0, must=True):
    """按正则替换并写回，返回命中次数"""
    p = os.path.join(BASE, path)
    if not os.path.exists(p):
        return 0
    with open(p, encoding="utf-8") as f:
        s = f.read()
    s2, n = re.subn(pattern, repl, s, count=count, flags=re.M)
    if n and s2 != s:
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(s2)
    if must and n == 0:
        print("  [warn] 未命中：%s  %s" % (path, pattern))
    return n


def main():
    arg = (sys.argv[1] if len(sys.argv) > 1 else "patch").lower()
    old = read_ver()
    if arg == "show":
        print(old)
        return
    new = bump(old, arg if arg in ("patch", "minor", "major") else "patch")
    code = vcode(new)

    with open(VER_FILE, "w", encoding="utf-8") as f:
        f.write(new + "\n")

    # app2.py
    sub_file("app2.py", r'^APP_VER\s*=\s*"[^"]*"', 'APP_VER = "%s"' % new)
    sub_file("app2.py", r'^PRESET_VER\s*=\s*"[^"]*"', 'PRESET_VER = "%s"' % new)
    # 注释里的版本标记（V7.8 之类）不批量改，避免误伤历史注释

    # ui.html —— V8.0.4 起版本号只有一个来源：APP_VER 常量，
    # <title>/侧栏徽标/关于页都由 fillVer() 在运行时填充，不再逐处替换（避免出现「还显示上一代版本」）
    sub_file("ui.html", r"const APP_VER='V[\d.]+';", "const APP_VER='V%s';" % new)

    # exe 文件属性（资源管理器 → 属性 → 详细信息）
    vinfo = os.path.join(BASE, "version_info.txt")
    if os.path.exists(vinfo):
        nums = [int(x) for x in new.split(".") if x.isdigit()]
        while len(nums) < 4:
            nums.append(0)
        tuple4 = "(%d, %d, %d, %d)" % tuple(nums[:4])
        sub_file("version_info.txt", r"filevers=\([^)]*\)", "filevers=%s" % tuple4, must=False)
        sub_file("version_info.txt", r"prodvers=\([^)]*\)", "prodvers=%s" % tuple4, must=False)
        sub_file("version_info.txt", r"'FileVersion', '[^']*'", "'FileVersion', '%s'" % new, must=False)
        sub_file("version_info.txt", r"'ProductVersion', '[^']*'", "'ProductVersion', '%s'" % new, must=False)
        sub_file("version_info.txt", r"'OriginalFilename', '[^']*'",
                 "'OriginalFilename', '热榜助手V%s.exe'" % new, must=False)

    # apk
    sub_file("apk/index.html", r"ver:'[\d.]+'", "ver:'%s'" % new)
    sub_file("apk/index.html", r"热榜助手 V[\d.]+ · 作者", "热榜助手 V%s · 作者" % new)
    sub_file("apk/build.gradle", r'versionCode \d+', 'versionCode %d' % code)
    sub_file("apk/build.gradle", r'versionName "[^"]*"', 'versionName "%s"' % new)
    sub_file("apk/AndroidManifest.xml", r'android:versionCode="\d+"', 'android:versionCode="%d"' % code)
    sub_file("apk/AndroidManifest.xml", r'android:versionName="[^"]*"', 'android:versionName="%s"' % new)

    # PyInstaller spec：只维护「最新那一个」，历史 spec 归档在 _trash/specs，不参与重命名
    specs = [fn for fn in os.listdir(BASE) if fn.endswith(".spec") and fn.startswith("热榜助手")]
    if not specs:
        print("  [warn] 找不到 热榜助手V*.spec，跳过")
    else:
        latest = max((os.path.join(BASE, f) for f in specs), key=os.path.getmtime)
        with open(latest, encoding="utf-8") as f:
            s = f.read()
        s2 = re.sub(r"name='热榜助手V[\d.]+'", "name='热榜助手V%s'" % new, s)
        dst = os.path.join(BASE, "热榜助手V%s.spec" % new)
        with open(latest, "w", encoding="utf-8") as f:
            f.write(s2)
        if os.path.abspath(dst) != os.path.abspath(latest):
            os.replace(latest, dst)
            print("  重命名 spec：%s -> %s" % (os.path.basename(latest), os.path.basename(dst)))

    print("版本：%s -> %s（versionCode %d）" % (old, new, code))


if __name__ == "__main__":
    main()
