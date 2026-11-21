#!/usr/bin/env python3
"""PDF 图像重建清理（路线 A，首选）。

原理：扫描 PDF 的正文是一张铺满整页的位图；页眉水印、iText 账号水印、
扫描 App 的 logo/二维码都是额外的叠加对象（矢量文字或小图），并不在底图
里。把底图原始字节取出来、剥掉图片级元数据，再按原位置拼成一个全新
PDF，就能在不改一个像素的前提下让水印、logo、二维码整体消失。

对于判定为 complex（多图拼贴 / Word 多图混排 / MRC 混合栅格）而原样保留的页面，
也会自动调用内嵌图片净化逻辑，剥除其中原生 JPEG 的 EXIF/XMP/COM 元数据。

用法:
    python rebuild_from_images.py --inspect in.pdf          # 侦察
    python rebuild_from_images.py in.pdf out.pdf           # 重建
    python rebuild_from_images.py in.pdf out.pdf --min-cover 0.85
"""
import argparse
import os
import sys

import pymupdf

from clean_pdf import strip_embedded_images_metadata, strip_jpeg_bytes


def _is_faint_overlay(page, entry, alpha_max=0.3):
    """判断大块叠加图是不是淡到几乎看不见的水印层。"""
    smask = entry.get("smask")
    if not smask:
        return False
    try:
        pix = pymupdf.Pixmap(page.parent, smask)
        samples = pix.samples
        return (sum(samples) / len(samples)) / 255 <= alpha_max
    except Exception:
        return False


def _text_is_stamp(page, total_max=200, distinct_max=3):
    """判断文字层是不是水印戳而不是正文。"""
    lines = [l.strip() for l in page.get_text("text").splitlines() if l.strip()]
    if not lines:
        return True
    if len("".join(lines)) > total_max:
        return False
    return len(set(lines)) <= distinct_max


def plan_page(page, dominant_min=0.9, loose_min=0.5,
              overlay_max=0.05, max_objects=8, text_max=50):
    """决定当前页走 single 重建还是 complex 原样保留。"""
    prect = page.rect
    page_area = prect.width * prect.height
    entries, seen = [], set()
    for img in page.get_images(full=True):
        xref = img[0]
        if xref in seen:
            continue
        seen.add(xref)
        rects = [r & prect for r in page.get_image_rects(xref)]
        covered = sum(abs(r.width * r.height) for r in rects)
        entries.append({"xref": xref, "px": (img[2], img[3]), "rects": rects,
                        "bpc": img[4], "smask": img[1],
                        "cover": covered / page_area if page_area else 0})

    if not entries:
        return {"mode": "none", "page": page, "keep": [], "drop": []}

    best = max(entries, key=lambda e: e["cover"])
    companions = [e for e in entries if e is not best]

    def single(reason):
        return {"mode": "single", "page": page, "keep": [best],
                "drop": companions, "reason": reason}

    if best["cover"] >= dominant_min:
        return single(f"主图覆盖 {best['cover'] * 100:.0f}%")

    if best["cover"] < loose_min:
        return {"mode": "complex", "page": page, "keep": [], "drop": entries}

    big = [e for e in companions if e["cover"] >= overlay_max]
    if any(not _is_faint_overlay(page, e) for e in big):
        return {"mode": "complex", "page": page, "keep": [], "drop": entries}
    if len(entries) > max_objects:
        return {"mode": "complex", "page": page, "keep": [], "drop": entries}
    if any(e["bpc"] == 1 for e in companions):
        return {"mode": "complex", "page": page, "keep": [], "drop": entries}
    if not _text_is_stamp(page, total_max=text_max * 4):
        return {"mode": "complex", "page": page, "keep": [], "drop": entries}

    return single(f"主图覆盖 {best['cover'] * 100:.0f}%，其余为微小叠加")


def image_payload(doc, xref):
    """取出单张图的可用载荷并剥除图片级元数据。"""
    info = doc.extract_image(xref)
    if info["ext"] in ("jpeg", "jpg"):
        payload, removed = strip_jpeg_bytes(info["image"])
        return "stream", payload, removed
    try:
        pix = pymupdf.Pixmap(doc, xref)
    except Exception:
        return None, None, []
    if pix.colorspace is None:
        return None, None, []
    if pix.colorspace.n > 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    return "pixmap", pix, ["PNG/BitmapChunks"]


