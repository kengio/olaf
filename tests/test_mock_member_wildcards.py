"""Member wildcards wired into the real validation pipeline.

`_run_validation` now loads the member cache BEFORE `Generate.rows` and hands it expanded rows.
`Generate.rows`' signature is untouched, so none of its call sites move; what changes is that the
member columns it sees are already literal.

The first two tests here are the ones that matter. Everything else in the epic — the unit tests, the
suite, 100 % branch coverage, and the base-vs-HEAD differential over wildcard-free configs — is
structurally blind to the failure they cover, because it only manifests on a row carrying a wildcard.
"""

from unittest import mock

from _olaf_runtime import LIST_SEP, Catalog, Generate, Hash
from _fakes import (
    CONFIG_TABLE,
    GRP_READERS,
    GRP_READERS_NAME,
    MAPPING_TABLE,
    MEMBER_TABLE,
    SAMPLE_FOLDERS,
    SVC_LOADER,
    SVC_LOADER_NAME,
    FakeFabricClient,
    build_spark,
    make_dep,
    member_cache_row,
    run_generate,
    sample_config_rows,
    seed_sample_members,
)


def wildcard_rows(pattern="glob:sg-*"):
    """The sample config with both roles naming their Group by pattern instead of by name."""
    rows = sample_config_rows()
    return [{**r, "include_group_names": pattern} for r in rows]


def validation_of(dep):
    """`_run_validation` with the folder listing routed to the sample catalog, so a folder grant
    needs no live OneLake — the same patch `run_validate` applies, but returning the raw tuple
    instead of validate()'s envelope, because these tests assert on the aggregated error list."""

    def _lister(base):
        return SAMPLE_FOLDERS.get(base, [])

    with mock.patch.object(Catalog, "fs_folder_lister", lambda *_ids: _lister):
        return dep._run_validation()


def ready(rows, seed=True):
    spark = build_spark()
    client = FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = rows
    if seed:
        seed_sample_members(spark)
    return spark, client


# --------------------------------------------------------------------- the STALE-deadlock blocker


def test_config_hash_is_unchanged_by_a_validation_pass_on_a_wildcard_config():
    """`config_hash` is a property recomputed on every access from `short_rows`, which hands back
    the SAME mutable list. If expansion mutated those rows, the hash would move mid-pipeline —
    generate reads it for the idempotency skip and again when stamping the mapping, with validation
    in between. Measured on a mutating implementation: 391f70c0… -> ed2cbbba….
    """
    spark, client = ready(wildcard_rows())
    dep = make_dep(spark, client, "generate")

    before = dep.config_hash
    validation_of(dep)
    after = dep.config_hash

    assert before == after
    # and the rows themselves are untouched, so a later reader sees the authored config
    assert all(r["include_group_names"] == "glob:sg-*" for r in dep.short_rows)
    assert Hash.config(dep.short_rows) == before


def test_generate_then_plan_round_trips_for_a_wildcard_config():
    """The consequence of the above, end to end. A mutating expansion stamps hash(expanded) into the
    mapping while plan re-derives hash(raw) — so plan rejects `STALE: short config changed after
    generate`, re-running generate stamps the expanded hash again, and the config can NEVER be
    applied. Unbreakable loop, and invisible to any wildcard-free differential.
    """
    spark, client = ready(wildcard_rows())
    run_generate(make_dep(spark, client, "generate"))

    res = make_dep(spark, client, "plan").plan()

    assert "STALE" not in str(res)
    assert res["data"]["drift"] == {"SalesReaders": "create", "RawReaders": "create"}


# ------------------------------------------------------------------------------------ end to end


def test_a_wildcard_config_generates_and_the_mapping_holds_the_expanded_name():
    spark, client = ready(wildcard_rows())

    res = run_generate(make_dep(spark, client, "generate"))

    assert res["changed"]
    rows = spark._store[MAPPING_TABLE]
    assert rows, "wildcard config produced no mapping rows"
    assert all(r["member_group_names"] == GRP_READERS_NAME for r in rows)
    # the pattern itself never reaches the lock-file — apply needs real objectIds
    assert not any("*" in str(r["member_group_names"]) for r in rows)
    assert all(r["member_group_ids"] for r in rows)


def test_a_literal_reaches_the_mapping_in_the_member_tables_spelling():
    """The canonicalisation decision, end to end: the config author's case does not reach the
    mapping, so a pattern-expanded row and a literal row can never disagree about it."""
    rows = [{**r, "include_group_names": GRP_READERS_NAME.upper()} for r in sample_config_rows()]
    spark, client = ready(rows)

    run_generate(make_dep(spark, client, "generate"))

    written = {r["member_group_names"] for r in spark._store[MAPPING_TABLE]}
    assert written == {GRP_READERS_NAME}


