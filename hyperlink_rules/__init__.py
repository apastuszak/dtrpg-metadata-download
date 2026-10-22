"""Publisher rule sets for hyperlink_engine.py, one module each.

Every module here that defines `Rules` (a `hyperlink_engine.RuleSet`
subclass) is a rule set, named after its file: `gurps.py` is
`--rules gurps`. Nothing else needs registering -- drop a file in and the
engine, CLI and GUI find it. A module whose name starts with "_" is
skipped.

These are Python files, run when found, so only add rule sets you trust.
"""

import functools
import importlib
import logging
import pkgutil

logger = logging.getLogger("hyperlink_rules")


@functools.cache
def discover():
    """{name: (Rules class, None)} for each rule set, or (None, error
    message) for a module that failed to import or has no valid `Rules`, so
    one broken file doesn't hide the others. Looked up once per process (a
    batch would otherwise log a broken file's warning once per book), so a
    rule set added while the GUI is open shows up after a restart."""
    from hyperlink_engine import RuleSet

    found = {}
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{__name__}.{info.name}")
        except Exception as exc:
            logger.warning("hyperlink rule set %r failed to load: %s", info.name, exc)
            found[info.name] = (None, f"{type(exc).__name__}: {exc}")
            continue
        rules = getattr(module, "Rules", None)
        if isinstance(rules, type) and issubclass(rules, RuleSet):
            found[info.name] = (rules, None)
        else:
            found[info.name] = (None, "no Rules class (a hyperlink_engine.RuleSet subclass)")
    return found
