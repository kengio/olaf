"""onelake_security_member as the No-Graph resolution cache.

It is the ONLY member-resolution source: a fully seeded cache resolves generate; a member ABSENT
from it (or a blank/garbage id, or an absent table) is a HARD error that names the member and
writes a 'rejected' audit row, all-or-nothing (mapping untouched). The 3-column cache has no
timestamp: an exact-duplicate row is harmless, a same-(type,name) case-collision is a hard error,
and an invalid member_type is a hard error.

Ported from `olaf_test_integration.ipynb` class `MemberCacheGate` (scope "mock").
"""

import pytest

from _olaf_runtime import Hash
from _fakes import (
    CONFIG_TABLE,
    GRP_READERS,
    GRP_READERS_NAME,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    MISSING_NAME,
    SVC_HEX,
    SVC_HEX_UPPER,
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


def ready(rows=None):
    """A set-up workspace with the short config authored and the member cache left EMPTY — each
    test seeds exactly the cache shape it is about."""
    # The ids are pinned, not defaulted: workspace_id/lakehouse_id are hashed into the mapping, and
    # test_stored_member_id_case_and_mapping_hash_are_unchanged pins two mapping_content goldens
    # captured against exactly these values. Defaulting them would re-baseline those hashes and
    # silently break the case-normalization decision guard they exist to hold.
    spark = build_spark()
    client = FakeFabricClient([], workspace_id="ws-demo-id", item_id="lh-demo-id")
    make_dep(spark, client, "setup").setup()  # creates all four control tables (member empty)
    spark._store[CONFIG_TABLE] = sample_config_rows() if rows is None else rows
    return spark, client


def generate_blocked(spark, client):
    """Run generate expecting the guard to block; returns the SystemExit message."""
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    return str(excinfo.value)


def test_preloaded_cache_resolves_generate():
    # every config member seeded with its objectId -> generate resolves entirely from the cache
    spark, client = ready()
    seed_sample_members(spark)
    res = run_generate(make_dep(spark, client, "generate"))
    assert res["data"]["grants"] == 3
    gids = {r["member_group_ids"] for r in spark._store[MAPPING_TABLE] if r.get("member_group_ids")}
    assert gids == {GRP_READERS}  # id came from the seeded cache


def test_missing_member_blocks_names_it_and_logs_rejection():
    spark, client = ready()
    seed_sample_members(spark)  # sg-readers present; the config below asks for an unseeded name
    spark._store[CONFIG_TABLE] = [dict(sample_config_rows()[0], include_group_names=MISSING_NAME)]
    message = generate_blocked(spark, client)
    assert "generate blocked" in message
    assert MISSING_NAME in message  # the error NAMES the missing member
    assert "onelake_security_member" in message
    assert spark._store[MAPPING_TABLE] == []  # all-or-nothing: nothing written
    rejected = [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]
    assert rejected
    assert MISSING_NAME in rejected[-1]["message"]


def test_absent_member_table_blocks():
    # onelake_security_member does not exist at all -> empty cache -> every name is a miss
    spark, client = ready()
    del spark._store[MEMBER_TABLE]
    assert "generate blocked" in generate_blocked(spark, client)
    assert spark._store.get(MAPPING_TABLE, []) == []


def test_blank_or_garbage_cache_id_is_not_trusted():
    # a cached member_id that is blank or not a GUID is skipped (treated as a miss), so a null or
    # wrong id never leaks into the mapping — generate blocks instead.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, ""),  # blank id -> skipped
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, "not-a-guid"),  # non-GUID -> skipped
    ]
    assert "generate blocked" in generate_blocked(spark, client)
    assert spark._store[MAPPING_TABLE] == []


