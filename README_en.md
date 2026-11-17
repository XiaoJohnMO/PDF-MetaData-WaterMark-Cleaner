<div align="center">
    
| 📖 **English** | 📖 [简体中文](README.md) |
| ---- | ---- |

</div>

Description
---
Removes private metadata from PDFs and images inside (EXIF/XMP/DocInfo/author/device fingerprint/scanner-software traces) together with page watermarks and scanning-app corner badges. Watermarks are removed by extracting the base image (learnt from Format Factory) and rebuilding a clean PDF, not by painting over them. Use when the user asks to "clean", "strip EXIF", "remove watermarks", "remove scanner info" or "remove device fingerprints". Covers Konica Minolta MFPs, CamScanner, Quark Scan, WPS, iText watermarks and similar sources.

It can be installed for AI Agents as a skill to use.

# PDF Metadata (and basic overlay watermark) Cleaning

## Iron rules

1. **Scout the page form first, then pick a route.** Never start editing the moment you receive a file.
2. **Never remove watermarks by redaction white-out.** A watermark grid always sits on top of body text; white-out cuts the glyphs away with it and leaves permanent, unrecoverable white holes.
3. **Output keeps the original filename**, or the name the user specifies. Never add a suffix of your own.
4. **Always run the diff check and look at the rendered pages yourself before delivery.** A script reporting "success" does not mean nothing broke.

## Dependencies

There are only two hard dependencies: Python 3 and PyMuPDF (`pip install pymupdf`). exiftool is an
**optional** extra, not something that has to be installed — the script selects automatically in the order "locally configured path → PATH → PyMuPDF built-in cleaning":

| Situation | Script behaviour |
|---|---|
| exiftool configured locally (constant at the top of `clean_pdf.py`) | Clean with it and re-verify |
| exiftool present on PATH | Clean with it and re-verify |
| Neither available | Fall back to PyMuPDF: clear Info + delete XMP; results are equivalent (cross-checked with an external exiftool — every privacy tag came back empty) |

**Why exiftool is not shipped inside the package**: exiftool is not a universally preinstalled tool. macOS ships perl but not exiftool; Windows ships neither. Bundling a 19 MB Perl tool means most people cannot run it at all. Since PyMuPDF's built-in cleaning produces equivalent results, there is no reason to pay the size and platform-compatibility cost.

> When sharing with others, the `LOCAL_EXIFTOOL` constant at the top of `clean_pdf.py` must be blanked (the packaging script sets it to `None` automatically), so that no absolute path remains inside the package.

## Step 0: Scout the page form

```bash
python scripts/rebuild_from_images.py --inspect <input.pdf>
```

It reports a form for every page, and it is always one of two:

| Form | Decision | Meaning |
|---|---|---|
| `single` | ① Dominant image coverage ≥ 90%; **or** ② dominant image ≥ 50% plus all of the following: every other image is either a tiny overlay < 5% or a faint overlay with mean opacity ≤ 30% (platform watermark), image objects ≤ 8, no 1-bit companion mask, and the text layer is nothing but a watermark stamp (≤ 200 characters total and ≤ 3 distinct lines) | One base image per page; watermarks and corner badges are extra overlay objects |
| `complex` | everything else | Multi-image collage, or MRC mixed raster (colour JPEG base + 1-bit text mask + many fragments) |

A single 90% threshold is not enough. Tier ② exists to catch two kinds of normal files that are easy to miss: scans from phone apps that leave a white border (body covers only 77%–88% of the page), and platform files with a nearly transparent PNG watermark laid across more than half the page. Tier ② also requires "no 1-bit mask, text layer is only a stamp", so it does not let MRC through and does not delete body text by mistake.

