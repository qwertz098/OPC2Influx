"""Reports: periods, figures, e-mail text and the scheduler."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.config import AppConfig, ReportConfig, ReportSignalConfig
from app.reports import periods as P
from app.reports import scheduler as S
from app.reports.compute import ReportFigures, SignalFigures, _apply, counter_flux, gauge_flux
from app.reports.render import fmt_change, fmt_num, html_body, subject, text_body

ST = P.parse_send_time("00:15")


# ─── Periods ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("now,kind,key", [
    (datetime(2026, 11, 13, 0, 10), "daily", "2026-11-11"),     # before 00:15: day before yesterday
    (datetime(2026, 11, 13, 0, 20), "daily", "2026-11-12"),
    (datetime(2026, 11, 16, 0, 10), "weekly", "2026-W45"),      # Monday before the send time
    (datetime(2026, 11, 16, 0, 20), "weekly", "2026-W46"),
    (datetime(2026, 11, 22, 23, 0), "weekly", "2026-W46"),
    (datetime(2026, 12, 1, 0, 5), "monthly", "2026-10"),
    (datetime(2026, 12, 1, 0, 20), "monthly", "2026-11"),
    (datetime(2027, 1, 1, 6, 0), "monthly", "2026-12"),
    (datetime(2027, 1, 4, 6, 0), "weekly", "2026-W53"),         # ISO week across the year end
])
def test_due_period_key(now, kind, key):
    assert P.due_period(kind, now, ST, "utc").key == key


def test_period_bounds_utc():
    p = P.due_period("monthly", datetime(2026, 3, 2, 12, 0), ST, "utc")
    assert p.start == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert p.end == datetime(2026, 3, 1, tzinfo=timezone.utc)
    assert p.prev_start == datetime(2026, 1, 1, tzinfo=timezone.utc)
    w = P.due_period("weekly", datetime(2026, 11, 18, 12, 0), ST, "utc")
    assert (w.start.weekday(), (w.end - w.start).days, (w.start - w.prev_start).days) == (0, 7, 7)


def test_last_completed_ignores_send_time():
    assert P.last_completed("daily", datetime(2026, 11, 13, 0, 1), "utc").key == "2026-11-12"


@pytest.mark.parametrize("raw,minutes", [("00:15", 15), ("6:30", 390), ("25:00", 15), ("x", 15)])
def test_parse_send_time(raw, minutes):
    assert P.parse_send_time(raw) == timedelta(minutes=minutes)


# ─── Figures ─────────────────────────────────────────────────────────────────
def test_flux_lines_never_start_with_parenthesis():
    # Flux reads a line starting with "(" as a call on the line before
    q = gauge_flux("A", "B", "C", "D", True) + counter_flux("A", "B")
    assert not any(line.lstrip().startswith("(") for line in q.splitlines())


def test_apply_gauge_with_energy_and_seed():
    sig = ReportSignalConfig(subscription_id="x", energy=True, energy_factor=0.001)
    t = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    fig = SignalFigures(label="x", kind="gauge")
    _apply(fig, sig, {"mean": (1000.0, t), "min": (0.0, t), "max": (2500.0, t), "energy": (24000.0, t),
                      "prev_seed": (500.0, t)}, hours=24, prev_hours=24)
    assert (fig.mean, fig.min, fig.max, fig.max_time) == (1000.0, 0.0, 2500.0, t)
    assert fig.energy == pytest.approx(24.0)
    # no value in the period before: the value before it held all the time
    assert fig.prev_mean == 500.0 and fig.prev_energy == pytest.approx(12.0)


def test_apply_counter():
    fig = SignalFigures(label="c", kind="counter")
    _apply(fig, ReportSignalConfig(subscription_id="c", kind="counter"),
           {"reading": (1071.0, None), "prev_reading": (1047.0, None)})
    assert fig.counter_change == 24.0


# ─── E-mail ──────────────────────────────────────────────────────────────────
def test_formatting():
    assert fmt_num(None) == "–"
    assert fmt_num(1234.5) == "1 234"
    assert fmt_num(12.345) == "12.35"
    assert fmt_change(110, 100) == "+10.00 (+10.0 %)"
    assert fmt_change(90, 100) == "−10.00 (−10.0 %)"
    assert fmt_change(None, 100) == "–"


def _figures():
    rep = ReportConfig(name="Plant <1>", signals=[])
    period = P.due_period("daily", datetime(2026, 11, 13, 6, 0), ST, "utc")
    t = datetime(2026, 11, 12, 6, tzinfo=timezone.utc)
    return ReportFigures(report=rep, period=period, source="Local", signals=[
        SignalFigures(label="Power", kind="gauge", unit="W", mean=1000, prev_mean=900, min=0, min_time=t,
                      max=2500, max_time=t, energy=24, prev_energy=21.6, energy_unit="kWh"),
        SignalFigures(label="Meter", kind="counter", unit="kWh", reading=1071, prev_reading=1047),
        SignalFigures(label="Gone", kind="gauge", error="subscription not found"),
    ])


def test_mail_bodies():
    fig = _figures()
    assert subject(fig) == "Daily report Plant <1>: Thu 12.11.2026"
    text = text_body(fig, "utc")
    assert "Mean: 1 000 W" in text and "+100.0 (+11.1 %)" in text and "Change: 24.00 kWh" in text
    assert "Error: subscription not found" in text
    page = html_body(fig, "utc")
    assert "Plant &lt;1&gt;" in page and "<table" in page and "24.00 kWh" in page


# ─── Scheduler ───────────────────────────────────────────────────────────────
class _State:
    def __init__(self, reports):
        self.config = AppConfig()
        self.config.reports.timezone = "utc"
        self.config.reports.reports = reports
        self.alarms = []

    def add_alarm(self, level, source, message):
        self.alarms.append(message)


@pytest.fixture
def sent_file(tmp_path, monkeypatch):
    monkeypatch.setattr(S, "state_path", lambda: tmp_path / "reports_state.json")
    return tmp_path / "reports_state.json"


def test_scheduler_sends_once_and_new_reports_wait(sent_file, monkeypatch):
    sent = []

    async def fake_send(state, report, period):
        sent.append((report.name, period.kind, period.key))
    monkeypatch.setattr(S, "send_report", fake_send)

    old = ReportConfig(name="old", daily=True, weekly=True, recipients=["a@x"],
                       signals=[ReportSignalConfig(subscription_id="s")])
    new = ReportConfig(name="new", daily=True, recipients=["a@x"],
                       signals=[ReportSignalConfig(subscription_id="s")])
    state = _State([old, new])
    S.mark_current_as_sent(state, new)       # created now: starts with the next period

    sched = S.ReportScheduler()
    asyncio.run(sched.tick(state))
    asyncio.run(sched.tick(state))           # second tick: nothing again
    kinds = sorted((n, k) for n, k, _ in sent)
    assert kinds == [("old", "daily"), ("old", "weekly")]
    assert {k: v for _, k, v in sent} == {k: S.load_sent()[old.id][k] for k in ("daily", "weekly")}


def test_scheduler_retries_after_failure(sent_file, monkeypatch):
    async def failing(state, report, period):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(S, "send_report", failing)
    rep = ReportConfig(name="r", recipients=["a@x"], signals=[ReportSignalConfig(subscription_id="s")])
    state = _State([rep])
    sched = S.ReportScheduler()
    asyncio.run(sched.tick(state))
    asyncio.run(sched.tick(state))           # within the retry pause: not tried again
    assert len(state.alarms) == 1 and "smtp down" in state.alarms[0]
    assert rep.id not in S.load_sent()


def test_scheduler_skips_incomplete_reports(sent_file, monkeypatch):
    calls = []

    async def fake_send(state, report, period):
        calls.append(report.name)
    monkeypatch.setattr(S, "send_report", fake_send)
    state = _State([ReportConfig(name="no recipients", signals=[ReportSignalConfig(subscription_id="s")]),
                    ReportConfig(name="no signals", recipients=["a@x"]),
                    ReportConfig(name="off", enabled=False, recipients=["a@x"],
                                 signals=[ReportSignalConfig(subscription_id="s")])])
    asyncio.run(S.ReportScheduler().tick(state))
    assert calls == []
