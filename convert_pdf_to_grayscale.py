#!/usr/bin/env python3
"""
pdf_grayscale.py — Convert a PDF to grayscale without losing hyperlinks,
metadata, or page labels.

Ghostscript's pdfwrite device converts color cleanly and preserves link
annotations, but it does NOT preserve document metadata (Info dict / XMP)
or /PageLabels — those get dropped during its full re-interpretation of
the input. This script runs Ghostscript for the color conversion, then
uses pikepdf to copy metadata and page labels back in from the original.

Ghostscript can also leave an image's original JPEG2000 (/JPXDecode)
encoding completely untouched if it decides that image's colorspace is
already gray-compatible — confirmed on a real book whose images use a
single-channel ink-separation colorspace. Since JPX support is spotty
across PDF viewers/e-readers, this script guarantees none survive: any
image still JPX-encoded after Ghostscript's pass gets properly decoded
(respecting its real colorspace, not just its raw bytes) and re-encoded.

Usage:
    python3 pdf_grayscale.py input.pdf
    python3 pdf_grayscale.py input.pdf output.pdf
    python3 pdf_grayscale.py input.pdf output.pdf --keep-producer

With no output path given, it's derived from the input filename by
inserting "-grayscale" before the extension (e.g. "Book.pdf" ->
"Book-grayscale.pdf"), written alongside the input file.

Interactively asks whether the front/back cover pages should be converted
to grayscale too. Answering no keeps those specific pages exactly as they
are in the original (full color, untouched) while everything else is
converted.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import pikepdf
import pymupdf as fitz  # PyMuPDF -- `import fitz` is the deprecated alias


def run_ghostscript(src: Path, dst: Path) -> None:
    """Convert src to grayscale, writing to dst, preserving link annotations."""
    cmd = [
        "gs",
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.6",
        "-sColorConversionStrategy=Gray",
        "-dProcessColorModel=/DeviceGray",
        "-dPreserveAnnots=true",
        "-dNOPAUSE",
        "-dBATCH",
        f"-sOutputFile={dst}",
        "-c", "/PreserveAnnotTypes [/Link] def",
        "-f", str(src),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"Ghostscript exited with code {result.returncode}")


def strip_residual_jpx(pdf_path: Path, pdf: pikepdf.Pdf) -> int:
    """Decode and re-encode any image Ghostscript's grayscale conversion
    left as JPXDecode (JPEG2000), guaranteeing none survive in the final
    output regardless of what Ghostscript itself decided to do with it.

    Confirmed on a real book (Tools for Frontier Living): Ghostscript
    passes an image through completely untouched -- original encoding
    included -- whenever it decides the image's colorspace is already
    compatible with the target (here, Gray), rather than only skipping
    unnecessary *pixel* conversion the way "already correct, don't
    touch" sounds like it should. The 5 survivors there use a single-
    channel `DeviceN [/Black] -> DeviceGray` colorspace (a "how much
    black ink" separation, not a direct luminosity value) that
    Ghostscript treats as already-gray-compatible and skips re-encoding
    entirely -- exactly the kind of image this conversion is supposed to
    guarantee is safely viewable, not exempt from it. Two other real
    JPX-source books in this same test corpus (Adventure Class Ships,
    the 1e Core Rulebook) convert every one of their JPX images away
    from JPX on their own with no help needed -- this only ever touches
    whatever Ghostscript didn't already handle.

    **First version of this function decoded the raw JPX codestream
    directly (`fitz.Pixmap(obj.read_raw_bytes())`) and used its samples
    as-is, and produced a fully inverted image -- confirmed by rendering
    the fixed output and comparing it side by side with the original
    rather than just checking the filter was gone.** A `DeviceN [/Black]`
    separation's raw sample is an *ink amount* (0 = no ink = white,
    255 = full ink = black) -- the exact opposite sense of a plain
    DeviceGray sample (0 = black, 255 = white) -- and turning ink-amount
    bytes directly into gray-level bytes with no transform in between
    is a literal photographic negative of the real image. Fixed by
    opening the same file fresh with PyMuPDF and building the pixmap via
    `fitz.Pixmap(doc, xref)` (MuPDF's own image decoder, which returns
    the correct *native* colorspace, `DeviceN` tint-transform function
    included) and then converting that through `fitz.Pixmap(fitz.csGRAY,
    pix)`, which is what actually evaluates the tint-transform function
    into a real gray value rather than assuming the raw bytes already
    are one. Re-verified against the real book: the previously-inverted
    illustration now renders visually identical to the original grayscale
    art (confirmed by rendering both and comparing, not just re-running
    the filter-count check), and this same two-step conversion is safe
    to apply unconditionally to a non-DeviceN JPX image too (Gray stays
    Gray, RGB collapses to Gray the normal way), so it isn't special-
    cased to the one colorspace that actually needed it.

    Returns how many images were converted."""
    targets = []
    for obj in pdf.objects:
        try:
            if obj.get("/Subtype") != "/Image":
                continue
            filt = obj.get("/Filter")
            filters = filt if isinstance(filt, pikepdf.Array) else [filt]
            if not any("JPX" in str(f) for f in filters):
                continue
        except Exception:
            continue
        targets.append(obj.objgen[0])

    if not targets:
        return 0

    doc = fitz.open(pdf_path)
    try:
        for objnum in targets:
            pix = fitz.Pixmap(doc, objnum)
            if pix.alpha:
                raise RuntimeError(
                    "Residual JPX image carries an embedded alpha channel -- "
                    "not seen in any real file this was tested against, and "
                    "stripping alpha here without testing against a real "
                    "example risks silently corrupting the image data."
                )
            gray = fitz.Pixmap(fitz.csGRAY, pix)

            obj = pdf.get_object((objnum, 0))
            obj.write(gray.samples, filter=pikepdf.Name("/FlateEncode"))
            obj.ColorSpace = pikepdf.Name("/DeviceGray")
            obj.BitsPerComponent = 8
            if "/DecodeParms" in obj:
                del obj.DecodeParms
            if "/Decode" in obj:
                del obj.Decode
    finally:
        doc.close()
    return len(targets)


def restore_metadata_and_labels(original: Path, converted: Path, final: Path,
                                 keep_producer: bool,
                                 keep_color_pages: "set[int]" = frozenset()) -> None:
    """Copy Info/XMP metadata and PageLabels from original onto converted,
    then swap back in any pages (0-indexed) that should stay in their
    original color instead of the grayscale-converted version."""
    with pikepdf.open(original) as src, pikepdf.open(converted) as out:
        # Restore XMP + DocInfo metadata
        with src.open_metadata() as src_meta, out.open_metadata() as out_meta:
            out_meta.load_from_docinfo(src.docinfo)
            out_meta.update(src_meta)
            if not keep_producer:
                # Let Ghostscript's Producer stand, showing the file was
                # regenerated — comment out this branch to fully spoof
                # the original Producer string instead.
                pass

        # Restore page labels (e.g. roman numerals for front matter)
        if "/PageLabels" in src.Root:
            out.Root.PageLabels = out.copy_foreign(src.Root.PageLabels)

        jpx_fixed = strip_residual_jpx(converted, out)
        if jpx_fixed:
            print(f"  Re-encoded {jpx_fixed} image(s) Ghostscript left as JPEG2000 (JPXDecode)")

        if keep_color_pages:
            if len(out.pages) != len(src.pages):
                raise RuntimeError(
                    "Page count changed during grayscale conversion "
                    f"({len(src.pages)} -> {len(out.pages)}) -- can't safely "
                    "map cover pages back by index."
                )
            for idx in keep_color_pages:
                out.pages[idx] = src.pages[idx]

        out.save(final)


def default_output_path(input_path: Path) -> Path:
    """input.pdf -> input-grayscale.pdf, alongside the input file."""
    return input_path.with_name(f"{input_path.stem}-grayscale{input_path.suffix}")


def prompt_yes_no(question: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        raw = input(f"{question} {suffix}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print('Please answer "y" or "n".')


def prompt_page_number(question: str, default: int, total_pages: int,
                        allow_none: bool = False) -> "int | None":
    """Prompt for a 1-based page number, re-asking until it's either a
    valid page in range, blank (accepts the default), or (if allowed)
    the literal "none"."""
    while True:
        raw = input(f"{question} [{default}]: ").strip()
        if not raw:
            return default
        if allow_none and raw.lower() == "none":
            return None
        if raw.isdigit() and 1 <= int(raw) <= total_pages:
            return int(raw)
        none_hint = ', or "none"' if allow_none else ""
        print(f"Please enter a page number from 1 to {total_pages}{none_hint}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Source PDF")
    parser.add_argument(
        "output", type=Path, nargs="?",
        help="Destination grayscale PDF (default: <input>-grayscale.pdf)",
    )
    parser.add_argument(
        "--keep-producer", action="store_true",
        help="Reserved for future use; currently a no-op placeholder.",
    )
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"Input file not found: {args.input}")

    output = args.output if args.output is not None else default_output_path(args.input)

    with pikepdf.open(args.input) as probe:
        total_pages = len(probe.pages)

    keep_color_pages: set[int] = set()
    if not prompt_yes_no("Convert the front and back cover to grayscale too?", default=True):
        front = prompt_page_number("Which page is the front cover?",
                                    default=1, total_pages=total_pages)
        back = prompt_page_number(
            'Which page is the back cover? Enter "none" if there is no back cover.',
            default=total_pages, total_pages=total_pages, allow_none=True)
        if front is not None:
            keep_color_pages.add(front - 1)
        if back is not None:
            keep_color_pages.add(back - 1)

    with tempfile.TemporaryDirectory() as tmp:
        gray_tmp = Path(tmp) / "gray.pdf"
        print(f"Converting to grayscale with Ghostscript...")
        run_ghostscript(args.input, gray_tmp)

        print("Restoring metadata and page labels with pikepdf...")
        restore_metadata_and_labels(args.input, gray_tmp, output,
                                     args.keep_producer, keep_color_pages)

    print(f"Done: {output}")


if __name__ == "__main__":
    main()