**Criterion for a faint overlay**: the overlay carries a smask and its mean opacity is ≤ 30% (the diagonal watermark on the [企业标准信息公共服务平台 (Enterprise Standards Information Public Service Platform of China)](https://www.qybz.org.cn/) files measures only 2%). Genuine illustrations are opaque or clearly semi-transparent.

Konica Minolta bizhub series (KM_3xx / C3xx) mostly come out **complex**;
PDFs produced by CamScanner or Quark Scan are usually **single**.

## The two kinds of "convert to images" must not be mixed up

One phrase covers two different operations, and getting them confused produces completely different results. They also play different roles:

| Operation | Command | Output | Role and use |
|---|---|---|---|
| **Extract embedded images** | `pdf_to_images.py <in> <dir> --mode extract` | The image objects actually embedded in the PDF — **no vector watermark and no text layer**, but logos, QR codes and text-mask fragments come along too | **One stage of the watermark-removal flow** (this is exactly what Route A takes). Used to identify which image is the body base and which are overlay pollutants |
| **Full-page render** | `pdf_to_images.py <in> <dir> --mode render` | One PNG/JPG per page, with **watermarks, headers, logos and QR codes all present in the picture** | **A convenience extra of the cleaning tool**: converting a whole PDF to images normally, for review, comparison, OCR hand-off or sending previews |

Whether an image is "the base" is decided by coverage alone: the highest-coverage image on a page is normally the body base, and the low-coverage ones are usually corner overlays or text-mask fragments. `--inspect` reports exactly this judgement.

`rebuild_from_images.py` follows the **extract embedded images** logic (it never renders), because it needs the original JPEG bytes — re-encoding would lose quality.

## Route A: image rebuild (preferred for `single`)

```bash
python scripts/rebuild_from_images.py <input.pdf> <output.pdf>
```

What it does: take the raw bytes of each page's base image → strip image-level metadata → place it at its original position in a brand-new PDF. **Not a single pixel is changed, and nothing is re-encoded.**

It solves four things at once:

- Watermark text, iText account watermarks (repeating grids such as `department:username`) and page-number watermarks — all vector objects drawn into the text layer; the new PDF has no text layer, so they simply do not exist;
- Scanning-app logos and QR-code badges — extra small images, dropped wholesale during the rebuild;
- The OCR text layer (low-quality, wrongly recognised text) — gone with it;
- Envelope Info/XMP, DocumentID and Creator/Producer device fingerprints — the new file is generated from scratch and is clean by nature.

The cost: the text layer is lost, so the scan is no longer full-text searchable (scans were never accurately searchable anyway). If searchability is genuinely needed, run OCR separately. Note, however, that the cleaning goal is met: this removes the maximum amount of machine-generated, machine-readable metadata that could carry private information.

The script runs its own diff check: the source and the result are rendered at the same resolution and compared page by page; any page differing by more than 25% raises a warning, which means the base-image decision was wrong and body content was lost — you must roll back and redo.

## Route B: metadata cleaning (`complex`, or metadata-only requests)

```bash
python scripts/clean_pdf.py <input.pdf> <output.pdf>
```

This no longer hammers the file with `exiftool -all= -o` (which also touches structural entries). Instead:
exiftool (if available locally) → PyMuPDF `set_metadata({})` → `garbage=4, deflate=True`, with image streams left untouched.

A `complex` page cannot be safely disassembled (each fragment may be the mask for a stretch of body text), so it is **kept as-is and only the envelope metadata is cleaned**. If such a page really does still carry a watermark, use Route C and inspect by hand.

## Route C: redaction white-out (last resort, lossy)

Usable only when both conditions hold: the watermark is standalone vector text, and the boxes returned by `search_for` do not intersect any body text box.

Run the overlap check before deciding:

```python
for r in page.search_for("<watermark keyword>"):
    hits = [b for b in page.get_text("blocks") if pymupdf.Rect(b[:4]).intersects(r)]
```

The moment there is an overlap, **abandon white-out** — switch to a Route A rebuild, or tell the user the watermark cannot be removed losslessly. A cautionary case: a PDF carrying an iText account watermark had its repeating grid white-outed, which cut away the body glyphs underneath and left permanent white holes in the scanned base — the original is missing and cannot be repaired.

## Key taboos (all learned the hard way)

- **APP14 (Adobe colour-transform marker) and APP2 (ICC) must never be stripped.** Strip only APP1/APP13/COM;
  removing APP14 flips a CMYK JPEG page into inverted black-background white text.
- **Do not** rewrite streams with the combined `[/FlateDecode /DCTDecode]` filter via `update_stream` — writing them back corrupts rendering with inverted colours.
- **Do not** re-encode embedded JPEGs with Pillow and put them back into the PDF.
- **Do not** call `set_metadata({})` on its own — it clears only the Info dictionary, not the XMP stream.
- **Do not** force-assemble `complex` pages; keeping them untouched and warning is preferable.

## Pre-delivery checklist

1. The diff check from `rebuild_from_images.py` reports no warnings;
2. The privacy-tag re-check is empty (Title/Author/Creator/Producer/Subject/Keywords/Description/Comment/all Dates/XMP);
3. Render the first page, the last page and one random middle page; confirm white background, black text, complete content, no missing glyphs or white holes;
4. The page count matches the source file;
5. Compute the ink ratio per page (for example the share of pixels < 200 in 40 dpi greyscale); any suspected blank page must be checked against the original — some pages really are blank apart from the watermark, and turning white once the watermark is gone is the correct outcome, not lost content.

## Common sources reference

| Source | Form | First response |
|---|---|---|
| Konica Minolta bizhub (KM_3xx / C3xx) | mostly complex | Route B; when the JPEG COM segment holds `KM_<model>`, strip COM only |
| CamScanner | single, body image usually 77%–88% (with a white border), plus a separate logo image of about 0.4% at bottom right | Route A — reaches the rebuild through the "dominant image ≥ 50% + tiny companions" tier |
| Quark Scan | single, body image 82%–93%, footer carries a logo **plus QR code** PNG of about 1.5% | Route A; logo and QR code are dropped together |
| Script-injected text watermark (e.g. `Scan By @xxx`) | body image usually fills the page; watermark is 8 pt grey vector text | Route A; the rebuild discards the text layer, no redaction needed |
| [企业标准信息公共服务平台 (Enterprise Standards Information Public Service Platform of China)](https://www.qybz.org.cn/) | body image 85%–87%, **diagonal semi-transparent PNG watermark** covering 30%–40% (lying across the table header), platform QR code top left, "扫描全能王 创建" (created with CamScanner) bottom right, date stamp in the text layer | Route A; the faint overlay is judged by its opacity and dropped as a whole layer, leaving the body unaffected |
| Format Factory conversions | single | Route A |
| iText account watermark (`department:username` grid) | depends on the envelope | Route A; white-out is forbidden |
| WPS Writer exports | usually no real image | Route B; an Author containing a real name is removed by the cleaning engine |

## Script overview

| Script | Responsibility |
|---|---|
| `rebuild_from_images.py` | Scout the form (`--inspect`), extract base images and rebuild (Route A), diff check |
| `clean_pdf.py` | Clean envelope metadata (Route B), with automatic three-tier engine selection |
| `pdf_to_images.py` | Full-page render / extract embedded images — the two modes are kept explicitly separate; it doubles as the built-in quick PDF-to-image tool |
