"""Wraps two sibling PDF-hyperlinking scripts -- `hyperlink_pdf_universal.py`
(GURPS) and `hyperlink_pdf_mongoose.py` (Mongoose Publishing's Traveller
line) -- now vendored as plain files in this project's own directory,
committed alongside it. Originally just the GURPS script (this file used
to be named `gurps_hyperlink.py`); the Mongoose script shares an
identical interface (confirmed: same `hyperlink_pdf(in_path, out_path)`
function signature, same `{"added", "chapter_added", "toc_added"}` return
shape, same `shutil.copyfile`-then-`saveIncr()` write pattern -- the
sibling repo's own CLAUDE.md describes it as "a fork of
hyperlink_pdf_universal.py's logic, not a from-scratch rewrite"), so one
generic function (`run_hyperlink_script()`) runs either one; only which
of `GURPS_HYPERLINK_SCRIPT`/`MONGOOSE_HYPERLINK_SCRIPT` is passed in
differs.

**This project previously loaded these from a configurable, machine-
specific absolute path (Preferences/config.yaml) instead of vendoring
them, specifically to avoid committing someone's real home directory/
username into a repo meant to go public.** Vendoring instead removes that
whole configuration surface -- and the failure mode it had: a path left
unset (the default, unconfigured state) made this feature silently do
nothing, which is exactly what happened with the watermark-removal
integration the first time it was tried for real (see
`docs/HISTORY.md`). `GURPS_HYPERLINK_SCRIPT`/`MONGOOSE_HYPERLINK_SCRIPT`
below are real, resolved-at-import-time paths next to this file --
there's nothing left to configure, and nothing that can silently be
unset.

Still loaded via `importlib` from an explicit file path rather than a
normal `import` -- these remain independently-authored, independently-
versioned scripts with their own top-level `main()`/argparse CLI, not
modules written to be imported as a library, so importing them the
normal way would run whatever import-time code they have and pollute
this project's own namespace with their globals. `pymupdf` (their only
real dependency, per each script's own REQUIREMENTS docstring) was
already a dependency of this project before they were vendored, so
nothing new needs installing.
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

GURPS_HYPERLINK_SCRIPT = Path(__file__).parent / "hyperlink_pdf_universal.py"
MONGOOSE_HYPERLINK_SCRIPT = Path(__file__).parent / "hyperlink_pdf_mongoose.py"


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
    """Imports a vendored script by absolute file path (not sys.path, not
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
    script_path: Path,
    log: Callable[[str], None] | None = None,
) -> HyperlinkResult:
    """Runs one of the two vendored scripts' `hyperlink_pdf()` against
    `path` (pass `GURPS_HYPERLINK_SCRIPT` or `MONGOOSE_HYPERLINK_SCRIPT`),
    re-saving over the same path. Never raises -- returns success=False
    with a message on any failure, matching `image_converter.py`'s own
    report-don't-crash convention (a caller processing a batch must not
    have one malformed/unsupported PDF take down the whole run).

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
    if not path.exists():
        return HyperlinkResult(success=False, message="file not found")
    if not script_path.exists():
        # Should be unreachable -- these are vendored, committed files --
        # but a from-scratch checkout with a broken/partial working tree
        # is a real enough possibility to degrade instead of crashing.
        return HyperlinkResult(success=False, message=f"expected vendored script missing: {script_path}")

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
