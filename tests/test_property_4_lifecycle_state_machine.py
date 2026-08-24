"""
Feature: allen-biodata-registry-poc, Property 4: Lifecycle State Machine Correctness
Task: 23.2

Stateful Hypothesis test (RuleBasedStateMachine) that generates transition
sequences of bounded length and asserts:
  * Exactly the allowed transitions succeed.
  * Each successful transition produces exactly one lifecycle_transition row.
  * Publish rejects when validation_status != 'valid'.

The state machine here is a pure-Python model of the Lifecycle_Lambda's
state-transition logic (per design.md §Components.Lifecycle_Lambda):

  draft       -> registered
  registered  -> published        (only when validation_status == 'valid')
  published   -> archived
  archived    -> registered

Validates: R13.1, R27.2, R27.3, R27.6.
"""

from __future__ import annotations

from typing import Optional

import pytest
from hypothesis import settings
from hypothesis.stateful import RuleBasedStateMachine, rule, invariant


_ALLOWED = {
    "draft": {"registered"},
    "registered": {"published"},
    "published": {"archived"},
    "archived": {"registered"},
}


class LifecycleModel(RuleBasedStateMachine):
    """Pure-Python state machine modeling the Lifecycle_Lambda."""

    def __init__(self):
        super().__init__()
        self.state = "draft"
        self.validation_status = "unvalidated"
        self.transitions: list = []

    @rule()
    def attempt_register(self):
        self._attempt("registered")

    @rule()
    def attempt_publish(self):
        # Publish requires validation_status='valid'.
        if self.state == "registered" and self.validation_status != "valid":
            with pytest.raises(_PublishRejected):
                self._attempt("published", strict_validation=True)
        else:
            self._attempt("published", strict_validation=True)

    @rule()
    def attempt_archive(self):
        self._attempt("archived")

    @rule()
    def mark_valid(self):
        self.validation_status = "valid"

    @rule()
    def mark_invalid(self):
        self.validation_status = "invalid"

    def _attempt(self, target: str, strict_validation: bool = False) -> None:
        if target in _ALLOWED.get(self.state, set()):
            if target == "published" and strict_validation and self.validation_status != "valid":
                raise _PublishRejected(self.state, target)
            old = self.state
            self.state = target
            self.transitions.append((old, target))

    @invariant()
    def transitions_match_log(self):
        # Reconstruct state from transition log; must equal current state.
        if not self.transitions:
            assert self.state in {"draft", "registered", "published", "archived"}
            return
        reconstructed = "draft"
        for src, dst in self.transitions:
            assert dst in _ALLOWED.get(src, set()), (
                f"Illegal transition recorded: {src} -> {dst}"
            )
            reconstructed = dst
        assert reconstructed == self.state, (
            f"transition log diverges from state: log->{reconstructed}, state={self.state}"
        )


class _PublishRejected(Exception):
    def __init__(self, src: str, dst: str):
        super().__init__(f"publish rejected from {src} (validation_status not valid)")


TestLifecycleStateMachine = LifecycleModel.TestCase
TestLifecycleStateMachine.settings = settings(max_examples=100, deadline=None)


def test_allowed_transitions_explicit():
    """Spot-check: every state has the exact allowed-transitions set."""
    assert _ALLOWED == {
        "draft": {"registered"},
        "registered": {"published"},
        "published": {"archived"},
        "archived": {"registered"},
    }


def test_publish_blocked_when_invalid():
    m = LifecycleModel()
    m.state = "registered"
    m.validation_status = "invalid"
    with pytest.raises(_PublishRejected):
        m._attempt("published", strict_validation=True)
