"""Indian number-plate grammar, confusion correction and multi-frame fusion.

Three ideas, in order of importance:

**A plate has a grammar, and the grammar tells you a character's class.** An Indian
registration is `AA 00 AA 0000` -- two state letters, an RTO number, a series of
letters, then up to four digits. Once a candidate is aligned to that shape, position 0
*must* be a letter and position 2 *must* be a digit, so an OCR that returned `0` at
position 0 is unambiguously wrong and `O` is the only sensible reading. Correction is
applied **only** where the grammar makes the class unambiguous; anywhere else, the
raw character stands.

**A correction is evidence, not a cleanup.** Every substitution is recorded with its
position, the raw value, the corrected value and the confidence at that position. A
corrected plate is never presented as a clean read -- the operator sees
`ANPR partial - 1 char corrected - 0.71`, and can judge it.

**N frames of one vehicle are one observation, not N.** Reading the same plate across
a track yields a per-character confidence distribution. Fusing those distributions
before deciding is strictly better than voting on N independently-decided strings,
because a character that was 0.4-confident in eight frames and 0.9-confident in two
should be decided by the two.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Sequence

# Confusion sets: characters an OCR genuinely confuses because they look alike. Each
# maps a character to the character of the *other* class it is mistaken for.
DIGIT_TO_LETTER: dict[str, str] = {
    "0": "O",
    "1": "I",
    "8": "B",
    "5": "S",
    "2": "Z",
    "6": "G",
    "7": "T",
    "4": "A",
    "9": "P",
}
LETTER_TO_DIGIT: dict[str, str] = {
    "O": "0",
    "D": "0",
    "Q": "0",
    "I": "1",
    "L": "1",
    "B": "8",
    "S": "5",
    "Z": "2",
    "G": "6",
    "T": "7",
    "A": "4",
    "P": "9",
}

# Every pair the sets above consider confusable, used to score fuzzy matches.
CONFUSABLE_PAIRS: frozenset[frozenset[str]] = frozenset(
    frozenset(pair) for mapping in (DIGIT_TO_LETTER, LETTER_TO_DIGIT) for pair in mapping.items()
)

# At most this many characters may be rewritten before a read is rejected as
# unparseable. Two is a genuine OCR near-miss; more is fabrication.
MAX_CORRECTIONS = 2

# Anchored, non-nested, no backtracking hazard. Groups: state, rto, series, number.
# Bounded quantifiers throughout, so a pathological input cannot cause catastrophic
# backtracking (a ReDoS on a public endpoint).
PLATE_RE = re.compile(r"^([A-Z]{2})(\d{1,2})([A-Z]{1,3})(\d{1,4})$")

# Bharat series (22BH1234AA) has a different shape and must not be forced through the
# state pattern, which would "correct" its trailing letters into digits.
BH_SERIES_RE = re.compile(r"^(\d{2})(BH)(\d{1,4})([A-Z]{1,2})$")


@dataclass
class Correction:
    """One character substitution, persisted to `detection.corrections`."""

    position: int
    raw: str
    corrected: str
    confidence: float
    reason: str

    def to_dict(self) -> dict[str, object]:
        return {
            "position": self.position,
            "raw": self.raw,
            "corrected": self.corrected,
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


@dataclass
class NormalisedPlate:
    raw: str
    normalised: str
    valid: bool
    corrections: list[Correction] = field(default_factory=list)
    pattern: str = "unknown"
    confidence: float = 0.0

    @property
    def is_clean(self) -> bool:
        """True only when nothing was corrected. Drives the UI's provenance badge."""
        return self.valid and not self.corrections


def _strip(text: str) -> str:
    """Uppercase and drop everything that cannot be part of a registration."""
    # 'IND', the emblem strip and hyphens all appear on real plates and in OCR output.
    out = re.sub(r"[^A-Z0-9]", "", text.upper())
    if out.startswith("IND") and len(out) > 9:
        out = out[3:]
    return out


def _class_of_position(cleaned: str) -> list[str] | None:
    """Return the required class ('A' letter, 'D' digit) for each position, or None.

    Alignment is by length against the standard layout. Where a length is ambiguous
    the function returns None rather than guessing, and no correction is applied.
    """
    n = len(cleaned)
    # state(2) + rto(1-2) + series(1-3) + number(1-4)
    layouts: dict[int, list[str]] = {
        9: list("AADDAADDDD"[:9]),
        10: list("AADDAADDDD"),
    }
    if n in layouts:
        return layouts[n]
    if n == 8:
        # Two readings are common (AA D AA DDDD and AA DD A DDDD) and we cannot tell
        # which without more information, so nothing is corrected at 8 characters.
        return None
    return None


def _apply_class_corrections(
    cleaned: str, classes: list[str], char_conf: Sequence[float] | None
) -> tuple[str, list[Correction]]:
    chars = list(cleaned)
    corrections: list[Correction] = []
    for i, (ch, want) in enumerate(zip(chars, classes)):
        conf = float(char_conf[i]) if char_conf and i < len(char_conf) else 0.0
        if want == "A" and ch.isdigit():
            repl = DIGIT_TO_LETTER.get(ch)
            if repl:
                corrections.append(Correction(i, ch, repl, conf, "grammar_expects_letter"))
                chars[i] = repl
        elif want == "D" and ch.isalpha():
            repl = LETTER_TO_DIGIT.get(ch)
            if repl:
                corrections.append(Correction(i, ch, repl, conf, "grammar_expects_digit"))
                chars[i] = repl
    return "".join(chars), corrections


