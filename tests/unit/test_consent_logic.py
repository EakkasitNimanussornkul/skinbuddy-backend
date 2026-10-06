"""The consent rules (app/core/consent.py): when a user must be asked again.

Pure functions over a users row, so no database or HTTP is involved. Docstrings
state the expected output and are lifted verbatim into the Test Record.
"""

import re

import pytest

from app.core import consent

T, H = consent.TERMS_VERSION, consent.HEALTH_CONSENT_VERSION
EARLIER, LATER = "2026-10-06T08:00:00+00:00", "2026-10-06T09:00:00.500000+00:00"


def accepted(**overrides):
    row = {"terms_accepted_at": EARLIER, "terms_version": T, "age_confirmed_at": EARLIER}
    row.update(overrides)
    return row


def health(**overrides):
    row = {"health_consent_at": EARLIER, "health_consent_version": H, "health_consent_withdrawn_at": None}
    row.update(overrides)
    return row


def test_policy_versions_are_dates():
    """Holds TERMS_VERSION and HEALTH_CONSENT_VERSION as non-empty YYYY-MM-DD
    strings, the form the frontend sends back."""
    for version in (consent.TERMS_VERSION, consent.HEALTH_CONSENT_VERSION):
        assert isinstance(version, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", version)


def test_a_user_who_has_never_consented_needs_both():
    """Reports needs_terms and needs_health_consent as true for a user row with
    none of the six consent columns set, and for a row that lacks the columns."""
    for row in ({}, {"terms_accepted_at": None, "health_consent_at": None}, None):
        obj = consent.consent_object(row)
        assert obj["needs_terms"] is True and obj["needs_health_consent"] is True


def test_terms_are_satisfied_by_the_current_version_with_the_age_confirmed():
    """Reports needs_terms as false when the terms were accepted at the current
    version and the age was confirmed."""
    assert consent.needs_terms(accepted()) is False


@pytest.mark.parametrize("change", [
    {"terms_accepted_at": None},
    {"terms_version": None},
    {"terms_version": "2020-01-01"},
    {"age_confirmed_at": None},
])
def test_terms_are_needed_again_when_any_part_is_missing_or_old(change):
    """Reports needs_terms as true when the acceptance time is missing, the version
    is missing or is not the current one, or the age was never confirmed."""
    assert consent.needs_terms(accepted(**change)) is True


def test_health_consent_is_satisfied_by_the_current_version_not_withdrawn():
    """Reports needs_health_consent as false when health consent was given at the
    current version and never withdrawn."""
    assert consent.needs_health_consent(health()) is False


@pytest.mark.parametrize("change", [
    {"health_consent_at": None},
    {"health_consent_version": None},
    {"health_consent_version": "2020-01-01"},
    {"health_consent_withdrawn_at": LATER},
])
def test_health_consent_is_needed_again_when_missing_old_or_withdrawn(change):
    """Reports needs_health_consent as true when the consent time is missing, the
    version is missing or is not the current one, or the consent was withdrawn after
    it was given."""
    assert consent.needs_health_consent(health(**change)) is True


def test_a_withdrawal_older_than_the_consent_does_not_count():
    """Reports needs_health_consent as false when health consent was given again
    after an earlier withdrawal, even if the withdrawal time was not cleared."""
    row = health(health_consent_at=LATER, health_consent_withdrawn_at=EARLIER)
    assert consent.health_withdrawn(row) is False
    assert consent.needs_health_consent(row) is False


def test_a_withdrawal_with_no_consent_on_record_counts_as_withdrawn():
    """Reports a withdrawn health consent when a withdrawal time is set and no
    consent time is."""
    assert consent.health_withdrawn({"health_consent_withdrawn_at": EARLIER}) is True


def test_terms_and_health_consent_are_independent():
    """Keeps the two answers apart: accepting the terms leaves needs_health_consent
    true, and giving health consent leaves needs_terms true."""
    assert consent.consent_object(accepted())["needs_health_consent"] is True
    assert consent.consent_object(health())["needs_terms"] is True


def test_the_consent_object_has_exactly_the_documented_fields():
    """Returns the consent object with these ten keys and no others: terms_accepted_at,
    terms_version, age_confirmed_at, health_consent_at, health_consent_version,
    health_consent_withdrawn_at, current_terms_version, current_health_version,
    needs_terms, needs_health_consent; the six stored values are passed through and
    the two current versions are the code's constants."""
    row = {**accepted(), **health(health_consent_withdrawn_at=LATER), "display_name": "Not consent"}
    obj = consent.consent_object(row)
    assert set(obj) == {"terms_accepted_at", "terms_version", "age_confirmed_at", "health_consent_at",
                        "health_consent_version", "health_consent_withdrawn_at",
                        "current_terms_version", "current_health_version", "needs_terms",
                        "needs_health_consent"}
    assert obj["terms_accepted_at"] == EARLIER and obj["health_consent_withdrawn_at"] == LATER
    assert obj["current_terms_version"] == T and obj["current_health_version"] == H
    assert obj["needs_terms"] is False and obj["needs_health_consent"] is True


def test_bumping_a_version_asks_everyone_again(monkeypatch):
    """Reports needs_terms and needs_health_consent as true for a user who had
    consented to the previous version once the current version constants change."""
    row = {**accepted(), **health()}
    assert consent.needs_terms(row) is False and consent.needs_health_consent(row) is False
    monkeypatch.setattr(consent, "TERMS_VERSION", "2099-01-01")
    monkeypatch.setattr(consent, "HEALTH_CONSENT_VERSION", "2099-01-01")
    obj = consent.consent_object(row)
    assert obj["needs_terms"] is True and obj["needs_health_consent"] is True
    assert obj["current_terms_version"] == "2099-01-01" and obj["current_health_version"] == "2099-01-01"
