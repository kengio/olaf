"""The Fabric/OS seams behind deep mocks: FabricClient's REST shell, Target's context resolvers,
Catalog's listers, Log's exception-safe reads, and generate's lakehouse-target guard.

Ported from `olaf_test_integration.ipynb` classes `FabricClientMocked`, `ResolversMocked`,
`CatalogAndAuditMocked`, `DeploymentGuardsMocked` and `LakehouseTargetGuard` (scope "mock").
"""

import contextlib
import os
import re
import sys
import tempfile
import types
from unittest import mock

import pytest

from _olaf_runtime import (
    Catalog,
    ControlDataGuardError,
    DARHTTPError,
    DARConflictError,
    Deployment,
    FabricClient,
    Log,
    Target,
    TargetAmbiguous,
    TargetNotFound,
    UsageError,
    ValidationError,
)
from _fakes import (
    CONFIG_TABLE,
    LOG_TABLE,
    MAPPING_TABLE,
    MEMBER_TABLE,
    SVC_LOADER,
    SVC_LOADER_NAME,
    TENANT,
    FakeFabricClient,
    FakeSpark,
    _FakeLogDataFrame,
    _FakeLogSpark,
    build_spark,
    make_dep,
    member_cache_row,
    run_generate,
    sample_config_rows,
    seed_sample_members,
)


# ---------------------------------------------------------------------------------------------
# Deep-mock helpers: fake requests / notebookutils in sys.modules
# ---------------------------------------------------------------------------------------------


class _FakeHTTPError(Exception):
    """What the real requests.Response.raise_for_status() raises on a >= 400 — stood in for here
    so a status the retry layer refuses to retry still FAILS the call, instead of the fake
    quietly answering None and hiding the difference."""


class _FakeResp:
    """A requests.Response stand-in. `headers` is a real attribute because the retry layer reads
    Retry-After off it, and `raise_for_status` genuinely raises: a fake that swallowed >= 400
    could not tell "retried and recovered" from "never retried at all"."""

    def __init__(self, payload, status=200, headers=None, text=""):
        self._payload, self.status_code = payload, status
        self.headers, self.text = dict(headers or {}), text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _FakeHTTPError(f"{self.status_code} for the fake request")
        return None

    def json(self):
        return self._payload


class _FakeRequests:
    """Minimal requests stand-in: get() pops queued responses; put() pops queued ones when it was
    given any, else records + returns 200."""

    def __init__(self, get_responses=None, put_responses=None):
        self._queue = list(get_responses or [])
        self._put_queue = list(put_responses or [])
        self.get_calls, self.put_calls = [], []

    def get(self, url, headers=None, timeout=None):
        self.get_calls.append({"url": url, "headers": headers, "timeout": timeout})
        return self._queue.pop(0)

    def put(self, url, headers=None, json=None, timeout=None):
        self.put_calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return self._put_queue.pop(0) if self._put_queue else _FakeResp({}, 200)


@contextlib.contextmanager
def fabric_fakes(requests_obj=None, context=None, token="tkn", fs_entries=None):
    saved = {}

    def _set(name, mod):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod

    nu = types.ModuleType("notebookutils")
    nu.runtime = types.SimpleNamespace(context=dict(context or {}))

    class _Cred:
        def getToken(self, *a, **k):
            return token

    nu.credentials = _Cred()

    class _FsEntry:
        def __init__(self, name, isDir):
            self.name, self.isDir = name, isDir

    class _Fs:
        def ls(self, base):
            return [_FsEntry(n, d) for (n, d) in (fs_entries or {}).get(base, [])]

    nu.fs = _Fs()
    _set("notebookutils", nu)
    if requests_obj is not None:
        _set("requests", requests_obj)
    try:
        yield nu
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@contextlib.contextmanager
def without_notebookutils():
    """No notebookutils at all -- the import-error branch of every resolver."""
    saved = sys.modules.pop("notebookutils", None)
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["notebookutils"] = saved


def deploy_log(spark, run_by, member_table=MEMBER_TABLE):
    """An apply-mode Log carrying the given run_by, resolved against `member_table`."""
    return Log(
        spark,
        LOG_TABLE,
        "B",
        "R",
        "dev",
        "apply",
        "WS_Demo",
        "LH_Demo",
        run_by=run_by,
        member_table=member_table,
    )


# ---------------------------------------------------------------------------------------------
# FabricClientMocked — header build / payload build / response parse / pagination.
# The only line not run is the literal REST call, which is pragma'd.
# ---------------------------------------------------------------------------------------------


# A continuation the client's URL fence accepts: a later page of the same Fabric API read.
# The poisoned variants that must NOT be fetched live in
# test_a_poisoned_continuation_uri_is_refused_not_fetched.
NEXT_PAGE = f"{FabricClient.BASE}/workspaces/ws1/items/it1/dataAccessRoles?continuationToken=t1"


def test_user_identity_headers_and_list_pagination():
    page1 = _FakeResp({"value": [{"name": "RoleA"}], "continuationUri": NEXT_PAGE})
    page2 = _FakeResp({"value": [{"name": "RoleB"}]})
    req = _FakeRequests([page1, page2])
    with fabric_fakes(requests_obj=req, token="TKN"):
        c = FabricClient("ws1", "it1")
        assert c._headers["Authorization"] == "Bearer TKN"
        assert c._headers["Content-Type"] == "application/json"
        roles = c.list_roles()
    assert [r["name"] for r in roles] == ["RoleA", "RoleB"]  # both pages merged
    assert len(req.get_calls) == 2  # followed the continuation
    assert req.get_calls[0]["url"].endswith("/dataAccessRoles")


def test_fabric_client_uses_the_documented_pbi_token_audience():
    """Changing the audience to a URL creates an unsupported Fabric REST credential request."""
    with fabric_fakes() as notebookutils:
        seen = []
        notebookutils.credentials.getToken = lambda audience: seen.append(audience) or "TKN"
        FabricClient("ws1", "it1")
    assert seen == ["pbi"]


def test_put_roles_dry_run_and_real():
    req = _FakeRequests([])
    with fabric_fakes(requests_obj=req, token="T"):
        c = FabricClient("ws1", "it1")
        assert c.put_roles([{"name": "X"}], dry_run=True) == 200
        assert c.put_roles([{"name": "X"}], allow_unconditional=True) == 200
    assert req.put_calls[0]["url"].endswith("?dryRun=true")  # dry-run flags the URL
    assert not req.put_calls[1]["url"].endswith("?dryRun=true")
    assert req.put_calls[1]["json"] == {"value": [{"name": "X"}]}  # full-set body


def test_real_put_without_etag_requires_explicit_unconditional_opt_out():
    """Deleting the guard would send a real full-set write without concurrency evidence."""
    req = _FakeRequests([])
    with fabric_fakes(requests_obj=req, token="T"):
        client = FabricClient("ws1", "it1")
        with pytest.raises(UsageError, match="ETag"):
            client.put_roles([{"name": "X"}])
    assert req.put_calls == []


def test_list_roles_captures_the_collection_etag():
    # the ETag rides the FIRST page's response headers (later pages are continuations of the
    # same read); a header-less response resets the token instead of leaving a stale one.
    page1 = _FakeResp({"value": [], "continuationUri": NEXT_PAGE}, headers={"ETag": '"tok-1"'})
    page2 = _FakeResp({"value": []}, headers={"ETag": '"tok-2-ignored"'})
    req = _FakeRequests([page1, page2, _FakeResp({"value": []})])
    with fabric_fakes(requests_obj=req, token="T"):
        c = FabricClient("ws1", "it1")
        assert c.roles_etag is None  # nothing read yet
        c.list_roles()
        assert c.roles_etag == '"tok-1"'  # first page's token, not the continuation's
        c.list_roles()  # a header-less response must not leave the old token behind
        assert c.roles_etag is None


