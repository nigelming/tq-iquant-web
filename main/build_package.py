#!/usr/bin/env python
"""Nuitka standalone(onedir) 构建 + 产物整理。

产物：<仓库根>/dist/tq-iquant/（含 exe、_internal/、web/dist、alembic、config 模板）
安装器由 packaging/installer.iss（Inno Setup 6）基于该目录生成。

注意：
- 旧版打包产物 main/dist/tq-iquant 由历史流程维护，本脚本不读写、不删除。
- 桥脚本（live/bridge/*.py）**不编译进 exe**，由安装器以明文放到 <安装目录>\\bridge\\。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent            # main/
REPO = ROOT.parent                                # 仓库根
WEB_DIST = REPO / "web" / "dist"
DIST_OUT = REPO / "dist" / "tq-iquant"            # 最终分发目录
BUILD_OUT = REPO / "dist" / "build"               # Nuitka 输出中转
NIGHT_BUILD_DIR = REPO / "dist" / "build" / "nuitka-build"
ENTRY = ROOT / "desktop.py"
ICON = REPO / "packaging" / "icon" / "app.ico"
CONFIG_TEMPLATE = REPO / "packaging" / "config.template.yaml"
VERSION = "1.0.0"


def run(cmd, cwd=None, env=None):
    print(f"\n>>> {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, cwd=cwd, env=env)
    if result.returncode != 0:
        print(f"FAILED: {cmd}", flush=True)
        sys.exit(1)


def purge_nuitka_cache():
    """缓存重定向到项目内：本机 %LOCALAPPDATA%\\Nuitka 写入不稳定（PermissionError）。"""
    if not NIGHT_BUILD_DIR.exists():
        NIGHT_BUILD_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["NUITKA_CACHE_DIR"] = str(NIGHT_BUILD_DIR)
    os.environ["NUITKA_BUILD_DIR"] = str(NIGHT_BUILD_DIR)
    module_cache = NIGHT_BUILD_DIR / "module-cache"
    if module_cache.exists():
        shutil.rmtree(module_cache, ignore_errors=True)
        print(f"已清空 Nuitka 模块缓存：{module_cache}")


def nuitka_cmd(python_exe: str):
    cmd = [
        python_exe, "-m", "nuitka",
        "--standalone",
        "--low-memory",
        "--enable-plugin=anti-bloat",
        "--include-package=core",
        "--include-package-data=core",   # tdx_formula 等包内数据
        "--include-package=tq_iquant_shared",
        "--include-package=sqlalchemy",
        "--include-package=fastapi",
        "--include-package=uvicorn",
        "--include-package=pandas",
        "--include-package=polars",
        "--include-package=pydantic",
        "--include-package=httpx",
        "--include-package=pytdx",
        "--include-package=openpyxl",
        "--include-package=yaml",
        "--include-package=alembic",
        # 同花顺 UI 下单通道：pywinauto 窗口自动化 + 剪贴板 + 截图
        "--include-package=pywinauto",
        "--include-package=pyperclip",
        "--include-package=PIL",
        # 验证码 OCR：ddddocr（onnxruntime 推理 + 内置 onnx 模型）
        "--include-package=ddddocr",
        "--include-package-data=ddddocr",
        "--include-package=onnxruntime",
        "--include-package=cv2",
        # Windows 内置 OCR 兜底（WinRT）
        "--include-package=winrt",
        "--include-package-data=winrt",
        # 桌面壳：pywebview + WebView2（pythonnet/clr）；加载失败时启动器自动回退浏览器
        # 注意：webview 交给 Nuitka 内置 pywebview 插件处理平台分支，
        # 显式 --include-package=webview 会与插件的平台模块决策冲突（FATAL）。
        "--include-package=pythonnet",
        "--include-package-data=pythonnet",
        "--include-package=clr_loader",
        "--include-package-data=clr_loader",
        "--include-module=clr",
        # 排除第三方包自带测试代码（--include-package 会连测试一起编译，极慢且无用）
        "--nofollow-import-to=*.tests",
        "--nofollow-import-to=*.testing",
        "--nofollow-import-to=alembic.testing",
        "--update-check=never",
        "--assume-yes-for-downloads",
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON}",
        "--company-name=创懿私募基金",
        "--product-name=创懿基金量化投资平台",
        f"--file-version={VERSION}",
        f"--product-version={VERSION}",
        "--output-filename=tq-iquant",
        f"--output-dir={BUILD_OUT}",
        str(ENTRY),
    ]
    return cmd


def copy_assets(dist_dir: Path):
    print("\n=== 复制运行期资源 ===")
    if not WEB_DIST.exists():
        sys.exit(f"缺少前端构建产物：{WEB_DIST}（先在 web/ 下执行 npm run build）")

    dest_web = dist_dir / "_internal" / "web" / "dist"
    dest_web.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WEB_DIST, dest_web, dirs_exist_ok=True)
    print(f"  web/dist -> {dest_web}")

    # core/main.py 打包版按 <exe>/_internal/web/dist 定位静态资源，此处不再额外复制。

    echarts = REPO / "web" / "node_modules" / "echarts" / "dist" / "echarts.min.js"
    if echarts.exists():
        shutil.copy2(echarts, dist_dir / "echarts.min.js")
        print("  echarts.min.js（离线报告内联运行时）")
    else:
        print("  WARNING: 未找到 echarts.min.js，离线报告导出将失败")

    if CONFIG_TEMPLATE.exists():
        shutil.copy2(CONFIG_TEMPLATE, dist_dir / "config.template.yaml")
        print("  config.template.yaml（首次启动生成用户 config.yaml）")

    alembic_src = ROOT / "alembic"
    if not alembic_src.exists():
        sys.exit("缺少 main/alembic，无法在目标机自动建库/升级")
    shutil.copytree(alembic_src, dist_dir / "alembic", dirs_exist_ok=True)
    print("  alembic/（首次启动自动迁移）")

    (dist_dir / "data").mkdir(parents=True, exist_ok=True)
    (dist_dir / "logs").mkdir(parents=True, exist_ok=True)
    # 数据目录只占位，安装器不覆盖用户已有 data/


def main():
    if not ENTRY.exists():
        sys.exit(f"缺少桌面入口：{ENTRY}")
    if not ICON.exists():
        sys.exit(f"缺少图标：{ICON}（先运行 packaging/icon/build_icon.py）")

    purge_nuitka_cache()
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable
    print(f"Python: {python_exe}")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO / "shared") + os.pathsep + env.get("PYTHONPATH", "")
    run(nuitka_cmd(python_exe), cwd=str(ROOT), env=env)

    built = BUILD_OUT / "tq-iquant.dist"
    if not built.exists():
        candidates = list(BUILD_OUT.glob("*.dist"))
        if not candidates:
            sys.exit(f"未找到 Nuitka 产物目录：{BUILD_OUT}")
        built = candidates[0]

    if DIST_OUT.exists():
        shutil.rmtree(DIST_OUT)
    shutil.move(str(built), str(DIST_OUT))
    print(f"\n=== Nuitka 产物 -> {DIST_OUT} ===")

    copy_assets(DIST_OUT)

    size_mb = sum(f.stat().st_size for f in DIST_OUT.rglob("*") if f.is_file()) / 1024 / 1024
    print(f"\n=== 完成：{DIST_OUT}（{size_mb:.1f} MB）===")


if __name__ == "__main__":
    main()
