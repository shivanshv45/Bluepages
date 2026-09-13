"""should_auto_approve: the four conditions that let a procurement clear
itself, and the default that keeps it off."""

from __future__ import annotations

from bluepages.agents.approval import should_auto_approve


def test_default_limit_of_zero_never_auto_approves() -> None:
    assert should_auto_approve(total=50, limit=0, remaining=1000, grounded=True) is False


def test_a_grounded_total_under_the_limit_and_remaining_clears() -> None:
    assert should_auto_approve(total=1350, limit=2000, remaining=6000, grounded=True) is True


def test_a_total_over_the_limit_does_not_clear() -> None:
    assert should_auto_approve(total=2500, limit=2000, remaining=6000, grounded=True) is False


def test_a_total_over_remaining_does_not_clear_even_under_the_limit() -> None:
    assert should_auto_approve(total=1500, limit=2000, remaining=1000, grounded=True) is False


def test_an_ungrounded_price_never_auto_approves() -> None:
    assert should_auto_approve(total=100, limit=2000, remaining=6000, grounded=False) is False


def test_a_zero_or_negative_total_never_auto_approves() -> None:
    assert should_auto_approve(total=0, limit=2000, remaining=6000, grounded=True) is False
    assert should_auto_approve(total=-5, limit=2000, remaining=6000, grounded=True) is False


def test_exactly_at_the_limit_and_remaining_clears() -> None:
    assert should_auto_approve(total=2000, limit=2000, remaining=2000, grounded=True) is True
