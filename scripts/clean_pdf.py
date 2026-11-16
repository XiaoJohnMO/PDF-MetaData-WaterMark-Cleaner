#!/usr/bin/env python3
"""PDF 隐私元数据清理（路线 B）：只清外壳 Info/XMP，图片流原封不动。

用法:
    python clean_pdf.py <input.pdf> [output.pdf]

- 优先用本机已装好的 exiftool（见下方 LOCAL_EXIFTOOL），其次用 PATH 里的 exiftool；
  两者都没有则用 PyMuPDF 自带能力完成同等清理，不依赖外部工具。
- 清完用 garbage=4 重构，不重编码内嵌图片（避免渲染损坏）。
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pymupdf

# 本机已装好的 exiftool 绝对路径（可选）。打包分享给他人时这一行会被清空，
# 脚本会自动退回 PATH 里的 exiftool 或 PyMuPDF 内置清法，无需额外安装。
LOCAL_EXIFTOOL = None

# 只校验隐私相关标签，不关心 PDFVersion/PageCount 这类结构信息
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


def exiftool_cmd():
    """按 本机绝对路径 → PATH exiftool 的顺序找，全部找不到返回 None。"""
    for path, label in ((LOCAL_EXIFTOOL, "本机路径"),
                        (Path(shutil.which("exiftool")) if shutil.which("exiftool") else None, "系统 PATH")):
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
        except Exception:
            continue
        return cmd, label
    return None, None


def clear_with_pymupdf(src: str, dst: str) -> None:
    """无 exiftool 时的等效实现：清 Info 字典 + 删 XMP 流 + 重构。"""
    doc = pymupdf.open(src)
    doc.set_metadata({})
    doc.del_xml_metadata()
    tmp = dst + ".tmp"
    doc.save(tmp, garbage=4, deflate=True)
    doc.close()
    os.replace(tmp, dst)


def verify_with_pymupdf(dst: str) -> bool:
    doc = pymupdf.open(dst)
    meta = {k: v for k, v in doc.metadata.items()
            if k not in ("format", "encryption") and v}
    xmp = doc.get_xml_metadata()
    doc.close()
    if meta or xmp.strip():
        print("⚠️  残留隐私元数据:", file=sys.stderr)
        for k, v in meta.items():
            print(f"  {k}={v}", file=sys.stderr)
        if xmp.strip():
            print("  XMP 流仍存在", file=sys.stderr)
        return False
    print(f"✅ {os.path.basename(dst)}: 隐私元数据已彻底清除 "
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
    print(f"✅ {os.path.basename(dst)}: 隐私元数据已彻底清除 "
          f"({os.path.getsize(dst) / 1024 / 1024:.1f} MB)")
    return True


def clean_pdf(src: str, dst: str) -> bool:
    cmd, label = exiftool_cmd()
    if cmd:
        subprocess.run([*cmd, "-all=", "-o", dst, src],
                       check=True, capture_output=True)
        doc = pymupdf.open(dst)
        doc.set_metadata({})
        tmp = dst + ".tmp"
        doc.save(tmp, garbage=4, deflate=True)
        doc.close()
        os.replace(tmp, dst)
        print(f"清理引擎: exiftool ({label})")
        return verify_with_exiftool(cmd, dst)

    clear_with_pymupdf(src, dst)
    print("清理引擎: PyMuPDF（未找到 exiftool，使用等效内置清法）")
    return verify_with_pymupdf(dst)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python clean_pdf.py <input.pdf> [output.pdf]")
        sys.exit(1)
    src = sys.argv[1]
    dst = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.getcwd(), os.path.basename(src)
    )
    sys.exit(0 if clean_pdf(src, dst) else 1)
