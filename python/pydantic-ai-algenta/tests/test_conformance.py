"""Conformance suite adapter tests: run the 12 demo scenarios through ``pydantic_ai_algenta.conformance``.

Uses ``tests.stub_conformance_server`` -- a real Starlette ASGI app over a real local HTTP socket --
as a deterministic stand-in for a self-hosted Algenta engine's decision-plan REST surface. No real
engine is reachable from this test environment.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from pydantic_ai_algenta.conformance import IMPLS, Engine, Status, _run_cli, _wrap, main, run

from .stub_conformance_server import StubConformanceServer


@pytest.fixture
async def stub_engine_url() -> AsyncIterator[str]:
    """Yield the base URL of a freshly started conformance stub server."""
    async with StubConformanceServer() as server:
        yield server.base_url


@pytest.mark.anyio
async def test_adapter_reports_nine_passed_three_blocked(stub_engine_url: str) -> None:
    results = await run(stub_engine_url, "test-api-key")

    passed = [r for r in results if r.passed is True]
    failed = [r for r in results if r.passed is False]
    blocked = [r for r in results if r.passed is None]

    assert len(results) == 12
    assert len(passed) == 9
    assert len(failed) == 0
    assert len(blocked) == 3

    blocked_numbers = {r.scenario.number for r in blocked}
    assert blocked_numbers == {3, 9, 12}


@pytest.mark.anyio
async def test_blocked_scenarios_carry_honest_reasons(stub_engine_url: str) -> None:
    results = await run(stub_engine_url, "test-api-key")
    blocked = {r.scenario.key: r for r in results if r.passed is None}

    assert "execution_paused_for_approval" in blocked
    assert "refuses rather than pauses" in blocked["execution_paused_for_approval"].detail.lower()

    assert "upstream_timeout_typed_retryable" in blocked
    assert "times out" in blocked["upstream_timeout_typed_retryable"].detail.lower()

    assert "replay_is_deterministic" in blocked
    assert "determinism" in blocked["replay_is_deterministic"].detail.lower()


@pytest.mark.anyio
async def test_exercisable_scenarios_are_all_wired(stub_engine_url: str) -> None:
    results = await run(stub_engine_url, "test-api-key")
    exercisable = [r for r in results if r.scenario.status is Status.EXERCISABLE]

    for r in exercisable:
        assert r.scenario.key in IMPLS, f"{r.scenario.key} is exercisable but not wired"
        assert r.passed is True, f"{r.scenario.key} failed: {r.detail}"


@pytest.mark.anyio
async def test_s1_read_only_recommendation_produces_proposed_plan(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["read_only_recommendation"]
    assert r.passed is True
    assert r.evidence["nonce_present"] is True
    assert len(r.evidence["plan_hash_prefix"]) == 16


@pytest.mark.anyio
async def test_s2_execution_denied_by_policy_gate(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["execution_denied_by_policy"]
    assert r.passed is True
    assert r.evidence["status"] == 409
    assert r.evidence["code"] == "plan_not_approved"


@pytest.mark.anyio
async def test_s4_approval_granted_then_execute_returns_receipt(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["approval_granted_then_execute"]
    assert r.passed is True
    assert r.evidence["approval_status"] == "approved"
    assert r.evidence["lifecycle"] == "executed"


@pytest.mark.anyio
async def test_s5_approval_rejected_for_wrong_nonce(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["approval_rejected"]
    assert r.passed is True
    assert r.evidence["status"] == 403
    assert r.evidence["code"] == "invalid_nonce"


@pytest.mark.anyio
async def test_s6_duplicate_execution_prevented(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["duplicate_execution_prevented"]
    assert r.passed is True
    assert r.evidence["first"] == 200
    assert r.evidence["second"] == 409
    assert r.evidence["code"] == "plan_not_approved"


@pytest.mark.anyio
async def test_s7_reused_approval_nonce_rejected(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["reused_challenge_rejected"]
    assert r.passed is True
    assert r.evidence["code"] == "plan_not_approvable"
    assert r.evidence["replay_status"] >= 400


@pytest.mark.anyio
async def test_s8_modified_plan_hash_rejected(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["modified_plan_hash_rejected"]
    assert r.passed is True
    assert r.evidence["status"] == 403
    assert r.evidence["code"] == "plan_hash_mismatch"


@pytest.mark.anyio
async def test_s10_receipt_carries_versioned_envelope(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["execution_receipt_versioned"]
    assert r.passed is True
    checks = r.evidence["checks"]
    assert all(checks.values())
    assert r.evidence["receipt_version"] == "1.0.0"


@pytest.mark.anyio
async def test_s11_audit_export_hash_verifies(stub_engine_url: str) -> None:
    results = {r.scenario.key: r for r in await run(stub_engine_url, "test-api-key")}
    r = results["audit_export_hash_verifies"]
    assert r.passed is True
    assert r.evidence["advertised"] == r.evidence["recomputed"]
    assert r.evidence["bytes"] > 0


def test_wrap_splits_long_text() -> None:
    words = ["a", *["b"] * 60]
    lines = _wrap(" ".join(words), 20)
    assert all(len(line) <= 20 for line in lines)
    assert " ".join(lines).split() == words


def test_main_without_credentials_returns_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALGENTA_BASE_URL", raising=False)
    monkeypatch.delenv("ALGENTA_API_KEY", raising=False)
    assert main([]) == 2


@pytest.mark.anyio
async def test_engine_code_of_extracts_detail_code(stub_engine_url: str) -> None:
    eng = Engine(stub_engine_url, "test-api-key")
    assert eng.code_of({"detail": {"code": "detail_code"}}) == "detail_code"
    assert eng.code_of({"error": {"code": "error_code"}}) == "error_code"
    assert eng.code_of("not a dict") == ""
    await eng.aclose()


@pytest.mark.anyio
async def test_run_records_exception_for_unwired_exercisable_scenario(
    stub_engine_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from demo.conformance.scenarios import BY_KEY

    scenario = BY_KEY["read_only_recommendation"]
    monkeypatch.setitem(
        IMPLS, scenario.key, lambda _eng: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    results = [
        r for r in await run(stub_engine_url, "test-api-key") if r.scenario.key == scenario.key
    ]
    assert len(results) == 1
    assert results[0].passed is False
    assert "boom" in results[0].detail


def test_cli_requires_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALGENTA_BASE_URL", raising=False)
    monkeypatch.delenv("ALGENTA_API_KEY", raising=False)
    assert main([]) == 2


@pytest.mark.anyio
async def test_cli_runs_all_scenarios_and_writes_json(stub_engine_url: str, tmp_path: Path) -> None:
    evidence_path = tmp_path / "evidence.json"

    exit_code = await _run_cli(
        base_url=stub_engine_url, api_key="test-api-key", json_path=str(evidence_path)
    )

    assert exit_code == 0
    assert evidence_path.exists()
    data = json.loads(evidence_path.read_text())
    assert data["passed"] == 9
    assert data["failed"] == 0
    assert data["blocked"] == 3
    assert data["total"] == 12
    assert data["adapter"] == "pydantic-ai-algenta"
    assert len(data["results"]) == 12


def test_cli_engine_url_has_no_default_and_exits_two_when_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALGENTA_BASE_URL", "http://localhost:8000")
    monkeypatch.delenv("ALGENTA_API_KEY", raising=False)
    assert main([]) == 2
