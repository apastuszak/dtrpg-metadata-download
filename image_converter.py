"""Converts CMYK/grayscale/JPEG2000 (JPX) images inside a PDF to RGB JPEG,
in place.

Ported from a separate personal project (github.com/plazman30/mac-pdf-rgb-fix
-- see that repo's CLAUDE.md for the full rationale behind each non-obvious
choice below; every constraint here was hard-won against real PDFs and real
renderers, not guessed, so nothing in this module should be "simplified"
without re-reading that history first). Stripped of that project's CLI
argument parsing and interactive metadata-prompting -- this project already
owns metadata writing (pdf_writer.py); this module is purely about
re-encoding image XObjects, nothing else.

Uses PyMuPDF (`fitz`) for color-managed CMYK->RGB conversion -- Pillow's own
CMYK->RGB produces a green cast -- and Pillow for JPEG encoding, since
PyMuPDF doesn't expose a JPEG encoder for arbitrary pixmaps. A different PDF
library entirely from pikepdf (which the rest of this project uses), and
this project's first new dependency since textual.

Runs on a PDF *before* pdf_writer.py's pikepdf-based metadata write, never
after: PyMuPDF's own save is a different library/code path than pikepdf's,
and pikepdf's Calibre-specific XMP writing (_set_calibre_series(), etc.)
reaches into private internals to produce a qualified/structured shape real
Calibre actually requires -- letting a second library's save happen *after*
that risks disturbing it in ways that would round-trip through pikepdf fine
but silently break in the real app, the exact failure mode CLAUDE.md's
Calibre-series section describes. Doing image conversion first means
pikepdf's metadata write is always the last thing to touch the file.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz
from PIL import Image

logger = logging.getLogger("image_converter")

# PDF stream filter name for JPEG 2000 encoding.
JPX_FILTER = "JPXDecode"

# Default JPEG output quality (95 = high quality, visually near-lossless).
DEFAULT_QUALITY = 95

# These colorspaces indicate palette/indexed images (e.g. diagrams, pixel
# art). They should stay lossless since JPEG would destroy their sharp edges.
LOSSLESS_COLORSPACES = {"Indexed"}

# Candidate paths for a generic CMYK ICC profile, used to get print-realistic
# (richer, darker) colors out of /DeviceCMYK images that carry no embedded
# profile of their own. Without this, PyMuPDF's default profile-less CMYK->RGB
# conversion tends to look noticeably lighter/less saturated than how the same
# file renders in Preview/Acrobat, which assume a real CMYK profile. macOS-
# only for now (this project's other platform-specific paths, e.g. Homebrew
# Python locations in CLAUDE.md, are similarly not chased down for every OS)
# -- falls back cleanly to PyMuPDF's default conversion when unavailable.
_ICC_CMYK_PROFILE_PATHS = [
    "/System/Library/ColorSync/Profiles/Generic CMYK Profile.icc",  # macOS
]

# fz_colorspace_type enum value for CMYK (mupdf/source/fitz/colorspace.h).
# Not exposed as a named constant by the mupdf Python bindings.
_FZ_COLORSPACE_CMYK = 4

_icc_cmyk_colorspace = None
_icc_cmyk_load_attempted = False

# Candidate paths for a real sRGB ICC profile, embedded once per output
# document and referenced from every converted RGB image in place of bare
# /DeviceRGB. /DeviceRGB is device-dependent, and some renderers (Apple's
# PDFKit/Preview, Foxit) apply their own default color transform for
# profile-less RGB data instead of assuming sRGB outright, producing a
# visible warm/red cast -- an explicit ICCBased colorspace removes the
# ambiguity.
_ICC_RGB_PROFILE_PATHS = [
    "/System/Library/ColorSync/Profiles/sRGB Profile.icc",  # macOS
]

_srgb_icc_bytes = None
_srgb_icc_load_attempted = False


def _load_first_readable_file(paths: list[str]) -> bytes | None:
    """Return the raw bytes of the first path in `paths` that exists and can
    actually be read, trying subsequent candidates if an earlier one exists
    but fails to open/read. Returns None if none of the candidates can be
    read (e.g. any non-macOS platform, for both lists above)."""
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                return f.read()
        except OSError:
            continue
    return None


def _get_srgb_icc_bytes() -> bytes | None:
    global _srgb_icc_bytes, _srgb_icc_load_attempted
    if _srgb_icc_load_attempted:
        return _srgb_icc_bytes
    _srgb_icc_load_attempted = True
    _srgb_icc_bytes = _load_first_readable_file(_ICC_RGB_PROFILE_PATHS)
    return _srgb_icc_bytes


def _get_icc_cmyk_colorspace() -> "fitz.Colorspace | None":
    global _icc_cmyk_colorspace, _icc_cmyk_load_attempted
    if _icc_cmyk_load_attempted:
        return _icc_cmyk_colorspace
    _icc_cmyk_load_attempted = True

    icc_bytes = _load_first_readable_file(_ICC_CMYK_PROFILE_PATHS)
    if icc_bytes:
        try:
            import mupdf
            buf = mupdf.fz_new_buffer_from_copied_data(icc_bytes)
            raw_cs = mupdf.fz_new_icc_colorspace(_FZ_COLORSPACE_CMYK, 0, "GenericCMYK", buf)
            _icc_cmyk_colorspace = fitz.Colorspace(raw_cs)
        except Exception:
            _icc_cmyk_colorspace = None

    return _icc_cmyk_colorspace


def needs_conversion(cs: str, filter_: str) -> bool:
    """An empty colorspace string means the object is an /ImageMask
    (stencil mask) -- a 1-bit bitonal shape with no color data of its own.
    There is nothing to color-convert, so it's left untouched rather than
    misread as "not RGB, needs converting" (treating it that way sends
    1-bit mask data through the JPEG pipeline and crashes)."""
    if not cs:
        return False
    if cs not in ("DeviceRGB", "sRGB"):
        return True
    if JPX_FILTER in (filter_ or ""):
        return True
    return False


def get_rgb_pixmap(doc: fitz.Document, xref: int) -> "fitz.Pixmap | None":
    """Decode an image and return it as an RGB Pixmap, or None on failure.
    Transparency is handled via a separate SMask object, not embedded
    alpha, so there's nothing to strip here."""
    try:
        pix = fitz.Pixmap(doc, xref)

        if pix.colorspace and pix.colorspace.n == 4:
            icc_cmyk = _get_icc_cmyk_colorspace()
            if icc_cmyk is not None:
                pix = fitz.Pixmap(icc_cmyk, pix.width, pix.height, pix.samples, 0)

        if pix.colorspace and pix.colorspace.n != 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)

        if pix.n != 3:
            return None

        return pix
    except Exception:
        return None


