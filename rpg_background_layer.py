"""Wraps a sibling PDF background-layer tool
(`castles_and_crusades_background_layer.py`), vendored as a plain file in
this project's own directory -- same vendoring reasoning as
`rpg_hyperlink.py`/`rpg_grayscale.py`'s own module docstrings (read those
first; this repeats only what's different). Tags each page's full-page
decorative background as a toggleable Optional Content Group layer, or
deletes it outright.

Unlike either of those two, this script has no separable non-interactive
library function to import -- its whole logic lives inside a single
`argparse`-driven `main()`, with no equivalent to `hyperlink_pdf()` or
`run_ghostscript()`/`restore_metadata_and_labels()`. Rather than
duplicate that orchestration logic here (the exact fork/drift risk
vendoring these scripts as plain files, rather than reimplementing their
logic, exists to avoid), this invokes the script as a real subprocess via
`sys.executable` (the same interpreter already running this project,
which already has `pikepdf` -- this script's only dependency), exactly
the way its own author runs it from a terminal:
`python3 castles_and_crusades_background_layer.py src dst [--remove]`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

BACKGROUND_LAYER_SCRIPT = Path(__file__).parent / "castles_and_crusades_background_layer.py"

# Unlike every other step in this family (importlib-loaded functions
# running in this same process), this one is a real subprocess -- with no
# timeout, a genuinely hung child process (rather than one that just
# fails/exits) would block this call, and therefore a `tag`/`write-pdfs`
# run, forever with no way out short of killing the whole thing. 5 minutes
# is generous for a single PDF, even an image-heavy one.
SUBPROCESS_TIMEOUT_SECONDS = 300


@dataclass
class BackgroundLayerResult:
    success: bool
    message: str = ""


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def apply_background_layer(
    path: Path,
    remove: bool = False,
    log: Callable[[str], None] | None = None,
) -> BackgroundLayerResult:
    """Runs the vendored script's background-tag/`--remove` logic against
    `path`, re-saving over the same path. Never raises -- returns
    success=False with a message on any failure, matching this whole
    family's report-don't-crash convention.

    Saves to a temp file and atomically replaces `path` only once the
    subprocess actually succeeds (`os.replace()`, same filesystem via
    `dir=path.parent`), same reasoning as the rest of this family: the
    script itself takes separate src/dst arguments (its own
    `pikepdf.open()` call has no `allow_overwriting_input=True`, so it
    can't write back over what it just read even if we wanted it to),
    and a failure partway through must leave the original untouched.
    """
    if not path.exists():
        return BackgroundLayerResult(success=False, message="file not found")
    if not BACKGROUND_LAYER_SCRIPT.exists():
        # Should be unreachable -- vendored, committed file -- but a
        # broken/partial working tree is a real enough possibility to
        # degrade instead of crashing.
        return BackgroundLayerResult(success=False, message=f"expected vendored script missing: {BACKGROUND_LAYER_SCRIPT}")

    # Hidden prefix -- see rpg_hyperlink.py's run_hyperlink_script() for
    # why (a force-killed run must not leave a "book" scan_pdfs() would
    # later try to match).
    tmp_fd, tmp_out_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_fd)
    tmp_out_path: Path | None = Path(tmp_out_str)

    try:
        cmd = [sys.executable, str(BACKGROUND_LAYER_SCRIPT), str(path), str(tmp_out_path)]
        if remove:
            cmd.append("--remove")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT_SECONDS)

        if log is not None:
            for line in result.stdout.splitlines():
                if line.strip():
                    log(line)

        if result.returncode != 0:
            stderr_lines = [line for line in result.stderr.splitlines() if line.strip()]
            message = stderr_lines[-1] if stderr_lines else f"exited with code {result.returncode}"
            return BackgroundLayerResult(success=False, message=message)

        # mkstemp() creates its file 0600, and the script's pikepdf save
        # writes into that existing file, keeping 0600 -- without this the
        # book would end up owner-only after the swap (unreadable to a
        # reader app running as another user, e.g. in Docker). Best-effort:
        # some network filesystems refuse chmod, and that must not throw
        # away an otherwise-successful step.
        try:
            shutil.copymode(path, tmp_out_path)
        except OSError:
            pass
        os.replace(tmp_out_path, path)
        tmp_out_path = None
        return BackgroundLayerResult(success=True)
    except Exception as exc:
        return BackgroundLayerResult(success=False, message=f"background-layer step failed: {exc}")
    finally:
        if tmp_out_path is not None:
            _unlink_quietly(tmp_out_path)
