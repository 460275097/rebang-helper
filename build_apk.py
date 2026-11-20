# -*- coding: utf-8 -*-
"""热榜助手 安卓版 · 命令行打包（不依赖 Android Studio 图形界面）

用系统里已有的 JDK + Android SDK build-tools 直接出 APK：
    javac -> d8 -> aapt2 link -> 合并 classes.dex -> zipalign -> apksigner

用法：
    python build_apk.py            # 打包并签名（debug 证书，可直接安装）
产物：
    dist/热榜助手V<版本>.apk
"""
import glob
import os
import re
import shutil
import subprocess
import sys
import zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
APK = os.path.join(BASE, "apk")
OUT = os.path.join(BASE, "dist")
WORK = os.path.join(APK, "_build")
DEPS = os.path.join(APK, "_deps")
# 打进 APK 的原生库架构（arm64 主流机型 + 32 位老机 + x86_64 模拟器验证）
ABIS = ["arm64-v8a", "armeabi-v7a", "x86_64"]


def find_jdk():
    cands = []
    if os.environ.get("JAVA_HOME"):
        cands.append(os.environ["JAVA_HOME"])
    cands += [r"C:\PoseLibDev\jdk-17.0.21+9",
              r"C:\Program Files\Android\Android Studio\jbr",
              r"C:\Program Files\Java\jdk-17",
              r"C:\Program Files\Eclipse Adoptium\jdk-17"]
    for c in cands:
        exe = os.path.join(c, "bin", "javac.exe")
        if os.path.exists(exe):
            return c
    raise SystemExit("找不到 JDK（需要 javac）。可设置 JAVA_HOME 后重试。")