def encode_jpeg(pix: "fitz.Pixmap", quality: int) -> bytes:
    """Embeds a real sRGB ICC profile directly in the JPEG stream (an APP2
    marker) when available, on top of the PDF-level /ColorSpace
    [/ICCBased ...] declaration convert_images_to_rgb() adds -- some
    readers' JPEG decoders trust the stream's own embedded profile over
    the PDF wrapper's colorspace entry."""
    img = Image.frombytes("RGB", (pix.width, pix.height), bytes(pix.samples))
    buf = io.BytesIO()
    save_kwargs = {"format": "JPEG", "quality": quality, "optimize": True}
    srgb_bytes = _get_srgb_icc_bytes()
    if srgb_bytes:
        save_kwargs["icc_profile"] = srgb_bytes
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def _repair_xref(input_path: str) -> str | None:
    """Some PDFs from print workflows store objects in non-standard
    locations that PyMuPDF/MuPDF cannot find via the xref table, dropping
    those objects on save. pikepdf/QPDF scans the whole file and rebuilds
    a clean xref, so PyMuPDF can then load and save them correctly.
    Returns a temp file path (caller must delete it), or None if repair
    isn't possible/needed -- caller falls back to the original path."""
    try:
        import pikepdf
    except ImportError:
        return None

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(tmp_fd)
    try:
        with pikepdf.open(input_path) as pdf:
            pdf.save(tmp_path)
        return tmp_path
    except Exception as exc:
        logger.debug("pikepdf xref repair skipped for %s: %s", input_path, exc)
        _unlink_quietly(tmp_path)
        return None


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


@dataclass
class ImageConversionResult:
    success: bool
    converted: int = 0
    skipped: int = 0
    message: str = ""


