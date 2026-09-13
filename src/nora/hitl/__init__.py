"""HITL seam — slice 4 (PR 4 commit 1).

Exposes the approval-token verifier so the migration tool
(``snmp_migrate_radio_frequency``) can gate the SET frame on a
verified :class:`HitlApprovalToken`. The full HITL ChangeRequest
state machine lands in the Phase-3 cluster.
"""

from nora.hitl.tokens import (
    HitlApprovalToken,
    mint_token,
    verify_approval_token,
)

__all__ = [
    "HitlApprovalToken",
    "mint_token",
    "verify_approval_token",
]
