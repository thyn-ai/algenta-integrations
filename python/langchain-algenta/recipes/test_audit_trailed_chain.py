"""Tests for `recipes/audit_trailed_chain.py` -- real stub-server round trips, zero credentials."""

from __future__ import annotations

from recipes.audit_trailed_chain import run_audited_chain, run_fingerprint


async def test_every_chain_run_leaves_a_decision_memory_record(stub_server: str) -> None:
    result = await run_audited_chain(stub_server, "ship the new pricing page")

    assert result["output"], "expected a non-empty chain output"
    assert result["decision_id"].startswith("decision-chain-run:")
    assert result["fingerprint"] in result["decision_id"]
    assert result["expected_value"] == 42.0  # the stub engine's simulate fixture


async def test_the_fingerprint_is_deterministic_and_input_sensitive(stub_server: str) -> None:
    first = await run_audited_chain(stub_server, "ship the new pricing page")
    repeat = await run_audited_chain(stub_server, "ship the new pricing page")
    other = await run_audited_chain(stub_server, "deprecate the v1 webhook")

    assert first["fingerprint"] == repeat["fingerprint"]
    assert first["decision_id"] == repeat["decision_id"]  # re-runs are detectable, not silent
    assert other["fingerprint"] != first["fingerprint"]


def test_run_fingerprint_matches_sha256_of_input_and_output() -> None:
    import hashlib

    expected = hashlib.sha256(b"q\no").hexdigest()[:16]
    assert run_fingerprint("q", "o") == expected