def test_malformed_cache_rows_skipped_valid_ones_resolve():
    # rows missing member_type or member_name are skipped by _load_member_cache; the two valid
    # rows still resolve, so a cache of (garbage + the real members) generates cleanly.
    #
    # This test once NAMED the skip in its comment and asserted `grants == 3`, which cannot see it
    # — nothing references the malformed rows, so the count stays 3 whether they are skipped or
    # cached. Dropping `not str(name or "").strip()` from the guard left the whole suite green.
    # The cache itself is now read, so the TWO operands this test reaches (member_type, member_name)
    # each have an assertion that fails without them. The id operand is held next door by
    # test_blank_or_garbage_cache_id_is_not_trusted — review caught the first version of this
    # comment claiming all three, which is the same over-claim the test itself was fixed for.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row(None, "x", "e0000000-0000-0000-0000-000000000009"),  # no type -> skipped
        member_cache_row("Group", "", "e0000000-0000-0000-0000-000000000008"),  # blank -> skipped
        member_cache_row("Group", None, "e0000000-0000-0000-0000-000000000007"),  # NULL -> skipped
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    cache, spellings, errors = make_dep(spark, client, "generate")._load_member_cache()
    assert errors == []
    assert set(cache) == {("Group", GRP_READERS_NAME), ("ServicePrincipal", SVC_LOADER_NAME)}
    assert set(spellings) == set(cache)
    assert run_generate(make_dep(spark, client, "generate"))["data"]["grants"] == 3


@pytest.mark.parametrize(
    "blank",
    [None, "", "   ", "\t", "\n"],
    ids=["NULL", "empty", "spaces", "tab", "newline"],
)
def test_a_member_row_with_a_blank_name_never_enters_the_cache(blank):
    """The operand nothing was holding.

    A row carrying a valid `member_type` and a valid GUID but no usable NAME is skipped. Dropping
    `not str(name).strip()` from the guard survived all 1246 tests, and a nameless principal was
    then indexed and trusted under the key `(mtype, "")`.

    The consequence grew after member wildcards arrived: `spellings` feeds
    member-pattern expansion, so a cached nameless row is matched by `glob:*`.

    🔴 **NULL is the case that is NOT contained, and it is why this parametrize exists.** The guard
    read `str(name).strip()`, and `str(None)` is the truthy literal `"None"` — so a SQL NULL name
    passed all three operands, cached as `(mtype, "none")`, and `Parse.list` does NOT drop it
    downstream because `"None"` is not empty. Its objectId reached the mapping and would have been
    granted at apply. Measured on the code this test was first written against.

    The four whitespace spellings, by contrast, are ONE input written four times: `Parse.trim_row`
    strips before the guard sees the value, so all four arrive as `""`. They are kept as
    defence-in-depth against that trim changing, not because they exercise four paths.
    """
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("Group", blank, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]

    cache, spellings, errors = make_dep(spark, client, "generate")._load_member_cache()

    assert errors == []
    assert ("Group", "") not in cache
    assert ("Group", "") not in spellings, "a nameless row reaching spellings is matched by glob:*"
    assert set(cache) == {("Group", GRP_READERS_NAME), ("ServicePrincipal", SVC_LOADER_NAME)}


@pytest.mark.parametrize("nameless", [None, "   "], ids=["NULL", "whitespace"])
def test_a_nameless_row_cannot_be_pulled_in_by_a_wildcard(nameless):
    """The end-to-end shape, now that `spellings` is an expansion source.

    The two spellings behave differently downstream, which is the whole point:

    * **whitespace** — a JOINT pin, not an either-layer one. With only the cache guard dropped this
      still passes, because `Parse.list` absorbs the empty segment; it goes red only under the
      conjunction. It contributes nothing to the per-operand kill matrix, and its job is to catch a
      guard regression once the containment layer stops absorbing it.
    * **NULL** — nothing absorbs it. `str(None)` is the truthy literal `"None"`, so the row was
      cached, expanded by `glob:*`, and its objectId reached the mapping and would have been granted
      at apply. This case fails the moment the guard regresses, with no second layer to hide it.
    """
    spark, client = ready(
        [
            {**r, "include_group_names": "glob:*", "include_sp_names": None}
            for r in sample_config_rows()
        ]
    )
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("Group", nameless, "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
    ]

    run_generate(make_dep(spark, client, "generate"))

    rows = spark._store[MAPPING_TABLE]
    assert {r["member_group_names"] for r in rows} == {GRP_READERS_NAME}
    assert not any("aaaaaaaa" in str(r["member_group_ids"]) for r in rows)