def convert_images_to_rgb(path: Path, jpeg_quality: int = DEFAULT_QUALITY) -> ImageConversionResult:
    """Converts every CMYK/grayscale/JPX image XObject in the PDF at `path`
    to RGB JPEG (or lossless FlateDecode for Indexed/palette images),
    re-saving over the same path. Never raises -- returns success=False
    with a message on any failure, matching write_metadata()'s own
    report-don't-crash convention, since a caller processing a batch must
    not have one malformed PDF take down the whole run.

    Saves to a temp file and atomically replaces `path` only once that
    succeeds (os.replace(), same filesystem via dir=path.parent) rather
    than writing PyMuPDF's output directly over `path`: PyMuPDF documents
    this as unsupported when `path` is also what was opened (a real
    constraint, not just caution), and it also means a failure partway
    through the per-image loop leaves the original file completely
    untouched instead of a half-converted PDF.
    """
    input_path = str(path)
    repaired_path = _repair_xref(input_path)
    load_path = repaired_path or input_path

    try:
        doc = fitz.open(load_path)
    except Exception as exc:
        if repaired_path:
            _unlink_quietly(repaired_path)
        return ImageConversionResult(success=False, message=f"could not open PDF: {exc}")

    tmp_out_path: str | None = None
    try:
        total_converted = 0
        total_skipped = 0

        # Embed a real sRGB ICC profile once per document (if available)
        # and reference it from every converted RGB image, instead of
        # writing bare /DeviceRGB for each one -- see _get_srgb_icc_bytes()
        # for why. Created lazily on first actual use so a document where
        # nothing ends up needing conversion doesn't ship an unreferenced
        # profile object (doc.save() runs with garbage=0 below, so nothing
        # would clean it up).
        icc_rgb_xref: int | None = None
        icc_rgb_attempted = False

        def _colorspace_entry() -> str:
            nonlocal icc_rgb_xref, icc_rgb_attempted
            if not icc_rgb_attempted:
                icc_rgb_attempted = True
                srgb_bytes = _get_srgb_icc_bytes()
                if srgb_bytes:
                    icc_rgb_xref = doc.get_new_xref()
                    doc.update_object(icc_rgb_xref, "<< /N 3 /Alternate /DeviceRGB >>")
                    doc.update_stream(icc_rgb_xref, srgb_bytes, compress=True)
            return f"[/ICCBased {icc_rgb_xref} 0 R]" if icc_rgb_xref else "/DeviceRGB"

        # Deduplicate by xref: images are XObjects that can be shared
        # across pages (e.g. a repeated background), and modifying a
        # shared object once updates it everywhere it's referenced.
        seen_xrefs: set[int] = set()
        images = []
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                if xref not in seen_xrefs:
                    seen_xrefs.add(xref)
                    images.append(img)

        for img in images:
            xref, smask, w, h, bpc, colorspace, _cs2, name, filter_, _enc = img[:10]

            if not needs_conversion(colorspace, filter_):
                total_skipped += 1
                continue

            pix = get_rgb_pixmap(doc, xref)
            if pix is None:
                total_skipped += 1
                continue

            use_lossless = colorspace in LOSSLESS_COLORSPACES

            # PyMuPDF's get_images() reports both /Mask (1-bit stencil) and
            # /SMask (8-bit soft mask) under the same "smask" field -- must
            # check the original dict to preserve the correct key. Writing
            # /SMask for a stencil mask corrupts transparency compositing
            # and can make unrelated images on the same page disappear.
            if smask:
                mask_key = "SMask"
                try:
                    if doc.xref_get_key(xref, "Mask")[0] != "null":
                        mask_key = "Mask"
                except Exception:
                    pass
                smask_entry = f"  /{mask_key} {smask} 0 R\n"
            else:
                smask_entry = ""

            if use_lossless:
                # Omit /Filter here -- update_stream(compress=True) adds
                # /Filter /FlateDecode and /Length automatically.
                new_obj_def = (
                    f"<<\n"
                    f"  /Type /XObject /Subtype /Image\n"
                    f"  /Width {pix.width} /Height {pix.height}\n"
                    f"  /ColorSpace {_colorspace_entry()} /BitsPerComponent 8\n"
                    f"{smask_entry}"
                    f">>"
                )
                doc.update_object(xref, new_obj_def)
                doc.update_stream(xref, bytes(pix.samples), compress=True)
            else:
                jpeg_bytes = encode_jpeg(pix, jpeg_quality)
                # PyMuPDF quirk: update_stream(compress=False) strips
                # /Filter from the object dict even if set via
                # update_object first -- add /Filter /DCTDecode after.
                new_obj_def = (
                    f"<<\n"
                    f"  /Type /XObject /Subtype /Image\n"
                    f"  /Width {pix.width} /Height {pix.height}\n"
                    f"  /ColorSpace {_colorspace_entry()} /BitsPerComponent 8\n"
                    f"{smask_entry}"
                    f">>"
                )
                doc.update_object(xref, new_obj_def)
                doc.update_stream(xref, jpeg_bytes, compress=False)
                doc.xref_set_key(xref, "Filter", "/DCTDecode")

            total_converted += 1

        # Hidden prefix -- see rpg_hyperlink.py's run_hyperlink_script() for why.
        tmp_fd, tmp_out_path = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
        os.close(tmp_fd)
        # garbage=0: objects are replaced in-place via update_object/
        # update_stream, never orphaned, so garbage collection isn't
        # needed for correctness -- and some hybrid/xref-stream PDFs from
        # print workflows have live objects MuPDF's GC can't fully trace,
        # which it would otherwise drop (blank spaces in the output).
        # deflate_images is intentionally NOT set -- PyMuPDF would
        # recompress the JPEG streams just written as FlateDecode,
        # undoing the encoding work above.
        doc.save(tmp_out_path, garbage=0, deflate_fonts=True)
        doc.close()
        # Keep the book's original permissions (best-effort) -- see rpg_background_layer.py.
        try:
            shutil.copymode(input_path, tmp_out_path)
        except OSError:
            pass
        os.replace(tmp_out_path, input_path)
        tmp_out_path = None

        return ImageConversionResult(success=True, converted=total_converted, skipped=total_skipped)
    except Exception as exc:
        return ImageConversionResult(success=False, message=f"image conversion failed: {exc}")
    finally:
        try:
            doc.close()
        except Exception:
            pass
        if tmp_out_path:
            _unlink_quietly(tmp_out_path)
        if repaired_path:
            _unlink_quietly(repaired_path)
