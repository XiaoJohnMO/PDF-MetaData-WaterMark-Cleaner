#!/usr/bin/env python3
"""PDF 隐私元数据清理（路线 B）：清理外壳 Info/XMP + 原地无损剥离内嵌 JPEG 元数据。

用法:
    python clean_pdf.py <input.pdf> [output.pdf]

- 外壳清理：优先本机指定 exiftool → 系统 PATH exiftool → PyMuPDF 内置清法。
- 内嵌图片清理：原地剥离 PDF 内所有纯 JPEG 图像流的 EXIF/XMP/IPTC/COM
  （包括 Word/WPS 转 PDF 里的多张照片、扫描仪 KM_xxx 注释等），不重编码像素，
  严格保留 APP2(ICC) 与 APP14(Adobe 色彩标记)，跳过组合滤镜以防反色。
"""
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pymupdf

# 本机已装好的 exiftool 绝对路径（可选）。打包分享给他人时这一行应保持为 None。
LOCAL_EXIFTOOL = None

# 只剥除 APP1(EXIF/XMP)、APP13(Photoshop/IPTC)、COM(注释/设备型号)。
# 必须保留 APP0(JFIF)、APP2(ICC)、APP14(Adobe 色彩变换)，否则 CMYK 会反色。
JPEG_DROP = {0xE1, 0xED}

PRIVACY_TAGS = [
    "-Title", "-Author", "-Creator", "-Producer", "-Subject", "-Keywords",
    "-Description", "-Comment", "-CreateDate", "-ModifyDate", "-MetadataDate",
    "-XMP:all", "-PDF:all",
]
HARMLESS = {
    "PDFVersion", "PageCount", "Linearized", "PageMode", "Encryption",
    "TaggedPDF", "Language", "Format", "FileSize", "FileName", "Directory",
    "FileModifyDate", "FileAccessDate", "FileInodeChangeDate",
    "FilePermissions", "FileType", "FileTypeExtension", "MIMEType",
}


def strip_jpeg_bytes(data: bytes):
    """剥除 JPEG 字节流中的元数据段，保留全部解码与色彩必需段。"""
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
        if marker == 0xDA:  # SOS 之后为压缩图像数据，原样拼接
            out += data[pos:]
            break
        if pos + 4 > n:
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


def strip_embedded_images_metadata(doc: pymupdf.Document) -> tuple[int, list[str]]:
    """原地无损剥离 PDF 内嵌图片对象的 EXIF/XMP/COM 及图片级 /Metadata 子流。"""
    cleaned_count = 0
    removed_tags = set()

    for xref in range(1, doc.xref_length()):
        try:
            subtype = doc.xref_get_key(xref, "Subtype")
            if subtype != ("name", "/Image"):
                continue

            # 1. 若图片 XObject 自身挂有独立 /Metadata (XMP) 子流，直接解绑
            meta_ref = doc.xref_get_key(xref, "Metadata")
            if meta_ref[0] != "null":
                doc.xref_set_key(xref, "Metadata", "null")
                removed_tags.add("ImageXMP")

            # 2. 仅处理纯 /DCTDecode 滤镜（原生 JPEG 流）；
            #    跳过 [/FlateDecode /DCTDecode] 等组合滤镜，防止写回后渲染反色。
            flt = doc.xref_get_key(xref, "Filter")
            is_pure_dct = (
                flt == ("name", "/DCTDecode")
                or flt[1].replace(" ", "") == "[/DCTDecode]"
            )
            if not is_pure_dct:
                continue

            decode_parms = doc.xref_get_key(xref, "DecodeParms")
            if decode_parms[0] != "null":
                continue

            raw = doc.xref_stream_raw(xref)
            if not raw:
                continue

            cleaned, removed = strip_jpeg_bytes(raw)
            if removed:
                doc.update_stream(xref, cleaned, compress=False)
                doc.xref_set_key(xref, "Filter", "/DCTDecode")
                cleaned_count += 1
                removed_tags.update(removed)
        except Exception:
            continue

    return cleaned_count, sorted(removed_tags)


