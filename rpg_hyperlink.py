"""Wraps *separate, sibling* projects' PDF-hyperlinking tools --
`hyperlink_pdf_universal.py` (GURPS) and `hyperlink_pdf_mongoose.py`
(Mongoose Publishing's Traveller line), both from the
`gurps_4e_revised_hyperlink` repo that happens to live alongside this one
on this machine -- as optional write-time steps here. Originally just the
GURPS script (this file used to be named `gurps_hyperlink.py`); the
Mongoose script shares an identical interface (confirmed: same
`hyperlink_pdf(in_path, out_path)` function signature, same `{"added",
"chapter_added", "toc_added"}` return shape, same `shutil.copyfile`-then-
`saveIncr()` write pattern -- the sibling repo's own CLAUDE.md describes
it as "a fork of hyperlink_pdf_universal.py's logic, not a from-scratch
rewrite"), so one generic function (`run_hyperlink_script()`) runs either
one; only the script path passed in differs.

This is a deliberately machine-specific integration, not a general
feature this project owns: each sibling script's reference-detection
logic is tuned for one publisher's own page/chapter-reference conventions
(see that repo's own CLAUDE.md), and its location on disk is nothing this
project can assume about anyone else's machine -- `config.yaml`'s
`gurps_hyperlink_script`/`mongoose_hyperlink_script` keys are placeholder
paths for exactly that reason (same convention as `root`, another
machine-specific value that ships as a placeholder), and the feature is a
no-op (a clear, non-fatal message) if a given path isn't actually
configured or doesn't exist.

`DEFAULT_GURPS_HYPERLINK_SCRIPT`/`DEFAULT_MONGOOSE_HYPERLINK_SCRIPT` below
are deliberately the same kind of generic placeholder, not a real path --
an earlier version of this hardcoded the actual absolute path from the
machine this was built on, which would have leaked that person's home
directory/username into this repo once it goes public, and (since
`config.yaml`'s own placeholder key already takes precedence whenever
it's present at all) didn't even work as a usable default in practice.
The real path belongs in whatever *un*committed, machine-specific config
file each person already keeps their other machine-specific values (like
`root`) in -- never in a file this project tracks.

Loaded via `importlib` from an absolute path rather than `pip install`ed
or vendored, since these are standalone scripts in an unrelated repo with
their own independent history/versioning -- not packages meant to be
depended on. `pymupdf` (their only real dependency, per each script's own
REQUIREMENTS docstring) is already a dependency of this project too, so
nothing extra needs installing.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_GURPS_HYPERLINK_SCRIPT = Path("/path/to/hyperlink_pdf_universal.py")
DEFAULT_MONGOOSE_HYPERLINK_SCRIPT = Path("/path/to/hyperlink_pdf_mongoose.py")


@dataclass
class HyperlinkResult:
    success: bool
    added: int = 0
    chapter_added: int = 0
    toc_added: int = 0
    message: str = ""


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _load_hyperlink_pdf(script_path: Path):
    """Imports a sibling script by absolute file path (not sys.path, not
    a package import) and returns its `hyperlink_pdf(in_path, out_path)`
    function -- the same function that script's own `main()` calls for
    single-file mode. A fresh module object every call, since each script
    keeps a module-level `TITLE_TRIGGER_WORD` global it resets on entry to
    `hyperlink_pdf()` itself, so re-importing isn't required for
    correctness, but keeps this call fully independent of whatever else
    might be loaded in this process -- including, now, whichever *other*
    sibling script was loaded earlier in the same run (a GURPS book and a
    Mongoose book processed back to back must not share any state)."""
    spec = importlib.util.spec_from_file_location(f"rpg_hyperlink_{script_path.stem}", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load a module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.hyperlink_pdf


def run_hyperlink_script(
    path: Path,
    script_path: str | Path,
    log: Callable[[str], None] | None = None,
) -> HyperlinkResult:
    """Runs a sibling script's `hyperlink_pdf()` against `path`,
    re-saving over the same path. Never raises -- returns success=False
    with a message on any failure, matching `image_converter.py`'s own
    report-don't-crash convention (a caller processing a batch must not
    have one malformed/unsupported PDF, or a misconfigured script path,
    take down the whole run).

    Saves to a temp file and atomically replaces `path` only once that
    succeeds (`os.replace()`, same filesystem via `dir=path.parent`),
    same reasoning as `convert_images_to_rgb()`: a failure partway
    through leaves the original file untouched. This is also the *only*
    way to call either sibling script at all -- each refuses to write to
    the same path it read from (`shutil.copyfile` raises `SameFileError`
    on a same-path copy), so there's no in-place mode to opt into even if
    this project wanted one.

    The sibling script's own `<out_stem>_link_report.csv` (everything it
    linked or skipped, and why) is kept, renamed to match `path`'s own
    stem, alongside the final file.
    """
    script_path = Path(script_path)
    if not path.exists():
        return HyperlinkResult(success=False, message="file not found")
    if not script_path.exists():
        return HyperlinkResult(
            success=False,
            message=f"hyperlink script not found at {script_path} -- set it in config.yaml",
        )

    try:
        hyperlink_pdf = _load_hyperlink_pdf(script_path)
    except Exception as exc:
        return HyperlinkResult(success=False, message=f"could not load hyperlink script: {exc}")

    # A hidden prefix (matching scan_pdfs()'s own dotfile skip in
    # matcher.py) means a temp file left behind by a force-killed run
    # can't later get picked up and "matched" as if it were a real book.
    tmp_fd, tmp_out_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_fd)
    tmp_out_path = Path(tmp_out_str)
    tmp_report_path = Path(tmp_out_str.rsplit(".", 1)[0] + "_link_report.csv")

    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            counts = hyperlink_pdf(str(path), str(tmp_out_path))
        if log is not None:
            for line in buf.getvalue().splitlines():
                if line.strip():
                    log(line)

        os.replace(tmp_out_path, path)
        tmp_out_path = None  # already moved -- don't clean it up in finally

        report_dest = path.with_name(f"{path.stem}_link_report.csv")
        if tmp_report_path.exists():
            os.replace(tmp_report_path, report_dest)
            tmp_report_path = None

        return HyperlinkResult(
            success=True,
            added=counts.get("added", 0),
            chapter_added=counts.get("chapter_added", 0),
            toc_added=counts.get("toc_added", 0),
        )
    except Exception as exc:
        return HyperlinkResult(success=False, message=f"hyperlinking failed: {exc}")
    finally:
        if tmp_out_path is not None:
            _unlink_quietly(tmp_out_path)
        if tmp_report_path is not None:
            _unlink_quietly(tmp_report_path)
