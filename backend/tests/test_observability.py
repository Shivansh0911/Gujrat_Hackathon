"""Structured logging and metrics: the two things an ops team reads at 3am.

The properties worth pinning are the ones that fail quietly:

* redaction must still run when the format changes, or adding JSON output becomes a
  second serialisation route around the filter that strips secrets;
* a metrics scrape must not 500 during an incident, because that removes the monitoring
  exactly when it is needed;
* no metric label may carry a camera id, because 80,000 time series is how a monitoring
  system falls over while reporting that everything is fine.
"""

from __future__ import annotations

import json
import logging
import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from services.api.metrics import NS, Metric, collect, render
from services.common import structured_log


def _record(**kwargs: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name=kwargs.pop("name", "test.logger"),  # type: ignore[arg-type]
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=kwargs.pop("msg", "hello"),  # type: ignore[arg-type]
        args=(),
        exc_info=None,
    )
    for key, value in kwargs.items():
        setattr(record, key, value)
    return record


# --------------------------------------------------------- structured logging


def test_a_line_is_one_json_object() -> None:
    parsed = json.loads(structured_log.JsonFormatter().format(_record()))
    assert parsed["message"] == "hello"
    assert parsed["level"] == "INFO"
    assert parsed["logger"] == "test.logger"


def test_the_timestamp_carries_an_explicit_offset() -> None:
    """A log line whose timezone has to be inferred from the host cannot be correlated
    across machines."""
    parsed = json.loads(structured_log.JsonFormatter().format(_record()))
    assert parsed["ts"].endswith("+00:00")


def test_the_correlation_id_appears_without_being_passed() -> None:
    """Threading it through every function that might log is the change that gets
    half-done, and the half that was missed is where the interesting failure happens."""
    token = structured_log.correlation_id.set("abc123def456")
    try:
        parsed = json.loads(structured_log.JsonFormatter().format(_record()))
        assert parsed["correlation_id"] == "abc123def456"
    finally:
        structured_log.correlation_id.reset(token)


def test_no_correlation_field_outside_a_request() -> None:
    """An empty string would be a value an aggregator indexes; absence is cleaner."""
    token = structured_log.correlation_id.set("")
    try:
        assert "correlation_id" not in json.loads(structured_log.JsonFormatter().format(_record()))
    finally:
        structured_log.correlation_id.reset(token)


def test_extra_fields_become_top_level_fields() -> None:
    """Which is the entire point of using `extra=` rather than formatting into the
    message -- a field query beats a regex over prose."""
    parsed = json.loads(
        structured_log.JsonFormatter().format(_record(camera_ref="cam22", frames=148))
    )
    assert parsed["camera_ref"] == "cam22"
    assert parsed["frames"] == 148


def test_an_unserialisable_field_does_not_take_the_line_down() -> None:
    """A formatter that can raise is one that loses the message explaining why
    everything else broke."""
    parsed = json.loads(structured_log.JsonFormatter().format(_record(obj=object())))
    assert "obj" in parsed and isinstance(parsed["obj"], str)


def test_redaction_still_runs_when_json_is_selected(caplog: pytest.LogCaptureFixture) -> None:
    """Adding a format must not become a second serialisation route around the filter.

    This is the property that matters most in this file: `redact.py` strips secrets from
    every record, and a JSON path that bypassed it would be a quiet regression in the
    one thing protecting credentials in logs.
    """
    from services.common import redact

    filt = redact.RedactingFilter()
    record = _record(msg="connecting with SETU_GATEWAY_ACCESS_CODE=ABCD-1234-EFGH")
    filt.filter(record)

    rendered = structured_log.JsonFormatter().format(record)
    assert "ABCD-1234-EFGH" not in rendered


def test_install_is_a_no_op_for_the_human_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """A developer's terminal is unaffected by default."""
    monkeypatch.delenv(structured_log.FORMAT_ENV, raising=False)
    structured_log.install(force_json=False)
    handler = logging.getLogger().handlers[0]
    assert not isinstance(handler.formatter, structured_log.JsonFormatter)


def test_install_selects_json_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(structured_log.FORMAT_ENV, "json")
    structured_log.install()
    try:
        handler = logging.getLogger().handlers[0]
        assert isinstance(handler.formatter, structured_log.JsonFormatter)
    finally:
        structured_log.install(force_json=False)


# ------------------------------------------------------------------- metrics