# ------------------------------------------------------------------------------- failing closed


def test_a_zero_match_pattern_blocks_and_never_draws_the_member_gates_advice():
    """The member gate's "add it (with its objectId)" advice must be unreachable for a member column
    in the WIRED path, not just in the
    unit test: errors are collected rather than raised, so a surviving pattern would flow on to
    `_check_known` and be told to register a glob as an Entra principal."""
    spark, client = ready(wildcard_rows("glob:sg-ghost-*"))
    dep = make_dep(spark, client, "generate")

    _grants, all_errors, *_ = validation_of(dep)

    assert any("rule C15" in e for e in all_errors)
    assert not any("with its objectId" in e for e in all_errors)
    assert not any("wildcards not allowed" in e for e in all_errors)


def test_a_wildcard_with_an_absent_member_table_blocks_rather_than_granting_nobody():
    """`_load_member_cache` tolerates an absent table by returning empty, so expansion must refuse
    instead of quietly resolving the pattern to an empty member list."""
    spark, client = ready(wildcard_rows(), seed=False)
    spark._store.pop(MEMBER_TABLE, None)
    dep = make_dep(spark, client, "generate")

    _grants, all_errors, *_ = validation_of(dep)

    assert any("rule C15" in e for e in all_errors)
    assert spark._store.get(MAPPING_TABLE, []) == []


def test_members_still_refuses_a_wildcard_that_reached_it_unexpanded():
    """The pre-existing rejection in `Generate._members` is KEPT as a fail-closed guard. After this
    step it is unreachable through the pipeline — expansion runs first — so it now proves that a
    pattern can never reach a grant unexpanded. If it ever fires again, expansion was bypassed.
    """
    errors = []
    Generate._members({"role_name": "R", "include_group_names": "sg-*"}, "row 1 (R)", errors)

    assert any("wildcards not allowed in member values" in e for e in errors)


def test_an_exclude_pattern_subtracts_from_an_include_pattern():
    rows = [
        {
            **r,
            "include_group_names": "glob:sg-*",
            "exclude_group_names": "glob:sg-read*",
            "include_sp_names": SVC_LOADER_NAME,
        }
        for r in sample_config_rows()
    ]
    spark, client = ready(rows)
    dep = make_dep(spark, client, "generate")

    _grants, all_errors, *_ = validation_of(dep)

    # every Group is excluded away, so only the service principal remains — not an empty grant
    assert not any("rule C15" in e for e in all_errors), all_errors
    assert not any("empty after exclusion" in e for e in all_errors), all_errors


