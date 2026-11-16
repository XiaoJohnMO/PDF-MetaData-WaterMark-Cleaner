#!/usr/bin/env python3
"""PDF 图像重建清理（路线 A，首选）。

原理：扫描 PDF 的正文是一张铺满整页的位图；页眉水印、iText 账号水印、
扫描 App 的 logo/二维码都是**额外的叠加对象**（矢量文字或小图），并不在底图
里。因此把底图原始字节取出来、剥掉图片级元数据，再按原位置拼成一个全新
PDF，就能在不改一个像素的前提下让水印、logo、二维码整体消失，也不会出现
redact 涂白导致的正文缺字。

适用判定（每页独立判断，详见 plan_page）：
    - 主图铺满整页（覆盖率 ≥ --min-cover，默认 0.9）→ 只看这一张，重建。
    - 主图只占 50%–90%，但其余图片全是 < 5% 的微小叠加、图片对象 ≤ 5 个、
      没有 1-bit 伴生蒙版、文字层 ≤ 50 字 → 同样重建。扫描 App 出图常留白边
      并把 logo 单独叠在页面外沿，这一档就是为它们准备的。
    - 多图拼贴或 MRC 混合栅格（彩色底图 + 1-bit 文字蒙版 + 大量碎块）→
      无法安全拆解，该页原样复制并告警，留给人工判断，绝不硬拼。

用法:
    python rebuild_from_images.py --inspect in.pdf          # 侦察
    python rebuild_from_images.py in.pdf out.pdf           # 重建
    python rebuild_from_images.py in.pdf out.pdf --min-cover 0.85 --dpi 200

注意:
    - 文字层（OCR）会随重建一并丢弃，水印文字正是画在这一层。
    - JPEG 的 APP14（Adobe 色彩变换标记）和 APP2（ICC 描述文件）必须保留，
      剥掉会让 CMYK 图整页反色成黑底白字。
"""
import argparse
import os
import struct
import sys

import pymupdf

# 只能剥这几类：APP1(EXIF/XMP)、APP13(Photoshop/IPTC)、COM。
# APP0(JFIF)、APP2(ICC)、APP14(Adobe 色彩变换) 是解码必需，动了就反色。
JPEG_DROP = {0xE1, 0xED}
PNG_DROP = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"tIME"}


def strip_jpeg(data: bytes):
    """剥除 JPEG 的元数据段，保留全部解码必需段。"""
    if data[:2] != b"\xff\xd8":
        return data, []
    out = bytearray(data[:2])
    removed, pos, n = [], 2, len(data)
    while pos < n - 1:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            out += data[pos:pos + 2]
            pos += 2
            continue
        if marker == 0xDA:                     # SOS 之后是熵编码数据，原样接上
            out += data[pos:]
            break
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if marker == 0xFE:
            removed.append("COM")
            pos += 2 + seg_len
            continue
        if marker in JPEG_DROP:
            removed.append(f"APP{marker - 0xE0}")
            pos += 2 + seg_len
            continue
        out += data[pos:pos + 2 + seg_len]
        pos += 2 + seg_len
    return bytes(out), removed