def test_a_metric_renders_in_exposition_format() -> None:
    metric = Metric(f"{NS}_things_total", "counter", "Things.", 42)
    assert metric.render() == f"{NS}_things_total 42"


def test_an_integer_value_renders_without_a_decimal_point() -> None:
    """Easier to read in an alert rule, and both forms are valid."""
    assert Metric("x", "gauge", "h", 3.0).render() == "x 3"
    assert Metric("x", "gauge", "h", 3.5).render() == "x 3.5"


def test_labels_render_sorted_and_escaped() -> None:
    metric = Metric("x", "gauge", "h", 1, labels={"b": "two", "a": 'say "hi"'})
    assert metric.render() == 'x{a="say \\"hi\\"",b="two"} 1'


def test_help_and_type_appear_once_per_name() -> None:
    """Repeating them is a parse error in some scrapers and noise in the rest."""
    body = render(
        [
            Metric("x", "gauge", "Help text.", 1, labels={"s": "a"}),
            Metric("x", "gauge", "Help text.", 2, labels={"s": "b"}),
        ]
    )
    assert body.count("# HELP x") == 1
    assert body.count("# TYPE x") == 1
    assert body.count("\nx{") == 2


def test_the_body_ends_with_a_newline() -> None:
    """Required by the format."""
    assert render([Metric("x", "gauge", "h", 1)]).endswith("\n")


def test_no_metric_is_labelled_by_camera() -> None:
    """80,000 time series is how a monitoring system falls over while reporting that
    everything is fine."""
    from services.api.metrics import _QUERIES

    for name, _, _, query in _QUERIES:
        assert "camera_id" not in query.lower() or "group by" not in query.lower(), name


class _BrokenSession:
    """A session whose every query fails, as during a database incident."""

    def execute(self, *_: object, **__: object) -> object:
        raise RuntimeError("database is unreachable")


def test_a_scrape_survives_a_database_outage() -> None:
    """A metrics endpoint that 500s during an incident removes the monitoring exactly
    when it is needed. A gap in a dashboard is visible; a failed scrape sends an
    operator chasing the wrong thing.
    """
    metrics = collect(_BrokenSession())  # type: ignore[arg-type]
    assert any(m.name.endswith("process_uptime_seconds") for m in metrics)
    assert render(metrics).endswith("\n")


# ------------------------------------------------- against a real database


def _db_url() -> str | None:
    if os.environ.get("SETU_DATABASE_URL"):
        return os.environ["SETU_DATABASE_URL"]
    from services.common.paths import ENV_FILE

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("SETU_DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    return None


def _reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        engine = create_engine(url, connect_args={"connect_timeout": 3})
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        engine.dispose()
        return True
    except Exception:
        return False


_URL = _db_url()
needs_db = pytest.mark.skipif(not _reachable(_URL), reason="metrics read the database")


@pytest.fixture
def session() -> Session:
    engine = create_engine(_URL or "", future=True)
    s: Session = sessionmaker(bind=engine, future=True)()
    s.execute(text("SELECT set_config('setu.is_admin', 'on', false)"))
    yield s
    s.rollback()
    s.close()
    engine.dispose()


@needs_db
def test_every_declared_metric_can_actually_be_collected(session: Session) -> None:
    """A metric whose query no longer matches the schema is one that silently vanishes
    from a dashboard, and nobody notices until they need it."""
    from services.api.metrics import _QUERIES

    collected = {m.name for m in collect(session)}
    missing = sorted({name for name, _, _, _ in _QUERIES} - collected)
    assert not missing, f"these metrics failed to collect: {missing}"


@needs_db
def test_the_scrape_answers_the_questions_an_operator_asks(session: Session) -> None:
    names = {m.name for m in collect(session)}
    for expected in (
        f"{NS}_detections_last_hour",  # has ingest stopped?
        f"{NS}_alerts_open",  # is anything waiting on a human?
        f"{NS}_webhook_deliveries_pending",  # is the dispatcher running?
        f"{NS}_cameras_unreachable",  # is the estate healthy?
    ):
        assert expected in names


@needs_db
def test_camera_status_is_one_series_per_status_not_per_camera(session: Session) -> None:
    """Bounded by the enum, which has eight members, rather than by the estate size."""
    series = [m for m in collect(session) if m.name == f"{NS}_cameras_by_status"]
    assert len(series) <= 8
    assert all(m.labels and set(m.labels) == {"status"} for m in series)