def test_exact_duplicate_cache_row_resolves():
    # No timestamp column exists to disambiguate duplicates (spec §4): an EXACT-duplicate row
    # (same type/name/id) is harmless — it just resolves to that id.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),  # exact duplicate
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    res = run_generate(make_dep(spark, client, "generate"))
    assert res["data"]["grants"] == 3
    gids = {r["member_group_ids"] for r in spark._store[MAPPING_TABLE] if r.get("member_group_ids")}
    assert gids == {GRP_READERS}


def test_cache_case_collision_blocks_generate():
    # Two member rows of the same type whose names differ ONLY by case are different principals —
    # a hard cache error that blocks generate (mirrors the config-side + table case guards).
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("Group", "SG-Readers", "e1111111-1111-1111-1111-111111111111"),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    message = generate_blocked(spark, client)
    assert "generate blocked" in message
    assert "differing only by case" in message
    assert spark._store[MAPPING_TABLE] == []  # all-or-nothing — nothing written


def test_cache_one_id_under_two_names_blocks_generate():
    # The mirror of the conflicting-id guard: one objectId spelled under two different names.
    # Uniqueness cannot be enforced on the table itself — Fabric accepts PRIMARY KEY / UNIQUE
    # only as NOT ENFORCED — so generate is where it has to be caught. Left unguarded, an
    # id->name read (run_by, who_can_access) would resolve by row order, and the two names look
    # like two principals to anyone reading config.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
        member_cache_row("ManagedIdentity", "mi-loader", SVC_LOADER),  # same id, other name
    ]
    message = generate_blocked(spark, client)
    assert "generate blocked" in message
    assert SVC_LOADER in message  # names the offending id
    assert "mi-loader" in message  # and both spellings
    assert SVC_LOADER_NAME in message
    assert spark._store[MAPPING_TABLE] == []  # all-or-nothing — nothing written


def test_cache_one_id_under_two_types_same_name_blocks_generate():
    # A principal is a user or a group, never both. The same id under two types would also be
    # sent to the DAR API with whichever objectType the config row picked — a payload naming a
    # principal that is not of that kind. The guard keys on the whole (type, name) identity,
    # not on the name, so this shape is caught even though the spelling never varies.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
        member_cache_row("ManagedIdentity", SVC_LOADER_NAME, SVC_LOADER),  # same id, same name
    ]
    message = generate_blocked(spark, client)
    assert SVC_LOADER in message
    assert "ServicePrincipal" in message
    assert "ManagedIdentity" in message
    assert spark._store[MAPPING_TABLE] == []


def test_cache_one_id_in_two_letter_cases_under_two_names_blocks_generate():
    # SECURITY: an objectId is case-INSENSITIVE hex, so SVC_HEX and SVC_HEX_UPPER are ONE
    # principal. Listed under two names it is the duplicate-id shape above, only spelled
    # differently — left case-sensitive the guard stays SILENT, the cache hands out two keys for
    # one principal, and that principal quietly collects BOTH roles' access.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_HEX),
        member_cache_row("ManagedIdentity", "mi-loader", SVC_HEX_UPPER),  # same id, other case
    ]
    message = generate_blocked(spark, client)
    assert "more than one principal" in message
    assert "mi-loader" in message  # both spellings named
    assert SVC_LOADER_NAME in message
    assert spark._store[MAPPING_TABLE] == []  # all-or-nothing — nothing written


def test_cache_same_id_in_two_letter_cases_under_one_identity_generates():
    # AVAILABILITY, the mirror: ONE (member_type, member_name) whose id is spelled twice in
    # different case is one principal listed twice, not two conflicting ids. Left case-sensitive
    # the conflicting-id guard blocks generate on a config that is perfectly valid.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_HEX),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_HEX_UPPER),  # same id
    ]
    res = run_generate(make_dep(spark, client, "generate"))
    assert res["data"]["grants"] == 3
    sp_ids = {r["member_sp_ids"] for r in spark._store[MAPPING_TABLE] if r.get("member_sp_ids")}
    # the STORED value is byte-preserved (a written spelling, never a normalized one) — it is
    # what reaches the DAR payload and the mapping hash.
    assert sp_ids == {SVC_HEX_UPPER}


