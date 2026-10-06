"""The health-consent gate on POST /analysis/log.

The gate is a dependency attached in app/main.py when the analysis router is
included (app/core/services/consent_gate.py); analysis.py is untouched. These
tests prove the order it runs in (login, then consent, then the route), that it
gates POST only, and that no Gemini analysis runs when it refuses.

The analysis step (analysis_service.analyze_log, which calls Gemini) is replaced
by a recorder, so a refusal can be shown to make no analysis call. Docstrings
state the expected output and are lifted verbatim into the Test Record.
"""

import pytest

from app.core import consent
from tests.conftest import FakeResponse

H = consent.HEALTH_CONSENT_VERSION
GIVEN = "2026-10-06T08:00:00+00:00"
WITHDRAWN = "2026-10-06T09:00:00+00:00"
GATE_BODY = {"detail": "Health consent is required before a weekly check-in.",
             "code": "health_consent_required"}
LOG = {"symptoms": [{"symptom": "redness", "severity": 2}], "affected_areas": ["cheeks"]}


def user_row(**columns):
    row = {"id": "user-1", "health_consent_at": None, "health_consent_version": None,
           "health_consent_withdrawn_at": None}
    row.update(columns)
    return row


class AnalysisTables:
    """What analysis.py's database calls need: an upsert that answers the row it was given."""

    def __init__(self):
        self.upserts = []

    def table(self, name):
        owner = self

        class Query:
            _row = None

            def __getattr__(self, attr):
                if attr.startswith("_"):
                    raise AttributeError(attr)
                return lambda *a, **k: self

            def upsert(self, payload, **kwargs):
                owner.upserts.append((name, payload))
                self._row = {"id": "log-1", **payload}
                return self

            def execute(self):
                return FakeResponse([self._row] if self._row else [])
        return Query()


@pytest.fixture
def checkin(patch_supabase, monkeypatch, as_user):
    """The gate's users table, the analysis tables and a recorded analysis step."""
    from app.api import analysis

    def _make(*users):
        as_user("user-1")
        patch_supabase({"users": list(users)}, "app.core.services.consent_gate")
        tables = AnalysisTables()
        monkeypatch.setattr(analysis, "supabase", tables)
        analyses = []
        monkeypatch.setattr(analysis.analysis_service, "analyze_log",
                            lambda log, uid: analyses.append((log, uid)) or {"status": "complete"})
        return tables, analyses
    return _make


def test_a_checkin_without_a_login_answers_401_before_the_gate(client):
    """Returns HTTP 401 with the login dependency's own body, not the consent 403,
    for POST /analysis/log with no Authorization header, whether or not the body is valid."""
    for body in (LOG, {}):
        resp = client.post("/analysis/log", json=body)
        assert resp.status_code == 401
        assert "code" not in resp.json()


def test_a_checkin_without_health_consent_answers_403_and_runs_nothing(client, checkin):
    """Returns HTTP 403 with detail 'Health consent is required before a weekly
    check-in.' and code health_consent_required for a user who never gave health
    consent, saves no log and makes no analysis call."""
    tables, analyses = checkin(user_row())
    resp = client.post("/analysis/log", json=LOG)
    assert resp.status_code == 403
    assert resp.json() == GATE_BODY
    assert tables.upserts == [] and analyses == []


def test_the_gate_answers_before_the_body_is_checked(client, checkin):
    """Returns HTTP 403 health_consent_required, not 422, for a body that would fail
    validation, from a user without health consent."""
    checkin(user_row())
    resp = client.post("/analysis/log", json={"symptoms": "not a list"})
    assert resp.status_code == 403 and resp.json() == GATE_BODY


def test_a_checkin_with_current_health_consent_runs_the_route(client, checkin):
    """Returns HTTP 200 with the saved log and the report, saves the log and
    analyses it once, for a user with current health consent."""
    tables, analyses = checkin(user_row(health_consent_at=GIVEN, health_consent_version=H))
    resp = client.post("/analysis/log", json=LOG)
    assert resp.status_code == 200
    assert resp.json()["log"]["user_id"] == "user-1"
    assert [name for name, _ in tables.upserts] == ["skin_logs", "skin_analysis_reports"]
    assert len(analyses) == 1 and analyses[0][1] == "user-1"


