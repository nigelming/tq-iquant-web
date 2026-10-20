# 项目封装分发 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将项目封装为 Nuitka standalone 文件夹，供没有 Python 环境的电脑使用。

**Architecture:** Nuitka `--standalone` 编译 Python 后端为独立 exe，前端 `web/dist/` 和 `config.yaml`、`data/` 放在 exe 同级目录。FastAPI 同时托管 API + 静态文件。`db.py` 和 `config.py` 的路径解析改为相对 exe 所在目录。

**Tech Stack:** Python 3.13 + Nuitka + FastAPI + uvicorn + Vue 3 + SQLite

## Global Constraints

- Nuitka `--standalone` 模式编译（非 --onefile）
- 目标电脑无需 Python 环境，但需安装通达信（TQ 模块依赖）
- config.yaml 和 dev.db 放在 exe 同级目录（外部配置 + 自动创建）
- 前端 `web/dist/` 已构建好，直接复制
- `shared/` 包由 Nuitka `--include-package=shared` 自动打包
- TQ 模块的 `tqcenter` 来自通达信，不打包进 exe
- exe 窗口模式（`--windows-disable-console`），不显示命令行窗口

---

### Task 1: 代码改动 — 静态文件挂载 + 路径解析 + 启动入口

**Files:**
- Modify: `main/core/main.py`
- Modify: `main/core/db.py`
- Modify: `main/core/config.py`

**Interfaces:**
- Produces: `tq-iquant.exe` 启动入口（`if __name__ == "__main__"`）
- Produces: 静态文件挂载 `app.mount("/", ...)`
- Produces: 数据库路径 `_db_path()` 相对 exe 解析
- Produces: 配置路径 `_default_config_path()` 相对 exe 解析

- [ ] **Step 1: main.py 新增静态文件挂载**

在 `include_router` 之后、`exception_handler` 之前插入：

```python
from fastapi.staticfiles import StaticFiles
from pathlib import Path as _Path

_dist = _Path(__file__).resolve().parent.parent.parent / "web" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
```

- [ ] **Step 2: main.py 新增 uvicorn 启动入口**

在文件末尾追加：

```python


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

- [ ] **Step 3: db.py 路径改为相对 exe 解析**

读取 `main/core/db.py` 当前内容，找到数据库路径定义。改为：

```python
import sys
from pathlib import Path

def _db_path() -> Path:
    """数据库路径：优先 config.yaml，fallback 到 exe 同级目录。"""
    from core.config import load_config
    cfg = load_config()
    sqlite_path = cfg.get("database", {}).get("sqlite_path", "data/dev.db")
    p = Path(sqlite_path)
    if not p.is_absolute():
        if hasattr(sys, 'frozen'):
            exe_dir = Path(sys.argv[0]).resolve().parent
        else:
            exe_dir = Path(__file__).resolve().parent.parent
        p = exe_dir / p
    return p
```

- [ ] **Step 4: config.py 路径改为相对 exe 解析**

读取 `main/core/config.py` 当前内容，找到 `_default_config_path()` 或等效函数。改为：

```python
import sys
from pathlib import Path

def _default_config_path() -> Path:
    if hasattr(sys, 'frozen'):
        return Path(sys.argv[0]).resolve().parent / "config.yaml"
    return Path(__file__).resolve().parent.parent.parent / "config.yaml"
```

- [ ] **Step 5: 运行测试确认无回归**

Run: `uv run pytest core/tests/ -q` from `D:\project\tq-iquant-web\main`
Expected: 全部通过

- [ ] **Step 6: 手动验证静态文件服务**

Run: `uv run uvicorn core.main:app --port 8000` from `D:\project\tq-iquant-web\main`
Run: 浏览器访问 `http://127.0.0.1:8000`
Expected: 前端页面正常加载

- [ ] **Step 7: 提交**

```bash
git add main/core/main.py main/core/db.py main/core/config.py
git commit -m "feat(package): 静态文件挂载 + exe 相对路径解析 + uvicorn 启动入口"
```

---

### Task 2: 构建脚本 — build_package.py

**Files:**
- Create: `main/build_package.py`
- Create: `main/启动.bat`
- Create: `main/README-分发说明.txt`

**Interfaces:**
- Consumes: Task 1 的代码改动
- Produces: `dist/tq-iquant/` 完整分发目录

- [ ] **Step 1: 创建 build_package.py**

