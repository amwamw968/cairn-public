"""AC3 — FSRS-6 scheduling correctness, pinned to fsrs==6.3.2.

The expected values below were captured from the pinned library
(fsrs==6.3.2, default 21 parameters, desired_retention=0.9,
learning_steps=(), relearning_steps=(), enable_fuzzing=False) BEFORE the
assertions were written — tests do not bend to the implementation.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fsrs import Card as RawCard
from fsrs import Rating as RawRating
from fsrs import Scheduler as RawScheduler
from fsrs import State as RawState

from cairn.models.review import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
)
from cairn.scheduler import FSRS_PACKAGE_VERSION, Scheduler

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


def test_pinned_version():
    assert FSRS_PACKAGE_VERSION == "6.3.2"
    import fsrs

    import importlib.metadata

    assert importlib.metadata.version("fsrs") == "6.3.2"


def test_fsrs_known_vector():
    """First review of a fresh card: exact 6.3.2 outputs per rating."""
    s = Scheduler()
    expected = {
        RATING_AGAIN: (
            datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc), 0.212, 6.4133, "review"
        ),
        RATING_HARD: (
            datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc), 1.2931, 5.112170705601056, "review"
        ),
        RATING_GOOD: (
            datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc), 2.3065, 2.118103970459016, "review"
        ),
        RATING_EASY: (
            datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc), 8.2956, 1.0, "review"
        ),
    }
    for rating, (due_at, stability, difficulty, state) in expected.items():
        r = s.schedule(rating, now=NOW)
        assert r.due_at == due_at, f"{rating}: due {r.due_at} != {due_at}"
        assert r.stability == stability, f"{rating}: S {r.stability} != {stability}"
        assert r.difficulty == difficulty, f"{rating}: D {r.difficulty} != {difficulty}"
        assert r.state == state


def test_multi_review_matches_raw_library():
    """Wrapper vs raw fsrs library, step by step: Again -> Good x3.

    The wrapper is fed only (S, D, due, last_review) from its own previous
    result — proving DB-persisted state is sufficient and lossless.
    """
    wrapper = Scheduler()
    raw = RawScheduler(enable_fuzzing=False, learning_steps=(), relearning_steps=())
    rating_map = {
        RATING_AGAIN: RawRating.Again,
        RATING_HARD: RawRating.Hard,
        RATING_GOOD: RawRating.Good,
        RATING_EASY: RawRating.Easy,
    }

    raw_card = RawCard(due=NOW)
    prior = None
    prev_at = None
    timeline = [
        (RATING_AGAIN, NOW),
        (RATING_GOOD, NOW.replace(hour=13)),
        (RATING_GOOD, NOW.replace(day=31)),
        (RATING_GOOD, NOW.replace(day=31, hour=13)),
    ]
    for rating, at in timeline:
        result = wrapper.schedule(
            rating,
            stability=prior.stability if prior else None,
            difficulty=prior.difficulty if prior else None,
            due=prior.due_at if prior else None,
            last_review=prev_at if prior else None,
            now=at,
        )
        raw_card, _ = raw.review_card(raw_card, rating_map[rating], review_datetime=at)

        assert result.due_at == raw_card.due
        assert result.stability == raw_card.stability
        assert result.difficulty == raw_card.difficulty
        assert result.state == RawState(raw_card.state).name.lower()

        # pin the captured 6.3.2 trajectory as well
        prior = result
        prev_at = at

    assert prior.stability == 0.32439327450133126
    assert prior.difficulty == 6.379778751497693
    assert prior.due_at == datetime(2026, 11, 1, 13, 0, tzinfo=timezone.utc)


def test_four_ratings_differ():
    """Same initial state, four ratings -> four distinct schedules."""
    s = Scheduler()
    results = [s.schedule(r, now=NOW) for r in (RATING_AGAIN, RATING_HARD, RATING_GOOD, RATING_EASY)]

    keys = [(r.due_at, r.stability, r.difficulty) for r in results]
    assert len(set(keys)) == 4, "four ratings must produce four distinct outcomes"