def test_put_roles_sends_if_match_on_the_real_put_only():
    req = _FakeRequests([])
    with fabric_fakes(requests_obj=req, token="T"):
        c = FabricClient("ws1", "it1")
        c.put_roles([{"name": "X"}], dry_run=True, etag='"tok"')  # dryRun: never conditional
        c.put_roles([{"name": "X"}], etag='"tok"')  # real: conditional
        c.put_roles([{"name": "X"}], allow_unconditional=True)
    assert "If-Match" not in req.put_calls[0]["headers"]
    assert req.put_calls[1]["headers"]["If-Match"] == '"tok"'
    assert "If-Match" not in req.put_calls[2]["headers"]
    assert "If-Match" not in c._headers  # the per-call copy never mutates the base headers


def test_a_412_is_a_distinct_conflict_error_and_never_retried(no_real_sleep):
    # Re-sending the same stale ETag cannot succeed, so 412 is not in RETRY_STATUSES — it
    # surfaces as its own conflict class (an http-category DARHTTPError subclass) naming the
    # remedy, with no retry burned.
    req = _FakeRequests(put_responses=[_FakeResp({}, 412, text="precondition failed")])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(DARConflictError) as excinfo:
            FabricClient("ws1", "it1").put_roles([{"name": "X"}], etag='"stale"')
    assert "re-run mode=plan" in str(excinfo.value)
    assert excinfo.value.ambiguous is False  # first attempt: the refusal really is clean
    assert len(req.put_calls) == 1  # no retry burned on a precondition that cannot heal
    assert no_real_sleep == []
    from _olaf_runtime import OLAFError

    assert OLAFError.classify(excinfo.value) == "http"


def test_a_retried_412_is_flagged_ambiguous_because_the_first_attempt_may_have_landed(
    no_real_sleep,
):
    # A gateway 502 can arrive AFTER the origin committed the PUT; the committed write
    # rotates the collection ETag, so the retry's re-sent stale If-Match draws a 412 that
    # proves nothing about what landed. Only the client knows a retry preceded the 412,
    # so the fact rides on the exception for the push record to act on.
    req = _FakeRequests(put_responses=[_FakeResp({}, 502, text="bad gateway"), _FakeResp({}, 412)])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(DARConflictError) as excinfo:
            FabricClient("ws1", "it1").put_roles([{"name": "X"}], etag='"stale"')
    assert excinfo.value.ambiguous is True
    assert "RETRIED attempt" in str(excinfo.value)
    assert len(req.put_calls) == 2
    # the retry re-sent the SAME stale token — exactly why this 412 proves nothing
    assert [c["headers"]["If-Match"] for c in req.put_calls] == ['"stale"', '"stale"']
    assert no_real_sleep == [1.0]


def test_a_conditional_put_still_retries_transients_with_the_same_token(no_real_sleep):
    # the ambiguity machinery must not scare the conditional PUT out of retrying at all:
    # a transient that then SUCCEEDS is the common case the retry exists for
    req = _FakeRequests(put_responses=[_FakeResp({}, 503), _FakeResp({}, 200)])
    with fabric_fakes(requests_obj=req, token="T"):
        assert FabricClient("ws1", "it1").put_roles([{"name": "X"}], etag='"tok"') == 200
    assert [c["headers"]["If-Match"] for c in req.put_calls] == ['"tok"', '"tok"']
    assert no_real_sleep == [1.0]


def test_put_roles_surfaces_api_error_body():
    # a >= 400 PUT response raises DARHTTPError carrying the status + body (raise_for_status
    # alone would drop the actionable policy-validation message).
    class _Resp400:
        status_code = 400
        text = '{"error":"policy invalid"}'

        def raise_for_status(self):
            return None

    class _Req400:
        def put(self, url, headers=None, json=None, timeout=None):
            return _Resp400()

    with fabric_fakes(requests_obj=_Req400(), token="T"):
        c = FabricClient("ws1", "it1")
        with pytest.raises(DARHTTPError) as excinfo:
            c.put_roles([{"name": "X"}], allow_unconditional=True)
    assert "400" in str(excinfo.value)
    assert "policy invalid" in str(excinfo.value)


def test_resolve_lakehouse_paginates_and_returns_canonical():
    # resolve_lakehouse drives _get_paged over the /lakehouses endpoint (following continuationUri)
    # and Target._single_named resolves the displayName case-insensitively to its canonical
    # (name, id).
    page1 = _FakeResp(
        {"value": [{"displayName": "Other", "id": "id-other"}], "continuationUri": NEXT_PAGE}
    )
    page2 = _FakeResp({"value": [{"displayName": "LH_Demo", "id": "lh-1"}]})
    req = _FakeRequests([page1, page2])
    with fabric_fakes(requests_obj=req, token="TKN"):
        c = FabricClient("ws1", "it1")
        name, oid = c.resolve_lakehouse("lh_demo")  # different case resolves
    assert (name, oid) == ("LH_Demo", "lh-1")  # canonical spelling + id
    assert len(req.get_calls) == 2  # followed the continuation across both pages
    assert req.get_calls[0]["url"].endswith("/lakehouses")


def test_every_fabric_request_carries_the_default_timeout():
    # An untimed requests call waits on the OS TCP timeout -- minutes, and per page. Every
    # GET and the PUT must send FabricClient.TIMEOUT unless a caller overrides it.
    req = _FakeRequests(
        [
            _FakeResp({"value": [{"name": "RoleA"}], "continuationUri": NEXT_PAGE}),
            _FakeResp({"value": [{"name": "RoleB"}]}),
            _FakeResp({"value": [{"displayName": "LH_Demo", "id": "lh-1"}]}),
        ]
    )
    with fabric_fakes(requests_obj=req, token="TKN"):
        c = FabricClient("ws1", "it1")
        c.list_roles()
        c.resolve_lakehouse("LH_Demo")
        c.put_roles([{"name": "X"}], allow_unconditional=True)
    assert [g["timeout"] for g in req.get_calls] == [FabricClient.TIMEOUT] * 3  # both pages + list
    assert req.put_calls[0]["timeout"] == FabricClient.TIMEOUT
    assert FabricClient.TIMEOUT == (10, 60)  # (connect, read)


def test_list_roles_quick_uses_the_shorter_failure_read_timeout():
    # The push-failure re-read decorates an error the operator is already waiting on, so it gets a
    # tighter bound than a call the run's result depends on.
    req = _FakeRequests([_FakeResp({"value": [{"name": "RoleA"}]})])
    with fabric_fakes(requests_obj=req, token="TKN"):
        roles = FabricClient("ws1", "it1").list_roles_quick()
    assert [r["name"] for r in roles] == ["RoleA"]
    assert req.get_calls[0]["timeout"] == FabricClient.FAILURE_READ_TIMEOUT == (5, 15)
    assert FabricClient.FAILURE_READ_TIMEOUT < FabricClient.TIMEOUT  # strictly tighter


# ---------------------------------------------------------------------------------------------
# Bounded retry — WHICH statuses get another attempt, how long it waits, and what still fails.
# ---------------------------------------------------------------------------------------------