```python
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
    run_cmd([
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--include-package=core",
        "--include-package=shared",
        "--include-package=sqlalchemy",
        "--include-package=fastapi",
        "--include-package=uvicorn",
        "--include-package=pandas",
        "--include-package=polars",
        "--include-package=pydantic",
        "--include-package=httpx",
        "--include-package=pytdx",
        "--include-package=openpyxl",
        "--include-package=pyyaml",
        "--include-package=alembic",
        "--include-package=tqdm",
        "--windows-disable-console",
        "--windows-icon-from-ico=" + str(ROOT.parent / "web" / "public" / "favicon.ico"),
        "--output-filename=tq-iquant",
        "--output-dir=" + str(ROOT / "dist" / "tq-iquant"),
        "core/main.py",
    ], cwd=str(ROOT))

    print("\n=== Step 2: 复制前端静态文件 ===")
    dest_web = OUTPUT_DIR / "_internal" / "web" / "dist"
    dest_web.parent.mkdir(parents=True, exist_ok=True)
    if WEB_DIST.exists():
        shutil.copytree(WEB_DIST, dest_web, dirs_exist_ok=True)
        print(f"  copied web/dist -> {dest_web}")
    else:
        print(f"  WARNING: {WEB_DIST} not found, run 'npm run build' first")

    print("\n=== Step 3: 复制 config.yaml ===")
    if CONFIG_YAML.exists():
        shutil.copy2(CONFIG_YAML, OUTPUT_DIR / "config.yaml")
        print(f"  copied config.yaml")

    print("\n=== Step 4: 创建 data/ 和 logs/ 目录 ===")
    (OUTPUT_DIR / "data").mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "logs").mkdir(parents=True, exist_ok=True)

    print("\n=== Step 5: 创建启动.bat ===")
    bat = OUTPUT_DIR / "启动.bat"
    bat.write_text('@echo off\r\nchcp 65001 >nul\r\ncd /d "%~dp0"\r\nstart "" "tq-iquant.exe"\r\n', encoding='utf-8')

    print("\n=== Step 6: 创建 README-分发说明.txt ===")
    readme = OUTPUT_DIR / "README-分发说明.txt"
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
```

- [ ] **Step 2: 验证脚本可运行**

Run: `python build_package.py` from `D:\project\tq-iquant-web\main`
Expected: 编译成功，`dist/tq-iquant/` 目录结构正确

- [ ] **Step 3: 提交**

```bash
git add main/build_package.py
git commit -m "feat(package): build_package.py 构建脚本"
```

---

### Task 3: 安装 Nuitka + 端到端验证

**Files:**
- Modify: `main/pyproject.toml`（将 pytest 移到 dev 依赖）

**Interfaces:**
- Consumes: Task 1 的代码改动 + Task 2 的构建脚本
- Produces: 可分发的 `dist/tq-iquant/` 文件夹

- [ ] **Step 1: 安装 Nuitka**

Run: `uv pip install nuitka orderedset zstandard` from `D:\project\tq-iquant-web\main`
Expected: 安装成功

- [ ] **Step 2: 清理旧构建产物**

Run: `Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue` from `D:\project\tq-iquant-web\main`
Expected: 清理干净

- [ ] **Step 3: 运行构建脚本**

Run: `python build_package.py` from `D:\project\tq-iquant-web\main`
Expected: Nuitka 编译成功（5-15 分钟），后处理完成

- [ ] **Step 4: 验证 exe 启动**

Run: `.\dist\tq-iquant\tq-iquant.exe` from `D:\project\tq-iquant-web\main`
Expected: 程序启动，无 Python 依赖缺失报错

- [ ] **Step 5: 验证 API 响应**

Run: `Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/backtest/records" -UseBasicParsing`
Expected: 返回 200 + JSON 数据

- [ ] **Step 6: 验证前端静态文件**

Run: 浏览器访问 `http://127.0.0.1:8000`
Expected: 前端页面正常加载

- [ ] **Step 7: 验证数据库自动创建**

Run: 检查 `dist/tq-iquant/data/dev.db` 是否存在
Expected: 数据库文件存在

- [ ] **Step 8: 验证暗色模式**

Run: 前端切换暗色模式
Expected: 暗色模式正常显示

- [ ] **Step 9: 验证无通达信时启动**

Run: 关闭通达信后启动 exe
Expected: 程序正常启动，提示通达信未运行但不崩溃

- [ ] **Step 10: 提交 pyproject.toml 改动**

```bash
git add main/pyproject.toml
git commit -m "chore(package): pytest 移到 dev 依赖"
```

---

## Self-Review

1. **Spec coverage**: 设计文档的 3 处代码改动（main.py/db.py/config.py）全部在 Task 1；构建脚本在 Task 2；端到端验证在 Task 3。
2. **Placeholder scan**: 无 TBD/TODO；所有代码块为完整实现。
3. **Type consistency**: `_db_path()` 和 `_default_config_path()` 在 Task 1 定义，Task 2/3 消费，一致。