def normalise_plate(raw: str, char_confidences: Sequence[float] | None = None) -> NormalisedPlate:
    """Normalise an OCR string to canonical Indian form, recording every correction."""
    cleaned = _strip(raw)
    mean_conf = sum(char_confidences) / len(char_confidences) if char_confidences else 0.0

    if not cleaned:
        return NormalisedPlate(raw=raw, normalised="", valid=False, confidence=mean_conf)

    # Bharat series first: its trailing letters would be destroyed by the state layout.
    if BH_SERIES_RE.match(cleaned):
        return NormalisedPlate(
            raw=raw,
            normalised=cleaned,
            valid=True,
            pattern="bh_series",
            confidence=mean_conf,
        )

    if PLATE_RE.match(cleaned):
        return NormalisedPlate(
            raw=raw,
            normalised=cleaned,
            valid=True,
            pattern="standard",
            confidence=mean_conf,
        )

    classes = _class_of_position(cleaned)
    if classes is None:
        # Length gives no unambiguous layout. Keep the raw reading rather than
        # inventing structure -- an unparseable plate is still evidence of a vehicle.
        return NormalisedPlate(
            raw=raw,
            normalised=cleaned,
            valid=False,
            pattern="unparsed",
            confidence=mean_conf,
        )

    corrected, corrections = _apply_class_corrections(cleaned, classes, char_confidences)

    # A grammar that has to rewrite a third of the string is not disambiguating a
    # near-miss, it is manufacturing a legal plate out of noise. Measured on real
    # footage this was the difference between plausible reads and confident nonsense:
    # a 9-character string with 4 substitutions is not evidence of anything.
    if len(corrections) > MAX_CORRECTIONS:
        return NormalisedPlate(
            raw=raw,
            normalised=cleaned,
            valid=False,
            pattern="too_many_corrections",
            confidence=mean_conf,
        )

    if PLATE_RE.match(corrected):
        return NormalisedPlate(
            raw=raw,
            normalised=corrected,
            valid=True,
            corrections=corrections,
            pattern="standard_corrected",
            confidence=mean_conf,
        )

    # Corrections did not produce a legal plate, so they were not justified. Discard
    # them: a correction that does not resolve the grammar is a guess, not a fix.
    return NormalisedPlate(
        raw=raw,
        normalised=cleaned,
        valid=False,
        pattern="unparsed",
        confidence=mean_conf,
    )


def is_confusable(a: str, b: str) -> bool:
    """True when two characters are a known OCR confusion pair."""
    return a == b or frozenset((a, b)) in CONFUSABLE_PAIRS


def confusion_aware_distance(a: str, b: str) -> tuple[int, int]:
    """(total differing positions, of which are explained by a confusion pair).

    Used by watchlist matching: a one-character difference that a known confusion set
    explains is a likely match, while the same difference between unrelated characters
    is a different vehicle.
    """
    if len(a) != len(b):
        return max(len(a), len(b)), 0
    diffs = [(x, y) for x, y in zip(a, b) if x != y]
    explained = sum(1 for x, y in diffs if is_confusable(x, y))
    return len(diffs), explained


# --------------------------------------------------------------- multi-frame fusion


@dataclass
class PlateObservation:
    """One OCR read of one plate in one frame."""

    text: str
    char_confidences: list[float]
    pts_ms: float


