"""统一响应 envelope：{"code":0,"message":"ok","data":...}。

设计文档 §5.6.1 规定所有 API 返回此格式。code=0 成功，非 0 错误码与 HTTP 状态码对齐
（404/409/422/500）。ok()/err() 强制三键齐全，杜绝手工构造 dict 时漏 message/data。

前端拦截器（web/src/api/index.ts）按 code !== 0 reject，依赖三键齐全的 envelope。
SSE 端点（live.py stream）与有意保留真实 HTTP 状态码的端点（backtest 409 并发锁）
不走本工具——它们需要真实 HTTP 错误码触发 EventSource onerror / 前端 catch。
"""


def ok(data=None, message: str = "ok") -> dict:
    """成功响应：code=0。data 省略时为 None（明确空值，非缺键 undefined）。"""
    return {"code": 0, "message": message, "data": data}


def err(code: int, message: str, data=None) -> dict:
    """错误响应：code 与 HTTP 状态码对齐（404/409/422/500...），HTTP 仍 200。

    前端拦截器见 code !== 0 即 reject 并带 message；data 默认 None。
    """
    return {"code": code, "message": message, "data": data}
