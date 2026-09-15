"""Prompt §4 Step 4 fallback clause — issue #42, Task 9.

Pins three regex-based content tests against
`src/nora/prompts/netops_orchestrator.md` between the `## 4.` and
`## 5.` headings:

* Prompt-S1 — §4 contains at least one `register_device` literal.
* Prompt-S2 — §4 references the validation contract (`sysDescr` OR
  the dotted OID `1.3.6.1.2.1.1.1.0`).
* Prompt-S3 — §4 does NOT instruct the orchestrator to echo the
  community string back to the operator (`echo` followed by
  `community`).

The prompt tests are content-based (NOT behavioural); this matches
the explicit design §3 note that prompt tests are regex scans.
"""

from __future__ import annotations

import re
from pathlib import Path

PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts" / "netops_orchestrator.md"
)


def _section_4_text() -> str:
    """Extract the body of §4 from the prompt file (between `## 4.` and `## 5.`)."""
    text = PROMPT_PATH.read_text()
    # Match `## 4. <title>` through the next `## ` heading.
    match = re.search(r"## 4\..*?(?=\n## |\Z)", text, flags=re.DOTALL)
    assert match, f"Could not locate §4 in {PROMPT_PATH}"
    return match.group(0)


def test_mentions_register_device_in_section_4() -> None:
    """§4 mentions `register_device` at least once."""
    body = _section_4_text()
    assert "register_device" in body, (
        f"§4 must mention `register_device`; body excerpt: {body[:200]!r}"
    )


def test_references_sysdescr_validation_contract() -> None:
    """§4 references the sysDescr validation contract (`sysDescr` OR `1.3.6.1.2.1.1.1.0`)."""
    body = _section_4_text()
    has_sysdescr = bool(re.search(r"\bsysDescr\b", body))
    has_oid = "1.3.6.1.2.1.1.1.0" in body
    assert has_sysdescr or has_oid, (
        f"§4 must reference the sysDescr validation contract (sysDescr OR 1.3.6.1.2.1.1.1.0); "
        f"body excerpt: {body[:300]!r}"
    )


def test_does_not_instruct_orchestrator_to_echo_community() -> None:
    """§4 does NOT contain `echo ... community` (case-insensitive)."""
    body = _section_4_text()
    pattern = re.compile(r"\becho\b[^.\n]*\bcommunity\b", re.IGNORECASE)
    matches = pattern.findall(body)
    assert matches == [], (
        f"§4 must NOT instruct the orchestrator to echo the community string; found: {matches!r}"
    )
