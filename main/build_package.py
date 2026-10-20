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
        "--low-memory",
        "--assume-yes-for-downloads",
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

    print("\n=== Step 2b: 复制 echarts.min.js（离线报告内联运行时）===")
    echarts_src = ROOT.parent / "web" / "node_modules" / "echarts" / "dist" / "echarts.min.js"
    if echarts_src.exists():
        shutil.copy2(echarts_src, dist_dir / "echarts.min.js")
        print(f"  copied echarts.min.js -> {dist_dir.name}/echarts.min.js")
    else:
        print(f"  WARNING: {echarts_src} not found, 离线报告导出将失败")

    print("\n=== Step 3: 复制 config.yaml ===")
    if CONFIG_YAML.exists():
        shutil.copy2(CONFIG_YAML, dist_dir / "config.yaml")
        print(f"  copied config.yaml")

    print("\n=== Step 4: 创建 data/ 和 logs/ 目录 ===")
    (dist_dir / "data").mkdir(parents=True, exist_ok=True)
    (dist_dir / "logs").mkdir(parents=True, exist_ok=True)

    print("\n=== Step 5: 复制 alembic 迁移目录 ===")
    alembic_src = ROOT / "alembic"
    if alembic_src.exists():
        dest_alembic = dist_dir / "alembic"
        shutil.copytree(alembic_src, dest_alembic, dirs_exist_ok=True)
        print(f"  copied alembic -> {dest_alembic}")
    else:
        print(f"  WARNING: {alembic_src} not found, 数据库迁移将无法执行")

    print("\n=== Step 6: 创建启动.bat ===")
    bat = dist_dir / "启动.bat"
    # 必须以 GBK(ANSI) 编码保存：cmd 对 UTF-8 bat 的中文行会按字节错位解析
    # （即使 chcp 65001），chcp 936 保证在任意父代码页下中文提示均正常。
    bat_lines = [
        "@echo off",
        "chcp 936 >nul",
        "setlocal",
        'cd /d "%~dp0"',
        "title 创懿量化交易平台",
        "",
        "rem ===== 1. 程序已在运行（8888端口监听中）则直接打开浏览器 =====",
        'netstat -ano -p TCP | findstr /C:":8888 " | findstr /C:"LISTENING" >nul 2>nul',
        "if %errorlevel%==0 goto already",
        "",
        "rem ===== 2. 启动主程序（不阻塞，后台运行）=====",
        "echo 正在启动创懿量化交易平台，请稍候...",
        'start "" "tq-iquant.exe"',
        "",
        "rem ===== 3. 等待服务就绪，最多约30秒 =====",
        "set n=0",
        ":wait",
        "set /a n+=1",
        "if %n% gtr 30 goto fail",
        "ping -n 2 127.0.0.1 >nul",
        'netstat -ano -p TCP | findstr /C:":8888 " | findstr /C:"LISTENING" >nul 2>nul',
        "if %errorlevel%==0 goto ready",
        "goto wait",
        "",
        ":ready",
        "echo 启动成功，正在打开浏览器...",
        'start "" "http://127.0.0.1:8888"',
        "exit /b 0",
        "",
        ":already",
        "echo 程序已在运行中，正在打开浏览器...",
        'start "" "http://127.0.0.1:8888"',
        "ping -n 3 127.0.0.1 >nul",
        "exit /b 0",
        "",
        ":fail",
        "echo.",
        "echo ============================================================",
        "echo [错误] 启动失败：等待30秒后服务仍未就绪",
        "echo.",
        "echo 请按以下顺序排查：",
        "echo   1. 端口8888是否被其他程序占用，关闭后重试",
        "echo   2. 杀毒软件/安全卫士是否拦截，请添加信任",
        "echo   3. 请保持本程序目录完整，不要单独移动exe",
        "echo.",
        "echo 需要技术支持时，请将本窗口截图发送",
        "echo ============================================================",
        "echo.",
        "pause",
        "exit /b 1",
        "",
    ]
    bat.write_text("\r\n".join(bat_lines), encoding="gbk")

    print("\n=== Step 7: 创建 README-分发说明.txt ===")
    readme = dist_dir / "README-分发说明.txt"
    readme.write_text(
        "创懿量化交易平台 — 分发说明\r\n"
        "========================\r\n"
        "\r\n"
        "1. 双击「启动.bat」运行程序\r\n"
        "2. 浏览器自动打开 http://127.0.0.1:8888\r\n"
        "3. 首次启动会自动创建数据库\r\n"
        "\r\n"
        "注意事项：\r\n"
        "- 需要安装通达信客户端并运行才能获取实时行情\r\n"
        "- config.yaml 可配置通达信路径\r\n"
        "- data/ 目录存放 SQLite 数据库\r\n"
        "- logs/ 目录存放运行日志\r\n"
        "- 端口 8888 被占用时请先关闭占用程序\r\n",
        encoding='utf-8'
    )

    print("\n=== Step 8: 整理最终分发目录 ===")
    # Nuitka standalone 产物为 <output-dir>/main.dist；将其提升为最终的 tq-iquant 目录。
    # 同时保留旧包中的用户数据库（data/）与用户配置（config.yaml），
    # 避免一次干净构建覆盖掉用户的实盘数据。
    built = dist_dir
    staging = OUTPUT_DIR.parent / "_tq_new"
    if staging.exists():
        shutil.rmtree(staging)
    shutil.move(str(built), str(staging))

    old_data = OUTPUT_DIR / "data"
    if old_data.exists():
        if (staging / "data").exists():
            shutil.rmtree(staging / "data")
        shutil.copytree(old_data, staging / "data")
        print("  保留旧包 data/（用户数据库）")
    old_cfg = OUTPUT_DIR / "config.yaml"
    if old_cfg.exists():
        shutil.copy2(old_cfg, staging / "config.yaml")
        print("  保留旧包 config.yaml（用户配置）")

    # 只清空 OUTPUT_DIR 的内容、保留目录本身：IDE/工具宿主可能以该目录为当前工作目录，
    # 删除目录本身会 WinError 32。带重试以容忍杀毒软件瞬时扫描占用。
    import time
    for attempt in range(5):
        entries = list(OUTPUT_DIR.iterdir())
        if not entries:
            break
        try:
            for entry in entries:
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
        except OSError:
            if attempt == 4:
                raise
            time.sleep(2)

    for entry in staging.iterdir():
        shutil.move(str(entry), str(OUTPUT_DIR / entry.name))
    shutil.rmtree(staging)

    print(f"\n=== 完成！分发目录: {OUTPUT_DIR} ===")

if __name__ == "__main__":
    main()
