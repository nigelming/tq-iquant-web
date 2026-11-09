"""桌面壳启动器（打包版入口）。

行为：
1. 解析可写根目录（安装目录不可写时回落 %LOCALAPPDATA%\\创懿基金量化投资平台），
   首次启动从 config.template.yaml 生成 config.yaml，并切换工作目录到该根目录
   （config.yaml 里的相对路径 data/dev.db、data/ths 都以此为准）。
2. 后台线程启动 uvicorn（默认 127.0.0.1:8888），等待 /health 就绪。
3. 优先用 pywebview + WebView2 开原生窗口；WebView2 缺失/CLR 加载失败时
   **自动回退默认浏览器**（验收底线：不允许因此启动失败）。
4. 窗口关闭 → 通知 uvicorn 退出；浏览器模式下由前端心跳/关闭探针负责退出
   （core.services.auto_shutdown），进程本身驻留等待。
"""
import logging
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

APP_TITLE = "创懿基金量化投资平台"
DEFAULT_PORT = 8888

logger = logging.getLogger("desktop")


def _setup_basic_logging() -> None:
    """uvicorn 起来前先把启动信息落到 stderr（打包版无控制台时仅内存）。"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _pick_port() -> tuple:
    """返回 (端口, 是否已有本程序在运行)。"""
    preferred = int(os.environ.get("TQ_PORT") or DEFAULT_PORT)
    url = f"http://127.0.0.1:{preferred}"
    if _port_free(preferred):
        return preferred, False
    if _healthy(url):
        return preferred, True
    for port in range(preferred + 1, preferred + 11):
        if _port_free(port):
            logger.warning("端口 %d 被占用，改用 %d", preferred, port)
            return port, False
    raise SystemExit(f"端口 {preferred}~{preferred + 10} 均被占用，请先关闭其它程序")


def _start_server(port: int):
    import uvicorn

    from core.main import app

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()
    return server, thread


def _wait_ready(url: str, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _healthy(url):
            return True
        time.sleep(0.25)
    return False


def _open_window(url: str) -> bool:
    """pywebview 原生窗口；任何失败都返回 False 交由浏览器回退。"""
    try:
        import webview
    except Exception:  # noqa: BLE001 — 依赖缺失/CLR 加载失败都走回退
        logger.exception("pywebview 导入失败，回退默认浏览器")
        return False
    try:
        webview.create_window(
            APP_TITLE, url, width=1440, height=900, min_size=(1024, 640)
        )
        webview.start()
        return True
    except Exception:  # noqa: BLE001
        logger.exception("WebView2 窗口启动失败，回退默认浏览器")
        return False


def main() -> int:
    _setup_basic_logging()

    os.environ.setdefault("PYTHONUTF8", "1")
    from core.config import app_root, ensure_config

    root = app_root()
    (root / "data").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    cfg_path = ensure_config()
    os.chdir(root)  # config.yaml 内的相对路径（data/dev.db、data/ths）以此为准
    logger.info("数据目录：%s", root)
    logger.info("配置文件：%s", cfg_path)

    port, already = _pick_port()
    url = f"http://127.0.0.1:{port}"

    if already:
        logger.info("检测到已在运行：%s", url)
        server = None
    else:
        server, _thread = _start_server(port)
        if not _wait_ready(url):
            logger.error("后端 60s 内未就绪，请查看 logs/core.log")
            if server is not None:
                server.should_exit = True
            return 2
        logger.info("后端就绪：%s", url)

    force_browser = "--browser" in sys.argv or os.environ.get("TQ_BROWSER") == "1"
    no_open = "--no-open" in sys.argv  # 仅自检/冒烟用：只起后端，不开界面
    opened = False if (force_browser or no_open) else _open_window(url)

    if no_open:
        logger.info("已跳过界面打开（--no-open），后端运行中：%s", url)
        while server is None or not server.should_exit:
            time.sleep(0.5)
        return 0

    if not opened:
        webbrowser.open(url)
        logger.info("已用默认浏览器打开 %s（关闭窗口后进程自动退出）", url)
        if server is not None:
            while not server.should_exit:
                time.sleep(0.5)
        else:
            # 复用已有实例：本进程无事可做，直接退出
            return 0
        return 0

    # 原生窗口已关闭：收尾退出
    if server is not None:
        server.should_exit = True
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
