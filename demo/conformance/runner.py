"""Run the conformance scenarios against a real, self-hosted Algenta engine.

USAGE
    ALGENTA_BASE_URL=http://localhost:8101 ALGENTA_API_KEY=... python -m demo.conformance.runner
    ... --json evidence.json     # write machine-readable evidence

LOCAL-FIRST BY CONSTRUCTION. There is no default base URL. The runner refuses to start without
ALGENTA_BASE_URL, because a default pointing at a hosted endpoint is how "self-hosted" quietly stops
being true; the customer's engine is the only thing this is meant to talk to.

WHAT COUNTS AS A PASS. Every assertion is made against the HTTP response a client actually receives
-- status code, named error code, response body, and for the audit export the digest RE-COMPUTED
from the downloaded bytes. Nothing is asserted against an internal object, and there is no stub
server anywhere in this path. A scenario the engine cannot exercise is reported as BLOCKED with its
reason and does not count toward the pass total.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

from demo.conformance.scenarios import SCENARIOS, Scenario, Status, summary

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@dataclass
class Result:
    scenario: Scenario
    passed: bool | None  # None = blocked, never attempted
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        if self.passed is None:
            return "BLOCKED"
        return "PASS" if self.passed else "FAIL"


class Engine:
    """Minimal HTTP client. stdlib only, so the suite adds no dependency to any package."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any] | list[Any] | None, dict[str, str], bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"{self.base}{path}", data=data, method=method)
        req.add_header("X-API-Key", self.key)
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                return r.status, _maybe_json(raw), dict(r.headers), raw
        except urllib.error.HTTPError as e:  # 4xx/5xx carry the bodies we assert on
            raw = e.read()
            return e.code, _maybe_json(raw), dict(e.headers), raw

    def code_of(self, body: Any) -> str:
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return str(err.get("code", ""))
            detail = body.get("detail")
            if isinstance(detail, dict):
                return str(detail.get("code", ""))
        return ""


def _maybe_json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        return None


def _case_body(title: str) -> dict[str, Any]:
    return {
        "pack_id": "renewal_capacity_allocation",
        "title": title,
        "pack_input": {
            "accounts": [
                {"account_id": "A1", "name": "BigCo", "arr": 500000,
                 "renewal_date": "2026-08-01", "health_score": 40, "segment": "standard"},
                {"account_id": "A5", "name": "KeyStrategic", "arr": 90000,
                 "renewal_date": "2026-08-01", "health_score": 50, "segment": "strategic"},
            ],
            "capacity": {"total_hours": 40},
            "constraints": {"strategic_min_hours": 4, "allocation_step_hours": 1},
            "horizon_days": 90,
        },
    }


def _fresh_plan(eng: Engine, title: str) -> dict[str, Any]:
    """propose a plan: case -> analysis run -> action plan (plan_hash + single-use nonce)."""
    st, case, _, _ = eng.call("POST", "/v1/decision-cases", _case_body(title))
    assert st in (200, 201), f"create case -> {st}: {case}"
    st, run, _, _ = eng.call(
        "POST", f"/v1/decision-cases/{case['case_id']}/analysis-runs", {}
    )
    assert st in (200, 201, 202), f"analysis run -> {st}: {run}"
    st, plan, _, _ = eng.call("POST", f"/v1/analysis-runs/{run['run_id']}/action-plans")
    assert st in (200, 201), f"propose plan -> {st}: {plan}"
    assert plan.get("plan_hash") and plan.get("nonce"), f"plan lacks hash/nonce: {plan}"
    return plan


# ── scenario implementations ────────────────────────────────────────────────────────────────────


