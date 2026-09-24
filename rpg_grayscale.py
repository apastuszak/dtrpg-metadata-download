"""Wraps a *separate, sibling* project's PDF-grayscale-conversion tool
(`convert_pdf_to_grayscale.py`, from the `gurps_4e_revised_hyperlink` repo
that happens to live alongside this one on this machine) as an optional
write-time step here -- same machine-specific-integration reasoning as
`rpg_hyperlink.py`'s own module docstring (read that first; this repeats
only what's different).

Unlike the hyperlink scripts (whose only real dependency is `pymupdf`,
already a dependency of this project too), this one shells out to the
real Ghostscript binary (`gs`) via `subprocess` -- a system package
(`brew install ghostscript` on macOS), not something PEP 723/uv/pip can
provide. A missing `gs` produces a clean, non-fatal message (same
degrade-gracefully contract as everything else in this family), not a
crash -- `subprocess.run(["gs", ...])` raises `FileNotFoundError` when
the binary isn't on PATH, caught specifically for an actionable message
rather than surfacing as a raw traceback.

The sibling script's own `main()` interactively prompts (whether to keep
the front/back cover in full color); this bypasses that prompt entirely
and calls its two non-interactive functions directly --
`run_ghostscript(src, dst)` and `restore_metadata_and_labels(original,
converted, final, keep_producer, keep_color_pages)` -- with
`keep_color_pages` always empty (convert everything, including covers),
matching that prompt's own default (accepting every prompt with Enter
converts everything) since there's no interactive terminal to ask a
per-file question in this integration.

Verified directly, not assumed, that this is safe to run *before* the
GURPS/Mongoose hyperlinking step (see `rpg_hyperlink.py`): a link
annotation added via PyMuPDF's `insert_link()`+`saveIncr()` (the exact
mechanism those scripts use) survives Ghostscript's grayscale conversion
pass intact (`-dPreserveAnnots=true`/`-c "/PreserveAnnotTypes [/Link]
def"` in `run_ghostscript()` is exactly why) -- confirmed by building a
synthetic PDF, adding a real link, running it through
`run_ghostscript()`, and reading the link back with `fitz.get_links()`.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_GRAYSCALE_SCRIPT = Path("/path/to/convert_pdf_to_grayscale.py")


@dataclass
class GrayscaleResult:
    success: bool
    message: str = ""


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _load_functions(script_path: Path):
    """Imports the sibling script by absolute file path and returns its
    `(run_ghostscript, restore_metadata_and_labels)` functions -- the
    same two functions its own `main()` calls, just without the
    interactive prompting in between them."""
    spec = importlib.util.spec_from_file_location(f"rpg_grayscale_{script_path.stem}", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load a module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.run_ghostscript, module.restore_metadata_and_labels


def convert_pdf_grayscale(
    path: Path,
    script_path: str | Path,
    log: Callable[[str], None] | None = None,
) -> GrayscaleResult:
    """Runs the sibling script's grayscale conversion against `path`,
    re-saving over the same path. Never raises -- returns success=False
    with a message on any failure, matching this whole family's
    report-don't-crash convention.

    Two temp files, not one: `run_ghostscript()` needs its own output
    path distinct from the input (`gs` won't write over what it's
    reading), and `restore_metadata_and_labels()` needs a separate
    `final` path distinct from both `original` and `converted` (it holds
    `original` open via pikepdf while writing `final`). Only the second
    temp file is atomically swapped onto `path` (`os.replace()`, same
    filesystem via `dir=path.parent`) once everything has actually
    succeeded; both temp files are always cleaned up.
    """
    script_path = Path(script_path)
    if not path.exists():
        return GrayscaleResult(success=False, message="file not found")
    if not script_path.exists():
        return GrayscaleResult(
            success=False,
            message=f"grayscale script not found at {script_path} -- set it in config.yaml",
        )

    try:
        run_ghostscript, restore_metadata_and_labels = _load_functions(script_path)
    except Exception as exc:
        return GrayscaleResult(success=False, message=f"could not load grayscale script: {exc}")

    # Hidden prefix -- see rpg_hyperlink.py's run_hyperlink_script() for why.
    tmp_gray_fd, tmp_gray_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_gray_fd)
    tmp_gray_path = Path(tmp_gray_str)
    tmp_final_fd, tmp_final_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_final_fd)
    tmp_final_path: Path | None = Path(tmp_final_str)

    try:
        if log is not None:
            log(f"Converting {path.name} to grayscale with Ghostscript...")
        try:
            run_ghostscript(path, tmp_gray_path)
        except FileNotFoundError:
            return GrayscaleResult(
                success=False,
                message="Ghostscript ('gs') not found on PATH -- install it, e.g. 'brew install ghostscript' on macOS",
            )

        if log is not None:
            log(f"Restoring metadata/page labels in {path.name}...")
        restore_metadata_and_labels(path, tmp_gray_path, tmp_final_path, keep_producer=False, keep_color_pages=set())

        os.replace(tmp_final_path, path)
        tmp_final_path = None

        return GrayscaleResult(success=True)
    except Exception as exc:
        return GrayscaleResult(success=False, message=f"grayscale conversion failed: {exc}")
    finally:
        _unlink_quietly(tmp_gray_path)
        if tmp_final_path is not None:
            _unlink_quietly(tmp_final_path)
