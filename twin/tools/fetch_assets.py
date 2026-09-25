#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数字层的第三方依赖与模型资产下载器，兼「这一步环境是否就绪」的检查器。

    python twin/tools/fetch_assets.py            缺什么补什么，大小正确的不重下
    python twin/tools/fetch_assets.py --force    全部重下
    python twin/tools/fetch_assets.py --no-proxy 忽略环境变量里的代理（git 里那个 7890 是坏的）
    python twin/tools/fetch_assets.py --check    只校验，不下载

下载完成后跑四层校验：
  1. 每个文件的实际字节数与远端 Content-Length 一致
  2. vendor 的 import 闭包扫描 —— 从已下文件递归解析 import，缺谁报谁
     （urdf-loader 静态 import 了 ColladaLoader，它又拖着 TGALoader 和 collada/ 两个文件，
       three.module.js 还依赖 three.core.js。漏任意一个文件整页白屏，报错却指向入口文件。）
  3. URDF 能被解析，且它引用的每个网格在本地都存在、非空
  4. STL 二进制结构：size >= 84 且 (size - 84) % 50 == 0
     （80 字节头 + 4 字节三角数 + 每个三角 50 字节；这条能挡掉被镜像塞回来的 HTML 错误页）
"""

from __future__ import annotations

import argparse
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
TWIN = os.path.dirname(HERE)
WEB = os.path.join(TWIN, "web")
VENDOR = os.path.join(WEB, "vendor")
MODELS = os.path.join(WEB, "models", "so101")

THREE = "three"
THREE_VER = "0.186.0"
URDF_LOADER = "urdf-loader"
URDF_LOADER_VER = "0.13.1"
SOARM_REPO = "TheRobotStudio/SO-ARM100"
SOARM_REF = "main"

MESHES = [
    "base_motor_holder_so101_v1.stl",
    "base_so101_v2.stl",
    "motor_holder_so101_base_v1.stl",
    "motor_holder_so101_wrist_v1.stl",
    "moving_jaw_so101_v1.stl",
    "rotation_pitch_so101_v1.stl",
    "sts3215_03a_no_horn_v1.stl",
    "sts3215_03a_v1.stl",
    "under_arm_so101_v1.stl",
    "upper_arm_so101_v1.stl",
    "waveshare_mounting_plate_so101_v2.stl",
    "wrist_roll_follower_so101_v1.stl",
    "wrist_roll_pitch_so101_v2.stl",
]

# (相对 vendor 的目标路径, npm 包名, 包内路径)
VENDOR_FILES = [
    ("three/three.module.js", THREE, "build/three.module.js"),
    ("three/three.core.js", THREE, "build/three.core.js"),
    ("three/examples/jsm/loaders/STLLoader.js", THREE, "examples/jsm/loaders/STLLoader.js"),
    ("three/examples/jsm/loaders/ColladaLoader.js", THREE, "examples/jsm/loaders/ColladaLoader.js"),
    ("three/examples/jsm/loaders/TGALoader.js", THREE, "examples/jsm/loaders/TGALoader.js"),
    ("three/examples/jsm/loaders/collada/ColladaParser.js", THREE, "examples/jsm/loaders/collada/ColladaParser.js"),
    ("three/examples/jsm/loaders/collada/ColladaComposer.js", THREE, "examples/jsm/loaders/collada/ColladaComposer.js"),
    ("three/examples/jsm/controls/OrbitControls.js", THREE, "examples/jsm/controls/OrbitControls.js"),
    ("urdf-loader/src/URDFLoader.js", URDF_LOADER, "src/URDFLoader.js"),
    ("urdf-loader/src/URDFClasses.js", URDF_LOADER, "src/URDFClasses.js"),
]

# 网格必须落在 assets/ 子目录里：URDF 里写的是相对路径 assets/xxx.stl，
# urdf-loader 按 URDF 所在目录去解析，换个位置就找不到。
MODEL_FILES = (
    ["so101_new_calib.urdf", "so101_old_calib.urdf", "README.md"]
    + [f"assets/{m}" for m in MESHES]
)

PKG_META = {THREE: (THREE_VER, "build/three.module.js"), URDF_LOADER: (URDF_LOADER_VER, "src/URDFLoader.js")}


# ---------------------------------------------------------------- 下载

def npm_sources(pkg: str, path: str) -> list[str]:
    ver = PKG_META[pkg][0]
    return [
        f"https://registry.npmmirror.com/{pkg}/{ver}/files/{path}",
        f"https://cdn.jsdelivr.net/npm/{pkg}@{ver}/{path}",
        f"https://unpkg.com/{pkg}@{ver}/{path}",
    ]


def gh_sources(path: str) -> list[str]:
    return [
        f"https://raw.githubusercontent.com/{SOARM_REPO}/{SOARM_REF}/Simulation/SO101/{path}",
        f"https://cdn.jsdelivr.net/gh/{SOARM_REPO}@{SOARM_REF}/Simulation/SO101/{path}",
    ]


class Fetcher:
    def __init__(self, no_proxy: bool, insecure: bool):
        handlers = []
        if no_proxy:
            handlers.append(urllib.request.ProxyHandler({}))
        ctx = ssl._create_unverified_context() if insecure else ssl.create_default_context()
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)
        self.opener.addheaders = [
            ("User-Agent", "Mozilla/5.0 (compatible; twin-fetch-assets/1.0)"),
            ("Accept", "*/*"),
        ]

    def head_size(self, url: str) -> int | None:
        req = urllib.request.Request(url, method="HEAD")
        try:
            with self.opener.open(req, timeout=20) as r:
                n = r.headers.get("Content-Length")
                return int(n) if n else None
        except Exception:
            return None

    def get(self, url: str, dest_part: str) -> int:
        req = urllib.request.Request(url)
        with self.opener.open(req, timeout=180) as r:
            declared = r.headers.get("Content-Length")
            with open(dest_part, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
        got = os.path.getsize(dest_part)
        if declared is not None and int(declared) != got:
            raise IOError(f"字节数不符：声明 {declared}，实得 {got}")
        if got == 0:
            raise IOError("下到 0 字节")
        return got


def fetch_one(fetcher: Fetcher, target: str, sources: list[str], force: bool) -> tuple[str, int, str]:
    """返回 (状态, 字节数, 说明)。状态是 ok / skip / fail。"""
    part = target + ".part"
    if os.path.exists(target) and not force:
        return ("skip", os.path.getsize(target), "已存在")

    os.makedirs(os.path.dirname(target), exist_ok=True)
    tried: list[str] = []
    for url in sources:
        host = url.split("/")[2]
        last = ""
        for attempt in range(3):
            try:
                size = fetcher.get(url, part)
                os.replace(part, target)
                return ("ok", size, host)
            except Exception as e:  # noqa: BLE001
                last = f"{type(e).__name__}: {e}"
                if os.path.exists(part):
                    try:
                        os.remove(part)
                    except OSError:
                        pass
                time.sleep(0.5 * (attempt + 1))
        tried.append(f"{host} → {last}")
    # 每个源各自的最后一条错误都留着，否则只剩最后一个源的报错，看不出主源为什么失败
    return ("fail", 0, " ｜ ".join(tried))


# ---------------------------------------------------------------- import 闭包

# 只认行首的 import/export —— three 的源文件在同一条语句外面写了
#   * @three_import import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
# 这种 JSDoc 示例，不锚定行首会把 'three/addons/...' 当成真依赖，白下一批文件。
IMPORT_RE = re.compile(r"""^\s*(?:import|export)\b[^'"]*?['"]([^'"]+)['"]""", re.MULTILINE)
BLOCK_COMMENT_RE = re.compile(r"/\*[\s\S]*?\*/")


def iter_imports(path: str):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = BLOCK_COMMENT_RE.sub("", f.read())
    except OSError:
        return
    for m in IMPORT_RE.finditer(text):
        yield m.group(1)


def resolve_specifier(importer: str, spec: str):
    """把 import 的裸标识符/相对路径映射成 (本地绝对路径, npm 包名, 包内路径) 或 None。"""
    if spec == THREE:
        return os.path.join(VENDOR, "three", "three.module.js"), None, None
    if spec.startswith(THREE + "/"):
        rest = spec[len(THREE) + 1:]
        return os.path.join(VENDOR, "three", *rest.split("/")), THREE, rest
    if not spec.startswith("."):
        return None

    resolved = os.path.normpath(os.path.join(os.path.dirname(importer), spec))
    for name, pkg in (("three", THREE), ("urdf-loader", URDF_LOADER)):
        base = os.path.join(VENDOR, name)
        if resolved.startswith(base + os.sep):
            rel = os.path.relpath(resolved, base).replace(os.sep, "/")
            return resolved, pkg, rel
    return resolved, None, None


def scan_closure(fetcher: Fetcher, force: bool, downloaded: list, allow_fetch: bool = True) -> list[str]:
    """递归扫 vendor 的 import 闭包；能映射到 npm 包的缺失文件自动补下来。"""
    problems = []
    for _ in range(12):
        missing = []
        for dirpath, _dirnames, filenames in os.walk(VENDOR):
            for fn in filenames:
                if not fn.endswith(".js"):
                    continue
                importer = os.path.join(dirpath, fn)
                for spec in iter_imports(importer):
                    resolved, pkg, pkgpath = resolve_specifier(importer, spec)
                    if resolved is None:
                        problems.append(f"{rel_posix(importer, VENDOR)}: 未识别的标识符 '{spec}'")
                        continue
                    if os.path.exists(resolved):
                        continue
                    if pkg is not None and allow_fetch:
                        missing.append((resolved, pkg, pkgpath))
                    else:
                        why = "无法推断下载来源" if pkg is None else "（只校验模式，没有下载）"
                        problems.append(
                            f"{rel_posix(importer, VENDOR)} 引用了 {spec}，本地缺 {rel_posix(resolved, VENDOR)}，{why}"
                        )
        if not missing:
            break
        for resolved, pkg, pkgpath in missing:
            status, size, note = fetch_one(fetcher, resolved, npm_sources(pkg, pkgpath), force)
            downloaded.append((f"vendor/{rel_posix(resolved, VENDOR)}", status, size, note))
    return problems


# ---------------------------------------------------------------- 校验

def rel_posix(path: str, base: str) -> str:
    return os.path.relpath(path, base).replace(os.sep, "/")


def validate_urdf(path: str) -> list[str]:
    problems = []
    try:
        tree = ET.parse(path)
    except Exception as e:  # noqa: BLE001
        return [f"{os.path.basename(path)} 解析失败：{e}"]

    # 同一个网格会被 visual 和 collision 各引用一次，去重后再查
    refs: list[str] = []
    for el in tree.getroot().iter("mesh"):
        ref = el.get("filename")
        if ref and ref not in refs:
            refs.append(ref)
    if not refs:
        problems.append(f"{os.path.basename(path)} 里没有 mesh 引用，文件可能不对")
    for ref in refs:
        local = os.path.normpath(os.path.join(os.path.dirname(path), ref))
        if not os.path.exists(local):
            problems.append(f"{os.path.basename(path)} 引用的 {ref} 在本地不存在")
        elif os.path.getsize(local) == 0:
            problems.append(f"{os.path.basename(path)} 引用的 {ref} 是空文件")
    return problems


def validate_stl(path: str) -> list[str]:
    size = os.path.getsize(path)
    if size < 84:
        return [f"{os.path.basename(path)} 只有 {size} 字节，不是有效的二进制 STL"]
    if (size - 84) % 50 != 0:
        return [f"{os.path.basename(path)} 大小 {size} 不符合二进制 STL 结构，可能是错误页"]
    return []


# ---------------------------------------------------------------- 主流程

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="全部重下")
    ap.add_argument("--no-proxy", action="store_true", help="忽略环境变量里的代理")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    ap.add_argument("--check", action="store_true", help="只校验，不下载")
    args = ap.parse_args()

    fetcher = Fetcher(args.no_proxy, args.insecure)
    rows: list[tuple[str, str, int, str]] = []

    if not args.check:
        print(f"目标：{WEB}\n")
        for rel, pkg, pkgpath in VENDOR_FILES:
            target = os.path.join(VENDOR, *rel.split("/"))
            status, size, note = fetch_one(fetcher, target, npm_sources(pkg, pkgpath), args.force)
            rows.append((f"vendor/{rel}", status, size, note))
        for name in MODEL_FILES:
            target = os.path.join(MODELS, *name.split("/"))
            status, size, note = fetch_one(fetcher, target, gh_sources(name), args.force)
            rows.append((f"models/so101/{name}", status, size, note))

        # 闭包扫描可能发现清单里没列到的文件，补下来
        extra: list[tuple[str, str, int, str]] = []
        closure_problems = scan_closure(fetcher, args.force, extra)
        if extra:
            print("\n闭包扫描补齐：")
            for name, _status, size, note in extra:
                print(f"  + {name}  {size / 1024:.0f} KB  {note}")
        rows.extend(extra)
    else:
        closure_problems = scan_closure(fetcher, False, [], allow_fetch=False)

    print("\n文件")
    for name, status, size, note in rows:
        tail = "已存在" if status == "skip" else (f"失败 {note}" if status == "fail" else note)
        print(f"  {name:<56} {size:>10,} B  {tail}")
    failed = len([n for n, s, _sz, _nt in rows if s == "fail"])

    print("\n校验")
    problems = list(closure_problems)

    if not args.check:
        if failed:
            print(f"  下载失败 {failed} 个")

    for name in MODEL_FILES:
        if not name.endswith(".urdf"):
            continue
        p = os.path.join(MODELS, *name.split("/"))
        if os.path.exists(p):
            problems += validate_urdf(p)
        else:
            problems.append(f"缺 {name}")

    absent_mesh = []
    bad_mesh = []
    for m in MESHES:
        p = os.path.join(MODELS, "assets", m)
        if not os.path.exists(p):
            absent_mesh.append(m)
            continue
        bad_mesh += validate_stl(p)
    if absent_mesh:
        problems.append(f"缺 {len(absent_mesh)} 个网格：{', '.join(absent_mesh[:4])}{' …' if len(absent_mesh) > 4 else ''}")
    problems += bad_mesh

    if not os.path.exists(os.path.join(VENDOR, "three", "three.module.js")):
        problems.append("缺 vendor/three/three.module.js")

    if problems:
        failed += len(problems)
        for p in problems:
            print(f"  [!] {p}")
    else:
        print(f"  import 闭包完整；2 个 URDF 可解析；13 个网格结构与引用都正确")

    print(f"\n{'有 ' + str(failed) + ' 个问题' if failed else '一切就绪'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
