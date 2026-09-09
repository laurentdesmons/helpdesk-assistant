"""Judge-model configuration for LLM-as-judge evaluators (Phase 3).

The classifier eval is code-based and needs no judge. This module exists now to
enforce one rule everywhere: **the judge is never a mini-tier model** (README §7 —
groundedness score quality degrades with judge tier, and it is not a fixable
prompt problem).
"""

from __future__ import annotations

import re

from helpdesk.config import Settings

_MINI = re.compile(r"mini|nano|small|lite", re.IGNORECASE)


def assert_not_mini(model: str) -> str:
    if _MINI.search(model):
        raise ValueError(
            f"Judge model {model!r} looks mini-tier. Use claude-sonnet-5 or full gpt-5 "
            "(README §7 / eval/judges.py). Set HELPDESK_JUDGE_MODEL."
        )
    return model


def judge_model(settings: Settings) -> str:
    return assert_not_mini(settings.judge_model)
