#!/usr/bin/env python
"""Nuitka 编译 + 后处理脚本"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # main/
WEB_DIST = ROOT.parent / "web" / "dist"
CONFIG_YAML = ROOT.parent / "config.yaml"
OUTPUT_DIR = ROOT / "dist" / "tq-iquant"

def run_cmd(cmd, cwd=None):
    print(f"\n>>> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=False)
    if result.returncode != 0:
        print(f"FAILED: {cmd}")
        sys.exit(1)

def main():
    print("=== Step 1: Nuitka 编译 ===")
    venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
    python_exe = str(venv_python) if venv_python.exists() else sys.executable
    nuitka_cmd = [
        python_exe, "-m", "nuitka",
        "--standalone",
        "--include-package=core",
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
        "--windows-disable-console",
        "--output-filename=tq-iquant",
        "--output-dir=" + str(ROOT / "dist" / "tq-iquant"),
    ]
    icon_path = ROOT.parent / "web" / "public" / "favicon.ico"
    if icon_path.exists():
        nuitka_cmd.append("--windows-icon-from-ico=" + str(icon_path))
    else:
        print(f"  WARNING: {icon_path} not found, skipping icon")
    nuitka_cmd.append("core/main.py")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT.parent / "shared") + os.pathsep + env.get("PYTHONPATH", "")
    print(f"  PYTHONPATH={env['PYTHONPATH']}")
    result = subprocess.run(nuitka_cmd, cwd=str(ROOT), env=env)
    if result.returncode != 0:
        print(f"FAILED: {nuitka_cmd}")
        sys.exit(1)

    dist_dir = OUTPUT_DIR / "main.dist"

    print("\n=== Step 2: 复制前端静态文件 ===")
    dest_web = dist_dir / "_internal" / "web" / "dist"
    dest_web.parent.mkdir(parents=True, exist_ok=True)
    if WEB_DIST.exists():
        shutil.copytree(WEB_DIST, dest_web, dirs_exist_ok=True)
        print(f"  copied web/dist -> {dest_web}")
    else:
        print(f"  WARNING: {WEB_DIST} not found, run 'npm run build' first")

    print("\n=== Step 3: 复制 config.yaml ===")
    if CONFIG_YAML.exists():
        shutil.copy2(CONFIG_YAML, dist_dir / "config.yaml")
        print(f"  copied config.yaml")

    print("\n=== Step 4: 创建 data/ 和 logs/ 目录 ===")
    (dist_dir / "data").mkdir(parents=True, exist_ok=True)
    (dist_dir / "logs").mkdir(parents=True, exist_ok=True)

    print("\n=== Step 5: 创建启动.bat ===")
    bat = dist_dir / "启动.bat"
    bat.write_text('@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\nstart "" "tq-iquant.exe"\r\n', encoding='utf-8')

    print("\n=== Step 6: 创建 README-分发说明.txt ===")
    readme = dist_dir / "README-分发说明.txt"
    readme.write_text(
        "创懿量化交易平台 — 分发说明\r\n"
        "========================\r\n"
        "\r\n"
        "1. 双击「启动.bat」运行程序\r\n"
        "2. 浏览器自动打开 http://127.0.0.1:8000\r\n"
        "3. 首次启动会自动创建数据库\r\n"
        "\r\n"
        "注意事项：\r\n"
        "- 需要安装通达信客户端并运行才能获取实时行情\r\n"
        "- config.yaml 可配置通达信路径\r\n"
        "- data/ 目录存放 SQLite 数据库\r\n"
        "- logs/ 目录存放运行日志\r\n"
        "- 端口 8000 被占用时请先关闭占用程序\r\n",
        encoding='utf-8'
    )

    print(f"\n=== 完成！分发目录: {OUTPUT_DIR} ===")

if __name__ == "__main__":
    main()
