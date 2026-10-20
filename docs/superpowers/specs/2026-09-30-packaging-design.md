# 项目封装分发设计文档

## 目标

将项目封装为 Nuitka standalone 文件夹，供没有 Python 环境的电脑使用。包括前端静态文件、SQLite 数据库、配置文件一并封装。

## 架构

### 编译产物目录结构

```
tq-iquant-web/
├── tq-iquant.exe          # Nuitka 编译的主程序
├── core/                  # Nuitka 自动打包的 Python 包
├── shared/                # Nuitka 自动打包的 shared 包
├── _internal/             # Nuitka standalone 依赖（Python runtime + DLLs）
├── web/
│   └── dist/              # 前端构建产物
├── config.yaml            # 外部配置（用户可编辑）
├── data/
│   └── dev.db             # SQLite 数据库（首次启动自动创建）
├── logs/                  # 日志目录（自动创建）
├── 启动.bat               # 双击启动脚本
└── README-分发说明.txt    # 使用说明
```

### 启动流程

1. 用户双击 `启动.bat`
2. bat 脚本设置工作目录为 exe 同级目录
3. 启动 `tq-iquant.exe`
4. exe 内部启动 uvicorn（程序内调用，非命令行）
5. 首次启动自动创建 `data/dev.db` + 运行 Alembic 迁移
6. FastAPI 同时托管 API + 前端静态文件
7. 浏览器自动打开 `http://127.0.0.1:8000`

## Nuitka 编译与构建流程

### 构建环境准备

```bash
uv pip install nuitka orderedset zstandard
```

### Nuitka 编译命令

```bash
cd main

python -m nuitka \
  --standalone \
  --onefile \
  --include-package=core \
  --include-package=shared \
  --include-package=sqlalchemy \
  --include-package=fastapi \
  --include-package=uvicorn \
  --include-package=pandas \
  --include-package=polars \
  --include-package=pydantic \
  --include-package=httpx \
  --include-package=pytdx \
  --include-package=openpyxl \
  --include-package=pyyaml \
  --include-package=alembic \
  --include-package=tqdm \
  --windows-disable-console \
  --windows-icon-from-ico=../web/public/favicon.ico \
  --output-filename=tq-iquant \
  core/main.py
```

### 编译后处理

1. 复制 `web/dist/` → `dist/tq-iquant/_internal/web/dist/`
2. 复制 `config.yaml` → `dist/tq-iquant/config.yaml`
3. 创建 `dist/tq-iquant/data/` 空目录
4. 创建 `dist/tq-iquant/logs/` 空目录
5. 创建 `启动.bat`
6. 复制 `README-分发说明.txt`

### TQ 模块处理

- `tqcenter` 包来自通达信安装目录，不打包进 exe
- exe 运行时从 `config.yaml` 的 `tdx_path` 读取通达信路径
- 启动时检测通达信是否运行，未运行则提示但允许启动

### shared/ 包处理

- `shared/` 是纯 Python 包，零外部依赖
- Nuitka `--include-package=shared` 自动打包

## 代码改动

### `main/core/main.py`（3 处）

1. **新增静态文件挂载**：
```python
from fastapi.staticfiles import StaticFiles
from pathlib import Path

_dist = Path(__file__).resolve().parent.parent.parent / "web" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="static")
```

2. **新增程序内 uvicorn 启动入口**：
```python
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
```

### `main/core/db.py`

数据库路径改为相对 exe 解析：
```python
def _db_path() -> Path:
    cfg = load_config()
    sqlite_path = cfg.get("database", {}).get("sqlite_path", "data/dev.db")
    p = Path(sqlite_path)
    if not p.is_absolute():
        exe_dir = Path(sys.argv[0]).parent if hasattr(sys, 'frozen') else Path(__file__).parent.parent
        p = exe_dir / p
    return p
```

### `main/core/config.py`

config.yaml 路径改为相对 exe 解析：
```python
def _default_config_path() -> Path:
    if hasattr(sys, 'frozen'):
        return Path(sys.argv[0]).parent / "config.yaml"
    return Path(__file__).resolve().parent.parent.parent / "config.yaml"
```

### `启动.bat`

```bat
@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "tq-iquant.exe"
```

## 不改动

- 前端代码零改动（`web/dist/` 已构建好）
- API 路由零改动
- 引擎/服务层零改动
- TQ 模块零改动（运行时从通达信加载）

## 测试策略

### 构建验证

- Nuitka 编译成功无错误
- exe 启动无 Python 依赖缺失
- 前端静态文件正常加载
- API 正常响应
- 数据库自动创建 + 迁移成功

### 功能验证

- 首页数据加载正常
- 回测列表加载正常
- 回测详情页正常
- 暗色模式切换正常

### 边界情况

- 无通达信时启动：提示但允许启动
- 无 config.yaml 时使用默认配置
- 无 data/ 目录时自动创建
- 端口 8000 被占用时提示

## 风险点

- Nuitka 编译时间较长（5-15 分钟）
- Nuitka 可能遗漏隐式导入的包（需测试验证）
- polars/pandas 等 C 扩展包可能需要额外 `--include-package`
- TQ 模块的 `tqcenter` 来自通达信，不在 Nuitka 打包范围
- 分发体积较大（~200MB），需压缩为 zip
