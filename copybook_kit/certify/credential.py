"""Certificate document model, digest, tamper checks and authorization routes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import CopybookKitError
from ..util import canonical_json, json_safe, sha256_bytes, sha256_text, utc_text
from datetime import datetime

CERTIFICATE_SCHEMA_VERSION = 1

LEVEL_FILE_REUSE = "file-reuse"
LEVEL_UNIVERSAL = "universal"
LEVELS = (LEVEL_FILE_REUSE, LEVEL_UNIVERSAL)

VERDICT_PASS = "pass"
VERDICT_REJECT = "reject"
VERDICT_UNKNOWN = "unknown"
VERDICTS = (VERDICT_PASS, VERDICT_REJECT, VERDICT_UNKNOWN)

# The two authorization routes. A certificate never implies the route it did
# not prove: file-level evidence cannot authorize future files, and a static
# proof is not granted from sample data.
ROUTE_AUTHORITY = {
    LEVEL_FILE_REUSE: {
        "scope": "this input file only",
        "permits": "one-time reuse of the archived file under the new copybook",
        "forbids": "enabling the new copybook for future files of the same contract",
        "proof_basis": "per-record byte and business-value comparison on the supplied input",
    },
    LEVEL_UNIVERSAL: {
        "scope": "every legal record of the old contract",
        "permits": "switching parsing to the new copybook without re-collecting future files",
        "forbids": "nothing inside the proved field paths; unsupported PIC/charset/redefinitions "
                   "stay unknown and are not silently accepted",
        "proof_basis": "static equivalence over all byte strings the old layout permits",
    },
}

_DIGEST_KEY = "digest"
_REQUIRED_TOP_KEYS = (
    "schema_version", "certificate_id", "created_at", "level", "authorization", "materials",
    "field_paths", "file_evaluation", "universal_evaluation",
)


class CertificateError(CopybookKitError):
    """Raised when a certificate is malformed, tampered with or below level."""


def certificate_digest(payload: dict[str, Any]) -> str:
    """Hash every claim except the digest itself, so any material change moves it."""
    body = {key: value for key, value in payload.items() if key != _DIGEST_KEY}
    return sha256_text(canonical_json(body))


def finalize_certificate(payload: dict[str, Any]) -> dict[str, Any]:
    sealed = dict(json_safe(payload))
    sealed["digest"] = certificate_digest(sealed)
    return sealed


def certificate_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact ledger entry identifying which proof authorized a child run."""
    return {
        "certificate_id": payload["certificate_id"],
        "digest": payload["digest"],
        "schema_version": payload["schema_version"],
        "requested_level": payload["level"]["requested"],
        "file_reuse": payload["level"][LEVEL_FILE_REUSE],
        "universal": payload["level"][LEVEL_UNIVERSAL],
        "input_sha256": payload["materials"]["input"]["sha256"],
        "old_copybook_sha256": payload["materials"]["old_copybook"]["sha256"],
        "new_copybook_sha256": payload["materials"]["new_copybook"]["sha256"],
        "rules_sha256": payload["materials"]["rules"]["sha256"],
    }


def meets_level(payload: dict[str, Any], required_level: str) -> bool:
    if required_level not in LEVELS:
        raise ValueError(f"unknown certification level {required_level!r}")
    return payload["level"][required_level] == VERDICT_PASS


def _check_structure(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CertificateError("certificate must be a JSON object", code="CF003")
    missing = [key for key in _REQUIRED_TOP_KEYS if key not in payload]
    if missing:
        raise CertificateError(f"certificate is missing keys: {missing}", code="CF003")
    if payload.get("schema_version") != CERTIFICATE_SCHEMA_VERSION:
        raise CertificateError(
            f"unsupported certificate schema_version {payload.get('schema_version')!r}", code="CF003")
    level = payload["level"]
    if not isinstance(level, dict) or any(level.get(name) not in VERDICTS
                                          for name in (LEVEL_FILE_REUSE, LEVEL_UNIVERSAL)):
        raise CertificateError("certificate has invalid level verdicts", code="CF003")
    return payload


def load_certificate(path: str | Path) -> dict[str, Any]:
    """Read and self-check a certificate file; never trust a broken digest."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise CertificateError(f"cannot read certificate {path}: {exc}", code="CF003") from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CertificateError(f"certificate {path} is not valid JSON: {exc}", code="CF003") from exc
    _check_structure(payload)
    stored = payload.get(_DIGEST_KEY)
    if not isinstance(stored, str) or stored != certificate_digest(payload):
        raise CertificateError("certificate digest does not match its contents", code="CF003")
    return payload


def verify_certificate(payload: dict[str, Any], *, input_path: str | Path,
                       old_copybook_path: str | Path, new_copybook_path: str | Path,
                       rules_path: str | Path) -> dict[str, Any]:
    """Re-hash every referenced file from raw bytes; names and mtimes are ignored."""
    _check_structure(payload)
    if payload.get(_DIGEST_KEY) != certificate_digest(payload):
        raise CertificateError("certificate digest does not match its contents", code="CF003")
    materials = payload["materials"]
    expected = {
        "input": (Path(input_path), materials["input"]["sha256"]),
        "old_copybook": (Path(old_copybook_path), materials["old_copybook"]["sha256"]),
        "new_copybook": (Path(new_copybook_path), materials["new_copybook"]["sha256"]),
        "rules": (Path(rules_path), materials["rules"]["sha256"]),
    }
    for label, (path, wanted) in expected.items():
        try:
            actual = sha256_bytes(Path(path).read_bytes())
        except OSError as exc:
            raise CertificateError(f"cannot read {label} material {path}: {exc}", code="CF001") from exc
        if actual != wanted:
            raise CertificateError(
                f"{label} bytes changed since certification; certificate {payload['certificate_id']} "
                "is invalid for this material", code="CF001")
    return payload


def require_level(payload: dict[str, Any], required_level: str) -> dict[str, Any]:
    """Gate used by the pipeline contract-selection entry point."""
    if required_level not in LEVELS:
        raise CertificateError(f"unknown certification level {required_level!r}", code="CF002")
    if not meets_level(payload, required_level):
        raise CertificateError(
            f"certificate {payload['certificate_id']} reaches "
            f"file-reuse={payload['level'][LEVEL_FILE_REUSE]}, "
            f"universal={payload['level'][LEVEL_UNIVERSAL]} but {required_level} is required; "
            "the new-copybook child run is blocked", code="CF002")
    return payload


def timestamp(now: datetime) -> str:
    return utc_text(now)