def test_a_checkin_after_withdrawal_answers_403_and_runs_nothing(client, checkin):
    """Returns HTTP 403 health_consent_required for a user who gave health consent
    and then withdrew it, saves no log and makes no analysis call."""
    tables, analyses = checkin(user_row(health_consent_at=GIVEN, health_consent_version=H,
                                        health_consent_withdrawn_at=WITHDRAWN))
    resp = client.post("/analysis/log", json=LOG)
    assert resp.status_code == 403 and resp.json() == GATE_BODY
    assert tables.upserts == [] and analyses == []


def test_a_checkin_after_a_new_consent_following_withdrawal_runs_the_route(client, checkin):
    """Returns HTTP 200 for a user who withdrew and then gave health consent again
    (the withdrawal time cleared)."""
    checkin(user_row(health_consent_at=WITHDRAWN, health_consent_version=H, health_consent_withdrawn_at=None))
    assert client.post("/analysis/log", json=LOG).status_code == 200


def test_a_checkin_with_an_old_consent_version_answers_403(client, checkin):
    """Returns HTTP 403 health_consent_required for a user whose consent was given
    for an earlier version of the text, and makes no analysis call."""
    _, analyses = checkin(user_row(health_consent_at=GIVEN, health_consent_version="2020-01-01"))
    resp = client.post("/analysis/log", json=LOG)
    assert resp.status_code == 403 and resp.json() == GATE_BODY
    assert analyses == []


def test_a_checkin_for_a_user_who_no_longer_exists_answers_403(client, checkin):
    """Returns HTTP 403 health_consent_required when no users row matches the caller
    (a deleted account whose token has not expired), and makes no analysis call."""
    _, analyses = checkin()
    resp = client.post("/analysis/log", json=LOG)
    assert resp.status_code == 403 and resp.json() == GATE_BODY
    assert analyses == []


def test_the_gate_reads_the_callers_own_row(client, checkin):
    """Lets user-1 through on user-1's consent even though another user has none, and
    refuses user-1 when only another user has consent."""
    checkin({"id": "user-2", "health_consent_at": None},
            user_row(health_consent_at=GIVEN, health_consent_version=H))
    assert client.post("/analysis/log", json=LOG).status_code == 200
    checkin({"id": "user-2", "health_consent_at": GIVEN, "health_consent_version": H},
            {"id": "user-1", "health_consent_at": None})
    assert client.post("/analysis/log", json=LOG).status_code == 403


def test_a_database_failure_in_the_gate_answers_500_with_a_code(client, checkin, monkeypatch):
    """Returns HTTP 500 with code internal_error and runs nothing when the consent
    cannot be read, rather than letting the check-in through."""
    from app.core.services import consent_gate
    _, analyses = checkin(user_row(health_consent_at=GIVEN, health_consent_version=H))
    monkeypatch.setattr(consent_gate, "supabase", type("Broken", (), {
        "table": staticmethod(lambda name: (_ for _ in ()).throw(RuntimeError("down")))})())
    resp = client.post("/analysis/log", json=LOG)
    assert resp.status_code == 500 and resp.json()["code"] == "internal_error"
    assert analyses == []


@pytest.mark.parametrize("path", ["/analysis/log/current", "/analysis/report", "/analysis/history"])
def test_reading_old_logs_and_reports_stays_open_after_withdrawal(client, checkin, path):
    """Returns HTTP 200 for GET /analysis/log/current, /report and /history from a
    user who has never given or has withdrawn health consent, so the gate affects
    POST only."""
    checkin(user_row(health_consent_at=GIVEN, health_consent_version=H,
                     health_consent_withdrawn_at=WITHDRAWN))
    assert client.get(path).status_code == 200


def test_reading_logs_without_a_login_still_answers_401(client):
    """Returns HTTP 401 for GET /analysis/log/current with no Authorization header."""
    assert client.get("/analysis/log/current").status_code == 401


def test_the_gate_is_attached_to_the_analysis_router_only(client, checkin):
    """Leaves other POST routes unaffected by the gate: POST /quiz/save for a user
    with no health consent is answered by the quiz route, not by the consent 403."""
    checkin(user_row())
    resp = client.post("/quiz/save", json={})
    assert resp.status_code != 403 or resp.json().get("code") != "health_consent_required"
