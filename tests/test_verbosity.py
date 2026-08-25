"""The `verbosity` parameter -- WHICH lines appear at each level.

The rest of the suite pins `verbose` (see the autouse fixture in conftest.py) because it asserts
what a line SAYS. This module is the other half: it overrides that pin and asserts what is
PRINTED AT ALL, level by level, including the real default.
"""

from unittest import mock

import pytest

from _olaf_runtime import PARAM_DEFAULTS, VERBOSITY_LEVELS, OLAF, Say, run_mode
from _fakes import (
    CONTROL_ATTESTATION,
    build_spark,
    ols_env,
    ols_seed,
    prepare_runtime_with_undeclared_role,
    run_runtime_blackbox,
)

# markers of each tier, picked so a tier can be detected independently of the others
VERDICT = "✅ success"  # quiet
DATA_KEY = "   · created:"  # info
PROGRESS = "🆕 created"  # detail
JSON_ECHO = "OLAF.last_result:"  # verbose

# The two request-construction previews name prior-live omission candidates and require post-state
# review. Both print at `info` -- the default level -- which is what the tests at the bottom pin.
PLAN_OMISSION = "⚠️  apply payload will omit"
APPLY_OMISSION = "⚠️  submitting payload with"
UNDECLARED = "LegacyManual"  # a prior-live role absent from the config-derived payload


def setup_at(level, spark=None):
    """A real `setup` run through the ▶️ Run dispatch path at `level`."""
    return run_runtime_blackbox(
        "setup", spark or build_spark(), params={"lakehouse_name": "LH_Demo", "verbosity": level}
    )


@pytest.mark.parametrize(
    "level,verdict,data,progress,echo",
    [
        ("silent", False, False, False, False),
        ("quiet", True, False, False, False),
        ("info", True, True, False, False),
        ("detail", True, True, True, False),
        ("verbose", True, True, True, True),
    ],
)
def test_each_level_prints_exactly_its_tier_and_everything_below(
    level, verdict, data, progress, echo, capsys
):
    """The ladder is CUMULATIVE, so this table is the contract: each level adds one tier and keeps
    every tier under it. Asserting the full row (not just the tier a level introduces) is what
    catches a gate that silences too much as well as one that silences too little."""
    setup_at(level)
    out = capsys.readouterr().out
    assert (VERDICT in out) is verdict, out
    assert (DATA_KEY in out) is data, out
    assert (PROGRESS in out) is progress, out
    assert (JSON_ECHO in out) is echo, out


def test_the_default_is_info_and_run_mode_falls_back_to_it(capsys):
    """A caller that names no level gets `info`. Asserted through run_mode directly, because the
    black-box driver seeds a level of its own — and with the suite's pin lifted first, or the
    fixture would be answering instead of the fallback.

    (The envelope does not echo `verbosity`: `params` carries what the MODE consumes, and this
    changes what a run prints, not what it does. `OLAF.show_params()` is where you read it back.)"""
    assert PARAM_DEFAULTS["verbosity"] == "info"
    Say.override = None
    OLAF._base_params.pop("verbosity", None)
    with ols_env(build_spark()):
        run_mode(
            "setup",
            {
                "lakehouse_name": "LH_Demo",
                "control_data_isolation_attestation": CONTROL_ATTESTATION,
            },
            build_spark(),
        )
        out = capsys.readouterr().out
    assert VERDICT in out and DATA_KEY in out  # info says what happened...
    assert PROGRESS not in out and JSON_ECHO not in out  # ...without the bulk


def test_silent_still_prints_a_blocked_verdict(capsys):
    """The one silence nobody asked for. `silent` drops every success line, and still says so when
    a run is refused -- otherwise a blocked apply in a scheduled job prints nothing at all."""
    outcome = setup_at("silent")  # no lakehouse_name mismatch here...
    capsys.readouterr()
    outcome = run_runtime_blackbox(
        "setup",
        build_spark(),
        params={"lakehouse_name": "NotTheAttachedOne", "verbosity": "silent"},
    )
    out = capsys.readouterr().out
    assert outcome.envelope["status"] == "blocked"
    assert "🚫" in out or "blocked" in out.lower(), out
    assert "NotTheAttachedOne" in out  # the reason, not just the badge