def test_a_colliding_member_table_canonicalises_to_the_sorted_first_spelling():
    """A DELIBERATE, pinned behaviour change — one of exactly three this feature's differential shows.

    When onelake_security_member itself holds two spellings of one name, that is already a hard
    cache error and generate is blocked with the mapping untouched. But `spellings` still has to
    answer for the key, and it answers `sorted(...)[0]`, so canonicalisation rewrites the config's
    literal to that arbitrary-but-deterministic pick. It surfaces only in the best-effort grants —
    which `explain()` renders as a preview, because these are CACHE errors, not author errors.

    Accepted rather than special-cased: excluding colliding keys from `spellings` would buy a
    cosmetic difference on an already-erroring path at the cost of `spellings.keys() ==
    cache.keys()`, the invariant that makes an expanded name provably present in the cache. Pinned
    here so it cannot drift silently.
    """
    spark, client = ready(sample_config_rows())
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("Group", GRP_READERS_NAME.upper(), GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    dep = make_dep(spark, client, "generate")

    grants, all_errors, *_ = validation_of(dep)

    assert any("differing only by case" in e for e in all_errors)
    assert {g["member_group_names"] for g in grants} == {GRP_READERS_NAME.upper()}
    assert spark._store.get(MAPPING_TABLE, []) == []  # blocked; nothing written


def test_a_hands_off_cell_carries_a_pattern_past_expansion_so_the_gate_skip_still_matters():
    """Why `Member._declared_names` still skips wildcard values now that expansion exists.

    The skip was added because "the wildcard rule in Generate._members owns them". Expansion owns
    them now, which looks like it makes the skip dead — the plan's first draft said to delete it.
    It is still load-bearing, for a NEW reason: a cell whose author wrote two spellings of one name
    is deliberately left UNTOUCHED by expand_wildcards, so that `_members`' within-cell case guard
    still fires, and a pattern sitting in that same cell therefore rides along into the gate.

    Without the skip the operator would be told to "add 'sg-*' (with its objectId)" — the exact
    reported symptom, on a config whose real problems are the two spellings and the unexpanded glob.
    """
    rows = [
        {**r, "include_group_names": f"SG-Readers;{GRP_READERS_NAME};sg-*"}
        for r in sample_config_rows()
    ]
    spark, client = ready(rows)
    dep = make_dep(spark, client, "generate")

    _grants, all_errors, *_ = validation_of(dep)

    # the cell was left alone, so both real problems are reported...
    assert any("differing only by case" in e for e in all_errors), all_errors
    assert any("wildcards not allowed in member values" in e for e in all_errors), all_errors
    # ...and the gate does NOT tell anyone to register a glob as an Entra principal
    assert not any("with its objectId" in e for e in all_errors), all_errors


def test_an_exclude_cell_of_only_rejected_globs_does_not_also_trip_the_pairing_rule():
    """Closes the one equivalent mutant the step-4 sabotage found.

    Dropping a rejected wildcard applies to the exclude side too, but no other test could tell:
    an exclude value never reaches `by_col` (it only subtracts), so the filter's sole observable
    effect is on the pairing rule. It takes an exclude cell that is ENTIRELY globs to see it — and
    such a cell only survives expansion when its two spellings disagree, which is what keeps
    `expand_wildcards` hands-off.

    With the filter the row reports what is actually wrong — the case disagreement, the rejected
    glob, and C1 — instead of also claiming an exclude has nothing to subtract from, which merely
    restates the include the author never wrote.
    """
    rows = [
        {**r, "include_group_names": None, "exclude_group_names": "SG-*;sg-*"}
        for r in sample_config_rows()
    ]
    spark, client = ready(rows)
    dep = make_dep(spark, client, "generate")

    _grants, all_errors, *_ = validation_of(dep)

    assert any("wildcards not allowed in member values" in e for e in all_errors), all_errors
    assert any("every row must declare at least one member" in e for e in all_errors), all_errors
    assert not any("has nothing to subtract from" in e for e in all_errors), all_errors


# ------------------------------------------------------- the idempotency opt-out (the whole point)


def test_adding_a_matching_member_to_the_table_regrants_on_the_next_generate():
    """THE test this feature exists to make pass.

    `config_hash` fingerprints the CONFIG ROWS only, so adding a principal to
    onelake_security_member leaves it identical. Without the opt-out, generate takes the
    idempotency skip, reports no_change, and the newly matching principal is NEVER granted —
    silently, on the path a pipeline runs every deploy. Measured on main before this change:
    member rows 2 -> 3, config_hash d2c5d6f18d348d2e unchanged, status skipped, changed=False.
    """
    spark, client = ready(wildcard_rows())
    first = run_generate(make_dep(spark, client, "generate"))
    assert first["changed"]
    assert {r["member_group_names"] for r in spark._store[MAPPING_TABLE]} == {GRP_READERS_NAME}

    hash_before = make_dep(spark, client, "generate").config_hash
    spark._store[MEMBER_TABLE] = spark._store[MEMBER_TABLE] + [
        member_cache_row("Group", "sg-extra", "a0000000-0000-0000-0000-0000000000ee")
    ]
    dep = make_dep(spark, client, "generate")
    assert dep.config_hash == hash_before, "the config did not change — that is the whole problem"

    second = run_generate(dep)

    assert second["changed"], "generate skipped, so the new principal was never granted"
    granted = {r["member_group_names"] for r in spark._store[MAPPING_TABLE]}
    assert granted == {f"sg-extra{LIST_SEP}{GRP_READERS_NAME}"}, granted


def test_a_config_without_member_wildcards_still_takes_the_idempotency_skip():
    """The optimisation must survive for everyone else. The fixture deliberately carries a TABLE
    glob (`include_tables: sales.*`), which is legal and common — a `has_wildcard` that scanned
    whole rows instead of the eight member columns would match it and disable idempotency
    fleet-wide, silently, with both of its branches still covered."""
    spark, client = ready(sample_config_rows())
    assert any("*" in str(r["include_tables"]) for r in spark._store[CONFIG_TABLE])

    run_generate(make_dep(spark, client, "generate"))
    second = run_generate(make_dep(spark, client, "generate"))

    assert not second["changed"]
    assert "config unchanged" in second["message"]


def test_rebuild_still_regenerates_a_wildcard_config():
    spark, client = ready(wildcard_rows())
    run_generate(make_dep(spark, client, "generate"))

    res = run_generate(make_dep(spark, client, "generate"), rebuild=True)

    assert res["changed"]
