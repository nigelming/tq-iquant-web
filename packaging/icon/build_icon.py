"""矢量图标母版 → Windows .ico 构建脚本。

流程：packaging/icon/src/*.svg（1024 矢量母版）
  → node render.js（resvg 光栅化，1024×1024 PNG）
  → 本脚本用 Lanczos 生成 16/32/48/64/128/256 各档并写入 app.ico

256 帧必须是 PNG 压缩（Windows Vista+ 图标规范，BMP 帧上限 128），
脚本末尾会解析 ICO 目录逐帧校验并打印。
"""
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
MASTER_SVG = "candles-ascending.svg"
MASTER_PNG = HERE / "master_1024.png"
ICO = HERE / "app.ico"
SIZES = [16, 32, 48, 64, 128, 256]


def render_master() -> None:
    node = subprocess.run(
        ["node", str(HERE / "render.js"), MASTER_SVG],
        cwd=str(HERE), capture_output=True, text=True,
    )
    if node.returncode != 0:
        print(node.stdout, node.stderr)
        raise SystemExit("resvg 渲染失败：请先在 packaging/icon 下执行 npm i")


def _bmp_frame(img: Image.Image) -> bytes:
    """32bpp BMP 帧（ICO 内嵌格式）：行序自下而上，附 1bpp AND 掩码。"""
    w, h = img.size
    px = img.load()
    rows = []
    for y in range(h - 1, -1, -1):
        row = bytearray()
        for x in range(w):
            r, g, b, a = px[x, y]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    pixels = b"".join(rows)

    stride = ((w + 31) // 32) * 4
    mask = bytearray()
    for y in range(h - 1, -1, -1):
        bits = bytearray(stride)
        for x in range(w):
            if px[x, y][3] == 0:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits

    header = struct.pack(
        "<IiiHHIIiiII",
        40, w, h * 2, 1, 32, 0, len(pixels) + len(mask), 0, 0, 0, 0,
    )
    return header + pixels + bytes(mask)


def _png_frame(img: Image.Image) -> bytes:
    from io import BytesIO

    buf = BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def write_ico(frames: dict, path: Path) -> None:
    """自定义写 ICO：16~128 用 BMP（兼容老 shell），256 用 PNG（Windows 规范）。"""
    blobs = {}
    for size, img in frames.items():
        blobs[size] = _png_frame(img) if size >= 256 else _bmp_frame(img)

    count = len(blobs)
    header = struct.pack("<HHH", 0, 1, count)
    offset = 6 + 16 * count
    entries, body = b"", b""
    for size in sorted(blobs, reverse=True):
        data = blobs[size]
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset
        )
        body += data
        offset += len(data)
    path.write_bytes(header + entries + body)


def verify_ico(path: Path) -> None:
    """解析 ICO 目录：确认各档尺寸齐全、256 帧为 PNG 压缩。"""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert (reserved, kind) == (0, 1), "ICO 头非法"
    print(f"ICO 校验：{path.name}  {count} 帧  共 {len(data)} 字节")
    for i in range(count):
        w, h, _, _, _, bpp, nbytes, offset = struct.unpack_from("<BBBBHHII", data, 6 + i * 16)
        size = w or 256
        is_png = data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
        print(f"  {size:>3}x{size:<3} bpp={bpp} 字节={nbytes:<7} 格式={'PNG' if is_png else 'BMP'}")
        if size >= 256 and not is_png:
            raise SystemExit("256 帧不是 PNG 压缩，Windows 资源管理器会显示异常")


def main() -> None:
    if not (HERE / "node_modules" / "@resvg").exists():
        print("首次运行：安装 resvg 渲染器 ...")
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not npm:
            raise SystemExit("未找到 npm：请先安装 Node.js，或在 packaging/icon 下执行 npm i")
        subprocess.run([npm, "i", "--silent"], cwd=str(HERE), check=True)
    render_master()

    master = Image.open(MASTER_PNG).convert("RGBA")
    if master.size != (1024, 1024):
        raise SystemExit(f"母版尺寸异常：{master.size}")

    rendered = {s: master.resize((s, s), Image.LANCZOS) for s in SIZES}
    write_ico(rendered, ICO)
    for s, img in rendered.items():
        img.save(HERE / f"preview_{s}.png")

    verify_ico(ICO)
    print(f"\n输出：{ICO}")


if __name__ == "__main__":
    sys.exit(main())
