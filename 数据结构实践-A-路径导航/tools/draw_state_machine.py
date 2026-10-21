"""画出报告用的交互状态机图（图 4-2）。

输出：docs/截图/10-交互状态机.png
用法：python tools/draw_state_machine.py
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "截图" / "10-交互状态机.png"

W, H = 1120, 440
BG = "#ffffff"
TEXT = "#0f172a"
MUTED = "#64748b"
LINE = "#94a3b8"

STATES = {
    "idle": ("空闲", "IDLE", "#94a3b8", "#f1f5f9"),
    "ready": ("就绪", "READY", "#2563eb", "#eaf1ff"),
    "computing": ("计算中", "COMPUTING", "#d97706", "#fff7e6"),
    "result": ("已展示", "RESULT", "#16a34a", "#e9f8ef"),
    "error": ("错误", "ERROR", "#dc2626", "#fdecec"),
}

BOXES = {
    "idle": (56, 150, 196, 214),
    "ready": (250, 150, 390, 214),
    "computing": (444, 150, 624, 214),
    "result": (720, 150, 880, 214),
    "error": (720, 306, 880, 370),
}


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    name = "msyhbd.ttc" if bold else "msyh.ttc"
    for candidate in (Path("C:/Windows/Fonts") / name, Path("C:/Windows/Fonts/msyh.ttc")):
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size)
    return ImageFont.load_default()


def arrow(draw, points, color=LINE, width=2, head=9, dash=None):
    """折线箭头；dash 为虚线间隔（None 表示实线）。"""
    for i in range(len(points) - 1):
        (x1, y1), (x2, y2) = points[i], points[i + 1]
        if dash:
            length = math.hypot(x2 - x1, y2 - y1)
            step = 0
            while step < length:
                t0 = step / length
                t1 = min((step + dash) / length, 1.0)
                draw.line(
                    [x1 + (x2 - x1) * t0, y1 + (y2 - y1) * t0,
                     x1 + (x2 - x1) * t1, y1 + (y2 - y1) * t1],
                    fill=color, width=width,
                )
                step += dash * 2
        else:
            draw.line([x1, y1, x2, y2], fill=color, width=width)
    (x1, y1), (x2, y2) = points[-2], points[-1]
    angle = math.atan2(y2 - y1, x2 - x1)
    left = (x2 - head * math.cos(angle - 0.45), y2 - head * math.sin(angle - 0.45))
    right = (x2 - head * math.cos(angle + 0.45), y2 - head * math.sin(angle + 0.45))
    draw.polygon([(x2, y2), left, right], fill=color)


def label(draw, xy, text, color=MUTED, size=13, anchor="mm"):
    f = font(size)
    bbox = draw.textbbox((0, 0), text, font=f)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    x, y = xy
    if anchor == "mm":
        x -= w / 2
        y -= h / 2
    draw.rectangle([x - 5, y - 2, x + w + 5, y + h + 3], fill=BG)
    draw.text((x, y - bbox[1]), text, font=f, fill=color)


def main() -> int:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    draw.text((40, 30), "界面搜索状态机（P3-03）", font=font(22, True), fill=TEXT)
    draw.text((40, 64), "实线：正常流转    虚线：取消 / 清除    灰线：提示与重试",
              font=font(12), fill=MUTED)

    for key, (cn, en, color, fill) in STATES.items():
        x1, y1, x2, y2 = BOXES[key]
        draw.rounded_rectangle([x1, y1, x2, y2], radius=12, fill=fill, outline=color, width=2)
        draw.text(((x1 + x2) / 2, y1 + 20), cn, font=font(17, True), fill=color, anchor="mm")
        draw.text(((x1 + x2) / 2, y1 + 44), en, font=font(10), fill=MUTED, anchor="mm")

    # 正常流转
    arrow(draw, [(196, 182), (250, 182)], color="#475569")
    label(draw, (223, 168), "选好起终点", size=11)
    arrow(draw, [(390, 182), (444, 182)], color="#475569")
    label(draw, (417, 168), "搜索 / Enter", size=11)
    arrow(draw, [(624, 182), (720, 182)], color="#475569")
    label(draw, (672, 168), "找到路径", size=11)

    # 不连通 / 数据错误
    arrow(draw, [(600, 214), (600, 338), (720, 338)], color="#dc2626")
    label(draw, (660, 352), "不连通 / 数据错误", size=11, color="#b91c1c")

    # 重试
    arrow(draw, [(720, 322), (560, 322), (560, 214)], color="#94a3b8")
    label(draw, (640, 308), "重试 F5", size=11)

    # 取消
    arrow(draw, [(444, 200), (390, 200)], color="#94a3b8", dash=6)
    label(draw, (417, 216), "取消", size=11)

    # 结果回到就绪
    arrow(draw, [(800, 214), (800, 258), (320, 258), (320, 214)], color="#94a3b8")
    label(draw, (560, 272), "改选起终点 / Esc 清除", size=11)

    # 就绪回到空闲
    arrow(draw, [(250, 196), (196, 196)], color="#94a3b8", dash=6)
    label(draw, (223, 214), "清空选择", size=11)

    # 空闲自环：未选点
    draw.arc([104, 126, 148, 174], start=180, end=360, fill="#b45309", width=2)
    arrow(draw, [(146, 144), (148, 151)], color="#b45309", head=7)
    label(draw, (126, 112), "未选点：弹窗提示", size=11, color="#b45309")

    # 计算中：按钮禁用、连点只算一次
    label(draw, (534, 118), "计算中：按钮禁用、连点只算一次", size=11, color="#b45309")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT)
    print(f"saved {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