@pytest.fixture
def no_real_sleep(monkeypatch):
    """Record the retry waits instead of taking them. The runtime sleeps through the MODULE-level
    `time`, so patching the module here is the same seam production uses -- a test that patched a
    private copy would pass against a retry loop that still slept for real."""
    import time as _time

    slept = []
    monkeypatch.setattr(_time, "sleep", slept.append)
    return slept


def test_a_transient_status_is_retried_and_the_second_attempt_stands(no_real_sleep):
    # 503 is the API being briefly unavailable, not this request being wrong: the SAME call can
    # survive it, so it gets another attempt rather than failing a deploy that was never wrong.
    req = _FakeRequests([_FakeResp({}, 503), _FakeResp({"value": [{"name": "RoleA"}]})])
    with fabric_fakes(requests_obj=req, token="T"):
        roles = FabricClient("ws1", "it1").list_roles()
    assert [r["name"] for r in roles] == ["RoleA"]
    assert len(req.get_calls) == 2  # retried once
    assert no_real_sleep == [1.0]  # the first backoff gap, not the second


def test_a_numeric_retry_after_header_wins_over_the_backoff(no_real_sleep):
    # The service knows its own throttle window; guessing shorter is how one 429 becomes three.
    req = _FakeRequests(
        [
            _FakeResp({}, 429, headers={"Retry-After": "7"}),
            _FakeResp({"value": [{"name": "RoleA"}]}),
        ]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        assert FabricClient("ws1", "it1").list_roles() == [{"name": "RoleA"}]
    assert no_real_sleep == [7.0]  # the header, not RETRY_BACKOFF[0]


def test_an_http_date_retry_after_is_parsed_and_floored_at_zero(no_real_sleep):
    # RFC 9110 permits an HTTP-date as well as delta-seconds; this past date means retry now.
    req = _FakeRequests(
        [
            _FakeResp({}, 503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}),
            _FakeResp({"value": []}),
        ]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        assert FabricClient("ws1", "it1").list_roles() == []
    assert no_real_sleep == [0.0]


def test_a_naive_http_date_retry_after_is_normalized_to_utc(no_real_sleep):
    """A parseable date without an offset must not fail datetime arithmetic or skip the retry."""
    req = _FakeRequests(
        [
            _FakeResp({}, 503, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00"}),
            _FakeResp({"value": []}),
        ]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        assert FabricClient("ws1", "it1").list_roles() == []
    assert no_real_sleep == [0.0]


def test_a_negative_retry_after_is_floored_at_zero_not_rejected(no_real_sleep):
    # A negative value is still a service saying "retry"; it just says "now".
    req = _FakeRequests(
        [_FakeResp({}, 502, headers={"Retry-After": "-5"}), _FakeResp({"value": []})]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        assert FabricClient("ws1", "it1").list_roles() == []
    assert no_real_sleep == [0.0]


def test_a_non_transient_4xx_is_never_retried(no_real_sleep):
    # 403 is this caller's identity being wrong. Repeating it changes nothing but the clock -- and
    # on a throttled tenant, spends budget that the retryable statuses need.
    req = _FakeRequests([_FakeResp({}, 403)])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(_FakeHTTPError):
            FabricClient("ws1", "it1").list_roles()
    assert len(req.get_calls) == 1  # no second attempt
    assert no_real_sleep == []


def test_the_retry_budget_is_three_attempts_and_then_the_failure_stands(no_real_sleep):
    # Bounded, not persistent: a service that is down stays down, and an unbounded retry turns a
    # failed deploy into a hung one.
    req = _FakeRequests([_FakeResp({}, 503) for _ in range(3)])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(_FakeHTTPError):
            FabricClient("ws1", "it1").list_roles()
    assert len(req.get_calls) == FabricClient.RETRY_ATTEMPTS == 3
    assert no_real_sleep == list(FabricClient.RETRY_BACKOFF) == [1.0, 2.0]
    # one backoff entry per GAP between attempts -- the two constants cannot drift apart
    assert len(FabricClient.RETRY_BACKOFF) == FabricClient.RETRY_ATTEMPTS - 1


def test_an_oversized_attempts_budget_is_clamped_not_an_index_error(no_real_sleep):
    # Issue #8: RETRY_BACKOFF has RETRY_ATTEMPTS - 1 entries, so attempts beyond the class
    # budget used to IndexError on the last wait — a crash inside the retry layer, replacing
    # the response it existed to deliver. Clamped: 99 behaves exactly like RETRY_ATTEMPTS.
    req = _FakeRequests([_FakeResp({}, 503) for _ in range(5)])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(_FakeHTTPError):
            FabricClient("ws1", "it1").list_roles(attempts=99)
    assert len(req.get_calls) == FabricClient.RETRY_ATTEMPTS  # capped, not crashed
    assert no_real_sleep == list(FabricClient.RETRY_BACKOFF)


def test_a_zero_attempts_budget_means_one_send(no_real_sleep):
    # ...and the degenerate floor is explicit: zero (or less) cannot mean "send nothing" —
    # the send has already happened when the retry layer is consulted — so it means one send.
    req = _FakeRequests([_FakeResp({}, 503), _FakeResp({"value": []})])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(_FakeHTTPError):
            FabricClient("ws1", "it1").list_roles(attempts=0)
    assert len(req.get_calls) == 1  # no retry was taken
    assert no_real_sleep == []


def test_put_roles_retries_because_the_full_set_put_is_idempotent(no_real_sleep):
    # The write is the FULL desired set, so a second attempt re-asserts the same end state rather
    # than compounding a partial one. That is what makes retrying a WRITE safe here at all.
    req = _FakeRequests(put_responses=[_FakeResp({}, 503), _FakeResp({}, 200)])
    with fabric_fakes(requests_obj=req, token="T"):
        assert (
            FabricClient("ws1", "it1").put_roles([{"name": "X"}], allow_unconditional=True) == 200
        )
    assert len(req.put_calls) == 2
    assert req.put_calls[1]["json"] == {"value": [{"name": "X"}]}  # same full set, re-asserted
    assert no_real_sleep == [1.0]


def test_put_roles_still_surfaces_the_api_body_after_the_retries_are_spent(no_real_sleep):
    # The retry layer adds attempts and nothing else: a failure that survives all of them raises
    # the SAME DARHTTPError carrying the status + body, so the push-failure forensics built around
    # it are unchanged.
    req = _FakeRequests(
        put_responses=[_FakeResp({}, 429, text='{"error":"too many requests"}') for _ in range(3)]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(DARHTTPError) as excinfo:
            FabricClient("ws1", "it1").put_roles([{"name": "X"}], allow_unconditional=True)
    assert "429" in str(excinfo.value) and "too many requests" in str(excinfo.value)
    assert len(req.put_calls) == 3
    assert no_real_sleep == [1.0, 2.0]


def test_a_huge_retry_after_is_capped_not_honoured_verbatim(no_real_sleep):
    # Retry-After is a number the SERVICE picks, and a throttled tenant can legitimately answer
    # with an hour. Honoured verbatim on the PUT path -- which has no paginated budget to trip --
    # a "bounded" retry becomes an unbounded wait inside a pipeline nobody is watching.
    req = _FakeRequests(
        put_responses=[_FakeResp({}, 429, headers={"Retry-After": "3600"}), _FakeResp({}, 200)]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        assert (
            FabricClient("ws1", "it1").put_roles([{"name": "X"}], allow_unconditional=True) == 200
        )
    assert no_real_sleep == [FabricClient.RETRY_WAIT_CAP] == [30.0]  # capped, not 3600
    # the cap is the ceiling on EVERY wait, from either source, so the backoff sits under it
    assert max(FabricClient.RETRY_BACKOFF) < FabricClient.RETRY_WAIT_CAP


def test_the_failure_read_never_retries_so_it_cannot_outlive_the_error_it_decorates(no_real_sleep):
    """The push-failure re-read opts OUT of the retry every other call wants.

    Its tighter FAILURE_READ_TIMEOUT exists because it runs inside an incident window the operator
    is already waiting through — and three attempts with backoff would silently spend ~48s of that
    window on a read whose result is only decoration (a failed one is already handled as
    `live_roles_after: null`). The retry layer would have undone the guarantee the timeout makes,
    so the attempt budget is threaded down beside it."""
    req = _FakeRequests([_FakeResp({}, 503)])  # only ONE response queued: a second GET would pop
    with fabric_fakes(requests_obj=req, token="T"):  # from an empty list and IndexError
        with pytest.raises(_FakeHTTPError):
            FabricClient("ws1", "it1").list_roles_quick()
    assert len(req.get_calls) == 1  # sent once, whatever came back
    assert no_real_sleep == []  # ...and waited for nothing
    assert req.get_calls[0]["timeout"] == FabricClient.FAILURE_READ_TIMEOUT  # both halves intact
    # ...and the opt-out is scoped to THIS caller: the same endpoint read the ordinary way is
    # pinned as retrying by test_a_transient_status_is_retried_and_the_second_attempt_stands.


def test_a_retry_wait_that_would_outlive_the_paged_budget_is_not_taken(monkeypatch, no_real_sleep):
    # PAGED_BUDGET_SECONDS bounds the WHOLE paginated call. A retry allowed to sleep past it would
    # make that bound a lie, so the wait is refused and the response is handed back as it stands.
    import time as _time

    # deadline = 0 + 120; the retry check then sees 119.5, and 119.5 + 1.0s of backoff crosses it
    clock = iter([0.0, 0.0, 119.5])
    monkeypatch.setattr(_time, "monotonic", lambda: next(clock))
    req = _FakeRequests([_FakeResp({}, 503)])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(_FakeHTTPError):
            FabricClient("ws1", "it1").list_roles()
    assert len(req.get_calls) == 1  # the second attempt was never made
    assert no_real_sleep == []  # ...and no time was spent pretending it would be


def test_paginated_get_stops_at_the_loop_budget(monkeypatch):
    # A per-request timeout bounds ONE page. A continuation chain of slow-but-not-timing-out pages
    # is bounded only by PAGED_BUDGET_SECONDS, which raises rather than looping on.
    import time as _time

    clock = iter([0.0, 1.0, 500.0])  # deadline=120 -> page 1 ok -> past the budget
    monkeypatch.setattr(_time, "monotonic", lambda: next(clock))
    req = _FakeRequests([_FakeResp({"value": [{"name": "RoleA"}], "continuationUri": NEXT_PAGE})])
    with fabric_fakes(requests_obj=req, token="TKN"):
        c = FabricClient("ws1", "it1")
        with pytest.raises(DARHTTPError) as excinfo:
            c.list_roles()
    msg = str(excinfo.value)
    assert "120s budget" in msg
    assert "after 1 page(s)" in msg  # the page that DID land is not lost from the report
    assert NEXT_PAGE in msg  # names where it stopped
    assert len(req.get_calls) == 1  # stopped instead of fetching page 2


@pytest.mark.parametrize(
    "uri",
    [
        "http://api.fabric.microsoft.com/v1/x",  # scheme downgrade
        "https://evil.example/v1/x",  # another host entirely
        "https://api.fabric.microsoft.com@evil.example/v1/x",  # classic userinfo trick
        "https://user@api.fabric.microsoft.com/v1/x",  # userinfo on the right host
        "https://:secret@api.fabric.microsoft.com/v1/x",  # password-only userinfo
        "https://api.fabric.microsoft.com:8443/v1/x",  # non-default port
        "https://api.fabric.microsoft.com:nope/v1/x",  # unparseable authority
        "https://api.fabric.microsoft.com/other/x",  # path outside the API prefix
    ],
)
def test_a_poisoned_continuation_uri_is_refused_not_fetched(uri):
    # Every request this client makes carries the bearer token, so a continuationUri is
    # only ever followed onto the SAME Fabric API it came from — following anything else
    # would hand a workspace-capable credential to whatever host the response named.
    req = _FakeRequests([_FakeResp({"value": [{"name": "RoleA"}], "continuationUri": uri})])
    with fabric_fakes(requests_obj=req, token="T"):
        with pytest.raises(DARHTTPError) as excinfo:
            FabricClient("ws1", "it1").list_roles()
    assert uri in str(excinfo.value)  # the refusal names the URL it refused
    assert len(req.get_calls) == 1  # the poisoned URL was never fetched


def test_an_explicit_443_port_is_the_default_port_spelled_out():
    # :443 on https IS the default port — a fence that refused it would break a legal
    # continuation for zero security gain; urlsplit().port normalizes it to 443.
    cont = NEXT_PAGE.replace("api.fabric.microsoft.com/", "api.fabric.microsoft.com:443/")
    req = _FakeRequests(
        [
            _FakeResp({"value": [{"name": "RoleA"}], "continuationUri": cont}),
            _FakeResp({"value": [{"name": "RoleB"}]}),
        ]
    )
    with fabric_fakes(requests_obj=req, token="T"):
        roles = FabricClient("ws1", "it1").list_roles()
    assert [r["name"] for r in roles] == ["RoleA", "RoleB"]
    assert req.get_calls[1]["url"] == cont


def test_resolve_lakehouse_not_found_raises():
    req = _FakeRequests([_FakeResp({"value": [{"displayName": "Other", "id": "x"}]})])
    with fabric_fakes(requests_obj=req, token="TKN"):
        c = FabricClient("ws1", "it1")
        with pytest.raises(TargetNotFound):
            c.resolve_lakehouse("LH_Demo")


def test_resolve_lakehouse_ambiguous_raises():
    # two workspace lakehouses whose displayNames differ only by case -> ambiguous target
    req = _FakeRequests(
        [
            _FakeResp(
                {
                    "value": [
                        {"displayName": "LH_Demo", "id": "a"},
                        {"displayName": "lh_demo", "id": "b"},
                    ]
                }
            )
        ]
    )
    with fabric_fakes(requests_obj=req, token="TKN"):
        c = FabricClient("ws1", "it1")
        with pytest.raises(TargetAmbiguous):
            c.resolve_lakehouse("LH_Demo")


# ---------------------------------------------------------------------------------------------
# ResolversMocked — Target.resolve / run_by / run_id / tenant and Catalog's listers
# ---------------------------------------------------------------------------------------------

ATTACHED_CONTEXT = {"currentWorkspaceId": "ctx-ws", "defaultLakehouseId": "ctx-lh"}


def test_resolve_target_from_context():
    # Target.resolve takes NO args: the attached lakehouse comes from the runtime context
    with fabric_fakes(context=ATTACHED_CONTEXT):
        assert Target.resolve() == ("ctx-ws", "ctx-lh")


def test_resolve_target_accepts_a_lakehouse_workspace_echoing_the_notebooks():
    with fabric_fakes(context=dict(ATTACHED_CONTEXT, defaultLakehouseWorkspaceId="ctx-ws")):
        assert Target.resolve() == ("ctx-ws", "ctx-lh")


def test_resolve_target_blocks_cross_workspace_attachment():
    # A notebook can pin a lakehouse that lives in ANOTHER workspace. The DAR endpoint
    # addresses an item under its own workspace, so pairing this notebook's workspace with
    # that lakehouse's id names nothing — and the control tables would be read from a
    # different lakehouse than the roles are pushed to. Refuse before either happens.
    ctx = dict(ATTACHED_CONTEXT, defaultLakehouseWorkspaceId="other-ws")
    with fabric_fakes(context=ctx), pytest.raises(SystemExit) as excinfo:
        Target.resolve()
    assert "other-ws" in str(excinfo.value)  # names both sides
    assert "ctx-ws" in str(excinfo.value)


def test_cross_workspace_message_names_the_workspaces_when_the_runtime_gives_names():
    # An operator inside a Fabric notebook cannot resolve a workspace GUID without leaving
    # the notebook, and this message is written for exactly that reader. The runtime carries
    # defaultLakehouseWorkspaceName / currentWorkspaceName, so use them.
    ctx = dict(
        ATTACHED_CONTEXT,
        defaultLakehouseWorkspaceId="other-ws",
        defaultLakehouseWorkspaceName="Finance Lakehouses",
        currentWorkspaceName="Security Ops",
    )
    with fabric_fakes(context=ctx), pytest.raises(SystemExit) as excinfo:
        Target.resolve()
    msg = str(excinfo.value)
    assert "'Finance Lakehouses' (other-ws)" in msg  # name AND id, so either is actionable
    assert "'Security Ops' (ctx-ws)" in msg


def test_cross_workspace_message_falls_back_to_the_bare_guid_per_side():
    # The two sides degrade independently: a name is used where the runtime supplies one and the
    # bare guid where it does not, rather than an all-or-nothing formatting choice.
    ctx = dict(
        ATTACHED_CONTEXT,
        defaultLakehouseWorkspaceId="other-ws",
        currentWorkspaceName="Security Ops",  # only THIS side is named
    )
    with fabric_fakes(context=ctx), pytest.raises(SystemExit) as excinfo:
        Target.resolve()
    msg = str(excinfo.value)
    assert "(other-ws)" in msg and "'' (other-ws)" not in msg  # bare guid, never an empty quote
    assert "'Security Ops' (ctx-ws)" in msg


def test_ws_label_treats_a_blank_name_as_no_name():
    # A context key present but empty/whitespace must not render as "'' (guid)".
    assert Target.ws_label({"i": "g", "n": "   "}, "i", "n") == "g"
    assert Target.ws_label({"i": "g"}, "i", "n") == "g"
    assert Target.ws_label({"i": "g", "n": "Named"}, "i", "n") == "'Named' (g)"


def test_resolve_target_raises_when_no_lakehouse_attached():
    # no attached lakehouse in the context -> a clear guard error, not an AttributeError
    with fabric_fakes(context={}), pytest.raises(SystemExit) as excinfo:
        Target.resolve()
    assert "no lakehouse attached" in str(excinfo.value)


def test_resolve_target_blocks_when_fabric_runtime_is_unavailable():
    """A sensitive operation cannot silently fall back to a schema-only local setup."""
    with without_notebookutils(), pytest.raises(SystemExit) as excinfo:
        Target.resolve()
    assert "DAR control-data boundary" in str(excinfo.value)


def test_resolve_tenant_explicit_wins_without_reading_the_context():
    assert Target.tenant("T-EXPLICIT") == "T-EXPLICIT"


def test_resolve_tenant_ignores_a_context_tenant_id():
    # Live-verified on both execution paths: Fabric publishes NO 'tenantId' runtime-context
    # key, so spark.conf 'trident.tenant.id' is the only source. A context key of that name
    # is not the customer tenant and must not be read, even when something puts one there.
    with fabric_fakes(context={"tenantId": "T-CTX"}):
        assert Target.tenant() is None


def test_resolve_tenant_none_when_context_has_no_tenant_id():
    with fabric_fakes(context={}):
        assert Target.tenant() is None


def test_resolve_tenant_none_without_a_spark_session():
    # No active session -> nothing to read 'trident.tenant.id' from; the caller decides
    # whether its mode needs a tenant (generate/plan/apply do, and refuse without one).
    with without_notebookutils():
        assert Target.tenant() is None


def test_resolve_run_by_prefers_context_username():
    with fabric_fakes(context={"userName": "carol@example.com"}):
        assert Target.run_by(spark=None) == "carol@example.com"


def test_resolve_run_by_uses_user_id_when_there_is_no_user_name():
    # A pipeline run under a service principal or managed identity carries no userName —
    # there is no user — but the runtime does report the running principal's object id as
    # userId (verified live: that id resolves to the servicePrincipal Fabric ran as).
    # Without this layer every prod run logged the generic spark current_user(), so the
    # audit trail could not say WHICH identity deployed.
    with fabric_fakes(context={"userId": "c0000000-obj-id"}):
        assert Target.run_by(spark=build_spark()) == "c0000000-obj-id"


def test_resolve_run_by_prefers_user_name_over_user_id():
    # An interactive run reports both; the human-readable UPN wins.
    with fabric_fakes(context={"userName": "carol@example.com", "userId": "obj-id"}):
        assert Target.run_by(spark=build_spark()) == "carol@example.com"


def test_log_run_by_labels_the_id_and_never_replaces_it():
    # A pipeline run's run_by is a bare object id — there is no user to name. A member row may
    # LABEL that id with a display name, it may never REPLACE it: the object id is the only
    # part the runtime attests, and member_table is a pipeline-overridable parameter over a
    # writable schema, so a replaced id would make the audit trail's actor name spoofable.
    spark = build_spark()
    seed_sample_members(spark)
    assert (
        deploy_log(spark, SVC_LOADER).row("start", "success")["run_by"]
        == f"{SVC_LOADER_NAME} ({SVC_LOADER})"
    )


def test_log_run_by_keeps_the_id_when_the_member_row_has_no_name():
    # A member row whose member_name is NULL names nothing. Without the truthiness filter the
    # set would carry the literal string "None" and run_by would read "None (<object id>)".
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [member_cache_row("ServicePrincipal", None, SVC_LOADER)]
    assert Log.resolve_principal(spark, MEMBER_TABLE, SVC_LOADER) == SVC_LOADER


@pytest.mark.parametrize(
    "member_table", [MEMBER_TABLE, None], ids=["id not in the table", "no member table at all"]
)
def test_log_run_by_keeps_the_id_when_the_member_table_cannot_name_it(member_table):
    spark = build_spark()
    seed_sample_members(spark)
    unlisted = "99999999" + "-9999-4999-8999-999999999999"
    log = deploy_log(spark, unlisted, member_table=member_table)
    assert log.row("start", "success")["run_by"] == unlisted


def test_log_run_by_leaves_a_upn_untouched():
    # a UPN is already a name — no lookup
    spark = build_spark()
    seed_sample_members(spark)
    log = deploy_log(spark, "carol@example.com")
    assert log.row("start", "success")["run_by"] == "carol@example.com"


def test_log_run_by_survives_an_unreadable_member_table():
    class _AngrySpark:
        def table(self, _name):
            raise RuntimeError("member table unavailable")

    assert Log.resolve_principal(_AngrySpark(), MEMBER_TABLE, SVC_LOADER) == SVC_LOADER


def test_log_run_by_keeps_the_id_when_one_id_carries_two_names():
    # Bad data generate DOES reject (the duplicate-id guard covers it, and
    # test_cache_one_id_under_two_names_blocks_generate pins exactly this shape), but
    # Log.resolve_principal is a separate read path: it queries the member table directly on
    # EVERY mode, so it still meets the shape when the table is edited after a generate, or on a
    # mode that never loads the cache. Picking either name would make run_by depend on row order,
    # so it keeps the id.
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [
        member_cache_row("ServicePrincipal", "svc-deploy", SVC_LOADER),
        member_cache_row("ManagedIdentity", "mi-deploy", SVC_LOADER),
    ]
    assert deploy_log(spark, SVC_LOADER).row("start", "success")["run_by"] == SVC_LOADER


@pytest.mark.parametrize("member_type", ["ServicePrincipal", "ManagedIdentity", "Group"])
def test_log_run_by_matches_on_the_id_regardless_of_member_type(member_type):
    # The objectId alone identifies the row: it is unique across principal types in Entra, and
    # the runtime cannot supply a type anyway (idtyp says only user vs app).
    spark = build_spark()
    spark._store[MEMBER_TABLE] = [member_cache_row(member_type, "deploy-identity", SVC_LOADER)]
    log = deploy_log(spark, SVC_LOADER)
    assert log.row("start", "success")["run_by"] == f"deploy-identity ({SVC_LOADER})"


def test_resolve_run_by_falls_back_to_spark_current_user():
    with fabric_fakes(context={}):  # neither userName nor userId -> spark path
        assert Target.run_by(spark=build_spark()) == "tester@example.com"


def test_resolve_run_by_none_when_current_user_empty():
    class _EmptyUserSpark:
        def sql(self, q):
            return types.SimpleNamespace(collect=lambda: [])  # no current_user row

    with fabric_fakes(context={}):  # no userName -> spark path -> empty -> None
        assert Target.run_by(spark=_EmptyUserSpark()) is None


def test_notebook_run_id_uses_activity_id():
    with fabric_fakes(context={"activityId": "act-99"}):
        assert Target.run_id() == "act-99"


def test_notebook_run_id_uuid_when_no_context():
    # absent activityId -> uuid fallback (context.get returns None, no exception)
    with fabric_fakes(context={}):
        assert re.match(r"^[0-9a-f-]{36}$", Target.run_id())


def test_notebook_run_id_uuid_when_notebookutils_missing():
    # no notebookutils at all -> import raises -> except branch -> uuid fallback
    with without_notebookutils():
        assert re.match(r"^[0-9a-f-]{36}$", Target.run_id())


def test_onelake_uri_maps_logical_scope_path_to_guid_uri():
    # A logical DAR scope path is NOT a filesystem path: notebookutils resolves a leading
    # slash against the WORKSPACE root, and a tenant with friendly-name support disabled
    # rejects name-based OneLake paths outright (400 FriendlyNameSupportDisabled).
    assert (
        Catalog.onelake_uri("WS-GUID", "ITEM-GUID", "/Files/raw")
        == "abfss://WS-GUID@onelake.dfs.fabric.microsoft.com/ITEM-GUID/Files/raw"
    )


def test_onelake_uri_tolerates_no_leading_slash_and_a_trailing_one():
    assert (
        Catalog.onelake_uri("WS-GUID", "ITEM-GUID", "Files/")
        == "abfss://WS-GUID@onelake.dfs.fabric.microsoft.com/ITEM-GUID/Files"
    )


@pytest.mark.parametrize(
    ("ws", "item"), [("", "ITEM-GUID"), ("WS-GUID", "")], ids=["no workspace", "no item"]
)
def test_onelake_uri_missing_target_id_raises(ws, item):
    with pytest.raises(ValidationError):
        Catalog.onelake_uri(ws, item, "/Files")


def test_fs_folder_lister_filters_to_directories():
    # The fake keys entries by the path the lister actually passes to fs.ls — so this
    # asserts the lister sends the resolved OneLake URI, not the logical scope path.
    uri = "abfss://WS-GUID@onelake.dfs.fabric.microsoft.com/ITEM-GUID/Files/raw"
    entries = {uri: [("region_a/", True), ("region_b", True), ("note.txt", False)]}
    with fabric_fakes(fs_entries=entries):
        # dirs only, trailing / stripped
        assert Catalog.fs_folder_lister("WS-GUID", "ITEM-GUID")("/Files/raw") == [
            "region_a",
            "region_b",
        ]


def test_export_lister_reads_dir_and_tolerates_missing():
    d = tempfile.mkdtemp()
    open(os.path.join(d, "b.csv"), "w").close()
    open(os.path.join(d, "a.csv"), "w").close()
    assert Catalog._export_lister(d) == ["a.csv", "b.csv"]  # sorted real listing
    assert Catalog._export_lister(os.path.join(d, "nope")) == []  # missing dir -> []


def test_resolve_tenant_from_spark_trident_conf():
    # tenant auto-resolves from spark.conf 'trident.tenant.id' when set; a None conf value and no
    # active session both fall through to the notebookutils context (None here).
    import pyspark.sql as _pysql

    class _Conf:
        def __init__(self, val):
            self._val = val

        def get(self, key, default=None):
            assert key == "trident.tenant.id"
            return self._val

    class _Sess:
        def __init__(self, val):
            self.conf = _Conf(val)

    class _SparkSession:
        _active = None

        @classmethod
        def getActiveSession(cls):
            return cls._active

    saved = getattr(_pysql, "SparkSession", None)
    _pysql.SparkSession = _SparkSession
    try:
        with fabric_fakes(context={}):  # no tenantId in context -> the spark conf decides
            _SparkSession._active = _Sess("T-SPARK")
            assert Target.tenant() == "T-SPARK"  # trident.tenant.id wins
            _SparkSession._active = _Sess(None)  # conf empty -> falls through to context -> None
            assert Target.tenant() is None
            _SparkSession._active = None  # no active session -> _s is None -> falls through
            assert Target.tenant() is None
    finally:
        if saved is None:
            del _pysql.SparkSession
        else:
            _pysql.SparkSession = saved


def test_fake_where_treats_quoted_and_and_in_as_data_not_syntax():
    # The pushdown equivalence tests lean on these fakes as their oracle, so a predicate
    # must never be silently mangled: AND / IN inside a string literal is data.
    rows = [
        {"member_name": "A AND B", "mode": "apply"},
        {"member_name": "x IN (y)", "mode": "plan"},
    ]
    frame = _FakeLogDataFrame(rows)
    got = [r.member_name for r in frame.where("member_name = 'A AND B'").collect()]
    assert got == ["A AND B"]  # the old splitter turned this into a never-match
    got = [r.member_name for r in frame.where("member_name = 'x IN (y)'").collect()]
    assert got == ["x IN (y)"]  # ...and the IN regex used to fire inside the literal
    got = [
        r.member_name for r in frame.where("member_name = 'A AND B' AND mode = 'apply'").collect()
    ]
    assert got == ["A AND B"]  # a real AND outside quotes still splits


def test_fake_where_raises_on_predicates_it_cannot_honor():
    # A fake that DROPS a predicate silently is how a dropped filter passes review — the
    # same principle as select() raising on unknown columns. NOT IN is unreachable from the
    # runtime today; it raises so a future author cannot lean on a no-op.
    frame = _FakeLogDataFrame([{"mode": "plan"}])
    with pytest.raises(ValueError):
        frame.where("mode NOT IN ('plan')")
    with pytest.raises(ValueError):
        frame.where("run_at >= '2026'")  # unsupported operator: loud, never a no-op
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    with pytest.raises(ValueError):
        spark.table(CONFIG_TABLE).where("role_name LIKE 'Sales%'")


def test_fake_dataframe_where_equality_is_null_aware():
    # mirrors _FakeLogDataFrame's already-fixed branch: a NULL cell never matches an
    # equality (str(None) == 'None' was a possible match before).
    spark = build_spark()
    spark._store["t.rows"] = [{"a": "x"}, {"a": None}, {"a": "None"}]
    got = [r.asDict() for r in spark.table("t.rows").where("a = 'None'").collect()]
    assert got == [{"a": "None"}]  # only the LITERAL 'None' string row — never the NULL


def test_fake_select_raises_on_unknown_columns_like_the_engine():
    # Issue #7: real PySpark raises AnalysisException for a column the schema does not hold;
    # the fake used to fill it with None, hiding every fail-closed path keyed on a missing
    # column from the suite.
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    frame = spark.table(CONFIG_TABLE)
    assert frame.select("role_name").columns == ["role_name"]  # known columns still project
    with pytest.raises(ValueError) as excinfo:
        frame.select("role_name", "no_such_column")
    assert "no_such_column" in str(excinfo.value)


def test_canonical_columns_are_lazy_and_equivalent_to_the_eager_listing():
    """Issue #1: canon['columns'] lists per table on FIRST access, memoized — but through the
    dict surface its consumers use it must answer exactly what the eager dict answered."""
    spark = build_spark()
    calls = []
    real = type(spark.catalog).listColumns

    def spy(self, name):
        calls.append(name)
        return real(self, name)

    with mock.patch.object(type(spark.catalog), "listColumns", spy):
        canon = Catalog.canonical(spark)
        assert calls == []  # building the snapshot lists NO columns at all
        # equivalence with the eager behavior, through every access pattern consumers use
        for key, canonical_name in canon["tables"].items():
            expected = [c.name for c in spark.catalog.listColumns(canonical_name)]
            assert canon["columns"].get(key) == expected
            assert canon["columns"][key] == expected
            assert key in canon["columns"]
        n_after_full_walk = len(calls)
        assert canon["columns"].get("sales.orders") is not None  # memoized —
        assert len(calls) == n_after_full_walk  # — a re-read lists nothing new
    assert canon["columns"].get("no.such_table") is None  # unknown reads as absent
    assert "no.such_table" not in canon["columns"]
    with pytest.raises(KeyError):
        canon["columns"]["no.such_table"]
    # deliberately NOT iterable — __getitem__ alone would trigger the legacy sequence
    # protocol (list() -> KeyError(0), bool() always True); the refusal names the reason
    with pytest.raises(TypeError) as excinfo:
        list(canon["columns"])
    assert "not iterable" in str(excinfo.value)


def test_generate_lists_columns_only_for_config_referenced_tables():
    # the point of #1: the sample config's column checks touch the sales tables it grants —
    # never hr.payroll / ref.calendar, which merely exist in the lakehouse.
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    seed_sample_members(spark)
    calls = []
    real = type(spark.catalog).listColumns

    def spy(self, name):
        calls.append(name)
        return real(self, name)

    with mock.patch.object(type(spark.catalog), "listColumns", spy):
        run_generate(make_dep(spark, client, "generate"))
    assert calls, "the RLS column checks genuinely consulted the catalog"
    assert set(calls) <= {"sales.orders", "sales.leads", "sales.returns"}
    assert "hr.payroll" not in calls and "ref.calendar" not in calls


# ---------------------------------------------------------------------------------------------
# CatalogAndAuditMocked — Catalog.canonical ambiguity guard + Log's exception-safe reads/writes
# ---------------------------------------------------------------------------------------------


def test_build_canonical_dict_rejects_case_ambiguous_tables():
    spark = FakeSpark({"s": ["Orders", "orders"]}, {"s.orders": [], "s.Orders": []})
    with pytest.raises(ValidationError) as excinfo:
        Catalog.canonical(spark)
    assert "differing only by case" in str(excinfo.value)


def test_audit_write_empty_is_noop():
    audit = Log(build_spark(), LOG_TABLE, "B", "R", "dev", "plan", "W", "L", run_by="x")
    assert audit.write([]) is None  # early return, nothing created


class _BoomSpark:
    """A spark whose every table read raises — the exception-safe read paths."""

    def __init__(self, message):
        self._message = message

    def table(self, name):
        raise RuntimeError(self._message)


def test_find_plan_record_returns_none_on_table_error():
    audit = Log(
        _BoomSpark("log table does not exist yet"),
        LOG_TABLE,
        "B",
        "R",
        "dev",
        "apply",
        "W",
        "L",
        run_by="x",
    )
    assert audit.find_plan_record("HASH", "MHASH") is None


def test_has_run_complete_is_false_on_an_unreadable_log():
    audit = Log(
        _BoomSpark("log table does not exist yet"),
        LOG_TABLE,
        "B",
        "R",
        "dev",
        "generate",
        "W",
        "L",
        run_by="x",
    )
    assert audit.has_run_complete("HASH") is False  # repair row gets written — the safe side


def test_find_plan_record_skips_unparseable_message():
    rows = [
        {
            "mode": "plan",
            "action": "complete",
            "status": "success",
            "env": "dev",
            "config_hash": "HASH",
            "mapping_hash": "MHASH",
            "run_at": "2026-01-01",
            "message": "not-json{",
        }
    ]
    audit = Log(_FakeLogSpark(rows), LOG_TABLE, "B", "R", "dev", "apply", "W", "L", run_by="x")
    assert audit.find_plan_record("HASH", "MHASH") is None  # bad JSON skipped -> no record


def test_grant_provenance_empty_on_table_error():
    audit = Log(_BoomSpark("no log"), LOG_TABLE, "B", "R", "dev", "show", "W", "L", run_by="x")
    assert audit.grant_provenance() == {}


# ---------------------------------------------------------------------------------------------
# DeploymentGuardsMocked — guard/reject paths the happy-path mock pipeline doesn't hit
# ---------------------------------------------------------------------------------------------


def test_generate_blocks_on_validation_errors():
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    bad = dict(sample_config_rows()[0], role_name="1illegal")  # invalid role_name (rule B1)
    spark._store[CONFIG_TABLE] = [bad]
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)


def test_plan_refuses_empty_mapping():
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    # mapping table exists (created by setup) but was never populated by generate
    with pytest.raises(SystemExit) as excinfo:
        make_dep(spark, client, "plan").plan()
    assert "mapping table empty" in str(excinfo.value)


def test_setup_unqualified_control_table_is_refused_before_schema_or_table_write():
    # The privacy boundary cannot map an unqualified table to one unambiguous OneLake path.
    spark, client = build_spark(), FakeFabricClient([])
    audit = Log(spark, "plainlog", "B", "R", "dev", "setup", "W", "L", run_by="x")
    dep = Deployment(
        spark,
        client,
        audit,
        TENANT,
        "plaincfg",
        "plainmap",
        "Files/security/o",
        "W",
        "L",
        member_table="plainmember",
    )
    with pytest.raises(ControlDataGuardError, match="two-part"):
        dep.setup()
    assert spark._store == {}


def test_reject_swallows_logging_failure_but_still_aborts():
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "plan")

    class _BoomAudit:
        def set_config_provenance(self, *a, **k):
            raise RuntimeError("audit backend down")

    dep.audit = _BoomAudit()  # its config_hash property still needs short_rows -> author config
    spark._store[CONFIG_TABLE] = sample_config_rows()
    with pytest.raises(SystemExit) as excinfo:
        dep._reject("STALE: forced")
    assert "STALE: forced" in str(excinfo.value)  # guard still raised despite log failure


def test_find_plan_record_mode_scope_is_enforced():
    # Issue #7 follow-through: the fakes' where() used to treat `mode IN (...)` as a no-op,
    # so this scope was untestable — a complete/success row from a NON-plan mode carrying
    # valid plan JSON must not open the gate.
    rows = [
        {
            "mode": "apply",  # not plan/rollback — a complete row apply itself wrote
            "action": "complete",
            "status": "success",
            "env": "dev",
            "config_hash": "HASH",
            "mapping_hash": "MHASH",
            "run_at": "2026-01-01",
            "message": '{"plan": {"RoleA": "create"}}',
        }
    ]
    audit = Log(_FakeLogSpark(rows), LOG_TABLE, "B", "R", "dev", "apply", "W", "L", run_by="x")
    assert audit.find_plan_record("HASH", "MHASH") is None


def test_has_run_complete_mode_scope_is_enforced():
    # same fidelity win: a plan-mode complete row must not satisfy the generate-side probe,
    # while generate- and rollback-mode rows do.
    base = {
        "action": "complete",
        "status": "success",
        "env": "dev",
        "mapping_hash": "MH",
        "run_at": "2026-01-01",
        "message": "generate: ...",
    }
    for mode, expected in (("plan", False), ("generate", True), ("rollback", True)):
        audit = Log(
            _FakeLogSpark([dict(base, mode=mode)]),
            LOG_TABLE,
            "B",
            "R",
            "dev",
            "generate",
            "W",
            "L",
            run_by="x",
        )
        assert audit.has_run_complete("MH") is expected, mode


def test_a_conflict_record_write_failure_never_masks_the_conflict():
    # the swallow in _record_push_conflict mirrors _reject's: a broken audit backend must
    # never displace the 412 the operator needs to see (the caller re-raises it).
    spark, client = build_spark(), FakeFabricClient([])
    dep = make_dep(spark, client, "apply")

    class _BoomWriteAudit:
        def row(self, action, status, **fields):
            return {"action": action, "status": status, **fields}

        def write(self, rows):
            raise RuntimeError("audit backend down")

    dep.audit = _BoomWriteAudit()
    dep._header_rows = []
    dep._record_push_conflict(DARConflictError("PUT ... -> 412"))  # must not raise


def test_plan_gate_refuses_out_of_order_use_without_a_mapping_fingerprint():
    # _desired_state sets self._mapping_hash before any caller reaches the gate; driving the
    # gate directly (out of order) must REFUSE — a guard-class SystemExit, never an
    # AttributeError, and never an unlock on an unfingerprinted mapping.
    spark, client = build_spark(), FakeFabricClient([])
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows()
    dep = make_dep(spark, client, "apply")
    with pytest.raises(SystemExit) as excinfo:
        dep._require_plan_and_no_drift({})
    assert "no mapping fingerprint" in str(excinfo.value)


# ---------------------------------------------------------------------------------------------
# LakehouseTargetGuard — generate's lakehouse target guard (_resolve_lakehouse_target +
# FabricClient.resolve_lakehouse + Target._single_named): config.lakehouse_name must name the
# ATTACHED lakehouse. A different-case name resolves and stamps the CANONICAL spelling into the
# mapping; not-found / ambiguous / id-mismatch / multi-lakehouse / blank all block generate with a
# naming reason and a forensic 'rejected' row.
# ---------------------------------------------------------------------------------------------


def lakehouse_ready(client, rows=None):
    spark = build_spark()
    make_dep(spark, client, "setup").setup()
    spark._store[CONFIG_TABLE] = sample_config_rows() if rows is None else rows
    seed_sample_members(spark)
    return spark


def test_case_insensitive_name_stamps_canonical_spelling():
    # config declares the lakehouse in a DIFFERENT case than the workspace's actual displayName;
    # generate resolves it and stamps the fake's CANONICAL spelling into the mapping.
    client = FakeFabricClient([], lakehouse_name="LH_Demo")  # canonical spelling
    rows = [dict(r, lakehouse_name="lh_DEMO") for r in sample_config_rows()]  # different case
    spark = lakehouse_ready(client, rows)
    run_generate(make_dep(spark, client, "generate", lh="LH_Demo"))
    stamped = {r["lakehouse_name"] for r in spark._store[MAPPING_TABLE]}
    assert stamped == {"LH_Demo"}  # canonical spelling, not the config's "lh_DEMO"


def test_lakehouse_not_found_blocks():
    client = FakeFabricClient([], lakehouses=[])  # workspace has no lakehouses
    spark = lakehouse_ready(client)
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert "not found" in str(excinfo.value)
    assert "LH_Demo" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []
    rejected = [r for r in spark._store[LOG_TABLE] if r.get("status") == "rejected"]
    assert rejected  # blocked generate leaves a forensic 'rejected' row


def test_lakehouse_ambiguous_case_variants_block():
    # two workspace lakehouses whose displayNames differ only by case -> ambiguous target
    client = FakeFabricClient(
        [],
        lakehouses=[
            {"displayName": "LH_Demo", "id": "lh-demo-id"},
            {"displayName": "lh_demo", "id": "lh-other-id"},
        ],
    )
    spark = lakehouse_ready(client)
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert "differing only by case" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []


def test_lakehouse_resolving_to_a_different_item_blocks():
    # config.lakehouse_name resolves to a lakehouse whose id != the ATTACHED item_id -> block,
    # reason names both ids (an apply to the wrong lakehouse is data exposure).
    client = FakeFabricClient(
        [], item_id="lh-demo-id", lakehouses=[{"displayName": "LH_Demo", "id": "lh-DIFFERENT-id"}]
    )
    spark = lakehouse_ready(client)
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert "lh-DIFFERENT-id" in str(excinfo.value)
    assert "lh-demo-id" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []


def test_multiple_distinct_lakehouses_block():
    # config rows name TWO different lakehouses -> one lakehouse per config, block.
    client = FakeFabricClient([])
    rows = sample_config_rows()
    rows[1] = dict(rows[1], lakehouse_name="Other_LH")
    spark = lakehouse_ready(client, rows)
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert "more than one lakehouse" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []


def test_blank_lakehouse_name_is_validation_error():
    # every config row with a blank lakehouse_name -> Generate.rows validation error AND the
    # _resolve_lakehouse_target "no declared name" branch; generate blocks.
    client = FakeFabricClient([])
    rows = [dict(r, lakehouse_name=None) for r in sample_config_rows()]
    spark = lakehouse_ready(client, rows)
    with pytest.raises(SystemExit) as excinfo:
        run_generate(make_dep(spark, client, "generate"))
    assert "generate blocked" in str(excinfo.value)
    assert "lakehouse_name is required" in str(excinfo.value)
    assert spark._store[MAPPING_TABLE] == []