class PlateAccumulator:
    """Fuses observations of a single vehicle track into one decision.

    Reads of one plate differ in length, because OCR drops characters. Which end it
    drops depends on the model: the 9-slot recogniser we started with could not emit
    a 10-character plate at all and lost a leading character, while the 10-slot model
    that replaced it usually gets the length right and occasionally loses a trailing
    one. Fusing them requires knowing which.

    An earlier version always right-aligned, on the reasoning that Indian plates end
    in the numeric group. That is right for a dropped *leading* character and wrong
    for a dropped trailing one, and when it was wrong it did not fail quietly -- it
    shifted every position by one and voted character against unrelated character.
    Three reads of `KA25AB1542` (`KA25AD1542`, `KA25AD154`, `KA25A0154`) fused to
    `KA25A1154`: worse than the best single read, because the disagreement was
    manufactured by the alignment rather than present in the evidence.

    So alignment is now chosen per observation rather than assumed. A consensus is
    built from the reads that already agree on the modal length, and every shorter
    read is placed at whichever offset best matches it. Ties prefer the right-hand
    placement, keeping the old behaviour as the tie-breaker it should always have
    been.
    """

    def __init__(self) -> None:
        self._lengths: dict[int, float] = defaultdict(float)
        self.observations: list[PlateObservation] = []

    def add(self, text: str, char_confidences: Sequence[float], pts_ms: float = 0.0) -> None:
        cleaned = _strip(text)
        if not cleaned:
            return
        confs = [float(c) for c in char_confidences]
        self.observations.append(PlateObservation(cleaned, confs, pts_ms))
        # Weight the length vote by mean confidence, so a confident 10-character read
        # outweighs several hesitant 9-character ones.
        self._lengths[len(cleaned)] += (sum(confs) / len(confs)) if confs else 0.5

    @property
    def frame_count(self) -> int:
        return len(self.observations)

    def _conf_at(self, obs: PlateObservation, index: int) -> float:
        if index < len(obs.char_confidences):
            return float(obs.char_confidences[index])
        return 0.5

    #: How much weaker the evidence for a longer plate may be before it is dropped in
    #: favour of a shorter one. See `_decide_length`.
    LENGTH_BIAS = 0.5

    def _decide_length(self) -> int:
        """How many characters the plate has.

        Not simply the best-supported length. Support is summed across reads, so two
        hesitant nine-character reads outvote one confident ten-character read, and
        that is the wrong way round for this failure mode: OCR truncates far more
        readily than it invents. A nine-character read is exactly what a truncated
        ten-character plate looks like, whereas a ten-character read cannot be
        produced from a nine-character plate without the model hallucinating a glyph
        it had no pixels for.

        Real example, from camera 7 on the government feed: `GJ36AR0180` was read as
        `GJ36AR018`, `GJ11AS4498` as `GJ71AS449`, `GJ09DC6441` as `GJ09DC644`. Every
        one lost its last character, and a plain majority vote on length preserved
        the loss.

        So the longest length still carrying at least `LENGTH_BIAS` of the best
        length's support wins. The floor is what stops a single spurious long read
        from dragging the answer out: it has to be respectable evidence, not merely
        present.
        """
        if not self._lengths:
            return 0
        best_weight = max(self._lengths.values())
        floor = best_weight * self.LENGTH_BIAS
        return max(length for length, w in self._lengths.items() if w >= floor)

    def _reference(self, length: int) -> list[str]:
        """A provisional consensus to align ragged reads against."""
        exact = [o for o in self.observations if len(o.text) == length]
        pool = exact or self.observations
        votes: list[dict[str, float]] = [defaultdict(float) for _ in range(length)]
        for obs in pool:
            # Reads of the modal length align directly; anything else falls back to
            # the right-hand placement purely to seed the reference.
            offset = length - len(obs.text) if len(obs.text) < length else 0
            for i, ch in enumerate(obs.text):
                pos = i + offset
                if 0 <= pos < length:
                    votes[pos][ch] += self._conf_at(obs, i)
        return [(max(v.items(), key=lambda kv: kv[1])[0] if v else "?") for v in votes]

    def _best_offset(self, obs: PlateObservation, reference: list[str]) -> int:
        """Where this read sits within a plate of the reference's length."""
        length = len(reference)
        span = len(obs.text)
        if span >= length:
            return 0
        best_offset, best_score = length - span, -1.0  # right-aligned by default
        for offset in range(length - span + 1):
            score = sum(
                self._conf_at(obs, i)
                for i, ch in enumerate(obs.text)
                if reference[i + offset] == ch
            )
            # `>=` so that, among equal scores, the largest offset wins -- the
            # right-hand placement stays the tie-break.
            if score >= best_score:
                best_offset, best_score = offset, score
        return best_offset

    def fused(self) -> NormalisedPlate | None:
        """Decide the plate from accumulated evidence. None if nothing was observed."""
        if not self.observations:
            return None

        length = self._decide_length()
        reference = self._reference(length)

        votes: list[dict[str, float]] = [defaultdict(float) for _ in range(length)]
        counts: list[dict[str, int]] = [defaultdict(int) for _ in range(length)]
        for obs in self.observations:
            offset = self._best_offset(obs, reference)
            for i, ch in enumerate(obs.text):
                pos = i + offset
                if 0 <= pos < length:
                    votes[pos][ch] += self._conf_at(obs, i)
                    counts[pos][ch] += 1

        chars: list[str] = []
        confidences: list[float] = []
        for pos in range(length):
            if not votes[pos]:
                chars.append("?")
                confidences.append(0.0)
                continue
            ch, weight = max(votes[pos].items(), key=lambda kv: kv[1])
            total = sum(votes[pos].values())
            chars.append(ch)
            # Two independent things matter and both must be represented:
            #   agreement - what share of the evidence at this position backs the
            #               winner (1.0 for a single frame, which says nothing), and
            #   strength  - how confident the OCR itself was, averaged over the reads
            #               that voted for the winner.
            # Reporting agreement alone made every single-frame read 1.00 confident,
            # which is exactly backwards: one hesitant glance is our weakest evidence.
            agreement = weight / total if total else 0.0
            n_votes = counts[pos].get(ch, 0)
            strength = (weight / n_votes) if n_votes else 0.0
            confidences.append(agreement * strength)

        fused_text = "".join(chars)
        result = normalise_plate(fused_text, confidences)
        # Record how much evidence backs this decision; the UI shows it.
        result.confidence = sum(confidences) / len(confidences) if confidences else 0.0
        return result
