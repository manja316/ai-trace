"""Tests for signed receipt functionality."""
import json
import time
from pathlib import Path

import pytest

from ai_trace import Tracer, SignedReceipt, ReceiptBuilder
from ai_trace.receipts import (
    canonicalize,
    content_hash,
    generate_keypair,
    public_key_to_base64,
    public_key_from_base64,
    _HAS_CRYPTO,
)


pytestmark = pytest.mark.skipif(not _HAS_CRYPTO, reason="cryptography not installed")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def keypair():
    return generate_keypair()


@pytest.fixture
def signed_tracer(tmp_path, keypair):
    priv, _ = keypair
    return Tracer(
        "test_agent",
        trace_dir=tmp_path,
        auto_save=False,
        sign=True,
        signing_key=priv,
    )


@pytest.fixture
def auto_sign_tracer(tmp_path):
    """Tracer with auto-generated session keys."""
    return Tracer("auto_agent", trace_dir=tmp_path, auto_save=False, sign=True)


# ── Canonicalization ──────────────────────────────────────────────────────────


def test_canonicalize_deterministic():
    d1 = {"b": 2, "a": 1, "c": {"z": 26, "a": 1}}
    d2 = {"c": {"a": 1, "z": 26}, "a": 1, "b": 2}
    assert canonicalize(d1) == canonicalize(d2)


def test_canonicalize_compact():
    result = canonicalize({"key": "value"})
    assert result == b'{"key":"value"}'


def test_content_hash_changes_with_data():
    h1 = content_hash({"x": 1})
    h2 = content_hash({"x": 2})
    assert h1 != h2
    assert len(h1) == 64  # SHA-256 hex


# ── Key management ────────────────────────────────────────────────────────────


def test_generate_keypair():
    priv, pub = generate_keypair()
    assert priv is not None
    assert pub is not None


def test_public_key_roundtrip(keypair):
    _, pub = keypair
    b64 = public_key_to_base64(pub)
    restored = public_key_from_base64(b64)
    assert public_key_to_base64(restored) == b64


# ── Receipt creation ─────────────────────────────────────────────────────────


def test_receipt_created_per_step(signed_tracer):
    with signed_tracer.step("scan", symbol="BTC") as step:
        step.log(signal=0.9)

    with signed_tracer.step("decide", signal=0.9) as step:
        step.log(action="BUY", reason="momentum")

    with signed_tracer.step("execute", action="BUY") as step:
        step.log(filled=True, price=67000)

    assert len(signed_tracer.receipts) == 3
    assert signed_tracer.receipts[0].step_name == "scan"
    assert signed_tracer.receipts[1].step_name == "decide"
    assert signed_tracer.receipts[2].step_name == "execute"


def test_receipt_contains_full_step_data(signed_tracer):
    with signed_tracer.step("analyze", model="claude", prompt="test") as step:
        step.log(confidence=0.95, label="bullish")

    r = signed_tracer.receipts[0]
    assert r.step_name == "analyze"
    assert r.agent_id == "test_agent"
    assert r.context == {"model": "claude", "prompt": "test"}
    assert r.outcome == "ok"
    assert len(r.logs) == 1
    assert r.logs[0]["confidence"] == 0.95
    assert r.duration_ms is not None
    assert r.duration_ms >= 0


def test_receipt_is_signed(signed_tracer):
    with signed_tracer.step("x"):
        pass

    r = signed_tracer.receipts[0]
    assert r.is_signed
    assert len(r.signature) > 0
    assert len(r.public_key) > 0
    assert len(r.content_hash) == 64


def test_receipt_chain_links(signed_tracer):
    for name in ["a", "b", "c"]:
        with signed_tracer.step(name):
            pass

    receipts = signed_tracer.receipts
    assert receipts[0].previous_hash is None
    assert receipts[1].previous_hash == receipts[0].content_hash
    assert receipts[2].previous_hash == receipts[1].content_hash


# ── Verification ─────────────────────────────────────────────────────────────


def test_verify_receipt_valid(signed_tracer):
    with signed_tracer.step("test"):
        pass

    result = ReceiptBuilder.verify_receipt(signed_tracer.receipts[0])
    assert result["valid"] is True
    assert result["hash_ok"] is True
    assert result["signature_ok"] is True


def test_verify_receipt_tampered_data(signed_tracer):
    with signed_tracer.step("test"):
        pass

    r = signed_tracer.receipts[0]
    # Tamper with the data
    r.context["injected"] = "malicious"

    result = ReceiptBuilder.verify_receipt(r)
    assert result["valid"] is False
    assert result["hash_ok"] is False


