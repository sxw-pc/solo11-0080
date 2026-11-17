"""Offline copybook replacement certification.

A certificate answers one business question: for raw records already accepted
under an old copybook contract, do the requested business field paths still
decode to the same values under a new copybook? Two levels exist:

* ``file-reuse``  — proven per record against one read-only data file only;
* ``universal``   — proven statically for every byte string the old layout
  permits, without enumerating inputs or treating the sample as a proof.

Neither level may claim the other one.
"""

from __future__ import annotations

from .credential import (
    CERTIFICATE_SCHEMA_VERSION,
    LEVEL_FILE_REUSE,
    LEVEL_UNIVERSAL,
    LEVELS,
    VERDICT_PASS,
    VERDICT_REJECT,
    VERDICT_UNKNOWN,
    VERDICTS,
    CertificateError,
    certificate_digest,
    certificate_summary,
    load_certificate,
    meets_level,
    require_level,
    verify_certificate,
)
from .engine import CertificationInput, build_and_certify, certify, resolve_field_paths

__all__ = [
    "CERTIFICATE_SCHEMA_VERSION",
    "LEVEL_FILE_REUSE",
    "LEVEL_UNIVERSAL",
    "LEVELS",
    "VERDICT_PASS",
    "VERDICT_REJECT",
    "VERDICT_UNKNOWN",
    "VERDICTS",
    "CertificationInput",
    "CertificateError",
    "build_and_certify",
    "certificate_digest",
    "certificate_summary",
    "certify",
    "load_certificate",
    "meets_level",
    "require_level",
    "resolve_field_paths",
    "verify_certificate",
]