def build(src, dst, dominant_min, verbose=True):
    doc = pymupdf.open(src)
    if doc.is_encrypted:
        doc.authenticate("")

    toc = doc.get_toc()
    out = pymupdf.open()
    stats = {"pages": 0, "rebuilt": 0, "copied": 0, "dropped": 0,
             "stripped": [], "text_lost": 0, "complex_pages": []}

    for pno, src_page in enumerate(doc, start=1):
        plan = plan_page(src_page, dominant_min)
        stats["pages"] += 1

        if plan["mode"] != "single":
            out.insert_pdf(doc, from_page=pno - 1, to_page=pno - 1)
            stats["copied"] += 1
            stats["complex_pages"].append(pno)
            if verbose:
                print(f"  p{pno}: {plan['mode']}（多图拼贴/混合栅格）→ 原样保留并清理元数据")
            continue

        prect = src_page.rect
        new_page = out.new_page(width=prect.width, height=prect.height)
        stats["text_lost"] += len(src_page.get_text("text"))

        entry = plan["keep"][0]
        kind, payload, removed = image_payload(doc, entry["xref"])
        if kind is None:
            out.delete_page(new_page.number)
            out.insert_pdf(doc, from_page=pno - 1, to_page=pno - 1)
            stats["copied"] += 1
            stats["complex_pages"].append(pno)
            if verbose:
                print(f"  p{pno}: 底图无法解码 → 原样保留")
            continue

        if removed:
            stats["stripped"].append((pno, removed))
        for rect in entry["rects"]:
            if kind == "stream":
                new_page.insert_image(rect, stream=payload)
            else:
                new_page.insert_image(rect, pixmap=payload)
        stats["rebuilt"] += 1
        stats["dropped"] += len(plan["drop"])
        if verbose and plan["drop"]:
            print(f"  p{pno}: 重建，丢弃叠加对象 {len(plan['drop'])} 个")

    # 对原样保留的 complex/none 页面中的内嵌图片也执行原地元数据剥离
    complex_img_cnt, complex_img_tags = strip_embedded_images_metadata(out)

    if toc:
        out.set_toc(toc)
    out.set_metadata({})
    out.del_xml_metadata()

    tmp = dst + ".tmp"
    out.save(tmp, garbage=4, deflate=True)
    out.close()
    doc.close()
    os.replace(tmp, dst)

    if verbose:
        print(f"\n✅ 重建完成: {os.path.basename(dst)}  "
              f"{os.path.getsize(dst) / 1048576:.1f}MB")
        print(f"   页数 {stats['pages']} / 图像重建 {stats['rebuilt']} 页 / "
              f"原样保留 {stats['copied']} 页 / 丢弃叠加对象 {stats['dropped']} 个")
        if toc:
            print(f"   保留原书签目录: {len(toc)} 条")
        if stats["stripped"]:
            kinds = sorted({",".join(v) for _, v in stats["stripped"]})
            print(f"   剥离重建底图元数据: {len(stats['stripped'])} 张（{'; '.join(kinds)}）")
        if complex_img_cnt:
            print(f"   剥离保留页内嵌图元数据: {complex_img_cnt} 张（{', '.join(complex_img_tags)}）")
        if not stats["stripped"] and not complex_img_cnt:
            print("   剥离图片级元数据: 无（图片流本身干净）")
        if stats["text_lost"]:
            print(f"   丢弃文字层: {stats['text_lost']} 字符（水印文字即在此层）")
        if stats["complex_pages"]:
            print(f"   ⚠️ 以下页未重建（已清元数据，但若有水印可能仍在）: {stats['complex_pages']}")
    return stats


def verify(src, dst, dpi=80):
    """同分辨率渲染原文件与结果，逐页比对差异占比。"""
    a, b = pymupdf.open(src), pymupdf.open(dst)
    if a.is_encrypted:
        a.authenticate("")
    print(f"\n差异核查（{dpi}dpi 灰度渲染）:")
    if a.page_count != b.page_count:
        print(f"  ❌ 页数不一致 {a.page_count} -> {b.page_count}")
    flagged = []
    for i in range(min(a.page_count, b.page_count)):
        pa = a[i].get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csGRAY)
        pb = b[i].get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csGRAY)
        if (pa.width, pa.height) != (pb.width, pb.height):
            flagged.append((i + 1, "尺寸不一致"))
            continue
        diff = sum(1 for x, y in zip(pa.samples, pb.samples)
                   if abs(x - y) > 25) / len(pa.samples)
        if diff > 0.25:
            flagged.append((i + 1, f"{diff * 100:.0f}%"))
    if flagged:
        print("  ⚠️ 需人工看图的页面:", flagged)
    else:
        print("  全部页面差异均在安全范围")
    a.close()
    b.close()


def inspect(src, dominant_min=0.9):
    doc = pymupdf.open(src)
    if doc.is_encrypted:
        doc.authenticate("")
    print(f"文件: {os.path.basename(src)}  页数: {doc.page_count}  "
          f"大小: {os.path.getsize(src) / 1048576:.1f}MB")
    meta = {k: v for k, v in doc.metadata.items() if v and v != "format"}
    print(f"外壳元数据: {meta if meta else '（空）'}")
    modes = {}
    for pno, page in enumerate(doc, start=1):
        plan = plan_page(page, dominant_min)
        modes[plan["mode"]] = modes.get(plan["mode"], 0) + 1
        if pno <= 3 or plan["mode"] != "single":
            print(f"\n p{pno}  {page.rect.width:.0f}x{page.rect.height:.0f}pt"
                  f"  文字层 {len(page.get_text('text'))} 字  形态={plan['mode']}")
            for e in plan["keep"][:2]:
                print(f"    底图 xref{e['xref']} {e['px'][0]}x{e['px'][1]}"
                      f" 覆盖{e['cover'] * 100:.0f}%")
            if plan["mode"] == "complex":
                print(f"    图片对象 {len(plan['drop'])} 个，最大覆盖 "
                      f"{max(e['cover'] for e in plan['drop']) * 100:.0f}%")
    print(f"\n形态统计: {modes}")
    doc.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--inspect", action="store_true", help="只侦察不重建")
    ap.add_argument("--min-cover", type=float, default=0.9,
                    help="判定'单图铺满整页'的覆盖率阈值，默认 0.9")
    ap.add_argument("--no-verify", action="store_true", help="跳过差异核查")
    args = ap.parse_args()

    if args.inspect:
        inspect(args.src, args.min_cover)
        return
    if not args.dst:
        ap.error("需要输出路径")
    build(args.src, args.dst, args.min_cover)
    if not args.no_verify:
        verify(args.src, args.dst)


if __name__ == "__main__":
    main()
