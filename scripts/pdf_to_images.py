#!/usr/bin/env python3
"""PDF 转图片 —— 明确区分两种完全不同的事，不要混用。

    --mode render  （默认）整页渲染：把一页当成一张图输出，所见即所得。
                   水印、页眉、扫描 App 的 logo/二维码都会保留，因为它们是页面
                   最终显示的一部分。用于人工审阅、前后对比、送 OCR、发预览图。

    --mode extract 提取内嵌图：把 PDF 里真正嵌着的图片对象原样导出。
                   不含矢量水印和文字层；可能同时导出 logo、二维码等叠加小图，
                   所以它是"素材清单"，不是"页面图"。用于确认哪一张才是正文底图。

用法:
    python pdf_to_images.py <input.pdf> [out_dir] [--mode render|extract]
                            [--dpi 150] [--format png|jpg] [--pages 1-3,5]

注意:render 与 extract 的输出不能互相替代。要"去水印后重建 PDF"，用
     rebuild_from_images.py；它走的是 extract 的底图逻辑，不是 render。
"""
import argparse
import os
import sys

import pymupdf


def parse_pages(spec, total):
    """支持 1-3,5,8-10 这种页码写法；不传则全部。"""
    if not spec:
        return list(range(1, total + 1))
    picked = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            picked.extend(range(int(a), int(b) + 1))
        else:
            picked.append(int(part))
    return [p for p in picked if 1 <= p <= total]


def render(src, out_dir, dpi, fmt, pages, prefix):
    doc = pymupdf.open(src)
    os.makedirs(out_dir, exist_ok=True)
    targets = parse_pages(pages, doc.page_count)
    total_bytes = 0
    for pno in targets:
        page = doc[pno - 1]
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        path = os.path.join(out_dir, f"{prefix}-{pno:03d}.{fmt}")
        pix.save(path)
        total_bytes += os.path.getsize(path)
        print(f"  p{pno:03d}  {pix.width}x{pix.height}px  {os.path.getsize(path) / 1024:.0f}KB")
    doc.close()
    print(f"\n✅ 整页渲染完成：{len(targets)} 页 → {out_dir}  "
          f"共 {total_bytes / 1048576:.1f}MB（{dpi}dpi {fmt}）")
    print("   注：这是页面图，水印/角标仍在画面里，属于正常现象。")


def extract(src, out_dir, prefix):
    """导出内嵌图片对象。按 xref 去重，同一张图跨页复用只写一次。"""
    doc = pymupdf.open(src)
    os.makedirs(out_dir, exist_ok=True)
    seen, count = set(), 0
    prect_area_cache = {}
    for pno, page in enumerate(doc, start=1):
        area = page.rect.width * page.rect.height
        prect_area_cache[pno] = area
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen:
                continue
            seen.add(xref)
            info = doc.extract_image(xref)
            rects = page.get_image_rects(xref)
            cover = 0
            for r in rects:
                rr = r & page.rect
                cover += abs(rr.width * rr.height)
            cover = cover / area if area else 0
            path = os.path.join(out_dir, f"{prefix}-p{pno:03d}-x{xref}.{info['ext']}")
            with open(path, "wb") as f:
                f.write(info["image"])
            count += 1
            print(f"  p{pno:03d} xref{xref}  {info['width']}x{info['height']}  "
                  f"{info['ext']}  {len(info['image']) / 1024:.0f}KB  "
                  f"本页覆盖 {cover * 100:.0f}%")
    doc.close()
    print(f"\n✅ 内嵌图片导出完成：{count} 张 → {out_dir}")
    print("   注：这些是素材对象，不是页面图；覆盖率最高的那张才是正文底图，")
    print("       低覆盖率小图多为 logo / 二维码 / 文字蒙版碎片。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out_dir", nargs="?")
    ap.add_argument("--mode", choices=["render", "extract"], default="render",
                    help="render=整页渲染（默认）；extract=导出内嵌图片对象")
    ap.add_argument("--dpi", type=int, default=150, help="仅 render 用，默认 150")
    ap.add_argument("--format", choices=["png", "jpg"], default="png")
    ap.add_argument("--pages", help="仅 render 用，如 1-3,5；不传为全部")
    ap.add_argument("--prefix", default="page", help="输出文件名前缀")
    args = ap.parse_args()

    if not args.out_dir:
        args.out_dir = os.path.splitext(os.path.basename(args.src))[0] + "_images"
    if args.mode == "render":
        render(args.src, args.out_dir, args.dpi, args.format, args.pages, args.prefix)
    else:
        extract(args.src, args.out_dir, args.prefix)


if __name__ == "__main__":
    sys.exit(main())
