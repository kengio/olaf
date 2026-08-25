"""The `keep_unmanaged` and `rebuild` Base parameters driven through the REAL dispatch.

Fabric Base parameters default to the String type, so a pipeline that sets keep_unmanaged to
"false" — meaning *do not keep unmanaged roles* — hands run_mode the STRING "false", and
`bool("false")` is True: the requested config payload would silently become an incremental payload.
Asserting the parsed value alone would not catch that, so every case here drives a real apply against
a prior-live role absent from config and asserts the request labels OLAF records.

Ported from `olaf_test_integration.ipynb` classes `RuntimeKeepUnmanagedParameterTyping` and
`RuntimeRebuildParameterTyping` (scope "mock_integration").
"""

import pytest

from _fakes import (
    build_spark,
    prepare_runtime_with_undeclared_role,
    run_runtime_blackbox,
)

UNDECLARED = "LegacyManual"  # prior-live role absent from the config-derived payload


def apply_with(keep_unmanaged):
    """One apply through the runtime with this `keep_unmanaged`. Returns (outcome, live names)."""
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox(
        "apply", spark, client=client, params={"keep_unmanaged": keep_unmanaged}
    )
    return outcome, {r["name"] for r in client.list_roles()}


@pytest.mark.parametrize("value", ["false", "False", "0", "no", "", False, 0])
def test_falsy_spellings_submit_config_payload_with_an_omission_candidate(value):
    outcome, _ = apply_with(value)
    res = outcome.envelope
    assert res["status"] == "success"
    assert res["params"]["keep_unmanaged"] is False  # parsed, not coerced
    assert res["data"]["keep_unmanaged"] is False  # ...and reached apply
    assert res["data"]["request"] == "config_payload"
    assert res["data"]["omitted_role_candidates"] == [UNDECLARED]
    assert res["data"]["drift_omission_candidates"] == [UNDECLARED]
    assert res["data"]["post_state_review_required"] is True


@pytest.mark.parametrize("value", ["true", "True", "1", "yes", True, 1])
def test_truthy_spellings_keep_the_undeclared_role(value):
    outcome, _ = apply_with(value)
    res = outcome.envelope
    assert res["status"] == "success"
    assert res["params"]["keep_unmanaged"] is True
    assert res["data"]["keep_unmanaged"] is True
    assert res["data"]["request"] == "incremental_payload"
    assert res["data"]["omitted_role_candidates"] == []
    assert res["data"]["drift_omission_candidates"] == [UNDECLARED]
    assert res["data"]["post_state_review_required"] is True


def test_unrecognised_spelling_is_blocked_before_request_submission():
    # a typo must fail LOUDLY — the whole point of rejecting instead of falling back to bool()
    outcome, live = apply_with("ture")
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "keep_unmanaged" in res["error"]
    assert "'ture'" in res["error"]  # the offending value is named
    assert UNDECLARED in live  # no request was submitted
    assert outcome.raised is not None  # native-failure: the activity FAILS
    assert outcome.exit_value is None  # blocked never reaches notebook.exit


@pytest.mark.parametrize("value", [2, -1])
def test_ambiguous_int_is_blocked_before_request_submission(value):
    # int 0/1 are accepted (above); any other int is a guess, and the guess that matters here
    # is the destructive one — refuse it exactly like a misspelt string.
    outcome, live = apply_with(value)
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "keep_unmanaged" in res["error"]
    assert f"got {value!r}" in res["error"]
    assert UNDECLARED in live  # no request was submitted
    assert outcome.raised is not None


def test_a_bad_keep_unmanaged_does_not_block_setup():
    """The refusal is scoped to the modes PARAMS_BY_MODE echoes `keep_unmanaged` for — apply, the
    only one that consumes it. Every other mode ignores `keep_unmanaged` entirely, so a garbage
    value must NOT turn a read-only/setup run into a blocked envelope: that would be a new failure
    for a pipeline that hands the SAME Base-parameter set to every mode, on operations where the
    value is unused. It is still refused the moment apply runs (above)."""
    outcome = run_runtime_blackbox("setup", build_spark(), params={"keep_unmanaged": "ture"})
    assert outcome.envelope["status"] == "success"
    assert "keep_unmanaged" not in outcome.envelope["params"]  # setup never echoes it
    assert outcome.raised is None


def test_a_bad_keep_unmanaged_does_not_block_show():
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox(
        "show",
        spark,
        client=client,
        params={"keep_unmanaged": 2, "by": "role", "subject": "SalesReaders"},
    )
    assert outcome.envelope["status"] == "success"
    assert "keep_unmanaged" not in outcome.envelope["params"]
    assert outcome.raised is None


# ---------------------------------------------------------------------------------------------
# `rebuild`'s half of the split. It is consumed by GENERATE ONLY, so the mode-scoped guard refuses
# an unparseable value there and deliberately ignores it on apply — an asymmetry PR-5 introduced:
# at base there was one `force` flag that apply read too, so every garbage boolean blocked apply.
# Both halves are pinned here, because the permissive half now lets a typo'd rebuild ride along
# into a run that submits a config-derived role payload.
# ---------------------------------------------------------------------------------------------


