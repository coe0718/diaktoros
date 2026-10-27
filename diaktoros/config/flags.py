"""The loop's plain on/off settings: each must be a JSON boolean, never a truthy string."""
from __future__ import annotations

# key -> what the message says after "must be a JSON boolean"
_BOOLEANS = {
    "unattended_fixer_push": "; only explicit true authorizes unattended fixer pushes",
    "review_after_ci": " (true holds a review until the head's checks finish)",
    "review_only_update": " (true lets the host push a clean merge of the base into a "
                          "review-only PR)",
    "fixer_scope_lock": " (true holds a fix round that changes files its findings do not name)",
    "fix_ci": " (true hands a failed required check on a fixer's PR to the fixer)",
    "attribution": " (true signs what the loop posts; false turns it off)",
}


def check(loop: dict, where: str) -> None:
    """Raise ``ConfigError`` for the first setting in ``loop`` that is not a JSON boolean."""
    from . import ConfigError   # here, not module level: the package imports this module
    for key, tail in _BOOLEANS.items():
        if type(loop[key]) is not bool:
            raise ConfigError(f"{where}: '{key}' must be a JSON boolean{tail}")
