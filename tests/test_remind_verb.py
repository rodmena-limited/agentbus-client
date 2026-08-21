"""`agentbus remind` — scheduled reminders.

Plan: ~/.claude/plans/i-d-like-to-ensure-sleepy-sonnet.md. Backend + RunFlow
integration is the server's half; this covers the client's.

TWO THINGS THESE TESTS EXIST TO PREVENT, both of which this workspace has paid
for in the last day:

1. A REMINDER BODY STORED IN PLAINTEXT. A reminder sits at rest until it is due,
   so an unsealed one is days of exposure on a workspace whose whole purpose is
   that there is none. That is the defect closed for MCP drafts on 2026-08-21.
2. SYNC/ASYNC DRIFT. That pair has diverged before — `phonebook(label=)` landed
   on one and not the other, and async `read` once skipped unsealing entirely. A
   caller switching to async must not silently lose the seal.
"""

from __future__ import annotations

import datetime as dt

import pytest

from agentbus_client._timefmt import _as_instant, _duration_seconds
from agentbus_client.cli._parser import build_parser

# ------------------------------------------------------------------ durations

@pytest.mark.parametrize(
    "given,seconds",
    [("90m", 5400), ("2h", 7200), ("3d", 259200), ("45", 45), (3600, 3600)],
)
def test_durations_coerce_to_seconds(given, seconds):
    assert _duration_seconds(given) == seconds


def test_timedelta_passes_through():
    assert _duration_seconds(dt.timedelta(hours=2)) == 7200


def test_none_stays_none_so_callers_can_splat_it():
    """None must survive, or every caller needs a branch to decide whether the
    key belongs in the payload at all."""
    assert _duration_seconds(None) is None
    assert _as_instant(None) is None


def test_bool_is_refused_even_though_it_is_an_int():
    """`--delay True` is a bug, not a 1-second delay. bool subclasses int, so
    without this it would silently mean one second."""
    with pytest.raises(ValueError):
        _duration_seconds(True)


def test_nonsense_duration_is_refused_locally():
    """Caught here, not as a confusing server 422."""
    with pytest.raises(ValueError):
        _duration_seconds("tuesday")


# ------------------------------------------------------------------- instants

def test_aware_datetime_converts_to_utc():
    moment = dt.datetime(2026, 8, 21, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=2)))
    assert _as_instant(moment) == "2026-08-21T07:00:00Z"


def test_subsecond_precision_survives():
    """RunFlow honours fire_at exactly; truncating it fires EARLY into a
    not-yet-due row, which reads as a green run that accomplished nothing.
    Their mail-api incident was exactly this."""
    moment = dt.datetime(2026, 8, 21, 9, 0, 25, 822000, tzinfo=dt.timezone.utc)
    assert "25.822" in _as_instant(moment)


def test_a_datetime_is_never_sent_as_a_bare_local_string():
    """A naive datetime must be resolved against the local zone and converted,
    not shipped as-is for a server in another zone to read as UTC. An
    off-by-hours reminder looks like flakiness, not like a defect."""
    naive = dt.datetime(2026, 8, 21, 9, 0)
    assert _as_instant(naive).endswith("Z")


def test_a_wrong_type_is_refused_rather_than_stringified():
    with pytest.raises(ValueError):
        _as_instant(12345)


# ----------------------------------------------------------------- the parser

def _parse(*argv):
    return build_parser().parse_args(["remind", *argv])


def test_target_is_optional_because_self_notes_are_the_common_case():
    assert _parse("-m", "x", "--delay", "2h").target is None


def test_every_documented_flag_is_accepted():
    args = _parse(
        "-m", "ship it", "--target", "alice", "--delay", "2h",
        "--expire", "3d", "--repeat", "daily",
        "--repeat-until", "2026-12-01", "--timezone", "Europe/London",
    )
    assert args.target == "alice" and args.delay == "2h" and args.expire == "3d"
    assert args.repeat == "daily" and args.repeat_until == "2026-12-01"
    assert args.timezone == "Europe/London"


def test_remind_and_reminders_are_different_commands():
    """`reminders` is ack-CHASING (#265): it nags about messages already
    delivered. `remind` schedules one not yet sent. Similar words, opposite
    directions, and conflating them would make both harder to reason about."""
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert {"remind", "reminds", "reminders"} <= set(sub.choices)


