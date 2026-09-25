"""User preferences (DriveThruRPG API key and account name) stored in
their own file -- deliberately *not* in config.yaml, whose own top
comment says "never hardcode [the API key] here" since that file is
meant to be safe to share/commit. This one isn't: the API key is a real
secret, so it's kept separate, gitignored (see .gitignore), and written
with owner-only file permissions.

**This changes this project's previous "the API key only ever comes from
DTRPG_API_KEY" policy** (still stated that way in older docs/comments
until this change): saving a key here means it now also lives in a
plaintext file on disk, not only in an environment variable. Chosen
deliberately, not by accident, so it's worth being explicit about the
trade-off: `os.chmod`-restricted to the owner is real protection against
other local accounts, but not against anything with access to *this*
account (another process running as you, a backup tool that copies file
contents rather than respecting permissions, etc.) the way an
environment variable set only in one shell session is. `resolve_api_key()`
still lets an existing `DTRPG_API_KEY` environment variable win over
whatever's saved here, so nothing changes for anyone already using it --
this file only matters as a fallback for people who'd rather not manage
an environment variable at all, reachable from the GUI's Preferences tab
or the `preferences` TUI subcommand.

**This file previously also carried five sibling-script paths**
(`gurps_hyperlink_script`/`mongoose_hyperlink_script`/`grayscale_script`/
`background_layer_script`/`watermark_script`) before those scripts were
vendored as plain files in this project's own directory (see
`rpg_hyperlink.py`/`rpg_grayscale.py`/`rpg_background_layer.py`/
`watermark_removal.py`) -- there's nothing left to configure for those,
so `Preferences`/`load_preferences()`/`save_preferences()` no longer
mention them at all. An existing `data/preferences.yaml` with those old
keys still loads fine: `load_preferences()` only reads the fields it
knows about, so the stale keys are silently ignored rather than causing
an error or needing a migration step.
"""

from __future__ import annotations

import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

DEFAULT_PREFERENCES_PATH = Path("data/preferences.yaml")


@dataclass
class Preferences:
    api_key: str = ""
    dtrpg_name: str = ""


def load_preferences(path: str | Path = DEFAULT_PREFERENCES_PATH) -> Preferences:
    path = Path(path)
    if not path.exists():
        return Preferences()
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # `.get(key) or default`, not `.get(key, default)` -- an explicit
    # YAML null (a present-but-blank line) must fall back the same as a
    # missing key, the same lesson matcher.py's load_manual_overrides()
    # already learned the hard way for a hand-editable YAML file.
    return Preferences(
        api_key=data.get("api_key") or "",
        dtrpg_name=data.get("dtrpg_name") or "",
    )


def save_preferences(prefs: Preferences, path: str | Path = DEFAULT_PREFERENCES_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(asdict(prefs), default_flow_style=False, sort_keys=False), encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only
    except OSError:
        pass  # best-effort -- not every platform/filesystem honors this


def resolve_api_key(prefs: Preferences) -> str:
    """An existing DTRPG_API_KEY environment variable always wins -- the
    preferences file is a fallback for people who haven't set one, not an
    override for people who have, so nothing changes for anyone already
    relying on the environment variable."""
    return os.environ.get("DTRPG_API_KEY") or prefs.api_key
