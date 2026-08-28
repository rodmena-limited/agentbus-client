"""`agentbus reminds` — listing scheduled reminders.

Split out of test_remind_verb.py (#47), which reached 577 lines and breached the
550 HARD cap. The seam was already there: everything above tests SENDING a
reminder, everything here tests LISTING them.

Farshid: "the client must have the ability to list reminders/crons (otherwise
they can't cancel the old ones)".
"""

from __future__ import annotations

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

    # THE STUB MODELS THE SERVER, NOT THE OLD CLIENT (#336).
    #
    # It used to return every row for both calls, because the CLI filtered
    # locally — which is precisely the defect: the state filter ran AFTER the
    # server had already truncated. Now the server filters, so `all=False` must
    # hand back only the scheduled rows, and `total` must describe the rows that
    # EXIST under that filter. A stub that kept returning everything would let a
    # regression to local-only filtering pass unnoticed here.
    live = [r for r in rows if r.get("state") == "scheduled"]

    def _page(self, all=False):
        chosen = rows if all else live
        return {
            "reminders": chosen,
            "count": len(chosen),
            "total": len(chosen),
            "has_more": False,
            "limit": 200,
        }

    bus = type(
        "B",
        (),
        {"reminds_page": _page, "reminds": lambda self, all=False: _page(self, all)["reminders"]},
    )()
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
        {
            "id": "CRON",
            "state": "scheduled",
            "due_at": "2026-08-21T09:00:00Z",
            "repeat": "0 9 * * *",
        },
    ]
    _reminds_output(rows, monkeypatch=monkeypatch)
    out = capsys.readouterr().out
    assert out.index("CRON") < out.index("ONESHOT")


def test_an_empty_live_list_says_where_the_others_went(capsys, monkeypatch):
    """'no reminders' when ten exist but all are finished would be a lie."""
    _reminds_output(
        [{"id": "A", "state": "fired", "due_at": "2026-08-21T03:00:00Z"}], monkeypatch=monkeypatch
    )
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

    args = build_parser().parse_args(["remind", "-m", "x", "--repeat", "daily", "--delay", "2h"])
    assert cmd_remind(args) == 2


def test_a_pure_recurrence_is_allowed():
    """`--repeat daily` with no delay must NOT be blocked by the client's
    when-check — the cron IS the when. This was a real 422 until the backend
    fixed it, and the client must not reintroduce the refusal locally."""
    from agentbus_client.cli._parser import build_parser

    args = build_parser().parse_args(["remind", "-m", "x", "--repeat", "daily"])
    assert args.repeat == "daily" and not args.delay and not args.at


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
