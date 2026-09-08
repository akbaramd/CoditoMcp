"""Submission success must never be interpreted as visible user consent UI."""

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from codito_agent.notifications import _toast_delivery, dismiss_native_toast


@pytest.mark.parametrize("state", ["Busy", "QuietTime", "PresentationMode", "NotPresent"])
def test_suppressed_submission_is_not_visible(state):
    delivery = _toast_delivery(
        {
            "ok": True,
            "delivery": "submitted",
            "setting": "Enabled",
            "user_state": state,
        }
    )
    assert delivery.submitted
    assert delivery.banner_visible is None
    assert not delivery.banner_expected


def test_accepted_submission_still_does_not_prove_visibility():
    delivery = _toast_delivery(
        {
            "ok": True,
            "delivery": "submitted",
            "setting": "Enabled",
            "user_state": "AcceptsNotifications",
            "banner_visible": True,
        }
    )
    assert delivery.banner_expected
    assert delivery.banner_visible is None


def test_unknown_broker_fields_are_not_trusted_or_logged():
    delivery = _toast_delivery(
        {
            "ok": True,
            "delivery": "submitted",
            "setting": {"private": "token"},
            "user_state": "secret user data",
            "xml": "approval credential",
        }
    )
    assert delivery.setting == "Unknown"
    assert delivery.user_state == "Unknown"
    assert not delivery.banner_expected


@pytest.mark.parametrize("payload", [None, [], {"ok": True}, {"ok": False}, {"ok": 1}])
def test_missing_or_failed_submission_rejected(payload):
    with pytest.raises(ValueError):
        _toast_delivery(payload)


def test_dismissal_addresses_only_own_hashed_tag(monkeypatch):
    seen = []

    def run(args, **kwargs):
        seen.append((args, json.loads(kwargs["input"])))
        return subprocess.CompletedProcess(args, 0, stdout=b'{"ok":true}')

    monkeypatch.setattr("codito_agent.notifications.subprocess.run", run)
    assert dismiss_native_toast(Path("broker.exe"), "synthetic_request")
    assert seen == [
        (
            ["broker.exe", "--json"],
            {
                "version": 1,
                "operation": "notify-remove",
                "notification_tag": hashlib.sha256(b"synthetic_request").hexdigest()[:16],
            },
        )
    ]


def test_failed_dismissal_does_not_raise_or_make_decision(monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired("broker", 3)

    monkeypatch.setattr("codito_agent.notifications.subprocess.run", run)
    assert dismiss_native_toast(Path("broker.exe"), "synthetic_request") is False
    assert dismiss_native_toast(None, "synthetic_request") is False
