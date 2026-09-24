"""Wraps a *separate, sibling* project's PDF background-layer tool
(`castles_and_crusades_background_layer.py`, from the
`gurps_4e_revised_hyperlink` repo that happens to live alongside this one
on this machine) as an optional write-time step here -- same machine-
specific-integration reasoning as `rpg_hyperlink.py`/`rpg_grayscale.py`'s
own module docstrings (read those first; this repeats only what's
different). Tags each page's full-page decorative background as a
toggleable Optional Content Group layer, or deletes it outright.

Unlike either of those two, this sibling script has no separable non-
interactive library function to import -- its whole logic lives inside a
single `argparse`-driven `main()`, with no equivalent to `hyperlink_pdf()`
or `run_ghostscript()`/`restore_metadata_and_labels()`. Rather than
duplicate that orchestration logic here (the exact fork/drift risk this
whole family of integrations exists to avoid), this invokes the script as
a real subprocess via `sys.executable` (the same interpreter already
running this project, which already has `pikepdf` -- this sibling
script's only dependency), exactly the way its own author runs it from a
terminal: `python3 castles_and_crusades_background_layer.py src dst
[--remove]`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_BACKGROUND_LAYER_SCRIPT = Path("/path/to/castles_and_crusades_background_layer.py")

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
    script_path: str | Path,
    remove: bool = False,
    log: Callable[[str], None] | None = None,
) -> BackgroundLayerResult:
    """Runs the sibling script's background-tag/`--remove` logic against
    `path`, re-saving over the same path. Never raises -- returns
    success=False with a message on any failure, matching this whole
    family's report-don't-crash convention.

    Saves to a temp file and atomically replaces `path` only once the
    subprocess actually succeeds (`os.replace()`, same filesystem via
    `dir=path.parent`), same reasoning as the rest of this family: the
    sibling script itself takes separate src/dst arguments (its own
    `pikepdf.open()` call has no `allow_overwriting_input=True`, so it
    can't write back over what it just read even if we wanted it to),
    and a failure partway through must leave the original untouched.
    """
    script_path = Path(script_path)
    if not path.exists():
        return BackgroundLayerResult(success=False, message="file not found")
    if not script_path.exists():
        return BackgroundLayerResult(
            success=False,
            message=f"background-layer script not found at {script_path} -- set it in config.yaml",
        )

    # Hidden prefix -- see rpg_hyperlink.py's run_hyperlink_script() for
    # why (a force-killed run must not leave a "book" scan_pdfs() would
    # later try to match).
    tmp_fd, tmp_out_str = tempfile.mkstemp(suffix=".pdf", prefix=".dtrpg-tmp-", dir=str(path.parent))
    os.close(tmp_fd)
    tmp_out_path: Path | None = Path(tmp_out_str)

    try:
        cmd = [sys.executable, str(script_path), str(path), str(tmp_out_path)]
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

        os.replace(tmp_out_path, path)
        tmp_out_path = None
        return BackgroundLayerResult(success=True)
    except Exception as exc:
        return BackgroundLayerResult(success=False, message=f"background-layer step failed: {exc}")
    finally:
        if tmp_out_path is not None:
            _unlink_quietly(tmp_out_path)