def strip_png(data: bytes):
    """剥除 PNG 的文本/EXIF 块，保留图像数据块。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return data, []
    out = bytearray(data[:8])
    removed, pos, n = [], 8, len(data)
    while pos < n:
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        ctype = data[pos + 4:pos + 8]
        if ctype in PNG_DROP:
            removed.append(ctype.decode())
            pos += 12 + length
            continue
        out += data[pos:pos + 12 + length]
        pos += 12 + length
    return bytes(out), removed


def _is_faint_overlay(page, entry, alpha_max=0.3):
    """判断大块叠加图是不是"淡到几乎看不见"的水印层。

    企业标准信息公共服务平台、部分政务平台会在正文上盖一层半透明 PNG，
    覆盖率能到几十个百分点，但它自身平均不透明度极低（常 <5%）。这种图
    只可能是水印，丢掉不影响正文。真正有内容的插图是不透明或半不透明的。
    """
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
    """文字层是不是"水印戳"而不是正文。

    水印文字特征极强：就那么一两句，反复出现。正文则行数多、每行都不同。
    这样才能安全地把"公开 2024年04月18日 08点43分 / 扫描全能王 创建"这类戳丢掉。
    """
    lines = [l.strip() for l in page.get_text("text").splitlines() if l.strip()]
    if not lines:
        return True
    if len("".join(lines)) > total_max:
        return False
    return len(set(lines)) <= distinct_max


def plan_page(page, dominant_min=0.9, loose_min=0.5,
              overlay_max=0.05, max_objects=8, text_max=50):
    """决定这一页走哪条路线。

    不是只看"铺满 90%"，而是分三档判断，避免把"扫描图带留白 + 右下角 logo"
    这种正常件误判成不能重建：

    1. 主图 ≥ dominant_min（默认 0.9）→ single，直接重建。
    2. 主图 ≥ loose_min（0.5），且满足全部条件 → 也按 single 重建：
       - 其余图片要么是微小叠加（覆盖率 < overlay_max），要么是淡到几乎
         看不见的半透明水印层（smask 平均不透明度 ≤ 0.3，平台水印属此类）；
       - 图片对象总数 ≤ max_objects（多了就是 MRC 碎块）；
       - 没有 1-bit 伴生蒙版（那是 MRC 的文字层，丢了正文就残）；
       - 文字层是"水印戳"（总字数 ≤ text_max×4 且不重复的行 ≤ 3），
         否则说明页面上有真文字，不能当水印丢掉。
    3. 其余一律 complex：保持原页，只清外壳元数据。
    """
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

    # 大块但"淡到几乎看不见"的叠加图（平台水印）可以丢；不透明的才算真内容
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
    """取出单张图的可用载荷。

    JPEG 走原始字节（无损、且保住 APP14/APP2 的色彩语义）；其余格式
    （CCITT/JBIG2/Flate 灰度）交给 MuPDF 解码成 pixmap，避免自己还原
    colorspace/Decode 时出错。
    """
    info = doc.extract_image(xref)
    if info["ext"] in ("jpeg", "jpg"):
        payload, removed = strip_jpeg(info["image"])
        return "stream", payload, removed
    try:
        pix = pymupdf.Pixmap(doc, xref)
    except Exception:
        return None, None, []
    if pix.colorspace is None:                 # 纯 stencil mask，单独插入无意义
        return None, None, []
    if pix.colorspace.n > 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    return "pixmap", pix, []


def build(src, dst, dominant_min, verbose=True):
    doc = pymupdf.open(src)
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
                print(f"  p{pno}: {plan['mode']}（多图拼贴/混合栅格）→ 原样保留，未重建")
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

    out.set_metadata({})
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
        if stats["stripped"]:
            kinds = sorted({",".join(v) for _, v in stats["stripped"]})
            print(f"   剥离图片级元数据: {len(stats['stripped'])} 张"
                  f"（{'; '.join(kinds)}）")
        else:
            print("   剥离图片级元数据: 无（底图本身干净）")
        if stats["text_lost"]:
            print(f"   丢弃文字层: {stats['text_lost']} 字符（水印文字即在此层）")
        if stats["complex_pages"]:
            print(f"   ⚠️ 以下页未重建、水印可能仍在: {stats['complex_pages']}")
    return stats


def verify(src, dst, dpi=80):
    """同分辨率渲染原文件与结果，逐页比对差异占比。

    重建页的差异只应出现在被去掉的水印/logo 位置；若整页大面积不同，
    说明底图判定错了、正文丢了。
    """
    a, b = pymupdf.open(src), pymupdf.open(dst)
    print(f"\n差异核查（{dpi}dpi 灰度渲染）:")
    if a.page_count != b.page_count:
        print(f"  ❌ 页数不一致 {a.page_count} -> {b.page_count}")
    flagged = []
    for i in range(min(a.page_count, b.page_count)):
        pa = a[i].get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csGRAY)
        pb = b[i].get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csGRAY)
        if (pa.width, pa.height) != (pb.width, pb.height):
            out = b[i].get_pixmap(dpi=dpi, alpha=False, colorspace=pymupdf.csGRAY)
            pb = out
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