def test_an_unrecognised_level_is_refused_not_coerced():
    """Same rule as the boolean parameters: a typo is named and refused. Coerced to `silent` it
    would hide the very run the operator was watching, which is the worst possible default.

    The refusal surfaces as the same structured 'blocked' envelope every sibling parameter
    guard produces (the guard moved inside the envelope boundary); the run still FAILS
    natively, the exit payload is the compact JSON envelope rather than a bare string, and
    the verbosity pin falls back to the default level so the blocked verdict still prints."""
    import json

    outcome = run_runtime_blackbox(
        "setup", build_spark(), params={"lakehouse_name": "LH_Demo", "verbosity": "verbos"}
    )
    res = outcome.envelope
    assert res["status"] == "blocked"
    assert "verbos" in res["error"] and "verbosity" in res["error"]
    for level in VERBOSITY_LEVELS:
        assert level in res["error"]  # the message lists what IS accepted
    assert outcome.raised is not None  # native-failure: the activity FAILS
    lean = json.loads(outcome.raised)  # ...and the exit payload is parseable JSON
    assert lean["status"] == "blocked"
    assert Say.override is None  # the run's pin was released even on the refusal path


def test_the_run_pin_is_released_so_the_next_interactive_call_is_not_stuck(capsys):
    """`Say.override` is module state in a single-namespace notebook: a run that left it set would
    make the next interactive call inherit that run's level instead of the configured one."""
    Say.override = None
    run_runtime_blackbox(
        "setup", build_spark(), params={"lakehouse_name": "LH_Demo", "verbosity": "silent"}
    )
    assert Say.override is None
    capsys.readouterr()
    spark, _ = build_spark(), None
    with ols_env(spark):
        OLAF._base_params["verbosity"] = "quiet"
        ols_seed(spark, upto="generate")
        capsys.readouterr()
        OLAF.status()
    assert capsys.readouterr().out.strip(), "the interactive call was still silenced by the run"


# ---------------------------------------------------------------------------------------------
# The omission previews — the visible request warning for prior-live role candidates.
# ---------------------------------------------------------------------------------------------


def test_the_omission_previews_are_visible_at_the_default_level(capsys):
    """Both request previews name the candidates before submission and require post-state review.

    They remain at `info`, so an operator who configured no verbosity still sees the warning.
    """
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    capsys.readouterr()  # drop the setup/generate/plan chatter the fixture already printed
    run_runtime_blackbox("plan", spark, client=client, params={"verbosity": "info"})
    out = capsys.readouterr().out
    assert PLAN_OMISSION in out and UNDECLARED in out and "review post-state" in out, out

    run_runtime_blackbox("apply", spark, client=client, params={"verbosity": "info"})
    out = capsys.readouterr().out
    assert APPLY_OMISSION in out and UNDECLARED in out and "review post-state" in out, out


def test_the_omission_previews_stay_inside_the_ladder_at_quiet(capsys):
    """`info`, not `silent`/`quiet`: the ladder is still the contract. An operator who asked for
    the verdict alone gets the verdict alone — the previews are one tier up, where the default is."""
    spark, client = prepare_runtime_with_undeclared_role(UNDECLARED)
    capsys.readouterr()
    run_runtime_blackbox("plan", spark, client=client, params={"verbosity": "quiet"})
    out = capsys.readouterr().out
    assert PLAN_OMISSION not in out, out
    assert VERDICT in out  # ...and quiet still says what happened


def test_a_baseexception_still_releases_the_run_pin():
    """The pin is released in a `finally`, not after the except handlers. Those two handlers catch
    SystemExit and Exception — a KeyboardInterrupt is neither, so the interrupted run used to
    leave `Say.override` set, and every later interactive call in that notebook namespace
    inherited the dead run's level. In a notebook the two happen minutes apart in the same
    process, which is exactly where the symptom is unattributable to its cause."""
    import _olaf_runtime as rt

    def _interrupted(self, rebuild=False):
        raise KeyboardInterrupt

    Say.override = None
    with ols_env(build_spark()), mock.patch.object(rt.Deployment, "setup", _interrupted):
        with pytest.raises(KeyboardInterrupt):
            run_mode(
                "setup",
                {
                    "lakehouse_name": "LH_Demo",
                    "verbosity": "silent",
                    "control_data_isolation_attestation": CONTROL_ATTESTATION,
                },
                build_spark(),
            )
    assert Say.override is None


def test_the_result_is_still_printed_at_the_runs_own_level(capsys):
    """...and the release did not move ahead of the printing. `_print_result` runs INSIDE the same
    finally, before the pin comes off, because saying what happened is part of the run and has to
    obey the level the run asked for. Released first, a `silent` run would print its whole verdict
    block at the default level instead."""
    run_runtime_blackbox(
        "setup", build_spark(), params={"lakehouse_name": "LH_Demo", "verbosity": "silent"}
    )
    assert capsys.readouterr().out == ""  # silent stayed silent all the way through the release