def exiftool_cmd():
    """按 本机绝对路径 → PATH exiftool 的顺序查找，均不可用返回 None。"""
    candidates = (
        (LOCAL_EXIFTOOL, "本机路径"),
        (Path(shutil.which("exiftool")) if shutil.which("exiftool") else None, "系统 PATH"),
    )
    for path, label in candidates:
        if not path:
            continue
        try:
            if not Path(path).exists():
                continue
            if Path(path).suffix == ".exe" or os.access(path, os.X_OK):
                cmd = [str(path)]
            elif shutil.which("perl"):
                cmd = ["perl", str(path)]
            else:
                continue
            return cmd, label
        except Exception:
            continue
    return None, None


def verify_with_pymupdf(dst: str) -> bool:
    doc = pymupdf.open(dst)
    meta = {k: v for k, v in doc.metadata.items()
            if k not in ("format", "encryption") and v}
    xmp = doc.get_xml_metadata()

    # 抽检内嵌 JPEG 是否仍有残留元数据
    img_leaks = 0
    for xref in range(1, doc.xref_length()):
        if doc.xref_get_key(xref, "Subtype") == ("name", "/Image"):
            flt = doc.xref_get_key(xref, "Filter")
            if flt == ("name", "/DCTDecode") or flt[1].replace(" ", "") == "[/DCTDecode]":
                _, rem = strip_jpeg_bytes(doc.xref_stream_raw(xref))
                if rem:
                    img_leaks += 1
    doc.close()

    if meta or xmp.strip() or img_leaks:
        print("⚠️  残留隐私元数据:", file=sys.stderr)
        for k, v in meta.items():
            print(f"  {k}={v}", file=sys.stderr)
        if xmp.strip():
            print("  XMP 流仍存在", file=sys.stderr)
        if img_leaks:
            print(f"  仍有 {img_leaks} 张内嵌 JPEG 含 EXIF/COM 段", file=sys.stderr)
        return False

    print(f"✅ {os.path.basename(dst)}: 外壳及内嵌图片隐私元数据已彻底清除 "
          f"({os.path.getsize(dst) / 1024 / 1024:.1f} MB)")
    return True


def verify_with_exiftool(cmd, dst: str) -> bool:
    result = subprocess.run([*cmd, "-s", *PRIVACY_TAGS, dst],
                            capture_output=True, text=True)
    hits = []
    for line in result.stdout.splitlines():
        if ":" not in line:
            continue
        tag, _, value = line.partition(":")
        tag, value = tag.strip(), value.strip()
        if tag in HARMLESS:
            continue
        if value and value not in ("0", "No", "(Binary data)"):
            hits.append(f"{tag}={value}")
    if hits:
        print("⚠️  残留隐私元数据:", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return False
    return verify_with_pymupdf(dst)


def clean_pdf(src: str, dst: str) -> bool:
    cmd, label = exiftool_cmd()
    tmp_exif = dst + ".exif.tmp"
    tmp_out = dst + ".tmp"

    try:
        if cmd:
            if os.path.exists(tmp_exif):
                os.remove(tmp_exif)
            subprocess.run([*cmd, "-all=", "-o", tmp_exif, src],
                           check=True, capture_output=True)
            working_src = tmp_exif
            engine_msg = f"exiftool ({label}) + PyMuPDF 图像流净化"
        else:
            working_src = src
            engine_msg = "PyMuPDF 内置清法 + 图像流净化"

        doc = pymupdf.open(working_src)
        if doc.is_encrypted:
            doc.authenticate("")

        doc.set_metadata({})
        doc.del_xml_metadata()
        img_cnt, img_tags = strip_embedded_images_metadata(doc)

        doc.save(tmp_out, garbage=4, deflate=True)
        doc.close()
        os.replace(tmp_out, dst)

        print(f"清理引擎: {engine_msg}")
        if img_cnt:
            print(f"内嵌图片净化: 清理 {img_cnt} 张 JPEG（剥离 {', '.join(img_tags)}）")
        else:
            print("内嵌图片净化: 无需清理（无残留 EXIF/COM 的 JPEG 流）")

        return verify_with_exiftool(cmd, dst) if cmd else verify_with_pymupdf(dst)
    finally:
        for f in (tmp_exif, tmp_out):
            if os.path.exists(f):
                try:
                    os.remove(f)
                except OSError:
                    pass


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python clean_pdf.py <input.pdf> [output.pdf]")
        sys.exit(1)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.getcwd(), os.path.basename(src)
    )
    sys.exit(0 if clean_pdf(src, dst) else 1)