def test_verify_receipt_tampered_signature(signed_tracer):
    with signed_tracer.step("test"):
        pass

    r = signed_tracer.receipts[0]
    # Corrupt the signature
    import base64
    bad_sig = base64.b64encode(b"x" * 64).decode()
    r.signature = bad_sig

    result = ReceiptBuilder.verify_receipt(r)
    assert result["valid"] is False
    assert result["signature_ok"] is False


def test_verify_chain_valid(signed_tracer):
    for name in ["scan", "decide", "execute"]:
        with signed_tracer.step(name):
            pass

    result = signed_tracer.verify_receipts()
    assert result["valid"] is True
    assert result["receipts_checked"] == 3
    assert result["errors"] == []


def test_verify_chain_broken_link(signed_tracer):
    for name in ["a", "b", "c"]:
        with signed_tracer.step(name):
            pass

    # Break the chain
    signed_tracer.receipts[1].previous_hash = "0000dead"

    # Also need to tamper the builder's internal list
    result = signed_tracer.verify_receipts()
    assert result["valid"] is False
    assert len(result["errors"]) > 0


# ── Auto-generated keys ─────────────────────────────────────────────────────


def test_auto_sign_generates_keys(auto_sign_tracer):
    assert auto_sign_tracer.public_key != ""

    with auto_sign_tracer.step("test"):
        pass

    r = auto_sign_tracer.receipts[0]
    assert r.is_signed

    result = ReceiptBuilder.verify_receipt(r)
    assert result["valid"] is True


# ── Serialization roundtrip ──────────────────────────────────────────────────


def test_receipt_to_dict_roundtrip(signed_tracer):
    with signed_tracer.step("roundtrip", x=42) as step:
        step.log(y=99)

    original = signed_tracer.receipts[0]
    d = original.to_dict()
    restored = SignedReceipt.from_dict(d)

    assert restored.step_name == original.step_name
    assert restored.content_hash == original.content_hash
    assert restored.signature == original.signature
    assert restored.public_key == original.public_key
    assert restored.previous_hash == original.previous_hash

    # Verify the restored receipt
    result = ReceiptBuilder.verify_receipt(restored)
    assert result["valid"] is True


def test_save_and_load_receipts(signed_tracer, tmp_path):
    for name in ["scan", "decide", "execute"]:
        with signed_tracer.step(name):
            pass

    out = signed_tracer.save_receipts(tmp_path / "test_receipts.json")
    assert out.exists()

    # Load and verify
    meta, receipts = ReceiptBuilder.load_receipts(out)
    assert meta["agent_id"] == "test_agent"
    assert meta["receipt_count"] == 3
    assert len(receipts) == 3

    # Verify loaded chain
    result = ReceiptBuilder.verify_chain_from_list(receipts)
    assert result["valid"] is True
    assert result["receipts_checked"] == 3


def test_third_party_verification(signed_tracer, tmp_path):
    """Simulate third-party verification: load receipts + public key, verify."""
    with signed_tracer.step("action", data="sensitive"):
        pass

    # Save receipts
    out = signed_tracer.save_receipts(tmp_path / "receipts.json")

    # Third party loads the file — they have NO access to the private key
    data = json.loads(out.read_text())
    pub_key_b64 = data["public_key"]
    receipts = [SignedReceipt.from_dict(r) for r in data["receipts"]]

    # Verify each receipt
    for r in receipts:
        assert r.public_key == pub_key_b64
        result = ReceiptBuilder.verify_receipt(r)
        assert result["valid"] is True
        assert result["signature_ok"] is True


# ── Error step receipts ──────────────────────────────────────────────────────


def test_error_step_gets_receipt(signed_tracer):
    with pytest.raises(ValueError):
        with signed_tracer.step("bad") as step:
            raise ValueError("boom")

    assert len(signed_tracer.receipts) == 1
    r = signed_tracer.receipts[0]
    assert r.outcome == "error"
    assert r.is_signed

    result = ReceiptBuilder.verify_receipt(r)
    assert result["valid"] is True


# ── No signing ───────────────────────────────────────────────────────────────


def test_tracer_without_signing(tmp_path):
    tracer = Tracer("plain", trace_dir=tmp_path, auto_save=False)
    with tracer.step("x"):
        pass

    assert tracer.receipts == []
    assert tracer.public_key == ""

    result = tracer.verify_receipts()
    assert result["valid"] is True
    assert result["receipts_checked"] == 0


def test_save_receipts_without_signing_raises(tmp_path):
    tracer = Tracer("plain", trace_dir=tmp_path, auto_save=False)
    with pytest.raises(ValueError, match="Signing not enabled"):
        tracer.save_receipts()
