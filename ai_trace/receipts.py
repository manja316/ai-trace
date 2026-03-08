"""Signed receipts — Ed25519 signed audit artifacts for decision steps.

Turns trace logs into legally defensible, tamper-evident audit artifacts.
Each receipt contains the full step data, a SHA-256 content hash, an Ed25519
signature, and an optional chain link to the previous receipt (hash chain).

Requires: pip install ai-decision-tracer[signed]
Without the cryptography package, receipts still work but produce hash-only
integrity (no signatures).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ai_trace.step import Step


# ── Crypto availability ──────────────────────────────────────────────────────

_HAS_CRYPTO = False
try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    from cryptography.hazmat.primitives import serialization
    from cryptography.exceptions import InvalidSignature

    _HAS_CRYPTO = True
except ImportError:
    pass


def _require_crypto():
    if not _HAS_CRYPTO:
        raise ImportError(
            "Signed receipts require the 'cryptography' package.\n"
            "Install with: pip install ai-decision-tracer[signed]"
        )


# ── Canonicalization ─────────────────────────────────────────────────────────

def canonicalize(data: dict) -> bytes:
    """Deterministic JSON serialization for hashing/signing.

    Sorted keys, compact separators, ASCII-only. This is the byte string
    that gets hashed and signed — any change to the data changes the output.
    """
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


def content_hash(data: dict) -> str:
    """SHA-256 hex digest of canonicalized data."""
    return hashlib.sha256(canonicalize(data)).hexdigest()


# ── Key management ───────────────────────────────────────────────────────────

def generate_keypair() -> tuple:
    """Generate an Ed25519 key pair. Returns (private_key, public_key) objects."""
    _require_crypto()
    private_key = Ed25519PrivateKey.generate()
    return private_key, private_key.public_key()


def private_key_to_bytes(key) -> bytes:
    """Serialize private key to raw 32 bytes."""
    _require_crypto()
    return key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )


def public_key_to_base64(key) -> str:
    """Serialize public key to base64 string (raw 32 bytes, base64 encoded)."""
    _require_crypto()
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return base64.b64encode(raw).decode("ascii")


def public_key_from_base64(b64: str):
    """Deserialize a base64 public key string back to an Ed25519PublicKey object."""
    _require_crypto()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    raw = base64.b64decode(b64)
    return Ed25519PublicKey.from_public_bytes(raw)


def load_private_key_from_pem(path: str):
    """Load a PEM-encoded Ed25519 private key (compatible with KYA keys)."""
    _require_crypto()
    return serialization.load_pem_private_key(
        Path(path).read_bytes(), password=None
    )


# ── SignedReceipt ────────────────────────────────────────────────────────────

@dataclass
class SignedReceipt:
    """A tamper-evident, signed record of one decision step.

    Self-contained: a third party with only the receipt JSON and the public
    key can verify authenticity and integrity.
    """

    # Step data
    step_name: str
    agent_id: str
    session_id: str
    timestamp: str
    context: Dict[str, Any]
    outcome: Optional[str]
    logs: List[Dict[str, Any]]
    duration_ms: Optional[float]

    # Integrity
    content_hash: str

    # Signature (empty strings if unsigned / crypto unavailable)
    signature: str = ""
    public_key: str = ""

    # Chain link
    previous_hash: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_name": self.step_name,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "context": self.context,
            "outcome": self.outcome,
            "logs": self.logs,
            "duration_ms": self.duration_ms,
            "content_hash": self.content_hash,
            "signature": self.signature,
            "public_key": self.public_key,
            "previous_hash": self.previous_hash,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SignedReceipt":
        return cls(
            step_name=d["step_name"],
            agent_id=d["agent_id"],
            session_id=d["session_id"],
            timestamp=d["timestamp"],
            context=d.get("context", {}),
            outcome=d.get("outcome"),
            logs=d.get("logs", []),
            duration_ms=d.get("duration_ms"),
            content_hash=d["content_hash"],
            signature=d.get("signature", ""),
            public_key=d.get("public_key", ""),
            previous_hash=d.get("previous_hash"),
        )

    @property
    def is_signed(self) -> bool:
        return bool(self.signature and self.public_key)

    @property
    def step_data(self) -> Dict[str, Any]:
        """The signable content — step data only, no signature/chain fields."""
        return {
            "step_name": self.step_name,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "context": self.context,
            "outcome": self.outcome,
            "logs": self.logs,
            "duration_ms": self.duration_ms,
        }


# ── Receipt builder ──────────────────────────────────────────────────────────

class ReceiptBuilder:
    """Creates and verifies signed receipts.

    Parameters
    ----------
    agent_id : str
        Agent identifier embedded in every receipt.
    session_id : str
        Session identifier embedded in every receipt.
    signing_key : Ed25519PrivateKey, optional
        If provided, every receipt is signed. If None and crypto is available,
        a session keypair is auto-generated. If crypto is unavailable, receipts
        are hash-only (still tamper-detectable, not signature-verifiable).
    """

    def __init__(
        self,
        agent_id: str,
        session_id: str,
        signing_key=None,
    ):
        self.agent_id = agent_id
        self.session_id = session_id
        self._receipts: List[SignedReceipt] = []
        self._last_hash: Optional[str] = None

        # Key setup
        self._private_key = None
        self._public_key = None
        self._public_key_b64 = ""

        if signing_key is not None:
            _require_crypto()
            self._private_key = signing_key
            self._public_key = signing_key.public_key()
            self._public_key_b64 = public_key_to_base64(self._public_key)
        elif _HAS_CRYPTO:
            # Auto-generate session keypair
            self._private_key, self._public_key = generate_keypair()
            self._public_key_b64 = public_key_to_base64(self._public_key)

    @property
    def public_key_base64(self) -> str:
        """Base64-encoded public key for third-party verification."""
        return self._public_key_b64

    @property
    def receipts(self) -> List[SignedReceipt]:
        return list(self._receipts)

    def create_receipt(self, step: "Step") -> SignedReceipt:
        """Create a signed receipt from a completed Step."""
        ts = datetime.fromtimestamp(
            step._started_at or 0, tz=timezone.utc
        ).isoformat()

        step_data = {
            "step_name": step.name,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "timestamp": ts,
            "context": step.context,
            "outcome": step.outcome,
            "logs": step.logs,
            "duration_ms": step.duration_ms,
        }

        chash = content_hash(step_data)

        # Sign if we have a key
        sig_b64 = ""
        if self._private_key is not None:
            # Sign the content hash (not the raw data) — deterministic
            sig_bytes = self._private_key.sign(chash.encode("utf-8"))
            sig_b64 = base64.b64encode(sig_bytes).decode("ascii")

        receipt = SignedReceipt(
            step_name=step.name,
            agent_id=self.agent_id,
            session_id=self.session_id,
            timestamp=ts,
            context=step.context,
            outcome=step.outcome,
            logs=step.logs,
            duration_ms=step.duration_ms,
            content_hash=chash,
            signature=sig_b64,
            public_key=self._public_key_b64,
            previous_hash=self._last_hash,
        )

        self._last_hash = chash
        self._receipts.append(receipt)
        return receipt

    # ── Verification ─────────────────────────────────────────────────────────

    @staticmethod
    def verify_receipt(receipt: SignedReceipt) -> Dict[str, Any]:
        """Verify a single receipt's hash and signature integrity.

        Returns a result dict: {"valid": bool, "hash_ok": bool, "signature_ok": bool|None, ...}
        """
        # 1. Verify content hash
        expected_hash = content_hash(receipt.step_data)
        hash_ok = expected_hash == receipt.content_hash

        result = {
            "valid": hash_ok,
            "hash_ok": hash_ok,
            "signature_ok": None,  # None = not checked (unsigned)
            "content_hash": receipt.content_hash,
        }

        if not hash_ok:
            result["error"] = "Content hash mismatch — data has been tampered with"
            return result

        # 2. Verify signature if present
        if receipt.is_signed:
            if not _HAS_CRYPTO:
                result["signature_ok"] = None
                result["warning"] = "cryptography package not installed, cannot verify signature"
                return result

            try:
                pub_key = public_key_from_base64(receipt.public_key)
                sig_bytes = base64.b64decode(receipt.signature)
                pub_key.verify(sig_bytes, receipt.content_hash.encode("utf-8"))
                result["signature_ok"] = True
            except InvalidSignature:
                result["valid"] = False
                result["signature_ok"] = False
                result["error"] = "Signature verification failed — receipt may have been tampered with"
            except Exception as e:
                result["valid"] = False
                result["signature_ok"] = False
                result["error"] = f"Signature verification error: {e}"

        return result

    def verify_chain(self) -> Dict[str, Any]:
        """Verify the entire receipt chain — hash links and all signatures.

        Returns:
            {"valid": bool, "receipts_checked": int, "errors": [...]}
        """
        errors = []

        for i, receipt in enumerate(self._receipts):
            # Verify individual receipt
            r = self.verify_receipt(receipt)
            if not r["valid"]:
                errors.append(f"Receipt {i} ({receipt.step_name}): {r.get('error', 'invalid')}")

            # Verify chain link
            if i == 0:
                if receipt.previous_hash is not None:
                    errors.append(f"Receipt 0: expected previous_hash=None, got {receipt.previous_hash}")
            else:
                expected_prev = self._receipts[i - 1].content_hash
                if receipt.previous_hash != expected_prev:
                    errors.append(
                        f"Receipt {i} ({receipt.step_name}): chain broken — "
                        f"previous_hash={receipt.previous_hash}, expected={expected_prev}"
                    )

        return {
            "valid": len(errors) == 0,
            "receipts_checked": len(self._receipts),
            "errors": errors,
        }

    @staticmethod
    def verify_chain_from_list(receipts: List[SignedReceipt]) -> Dict[str, Any]:
        """Verify a chain of receipts loaded from JSON (no ReceiptBuilder needed)."""
        errors = []

        for i, receipt in enumerate(receipts):
            r = ReceiptBuilder.verify_receipt(receipt)
            if not r["valid"]:
                errors.append(f"Receipt {i} ({receipt.step_name}): {r.get('error', 'invalid')}")

            if i == 0:
                if receipt.previous_hash is not None:
                    errors.append(f"Receipt 0: expected previous_hash=None, got {receipt.previous_hash}")
            else:
                expected_prev = receipts[i - 1].content_hash
                if receipt.previous_hash != expected_prev:
                    errors.append(
                        f"Receipt {i} ({receipt.step_name}): chain broken — "
                        f"previous_hash={receipt.previous_hash}, expected={expected_prev}"
                    )

        return {
            "valid": len(errors) == 0,
            "receipts_checked": len(receipts),
            "errors": errors,
        }

    # ── Persistence ──────────────────────────────────────────────────────────

    def save_receipts(self, path: Optional[str | Path] = None) -> Path:
        """Save all receipts to a JSON file. Atomic write.

        Parameters
        ----------
        path : str or Path, optional
            Output file path. Defaults to ./receipts/{agent_id}_{session_id}_receipts.json

        Returns
        -------
        Path to the written file.
        """
        if path is None:
            out_dir = Path("receipts")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{self.agent_id}_{self.session_id}_receipts.json"
        else:
            out_path = Path(path)
            out_path.parent.mkdir(parents=True, exist_ok=True)

        payload = {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "public_key": self._public_key_b64,
            "receipt_count": len(self._receipts),
            "saved_at": datetime.now(tz=timezone.utc).isoformat(),
            "receipts": [r.to_dict() for r in self._receipts],
        }

        # Atomic write
        fd, tmp = tempfile.mkstemp(
            dir=str(out_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            os.replace(tmp, str(out_path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        return out_path

    @staticmethod
    def load_receipts(path: str | Path) -> tuple[Dict[str, Any], List[SignedReceipt]]:
        """Load receipts from a JSON file.

        Returns (metadata_dict, list_of_receipts).
        """
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        receipts = [SignedReceipt.from_dict(r) for r in data["receipts"]]
        meta = {k: v for k, v in data.items() if k != "receipts"}
        return meta, receipts