def test_unrecognised_rebuild_blocks_generate():
    # generate DOES read rebuild -> a typo must fail LOUDLY rather than fall back to bool()
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox("generate", spark, client=client, params={"rebuild": "ture"})
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "rebuild" in res["error"]  # the offending parameter is named
    assert "'ture'" in res["error"]  # ...and so is the offending value
    assert outcome.raised is not None  # native-failure: the activity FAILS
    assert outcome.exit_value is None  # blocked never reaches notebook.exit


def test_unrecognised_rebuild_blocks_setup_too():
    """setup consumes `rebuild` now, and the guard is driven by PARAMS_BY_MODE's echo rather than a
    second hand-kept list — so adding the parameter to setup's echo armed the refusal for setup
    automatically. It matters more here than anywhere: a typo'd rebuild parsed as False would
    silently NOT repair the schema while the operator believes it did."""
    outcome = run_runtime_blackbox("setup", build_spark(), params={"rebuild": "ture"})
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "rebuild" in res["error"]
    assert "'ture'" in res["error"]
    assert outcome.raised is not None


def test_unrecognised_rebuild_does_not_block_apply_or_change_the_payload_kind():
    """apply does NOT consume rebuild, so a garbage value must ride along harmlessly — the
    deliberate PR-5 decision: the run still succeeds with its config-derived request payload.
    Locked in so a later "block every bad boolean everywhere" change has to be a conscious one."""
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox("apply", spark, client=client, params={"rebuild": "ture"})
    res = outcome.envelope
    assert res["status"] == "success"
    assert "rebuild" not in res["params"]  # apply never echoes it -> never reads it
    assert res["data"]["request"] == "config_payload"
    assert res["data"]["omitted_role_candidates"] == [UNDECLARED]
    assert outcome.raised is None


# ---------------------------------------------------------------------------------------------
# `if_match` — the conditional-PUT escape hatch (PR #19 review round). Same Base-parameter
# typing rules as its siblings, plus the audit-visibility contract: the EFFECTIVE conditional
# state is recorded in the envelope and on the apply complete row, so an unconditional write
# (disabled, or silently degraded by an ETag-less service response) is always observable.
# ---------------------------------------------------------------------------------------------


def test_if_match_false_sends_the_put_unconditionally_and_records_it():
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox("apply", spark, client=client, params={"if_match": "false"})
    res = outcome.envelope
    assert res["status"] == "success"
    assert res["params"]["if_match"] is False  # parsed, not coerced
    assert res["data"]["if_match"] == "unconditional (if_match=false)"
    real = [c for c in client.put_calls if not c["dry_run"]]
    assert real and all(c["etag"] is None for c in real)  # nothing conditional went out
    from _fakes import LOG_TABLE

    complete = [
        r
        for r in spark._store[LOG_TABLE]
        if r.get("mode") == "apply" and r.get("action") == "complete"
    ][-1]
    assert "unconditional (if_match=false)" in complete["message"]  # audit-visible


def test_if_match_default_stays_conditional():
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox("apply", spark, client=client)
    res = outcome.envelope
    assert res["status"] == "success"
    assert res["params"]["if_match"] is True
    assert res["data"]["if_match"] == "conditional"
    real = [c for c in client.put_calls if not c["dry_run"]]
    assert real and all(c["etag"] for c in real)


def test_an_etag_less_service_blocks_every_sensitive_stage_without_writes():
    # Missing collection-version evidence is not a concurrency opt-out. The pipeline stops
    # before generate can export/write and before any dry-run or real PUT.
    from _fakes import FakeFabricClient

    class _NoEtag(FakeFabricClient):
        def list_roles(self, timeout=None):
            out = super().list_roles(timeout)
            self.roles_etag = None
            return out

    spark = build_spark()
    client = _NoEtag([])
    from _fakes import (
        CONFIG_TABLE,
        LOG_TABLE,
        MAPPING_TABLE,
        sample_config_rows,
        seed_sample_members,
    )

    run_runtime_blackbox("setup", spark)
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    log_before = [dict(row) for row in spark._store[LOG_TABLE]]
    outcomes = [
        run_runtime_blackbox(mode, spark, client=client) for mode in ("generate", "plan", "apply")
    ]
    assert all(outcome.envelope["status"] == "blocked" for outcome in outcomes)
    assert all("ETag" in outcome.envelope["error"] for outcome in outcomes)
    assert client.put_calls == []
    assert spark._store[MAPPING_TABLE] == []
    assert spark._store[LOG_TABLE] == log_before


def test_an_unrecognised_if_match_is_blocked_on_apply_and_ignored_on_setup():
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    outcome = run_runtime_blackbox("apply", spark, client=client, params={"if_match": "ture"})
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "if_match" in res["error"] and "'ture'" in res["error"]
    assert UNDECLARED in {r["name"] for r in client.list_roles()}  # nothing applied or deleted
    # setup never consumes it, so the same garbage value must not block a setup run
    o2 = run_runtime_blackbox(
        "setup", build_spark(), params={"if_match": "ture", "lakehouse_name": "LH_Demo"}
    )
    assert o2.envelope["status"] == "success"
    assert "if_match" not in o2.envelope["params"]