# --------------------------------------------------------- the sealing rule

class _Spy:
    def __init__(self, sealed_marker="-----BEGIN AGE ENCRYPTED FILE-----"):
        self.body = None
        self.marker = sealed_marker

    def __call__(self, method, path, **kw):
        if path == "/v1/reminders":
            self.body = kw.get("json")
        return {"id": "01M0X", "due_at": "2026-08-21T09:00:00Z"}


def _bus_with_spy():
    from agentbus_client.client import AgentBus

    bus = AgentBus(api_key="ab_sk_x_y", base_url="http://localhost")
    spy = _Spy()
    bus._request = spy
    return bus, spy


def test_a_self_note_seals_to_self_not_to_a_recipient():
    """Author and recipient are the same agent, so `_seal_to_self` is right —
    and `_seal_if_needed` would have no recipient to resolve against."""
    bus, _spy = _bus_with_spy()
    called = {}

    def fake_seal_to_self(body, agent):
        called["self"] = True
        return body        # must return the BODY; `x or body` returns True here

    bus._seal_to_self = fake_seal_to_self
    bus.remind("note", delay=dt.timedelta(hours=2))
    assert called.get("self"), "a self-note must seal to the author's own key"


def test_a_targeted_reminder_seals_to_THAT_agent():
    """Sealing a targeted reminder to SELF would deliver something the recipient
    cannot open — data loss wearing security's clothes, and it would only be
    discovered when the reminder came due."""
    bus, _spy = _bus_with_spy()
    seen = {}

    def fake_seal(body, agent, **kw):
        seen["resolve"] = kw.get("resolve_body")
        return body, None

    bus._seal_if_needed = fake_seal
    bus.remind("note", target="alice", delay=dt.timedelta(hours=2))
    assert seen["resolve"]["to"] == ["alice"]


def test_the_payload_carries_the_schedule_not_a_local_clock_guess():
    bus, spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    bus.remind("note", delay="2h", expire="3d", repeat="daily")
    assert spy.body["delay_seconds"] == 7200
    assert spy.body["repeat"] == "daily"
    # CONTRACT: the server takes `expires_at` (absolute), not a duration —
    # "3d" is ambiguous about 3 days from what, and for a reminder due next
    # week with a 3-day expiry those readings are five days apart.
    assert "expire_seconds" not in spy.body
    assert spy.body["expires_at"].endswith("Z")


def test_absent_options_are_omitted_rather_than_sent_as_null():
    """Sending `repeat: null` on every reminder makes a plain one-shot
    indistinguishable from a recurrence someone cleared — the same shape as the
    labels:{} defect fixed in 0.9.46."""
    bus, spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    bus.remind("note", delay="2h")
    for absent in ("repeat", "repeat_until", "timezone", "expires_at", "due_at"):
        assert absent not in spy.body, f"{absent} should be omitted, not null"


# -------------------------------------------------------------- twin parity

def test_the_async_twin_has_the_same_signature():
    """Asserted, not assumed. This pair has drifted on smaller details."""
    import inspect

    from agentbus_client.client import AgentBus, AsyncAgentBus

    sync = inspect.signature(AgentBus.remind).parameters
    async_ = inspect.signature(AsyncAgentBus.remind).parameters
    assert list(sync) == list(async_), "remind() drifted between the twins"


@pytest.mark.parametrize("verb", ["remind", "reminds", "cancel_remind"])
def test_both_surfaces_carry_every_verb(verb):
    from agentbus_client.client import AgentBus, AsyncAgentBus

    assert callable(getattr(AgentBus, verb, None))
    assert callable(getattr(AsyncAgentBus, verb, None))