def find_sdk():
    for c in [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Android", "Sdk")]:
        if c and os.path.isdir(os.path.join(c, "build-tools")):
            return c
    raise SystemExit("找不到 Android SDK（build-tools）。")


def newest_dir(path):
    items = [d for d in os.listdir(path) if os.path.isdir(os.path.join(path, d))]
    if not items:
        return None

    def key(s):
        return [int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", s)]
    return os.path.join(path, sorted(items, key=key)[-1])


def read_ver():
    p = os.path.join(BASE, "VERSION")
    if os.path.exists(p):
        return open(p, encoding="utf-8").read().strip()
    s = open(os.path.join(BASE, "app2.py"), encoding="utf-8").read()
    m = re.search(r'^APP_VER\s*=\s*"([\d.]+)"', s, re.M)
    return m.group(1) if m else "0.0"


def vcode(v):
    n = [int(x) for x in v.split(".") if x.isdigit()] + [0, 0, 0]
    return n[0] * 10000 + n[1] * 100 + n[2]


def run(cmd, env=None, quiet=False):
    if not quiet:
        print("  $", " ".join(cmd[:4]), "..." if len(cmd) > 4 else "")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", env=env)
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        raise SystemExit("命令失败：%s" % cmd[0])
    return r.stdout


def main():
    ver = read_ver()
    code = vcode(ver)
    jdk, sdk = find_jdk(), find_sdk()
    bt = newest_dir(os.path.join(sdk, "build-tools"))
    platform = newest_dir(os.path.join(sdk, "platforms"))
    android_jar = os.path.join(platform, "android.jar")
    print("版本 %s（versionCode %d）" % (ver, code))
    print("JDK:", jdk)
    print("build-tools:", os.path.basename(bt), "| platform:", os.path.basename(platform))

    env = dict(os.environ)
    env["JAVA_HOME"] = jdk
    env["PATH"] = os.path.join(jdk, "bin") + os.pathsep + env.get("PATH", "")

    shutil.rmtree(WORK, ignore_errors=True)
    for d in ("classes", "dex", "assets"):
        os.makedirs(os.path.join(WORK, d), exist_ok=True)

    # 1) 页面资源：APK 是「网页版外壳」，不再自带一套手机页面，
    #    壳脚本只做三件事：ML Kit 端侧翻译接管、离线面板接管、手机端布局增强
    for asset in ("shell.js", "shell.css"):
        src = os.path.join(APK, asset)
        if not os.path.exists(src):
            raise SystemExit("缺少资源 %s" % asset)
        shutil.copy2(src, os.path.join(WORK, "assets", asset))

    # 1.1) 清单里的 versionCode / versionName 会覆盖命令行参数，先按 VERSION 改写一份到 _build
    man_src = open(os.path.join(APK, "AndroidManifest.xml"), encoding="utf-8").read()
    man_src = re.sub(r'android:versionCode="\d+"', 'android:versionCode="%d"' % code, man_src)
    man_src = re.sub(r'android:versionName="[^"]*"', 'android:versionName="%s"' % ver, man_src)
    manifest = os.path.join(WORK, "AndroidManifest.xml")
    with open(manifest, "w", encoding="utf-8") as f:
        f.write(man_src)

    # 2) ML Kit 依赖（AAR/JAR）：首次自动下载，之后离线复用
    jars = sorted(glob.glob(os.path.join(DEPS, "jars", "*.jar")))
    if not jars:
        print("首次构建：下载 ML Kit 英译中依赖…")
        run([sys.executable, os.path.join(APK, "fetch_mlkit.py")])
        jars = sorted(glob.glob(os.path.join(DEPS, "jars", "*.jar")))
    if not jars:
        raise SystemExit("ML Kit 依赖缺失，无法打包")
    cp = os.pathsep.join(jars)
    print("依赖：%d 个 jar" % len(jars))

    # 2.1) 依赖 AAR 的资源。注意：AAR 里**不带 R 类**（那是 Gradle 构建时按 R.txt 生成的），
    #      只把 res 塞进 APK 的话，ML Kit 初始化到 R.xml / R.raw 就会
    #      NoClassDefFoundError: Lcom/google/mlkit/nl/translate/R$xml;
    #      所以必须用 aapt2 link --java + --extra-packages 现生成各库 R 类，再一起编进 dex。
    AAR_RES = [
        ("translate-17.0.3.aar", "com.google.mlkit.nl.translate"),
        ("play-services-base-18.5.0.aar", "com.google.android.gms.base"),
    ]
    aapt2 = os.path.join(bt, "aapt2.exe")
    res_zips = []
    extra_pkgs = []
    for aar_name, pkg in AAR_RES:
        aar = os.path.join(DEPS, "aar", aar_name)
        if not os.path.exists(aar):
            print("  ! 缺少 %s，跳过其资源" % aar_name)
            continue
        tag = os.path.splitext(aar_name)[0]
        dst = os.path.join(WORK, "aar_" + tag)
        with zipfile.ZipFile(aar) as z:
            z.extractall(dst)
        aar_res = os.path.join(dst, "res")
        if not os.path.isdir(aar_res):
            continue
        flat = os.path.join(WORK, "res_%s.zip" % tag)
        run([aapt2, "compile", "--dir", aar_res, "-o", flat], env)
        res_zips.append(flat)
        extra_pkgs.append(pkg)

    # 2.2) 应用自己的资源（图标 mipmap 等）
    app_res = os.path.join(APK, "res")
    if os.path.isdir(app_res):
        app_flat = os.path.join(WORK, "res_app.zip")
        run([aapt2, "compile", "--dir", app_res, "-o", app_flat], env)
        res_zips.append(app_flat)

    # 2.3) 资源 + 清单 -> 未签名 APK，同时生成 R 类（app 包 + 各依赖库包）
    unsigned = os.path.join(WORK, "unsigned.apk")
    gen = os.path.join(WORK, "gen")
    os.makedirs(gen, exist_ok=True)
    link_cmd = [aapt2, "link", "-o", unsigned,
                "-I", android_jar,
                "--manifest", manifest,
                "-A", os.path.join(WORK, "assets"),
                "--min-sdk-version", "23", "--target-sdk-version", "34",
                "--version-code", str(code), "--version-name", ver,
                "--auto-add-overlay",
                "--java", gen]
    for flat in res_zips:
        link_cmd += ["-R", flat]
    if extra_pkgs:
        link_cmd += ["--extra-packages", ":".join(extra_pkgs)]
    run(link_cmd, env)

    # 3) 编译 Java（MainActivity + 自动生成的 R.java）
    java_src = os.path.join(APK, "MainActivity.java")
    gen_srcs = []
    for root, _, files in os.walk(gen):
        for f in files:
            if f.endswith(".java"):
                gen_srcs.append(os.path.join(root, f))
    print("自动生成 R 类：%d 个" % len(gen_srcs))
    for g in gen_srcs:
        print("   ", os.path.relpath(g, gen).replace("\\", "/"))
    run([os.path.join(jdk, "bin", "javac.exe"), "-encoding", "UTF-8",
         "-source", "1.8", "-target", "1.8", "-nowarn",
         "-bootclasspath", android_jar, "-classpath", cp,
         "-d", os.path.join(WORK, "classes"), java_src] + gen_srcs, env)

    # 4) dex（ML Kit + AndroidX 方法数超过 64K，d8 在 min-api 23 下自动多 dex）
    #    命令行有 8K 长度上限，类多时必须先打成一个 jar 再交给 d8，否则报「命令行太长」
    classes_jar = os.path.join(WORK, "classes.jar")
    with zipfile.ZipFile(classes_jar, "w", zipfile.ZIP_DEFLATED) as zj:
        root_dir = os.path.join(WORK, "classes")
        for root, _, files in os.walk(root_dir):
            for f in files:
                if f.endswith(".class"):
                    full = os.path.join(root, f)
                    zj.write(full, os.path.relpath(full, root_dir).replace("\\", "/"))
    d8 = os.path.join(bt, "d8.bat")
    run(["cmd", "/c", d8, "--lib", android_jar, "--min-api", "23",
         "--output", os.path.join(WORK, "dex"), classes_jar] + jars, env)

    # 4.1) 自检：dex 里必须真的有 ML Kit 的 R 类，否则手机上必然「SDK 初始化失败」
    dex_blob = b""
    for f in sorted(os.listdir(os.path.join(WORK, "dex"))):
        if f.endswith(".dex"):
            dex_blob += open(os.path.join(WORK, "dex", f), "rb").read()
    for need in (b"com/google/mlkit/nl/translate/R$xml",
                 b"com/google/mlkit/nl/translate/R$raw"):
        if need not in dex_blob:
            raise SystemExit("打包自检失败：dex 缺少 %s，手机上会 NoClassDefFoundError"
                             % need.decode())
    print("自检通过：ML Kit R 类已进 dex")

    # 6) 合并 classes.dex / classes2.dex… + 原生库
    with_dex = os.path.join(WORK, "with_dex.apk")
    with zipfile.ZipFile(unsigned, "r") as zi, zipfile.ZipFile(with_dex, "w", zipfile.ZIP_DEFLATED) as zo:
        for item in zi.infolist():
            zo.writestr(item, zi.read(item.filename))
        dex_dir = os.path.join(WORK, "dex")
        for f in sorted(os.listdir(dex_dir)):
            if f.endswith(".dex"):
                zo.write(os.path.join(dex_dir, f), f)
        for abi in ABIS:
            src = os.path.join(DEPS, "jni", abi)
            if not os.path.isdir(src):
                continue
            for so in os.listdir(src):
                if so.endswith(".so"):
                    zo.write(os.path.join(src, so), "lib/%s/%s" % (abi, so))

    # 6) 对齐
    aligned = os.path.join(WORK, "aligned.apk")
    run([os.path.join(bt, "zipalign.exe"), "-f", "-p", "4", with_dex, aligned], env)

    # 7) 签名：证书固定放在 apk/rebang.keystore（不要放 _build，每次构建会被清空，
    #    否则每次签名都换一把钥匙，手机覆盖安装会报「签名不一致」）
    ks = os.path.join(APK, "rebang.keystore")
    if not os.path.exists(ks):
        run([os.path.join(jdk, "bin", "keytool.exe"), "-genkeypair", "-v",
             "-keystore", ks, "-alias", "rebang",
             "-storepass", "rebang2026", "-keypass", "rebang2026",
             "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
             "-dname", "CN=Rebang,O=Qidu,C=CN"], env, quiet=True)

    os.makedirs(OUT, exist_ok=True)
    final = os.path.join(OUT, "热榜助手V%s.apk" % ver)
    if os.path.exists(final):
        os.remove(final)
    run(["cmd", "/c", os.path.join(bt, "apksigner.bat"), "sign",
         "--ks", ks, "--ks-pass", "pass:rebang2026", "--key-pass", "pass:rebang2026",
         "--v1-signing-enabled", "true", "--v2-signing-enabled", "true",
         "--out", final, aligned], env)

    size = os.path.getsize(final) / 1024.0 / 1024.0
    print("完成：%s（%.1f MB）" % (final, size))


if __name__ == "__main__":
    main()