def test_stored_member_id_case_and_mapping_hash_are_unchanged():
    # DECISION GUARD for the case-normalization design: only KEYS and COMPARISONS normalize.
    # The id written into the mapping keeps the author's spelling, and mapping_hash — which
    # names the mapping-history CSV and drives its reuse check — is byte-identical to what the
    # pre-normalization code produced. Both hashes below were captured BEFORE the change.
    spark, client = ready()
    seed_sample_members(spark)
    run_generate(make_dep(spark, client, "generate"))
    assert Hash.mapping_content(spark._store[MAPPING_TABLE]) == "0118003e0a313e26"
    upper, client2 = ready()
    upper._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, SVC_HEX_UPPER),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    run_generate(make_dep(upper, client2, "generate"))
    gids = {r["member_group_ids"] for r in upper._store[MAPPING_TABLE] if r.get("member_group_ids")}
    assert gids == {SVC_HEX_UPPER}  # stored as written, not lowered
    assert Hash.mapping_content(upper._store[MAPPING_TABLE]) == "2397947520d838b0"


def test_cache_case_variants_with_distinct_ids_are_not_called_duplicates():
    # Two case-variant names, each with its OWN id: already blocked by the case-collision and
    # conflicting-id guards. The duplicate-id guard must stay silent — building its map by
    # pairing the per-key name set with the per-key id set would cross-multiply and accuse both
    # ids of a duplication neither is part of, putting a false line in a collect-all reject.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("Group", "SG-Readers", "e1111111-1111-1111-1111-111111111111"),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    message = generate_blocked(spark, client)
    assert "differing only by case" in message  # the guards that SHOULD fire
    assert "conflicting objectIds" in message
    assert "more than one principal" not in message  # the one that must not


def test_cache_one_id_per_principal_generates_cleanly():
    # The normal shape: every id appears exactly once. Guard silent, generate proceeds.
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "sg-readers", GRP_READERS),
        member_cache_row("ServicePrincipal", SVC_LOADER_NAME, SVC_LOADER),
    ]
    run_generate(make_dep(spark, client, "generate"))
    assert spark._store[MAPPING_TABLE]  # generated


def test_invalid_member_type_blocks_generate():
    # A member row whose member_type is not one of the four Entra types is a hard cache error —
    # the row is skipped AND named, so generate blocks (its member is unresolved too).
    spark, client = ready()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("Robot", SVC_LOADER_NAME, SVC_LOADER),  # invalid member_type
    ]
    message = generate_blocked(spark, client)
    assert "generate blocked" in message
    assert "invalid member_type" in message
    assert spark._store[MAPPING_TABLE] == []


def test_cache_also_returns_the_name_as_written():
    """The cache keys on member_name.lower() and stores only the objectId, so nothing in it
    can tell a wildcard expansion how a principal is actually SPELLED. Expanding from the keys would
    write lowercase names into member_*_names — which are MAPPING_COLUMNS, and therefore feed
    Hash.mapping_content -> mapping_hash -> the history CSV name and its reuse check — so a
    pattern-expanded row would disagree with a literal row about case and move the hash.

    _load_member_cache therefore returns a third value: {(member_type, name.lower()): as-written}.
    """
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", "SG-Analysts", GRP_READERS),
        member_cache_row("ServicePrincipal", "  Svc-Loader  ", SVC_LOADER),  # autotrimmed
    ]
    dep = make_dep(spark, FakeFabricClient(), "generate")

    cache, spellings, errors = dep._load_member_cache()

    assert errors == []
    # the cache's own contract is UNCHANGED — still keyed on the lowered name, still the id
    assert cache == {
        ("Group", "sg-analysts"): GRP_READERS,
        ("ServicePrincipal", "svc-loader"): SVC_LOADER,
    }
    # ...and the new map carries the spelling the table actually holds, same autotrim
    assert spellings == {
        ("Group", "sg-analysts"): "SG-Analysts",
        ("ServicePrincipal", "svc-loader"): "Svc-Loader",
    }
    # Keys are identical BY CONSTRUCTION (both are written on adjacent lines after every skip).
    # This is why an expanded name can never be "missing from the member table" — a fact the
    # plan's Step 4 originally tried to write a test against, which no input could satisfy.
    assert spellings.keys() == cache.keys()