def test_the_client_never_calls_the_scheduler():
    """Operator ruling: one platform credential, held server-side, never on a
    user's machine. The client posts to its own backend and stops there.

    ASSERTS THE COUPLING, NOT THE WORD. An earlier draft grepped for the
    scheduler's NAME and failed on seven pre-existing comments that credit a
    peer AGENT of the same name — the same over-broad-grep mistake made against
    llms.txt earlier the same day. What matters is whether the client can REACH
    the scheduler: its base URL, its credential, or its API paths.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "agentbus_client"
    forbidden = (
        "runflow.rodmena.app",      # its host
        "RUNFLOW_API_KEY",          # its credential
        "/api/v1/timers",           # its API
        "/api/v1/schedules",
        "rak_",                     # its key prefix
    )
    for path in root.rglob("*.py"):
        body = path.read_text()
        for token in forbidden:
            assert token not in body, (
                f"{path.name} can reach the scheduler directly ({token!r}); the "
                f"client must only talk to its own backend"
            )


# ------------------------------------------------- the agreed wire contract
#
# Settled with the backend on thread 01M0H0W8H0RZG54QTYTTZV5DGV. These assert
# the SHAPE WE AGREED, so a later refactor cannot quietly drift back to the one
# I proposed before asking.


def test_delay_and_at_are_refused_together():
    """The server 422s the pair; catching it here names the conflict rather
    than relaying a status code."""
    bus, _spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    with pytest.raises(ValueError, match="not both"):
        bus.remind("note", delay="2h", at="2026-08-21T09:00:00Z")


def test_delay_is_sent_as_seconds_for_the_server_to_resolve():
    """SERVER-SIDE RESOLUTION WINS, deliberately. A laptop 40s fast would
    otherwise fire everything 40s early and nobody would ever diagnose it."""
    bus, spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    bus.remind("note", delay="2h")
    assert spy.body["delay_seconds"] == 7200
    assert "due_at" not in spy.body, "the client must not resolve the instant itself"


def test_an_absolute_at_is_sent_as_due_at():
    """Only for a genuinely absolute instant the user typed."""
    bus, spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    bus.remind("note", at="2026-08-21T09:00:00Z")
    assert spy.body["due_at"] == "2026-08-21T09:00:00Z"
    assert "delay_seconds" not in spy.body


def test_an_absolute_expiry_date_passes_through_unconverted():
    """`--expire 3d` is a duration; `--expire 2026-12-01` is already an
    instant. Both spellings are accepted and must not be confused."""
    from agentbus_client._timefmt import _expiry_instant

    assert _expiry_instant("2026-12-01T00:00:00Z") == "2026-12-01T00:00:00Z"
    assert _expiry_instant("3d").endswith("Z")


def test_timezone_is_never_defaulted_by_the_client():
    """The server defaults to UTC and SAYS SO. A client guessing the local zone
    would make the same reminder mean different things on two machines."""
    bus, spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    bus.remind("note", repeat="daily")
    assert "timezone" not in spy.body


def test_repeat_until_is_refused_because_the_server_forbids_it():
    """VERIFIED AGAINST THE SERVED SCHEMA, not against my proposal.

    The agreed contract included `repeat_until`; the SERVED RemindRequest does
    not carry it and forbids extra inputs. Confirmed live with a full-scope key:

        POST /v1/reminders {..., "repeat_until": "..."}
        -> 422 repeat_until: Extra inputs are not permitted

    So sending it fails the ENTIRE create, not just that field. Refusing locally
    names the reason; passing it through would turn a recurring reminder into a
    confusing 422 about a field the user did not know was optional.

    This is why the served artifact gets diffed rather than the agreement read:
    the contract and the deployment disagreed, and only the deployment matters.
    """
    bus, _spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    with pytest.raises(ValueError, match="not accepted by the server"):
        bus.remind("note", repeat="daily", repeat_until="2026-12-01")


def test_the_payload_carries_only_fields_the_server_accepts():
    """The whole payload, checked against the SERVED RemindRequest field set.

    A field the server forbids fails the entire create, so this asserts the
    envelope rather than any single key — a new field added here without a
    server that accepts it would break every reminder, not just the one using it.
    """
    served = {
        "target", "subject", "text", "sealed",
        "delay_seconds", "due_at", "expires_at", "repeat", "timezone",
    }
    bus, spy = _bus_with_spy()
    bus._seal_to_self = lambda body, agent: body
    bus.remind("note", delay="2h", expire="3d", repeat="daily", timezone="Europe/London")
    assert set(spy.body) <= served, f"sends fields the server forbids: {set(spy.body) - served}"


# ------------------------------------------------- listing, live-by-default
#
# Farshid: "the client must have the ability to list reminders/crons (otherwise
# they can't cancel the old ones)". The verb existed; what it lacked was making
# the LIVE ones findable — after a handful of one-shots the entries you can
# still act on are a minority of the output, and a recurring reminder, the thing
# you most need to find because it fires forever until stopped, is buried among
# dead rows that read almost identically.


def _reminds_output(rows, show_all=False, monkeypatch=None):
    """Render the listing with a stubbed bus.

    MONKEYPATCH, NOT A BARE ASSIGNMENT. An earlier version assigned
    `_remind._common._bus = ...` directly, which permanently replaced the
    accessor on a SHARED module for the rest of the session — two unrelated
    watcher tests then failed because they got this stub instead of their own.
    A test that breaks other tests is worse than one that fails: the damage
    lands somewhere nobody is looking, and the file that caused it looks green.
    """
    from agentbus_client.cli import _remind

    class _A:
        json = False
        all = show_all
        agent = None

    bus = type("B", (), {"reminds": lambda self, all=False: rows})()
    monkeypatch.setattr(_remind._common, "_bus", lambda args: bus)
    return _remind.cmd_reminds(_A())


def test_finished_reminders_are_hidden_by_default(capsys, monkeypatch):
    rows = [
        {"id": "A", "state": "cancelled", "due_at": "2026-08-21T03:00:00Z"},
        {"id": "B", "state": "scheduled", "due_at": "2026-08-21T09:00:00Z"},
    ]
    _reminds_output(rows, monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert "B" in out
    assert "A" not in out, "a cancelled reminder crowds out the ones you can act on"


def test_hidden_rows_are_counted_never_silently_dropped(capsys, monkeypatch):
    """Filtering the display must announce itself. A list that quietly omits
    rows is how someone concludes a reminder is gone when it is merely fired."""
    rows = [
        {"id": "A", "state": "cancelled", "due_at": "2026-08-21T03:00:00Z"},
        {"id": "B", "state": "scheduled", "due_at": "2026-08-21T09:00:00Z"},
    ]
    _reminds_output(rows, monkeypatch=monkeypatch)
    assert "1 finished reminder(s) hidden" in capsys.readouterr().out


def test_recurring_reminders_sort_first(capsys, monkeypatch):
    """They fire forever until cancelled, so they are what a reader is here
    to find."""
    rows = [
        {"id": "ONESHOT", "state": "scheduled", "due_at": "2026-08-21T03:00:00Z"},
        {"id": "CRON", "state": "scheduled", "due_at": "2026-08-21T09:00:00Z",
         "repeat": "0 9 * * *"},
    ]
    _reminds_output(rows, monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert out.index("CRON") < out.index("ONESHOT")


def test_an_empty_live_list_says_where_the_others_went(capsys, monkeypatch):
    """'no reminders' when ten exist but all are finished would be a lie."""
    _reminds_output([{"id": "A", "state": "fired", "due_at": "2026-08-21T03:00:00Z"}],
                     monkeypatch=monkeypatch)
    assert "see them with --all" in capsys.readouterr().out


def test_repeat_with_a_delay_is_refused_locally():
    """A cron already specifies its own first fire, so `--repeat` plus `--delay`
    asks twice and the two can disagree. The server refuses the pair (422); the
    CLI catches it first so the message names the FLAG the user typed rather
    than the wire field they have never heard of.

    Verified against the live server after the backend's fix:
        POST {"repeat":"* * * * *","delay_seconds":60}
        -> 422 "a recurring reminder takes its first fire from the cron
                expression; drop delay_seconds/due_at, or drop repeat"
    """
    from agentbus_client.cli._parser import build_parser
    from agentbus_client.cli._remind import cmd_remind

    args = build_parser().parse_args(
        ["remind", "-m", "x", "--repeat", "daily", "--delay", "2h"]
    )
    assert cmd_remind(args) == 2


def test_a_pure_recurrence_is_allowed():
    """`--repeat daily` with no delay must NOT be blocked by the client's
    when-check — the cron IS the when. This was a real 422 until the backend
    fixed it, and the client must not reintroduce the refusal locally."""
    from agentbus_client.cli._parser import build_parser

    args = build_parser().parse_args(["remind", "-m", "x", "--repeat", "daily"])
    assert args.repeat == "daily" and not args.delay and not args.at


def test_a_targeted_reminder_does_not_send_fields_the_route_forbids():
    """REGRESSION: `--target` was broken while a self-note worked.

    `_apply_seal` sets `html=None` because it was written for POST /v1/messages,
    where html is a legal field. Only the TARGETED path goes through it
    (a self-note uses `_seal_to_self`), so a targeted reminder died with

        could not schedule: html: Extra inputs are not permitted

    while every self-note succeeded — which is why it survived my own testing
    and needed a second pair of eyes. Reported by macbook-admin-bd8e86.

    The route forbids extra inputs, so one stray key fails the ENTIRE create.
    Asserting the whole envelope rather than the absence of `html` alone: the
    next field the sealer adds would break this the same way.
    """
    from agentbus_client.client import AgentBus

    bus = AgentBus(api_key="ab_sk_x_y", base_url="http://localhost")
    spy = _Spy()
    bus._request = spy

    def seal_like_the_send_route(body, agent, **kw):
        out = dict(body)
        out["html"] = None          # what _apply_seal really does
        out["sealed"] = True
        return out, None

    bus._seal_if_needed = seal_like_the_send_route
    bus.remind("note", target="alice", delay="2h")

    served = {
        "target", "subject", "text", "sealed",
        "delay_seconds", "due_at", "expires_at", "repeat", "timezone",
    }
    extra = set(spy.body) - served
    assert not extra, f"sends fields the reminders route forbids: {extra}"


def test_the_feature_is_discoverable_from_quickref():
    """DISCOVERY GAP: `agentbus quickref` had ZERO mentions of remind.

    Found by macbook-admin-bd8e86, who checked the served skill doc too — also
    zero. So the only way to learn this verb existed was `--help` on a command
    you already had to know to try. A feature nobody can find has not shipped
    to anyone, whatever the release notes say.

    Asserts the CANCEL path specifically, not just the word "remind": a
    recurrence has no end date, so an agent who creates one and cannot find how
    to stop it has a reminder firing forever.
    """
    from agentbus_client.cli._diag import QUICKREF

    assert "agentbus remind" in QUICKREF
    assert "agentbus reminds" in QUICKREF, "listing is how you find one to cancel"
    assert "--cancel" in QUICKREF, "a recurrence fires until cancelled"
    assert "reminders" in QUICKREF, "the ack-chasing collision must be disambiguated"


def test_repeat_until_is_a_usage_error_not_a_traceback():
    """REGRESSION: `--repeat-until` printed a raw Python stack trace.

    The SDK raises ValueError for the unsupported flag — correct there, since a
    library caller wants an exception. But `cmd_remind` did not catch it, so the
    CLI user saw a traceback ending in the right sentence.

    A traceback tells the reader their INSTALL is broken. This is a usage error:
    their command is wrong and the fix is one flag away. Exit 2, message on
    stderr, no stack. Found by bikeroom-freebsd-operato-b124c2 while testing the
    refusal I had documented — the message was right and the delivery was not.
    """
    from agentbus_client.cli._parser import build_parser
    from agentbus_client.cli._remind import cmd_remind

    args = build_parser().parse_args(
        ["remind", "-m", "x", "--repeat", "daily", "--repeat-until", "2026-12-01"]
    )
    assert cmd_remind(args) == 2, "must be a clean usage error, never an exception"


def test_the_refusal_points_at_the_flag_that_actually_works(capsys):
    """`--repeat-until` does not exist and will not — `--expire` IS the end date.

    An earlier draft of this refusal said "a recurring reminder has no end date
    and fires until you stop it", which was WRONG in the direction that costs
    someone work: it would have sent them building a cancellation cron they do
    not need. There IS an end date.

    The backend demonstrated it rather than asserting, after disclosing they had
    claimed it twice without ever running it — and this spec was about to carry
    that unverified claim as documentation.

    A refusal that names the alternative is the difference between a dead end and
    a redirect.
    """
    from agentbus_client.cli._parser import build_parser
    from agentbus_client.cli._remind import cmd_remind

    args = build_parser().parse_args(
        ["remind", "-m", "x", "--repeat", "daily", "--repeat-until", "2026-12-01"]
    )
    cmd_remind(args)
    err = capsys.readouterr().err
    assert "--expire" in err, "must name the flag that actually works"
    assert "no end date" not in err, "there IS an end date; saying otherwise misleads"