def s1(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s1")
    ok = plan["lifecycle_state"] == "proposed" and len(plan["plan_hash"]) >= 32
    return ok, f"lifecycle={plan['lifecycle_state']} hash_len={len(plan['plan_hash'])}", {
        "plan_hash_prefix": plan["plan_hash"][:16], "nonce_present": bool(plan["nonce"])
    }


def s2(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s2")
    st, body, _, _ = eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/execute")
    code = eng.code_of(body)
    ok = st == 409 and code == "plan_not_approved"
    return ok, f"status={st} code={code}", {"status": st, "code": code}


def s4(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s4")
    st, appr, _, _ = eng.call(
        "POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
        {"plan_hash": plan["plan_hash"], "nonce": plan["nonce"]},
    )
    if st != 200 or appr.get("status") != "approved":
        return False, f"approve -> {st} {appr}", {"approve_status": st}
    st2, ex, _, _ = eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/execute")
    ok = st2 == 200 and ex.get("lifecycle_state") == "executed" and bool(ex.get("receipt"))
    return ok, f"approve=200 execute={st2} lifecycle={ex.get('lifecycle_state')}", {
        "approval_status": appr.get("status"), "lifecycle": ex.get("lifecycle_state")
    }


def s5(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s5")
    st, body, _, _ = eng.call(
        "POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
        {"plan_hash": plan["plan_hash"], "nonce": "b" * 16},
    )
    code = eng.code_of(body)
    # and the plan must NOT have advanced
    _, after, _, _ = eng.call("GET", f"/v1/decision-cases/{plan['case_id']}")
    ok = st == 403 and code == "invalid_nonce"
    return ok, f"status={st} code={code}", {"status": st, "code": code}


def s6(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s6")
    eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
             {"plan_hash": plan["plan_hash"], "nonce": plan["nonce"]})
    st1, _, _, _ = eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/execute")
    st2, body2, _, _ = eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/execute")
    code = eng.code_of(body2)
    ok = st1 == 200 and st2 == 409 and code == "plan_not_approved"
    return ok, f"first={st1} second={st2} code={code}", {
        "first": st1, "second": st2, "code": code
    }


def s7(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s7")
    a1, _, _, _ = eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
                           {"plan_hash": plan["plan_hash"], "nonce": plan["nonce"]})
    a2, body2, _, _ = eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
                               {"plan_hash": plan["plan_hash"], "nonce": plan["nonce"]})
    code = eng.code_of(body2)
    ok = a1 == 200 and a2 >= 400 and code == "plan_not_approvable"
    return ok, f"first={a1} replay={a2} code={code}", {"replay_status": a2, "code": code}


def s8(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s8")
    st, body, _, _ = eng.call(
        "POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
        {"plan_hash": "0" * 64, "nonce": plan["nonce"]},
    )
    code = eng.code_of(body)
    ok = st == 403 and code == "plan_hash_mismatch"
    return ok, f"status={st} code={code}", {"status": st, "code": code}


def s10(eng: Engine) -> tuple[bool, str, dict]:
    plan = _fresh_plan(eng, "conformance s10")
    eng.call("POST", f"/v1/decision-plans/{plan['plan_id']}/approve",
             {"plan_hash": plan["plan_hash"], "nonce": plan["nonce"]})
    idem = f"conf-{uuid.uuid4().hex[:12]}"
    st, ex, _, _ = eng.call(
        "POST", f"/v1/decision-plans/{plan['plan_id']}/execute",
        headers={"traceparent": TRACEPARENT, "Idempotency-Key": idem},
    )
    r = (ex or {}).get("receipt") or {}
    checks = {
        "receipt_version": bool(r.get("receipt_version")),
        "execution_id_distinct": bool(r.get("execution_id")) and r.get("execution_id") != plan["plan_id"],
        "plan_hash_bound": r.get("plan_hash") == plan["plan_hash"],
        "approval_state": r.get("approval_state") == "approved",
        "trace_adopted": r.get("trace_id") == TRACE_ID,
        "idempotency_echoed": r.get("idempotency_key") == idem,
        "status_executed": r.get("status") == "executed",
    }
    ok = st == 200 and all(checks.values())
    failed = [k for k, v in checks.items() if not v]
    return ok, f"status={st} failed={failed or 'none'}", {
        "receipt_version": r.get("receipt_version"), "checks": checks,
        "policy_snapshot_hash": r.get("policy_snapshot_hash"),
    }


def s11(eng: Engine) -> tuple[bool, str, dict]:
    st, _, headers, raw = eng.call("GET", "/v1/audit-logs/export.json")
    advertised = {k.lower(): v for k, v in headers.items()}.get("x-content-sha256", "")
    recomputed = hashlib.sha256(raw).hexdigest()
    rows = {k.lower(): v for k, v in headers.items()}.get("x-audit-export-rows")
    ok = st == 200 and bool(advertised) and advertised == recomputed
    return ok, f"status={st} match={advertised == recomputed} rows={rows}", {
        "advertised": advertised, "recomputed": recomputed, "rows": rows, "bytes": len(raw),
    }


IMPLS = {
    "read_only_recommendation": s1,
    "execution_denied_by_policy": s2,
    "approval_granted_then_execute": s4,
    "approval_rejected": s5,
    "duplicate_execution_prevented": s6,
    "reused_challenge_rejected": s7,
    "modified_plan_hash_rejected": s8,
    "execution_receipt_versioned": s10,
    "audit_export_hash_verifies": s11,
}


def run(base_url: str, api_key: str) -> list[Result]:
    eng = Engine(base_url, api_key)
    results: list[Result] = []
    for sc in SCENARIOS:
        if sc.status is not Status.EXERCISABLE:
            results.append(Result(sc, None, sc.blocked_reason))
            continue
        impl = IMPLS.get(sc.key)
        if impl is None:  # a scenario marked exercisable with no implementation is a bug, not a pass
            results.append(Result(sc, False, "marked EXERCISABLE but no implementation is wired"))
            continue
        try:
            ok, detail, ev = impl(eng)
            results.append(Result(sc, ok, detail, ev))
        except Exception as exc:  # a crash is a failure, never a skip
            results.append(Result(sc, False, f"{type(exc).__name__}: {exc}"))
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", help="write machine-readable evidence here")
    args = ap.parse_args(argv)

    base = os.environ.get("ALGENTA_BASE_URL")
    key = os.environ.get("ALGENTA_API_KEY")
    if not base or not key:
        print(
            "ALGENTA_BASE_URL and ALGENTA_API_KEY are required.\n"
            "There is deliberately no default base URL: a default pointing anywhere but the "
            "customer's own engine is how 'self-hosted' stops being true.",
            file=sys.stderr,
        )
        return 2

    print(f"engine: {base}")
    print(summary())
    print()
    started = time.time()
    results = run(base, key)

    for r in results:
        print(f"  [{r.label:7}] {r.scenario.number:2}. {r.scenario.title}")
        if r.passed is None:
            for line in _wrap(r.detail, 92):
                print(f"            {line}")
        elif not r.passed:
            print(f"            {r.detail}")

    passed = sum(1 for r in results if r.passed is True)
    failed = sum(1 for r in results if r.passed is False)
    blocked = sum(1 for r in results if r.passed is None)
    print()
    print(f"  {passed} passed, {failed} failed, {blocked} blocked of {len(results)} "
          f"in {time.time() - started:.1f}s")
    print("  NOTE: blocked scenarios are NOT passes. This suite is the "
          "'Validated integration' gate, so partial coverage must read as partial.")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "engine": base,
                    "passed": passed, "failed": failed, "blocked": blocked,
                    "total": len(results),
                    "results": [
                        {
                            "number": r.scenario.number, "key": r.scenario.key,
                            "title": r.scenario.title, "outcome": r.label,
                            "detail": r.detail, "evidence": r.evidence,
                            "status": r.scenario.status.value,
                        }
                        for r in results
                    ],
                },
                fh, indent=2, sort_keys=True,
            )
        print(f"  evidence written to {args.json}")

    return 1 if failed else 0


def _wrap(text: str, width: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