def test_cache_spelling_pick_is_deterministic_under_a_case_collision():
    """A same-type case collision is a collected ERROR, not a raise — so the cache is still built,
    and `spellings` still has to answer for that key. The pick must not follow dict insertion order
    (that is Delta row order, which is not stable run to run); it mirrors the `sorted(...)[0]`
    representative the conflicting-objectId message already uses one guard below.

    What this guard does and does not catch, measured rather than assumed:
      • a DETERMINISTIC mis-implementation — `sorted(v)[-1]`, `max(v)` — fails 8 of 8 hash seeds.
        That is the class a refactor actually introduces, and it is what this test exists to pin.
      • `list(v)[0]` (set-iteration order) failed only 6 of 8 seeds, because the MUTANT is itself
        nondeterministic: with N spellings it lands on the sorted-first one about 1/N of the time.
        Three variants rather than two narrows that window; it cannot close it, and no assertion
        here can, since the wrong code is right by coincidence. In CI such a slip shows up as a
        flaky test, which is its own signal.
    """
    spark = build_spark()
    variants = ["sg-analysts", "SG-Analysts", "Sg-AnAlYsTs"]
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", variants[0], GRP_READERS),
        member_cache_row("Group", variants[1], SVC_LOADER),
        member_cache_row("Group", variants[2], SVC_HEX),
    ]
    dep = make_dep(spark, FakeFabricClient(), "generate")

    _cache, spellings, errors = dep._load_member_cache()

    assert any("differing only by case" in e for e in errors)
    assert spellings[("Group", "sg-analysts")] == sorted(variants)[0]


def test_a_member_name_carrying_a_glob_metacharacter_or_separator_is_refused():
    """From the privilege-boundary review. Entra permits `*`, `?` and `;` in a displayName; this
    framework cannot represent any of them, so the row is refused at the table rather than stored.

    `*` / `?` — a config value containing one is read as a PATTERN, so such a principal could never
    be named literally. Left storable, the framework would be unable to grant it while happily
    granting its NEIGHBOURS: `Sales? Reporting` expands against `Salesx Reporting` with no error.

    `;` — expansion re-joins matches with LIST_SEP and every downstream reader re-splits, so one
    such row becomes several members. Measured before this guard: pattern `sg-read*` against a row
    named `sg-readers-eu; sg-domain-admins` produced `sg-readers;sg-readers-eu;sg-domain-admins`,
    injecting a principal the pattern never matched and dropping the one it did.

    Refused at the table because that is the only seam where the ambiguity can be REMOVED rather
    than merely detected — the same seam that already refuses a non-GUID member_id.
    """
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("Group", GRP_READERS_NAME, GRP_READERS),
        member_cache_row("Group", "sg-star*", "a0000000-0000-0000-0000-0000000000a1"),
        member_cache_row("Group", "sg-quest?", "a0000000-0000-0000-0000-0000000000a2"),
        member_cache_row("Group", "sg-a; sg-b", "a0000000-0000-0000-0000-0000000000a3"),
    ]
    dep = make_dep(spark, FakeFabricClient(), "generate")

    cache, spellings, errors = dep._load_member_cache()

    assert len(errors) == 3, errors
    assert all("cannot hold a wildcard metacharacter" in e for e in errors), errors
    # refused, not stored — so neither expansion nor resolution can ever see them
    assert set(cache) == {("Group", GRP_READERS_NAME)}
    assert set(spellings) == {("Group", GRP_READERS_NAME)}
