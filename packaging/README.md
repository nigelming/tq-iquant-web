# 打包与安装器

面向 Windows 目标机（**无需安装 Python**）。采用 **Nuitka standalone（onedir，非 onefile）**
+ **Inno Setup 6** 安装包。

## 构建顺序

```powershell
# 1) 图标（矢量母版 -> app.ico，仅首次或改图标时执行）
main\.venv\Scripts\python.exe packaging\icon\build_icon.py

# 2) 前端（文案改动稳定后再执行）
cd web; npm run build; cd ..

# 3) 后端 onedir 编译（产物 dist/tq-iquant）
main\.venv\Scripts\python.exe main\build_package.py

# 4) 安装包（产物 dist/installer/*.exe）
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
```

## 产物与约定

| 路径 | 说明 |
|---|---|
| `dist/tq-iquant/` | onedir 分发目录：`tq-iquant.exe` + `_internal/`（含 `web/dist` 前端）+ `alembic/` + `config.template.yaml` |
| `dist/installer/` | Inno 安装包（`创懿基金量化投资平台-<版本>-安装程序.exe`） |
| `dist/build/` | Nuitka 中间产物与模块缓存（不入库） |

- 安装位置默认 `D:\创懿基金量化投资平台`，安装需管理员权限；**程序日常运行不提权**。
- 数据根目录：优先 `<安装目录>`（需可写），不可写时自动回落
  `%LOCALAPPDATA%\创懿基金量化投资平台`。`data/`（SQLite、同花顺交接文件）、
  `logs/`、`config.yaml` 都在该根目录下；首次启动从 `config.template.yaml` 生成
  `config.yaml`，此后用户修改不会被覆盖。
- 卸载默认**保留**数据目录（卸载时可选删除）；升级安装不触碰 `data/`。
- `bridge\` 目录随包附带**明文**桥脚本（`iquant_bridge.py` 8790 股票账户、
  `goldminer_bridge.py` 7801）与部署说明；桥脚本**不编译进 exe**，必须在
  QMT 客户端 Python 3.6.8 / 掘金终端内运行。
- 不要求代码签名，因此首次运行会触发 SmartScreen 提示（已知取舍）。

## 授权/许可证机制的预留位置

本轮不含任何授权校验。将来引入时，唯一侵入点是**启动校验 + 写操作中间件**：

1. **启动校验**：`main/desktop.py::main()` 在 `_start_server()` 之前插入校验，
   校验失败直接弹窗并退出，不启动后端（WebView2/浏览器均拿不到界面）。
2. **写操作中间件**：`main/core/main.py` 的 FastAPI app 上挂一个全局依赖
   （或 `@app.middleware("http")`），统一拦截 `POST/PUT/PATCH/DELETE`，
   校验通过后放行；读接口与 SSE 不受影响。

这样授权逻辑不侵入业务路由与引擎，现有结构无需为将来改动。
