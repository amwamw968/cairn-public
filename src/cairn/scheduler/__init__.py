"""FSRS-6 thin wrapper around the pinned ``fsrs==6.3.2`` package.

We do NOT implement the algorithm — we wrap it:

    input : rating (again/hard/good/easy) + prior FSRS state (optional)
    output: ScheduleResult(due_at, stability, difficulty, state)

Design decisions (see docs/PLAN.md):

* Learning/relearning steps are disabled (``learning_steps=()``,
  ``relearning_steps=()``). Every rated card lands in the Review state,
  so the DB-persisted (stability, difficulty, due, last_review) fully
  determines the next schedule — no hidden scheduler state.
* Fuzzing is disabled → deterministic output; AC3 pins exact values
  against ``fsrs==6.3.2``.

Rating mapping: Again/Hard/Good/Easy -> FSRS Rating 1..4 (report §5.1).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from fsrs import Card as FsrsCard
from fsrs import Rating as FsrsRating
from fsrs import Scheduler as FsrsScheduler
from fsrs import State as FsrsState

from cairn.models.learner import (
    DEFAULT_RETENTION_MATH,
    DEFAULT_RETENTION_OTHER,
    Learner,
)
from cairn.models.review import (
    RATING_AGAIN,
    RATING_EASY,
    RATING_GOOD,
    RATING_HARD,
)

# pinned upstream implementation; AC3 asserts against this exact version
FSRS_PACKAGE_VERSION = "6.3.2"

# Subject decides which retention the learner's stored target feeds FSRS.
# Free-text subject is a known wart: historical batches stored "math"/"english"
# codes, later entry stores 中文名, so both spellings count as maths.
_MATH_SUBJECT_PREFIXES = ("数学", "math")

_RATING_MAP: dict[str, FsrsRating] = {
    RATING_AGAIN: FsrsRating.Again,
    RATING_HARD: FsrsRating.Hard,
    RATING_GOOD: FsrsRating.Good,
    RATING_EASY: FsrsRating.Easy,
}

_STATE_MAP: dict[int, str] = {
    int(FsrsState.Learning): "learning",
    int(FsrsState.Review): "review",
    int(FsrsState.Relearning): "relearning",
}


@dataclass(frozen=True)
class ScheduleResult:
    """Outcome of scheduling one review."""

    due_at: datetime
    stability: float
    difficulty: float
    state: str


class Scheduler:
    """Deterministic FSRS-6 scheduler wrapper."""

    def __init__(self, desired_retention: float = 0.9) -> None:
        self._scheduler = FsrsScheduler(
            desired_retention=desired_retention,
            learning_steps=(),
            relearning_steps=(),
            enable_fuzzing=False,
        )

    def schedule(
        self,
        rating: str,
        stability: float | None = None,
        difficulty: float | None = None,
        due: datetime | None = None,
        last_review: datetime | None = None,
        now: datetime | None = None,
    ) -> ScheduleResult:
        """Schedule the next review.

        rating      : again/hard/good/easy
        stability   : prior stability            (None -> new card)
        difficulty  : prior difficulty           (None -> new card)
        due         : prior card due timestamp   (None -> new card)
        last_review : prior last-review timestamp(None -> new card)
        now         : review timestamp (naive -> assumed UTC)
        """
        if rating not in _RATING_MAP:
            raise ValueError(f"unknown rating: {rating!r}")

        review_at = _ensure_utc(now) if now is not None else datetime.now(timezone.utc)

        card = FsrsCard(due=review_at)
        if stability is not None and difficulty is not None and due is not None and last_review is not None:
            # restore a card that has already entered the Review state;
            # with empty learning/relearning steps every reviewed card is
            # in Review state, so this reconstruction is lossless.
            card.stability = stability
            card.difficulty = difficulty
            card.due = _ensure_utc(due)
            card.last_review = _ensure_utc(last_review)
            card.state = FsrsState.Review
            card.step = None

        new_card, _log = self._scheduler.review_card(
            card, _RATING_MAP[rating], review_datetime=review_at
        )
        return ScheduleResult(
            due_at=new_card.due,
            stability=new_card.stability,
            difficulty=new_card.difficulty,
            state=_STATE_MAP[int(new_card.state)],
        )


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def is_math_subject(subject: str | None) -> bool:
    """True for 数学 (and the legacy ``math`` code); everything else is 其他."""
    key = (subject or "").strip().casefold()
    return key.startswith(_MATH_SUBJECT_PREFIXES)


def desired_retention(subject: str | None, learner: Learner | None = None) -> float:
    """The learner's target retention for *subject*, with model defaults.

    Maths reviews can be spaced wider (0.85); language subjects want more
    frequent contact (0.90). Values outside (0, 1) are ignored rather than
    trusted — a hand-edited DB row must not make FSRS raise mid-review.
    """
    if is_math_subject(subject):
        default, stored = DEFAULT_RETENTION_MATH, getattr(
            learner, "desired_retention_math", None
        )
    else:
        default, stored = DEFAULT_RETENTION_OTHER, getattr(
            learner, "desired_retention_other", None
        )
    if isinstance(stored, (int, float)) and not isinstance(stored, bool):
        if 0.0 < float(stored) < 1.0:
            return float(stored)
    return default


# FSRS schedulers are cheap but stateless; one per retention value keeps the
# review path allocation-free without inventing a second scheduling rule.
_SCHEDULERS: dict[float, "Scheduler"] = {}


def scheduler_for(
    subject: str | None, learner: Learner | None = None
) -> "Scheduler":
    """The FSRS scheduler to use for one review of *subject*."""
    retention = desired_retention(subject, learner)
    scheduler = _SCHEDULERS.get(retention)
    if scheduler is None:
        scheduler = _SCHEDULERS[retention] = Scheduler(desired_retention=retention)
    return scheduler
