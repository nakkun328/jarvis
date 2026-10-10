"""Deterministic, offline detection of general Memory candidates.

This extractor plugs into ``MemoryConsolidator`` through the existing
``CandidateExtractor`` protocol. It only proposes ``ExtractedCandidate`` values:
staging, duplicate/conflict flags, review, approval and index refresh stay in
``consolidation.py``. It uses fixed Japanese/English patterns, no network and no
model, so a miss is expected and a hit is a review cue, not a verified fact.

Safety rules enforced here:

* Only ``user`` turns are fact sources; assistant text is never extracted.
* Turn text is data. Nothing in it can change extraction behavior.
* A turn that looks like it contains a credential yields no candidate at all and
  is reported by reason and offsets only. Reports never carry turn text.
* Hedged statements stay user statements with low confidence and a ``hedged`` tag.
  Inferences use ``MemoryOrigin.AI_INFERENCE`` with low confidence and an
  ``inference`` tag. Neither is ever approved by this module.
* Provenance is ``conversation:<id>:message:<n>:chars:<start>-<end>``: the exact
  half-open code-point span of the user's sentence in the original turn.
"""

import hashlib
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from backend.memory.consolidation import (
    CandidateExtractor,
    ConversationEvidence,
    Evidence,
    ExplicitExtractor,
    ExtractedCandidate,
)
from backend.memory.model import MemoryCategory, MemoryOrigin

MAX_TURN_CHARS = 4000
MAX_SENTENCE_CHARS = 300
MAX_SENTENCES = 60
MAX_CANDIDATES = 8
EXPLICIT_CONFIDENCE = 0.8
HEDGED_CONFIDENCE = 0.4
INFERENCE_CONFIDENCE = 0.3

_SPAN_SUFFIX = re.compile(r":chars:(\d+)-(\d+)$")


class SkipReason(StrEnum):
    NOT_USER_TURN = "not_user_turn"
    OVERSIZE = "oversize"
    CREDENTIAL = "credential"
    QUESTION = "question"
    HYPOTHETICAL = "hypothetical"
    QUOTED = "quoted"
    AMBIGUOUS = "ambiguous"
    TOO_LONG = "too_long"
    NO_PATTERN = "no_pattern"
    LIMIT = "limit"


@dataclass(frozen=True)
class SkippedSpan:
    """Where and why nothing was proposed. It deliberately carries no text."""

    reason: SkipReason
    start: int
    end: int


@dataclass(frozen=True)
class DetectionReport:
    candidates: tuple[ExtractedCandidate, ...]
    skipped: tuple[SkippedSpan, ...]


@dataclass(frozen=True)
class _Proposal:
    category: MemoryCategory
    topic: str
    content: str
    origin: MemoryOrigin
    confidence: float
    importance: float
    tags: tuple[str, ...] = ()
    project: str | None = None


def candidate_span(source: str) -> tuple[int, int] | None:
    """Return the half-open turn offsets encoded in a detected candidate's source."""
    match = _SPAN_SUFFIX.search(source)
    return (int(match.group(1)), int(match.group(2))) if match else None


@dataclass(frozen=True)
class _Rule:
    category: MemoryCategory
    slot: str
    patterns: tuple[re.Pattern[str], ...]
    keyed: bool = False  # True: group 1 selects one of many independent topics.
    importance: float = 0.5


def _p(*patterns: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern, re.IGNORECASE) for pattern in patterns)


_I = r"(?:私|僕|ぼく|俺|わたし|自分)"
_VALUE = r"([^、,]{1,40}?)"
_OBJECT = r"([^はがを、,]{1,40}?)"

_USER_RULES = (
    _Rule(
        MemoryCategory.USER, "user-name",
        _p(rf"^{_I}の名前は{_VALUE}(?:です|だ|といいます|と言います|と申します)?$",
           r"^my name is ([^,]{1,40})$", r"^(?:please )?call me ([^,]{1,40})$"),
        importance=0.6,
    ),
    _Rule(
        MemoryCategory.USER, "user-location",
        _p(rf"^(?:{_I}は)?{_VALUE}に住んで(?:いる|います|る)$",
           r"^i (?:currently |now )?live in ([^,]{1,40})$",
           r"^i(?:'m| am) (?:based|living) in ([^,]{1,40})$"),
        importance=0.6,
    ),
    _Rule(
        MemoryCategory.USER, "user-work",
        _p(rf"^(?:{_I}は)?{_VALUE}で(?:働いて|勤務して)(?:いる|います)$",
           r"^i work (?:at|for) ([^,]{1,40})$"),
        importance=0.6,
    ),
    _Rule(
        MemoryCategory.USER, "user-birthday",
        _p(rf"^{_I}の誕生日は{_VALUE}(?:です|だ)?$", r"^my birthday is ([^,]{1,30})$"),
        importance=0.5,
    ),
    _Rule(
        MemoryCategory.USER, "user-allergy",
        _p(rf"^(?:{_I}は)?{_OBJECT}アレルギー(?:が(?:あります|ある)|です|持ち(?:です)?)$",
           r"^i(?:'m| am) allergic to ([^,]{1,40})$"),
        keyed=True, importance=0.8,
    ),
    _Rule(
        MemoryCategory.USER, "user-favorite",
        _p(rf"^(?:{_I}の)?好きな([^はがを、,]{{1,20}}?)は{_VALUE}(?:です|だ)?$",
           r"^my favou?rite ([a-z ]{1,20}?) is ([^,]{1,40})$"),
        keyed=True,
    ),
    _Rule(
        MemoryCategory.USER, "user-dislikes",
        _p(rf"^(?:{_I}は)?{_OBJECT}(?:が|は)(?:嫌い|苦手)(?:です|だ)?$",
           r"^i (?:really |absolutely )?(?:hate|dislike|can't stand|cannot stand|don't like)"
           r" ([^,]{1,40})$"),
        keyed=True,
    ),
    _Rule(
        MemoryCategory.USER, "user-likes",
        _p(rf"^(?:{_I}は)?{_OBJECT}が(?:大?好き|好み)(?:です|だ)?$",
           r"^i (?:really |absolutely )?(?:like|love|enjoy|prefer) ([^,]{1,40})$"),
        keyed=True,
    ),
)

_PROJECT_RULES = (
    _Rule(
        MemoryCategory.PROJECT, "project-deadline",
        _p(r"(?:締め切り|締切|期限|納期)は.+", r"\b(?:deadline|due date) is\b.+"),
        importance=0.6,
    ),
    _Rule(
        MemoryCategory.PROJECT, "project-tooling",
        _p(r"(.{1,40}?)(?:を|で)(?:使(?:う|っている|っています|用している)|採用(?:する|している|しました))$",
           r"\b(?:uses?|is using|adopts?) ([^,]{1,40})$", r"\bwe use ([^,]{1,40})$"),
        keyed=True, importance=0.6,
    ),
    _Rule(
        MemoryCategory.PROJECT, "project-decision",
        _p(r"(.{1,60}?)(?:に決定|で決まり|に決めた|に決めました|と決めた)",
           r"\b(?:we(?:'ve)? decided|we agreed|decision is) (.{1,60})$"),
        keyed=True, importance=0.6,
    ),
)

_PROJECT_MARKER = re.compile(
    r"プロジェクト「([^」]{1,60})」|\bproject [\"“']([^\"”']{1,60})[\"”']"
    r"|このプロジェクト|当プロジェクト|うちのプロジェクト|\bthis project\b|\bour project\b",
    re.IGNORECASE,
)
_FUTURE = re.compile(
    r"今後は?|これからは?|次(?:回)?からは?|以後|以降|今度から"
    r"|\b(?:from now on|going forward|in the future|in future|next time)\b",
    re.IGNORECASE,
)
_REQUEST = re.compile(
    r"して|にして|ないで|ください|くれ|ほしい|欲しい|お願い|こと$|ように|なさい"
    r"|\b(?:please|always|never|don't|do not|use|keep|stop|answer|reply|respond|call me)\b",
    re.IGNORECASE,
)
_RESPONSE_NOUN = re.compile(
    r"回答|返事|返信|答え|説明|\b(?:answers?|repl(?:y|ies)|responses?|explanations?)\b",
    re.IGNORECASE,
)
_RESPONSE_REQUEST = re.compile(
    r"してください|にして|でお願い|ほしい|欲しい|ないで"
    r"|\b(?:please|should|must|always|never|keep)\b",
    re.IGNORECASE,
)
_INFERENCE_RULES = (
    (
        "brevity",
        re.compile(r"長すぎ|くどい|冗長|\btoo (?:long|verbose|wordy)\b", re.IGNORECASE),
        "推測（未確認）: ユーザーは簡潔な回答を好む可能性がある。",
        "Inference (unconfirmed): the user may prefer concise answers.",
    ),
    (
        "plain-language",
        re.compile(
            r"(?:説明|回答|答え|返事|文章).{0,10}(?:難しい|わかりにくい|分かりにくい)"
            r"|\btoo technical\b|\bjargon\b",
            re.IGNORECASE,
        ),
        "推測（未確認）: ユーザーは平易な説明を好む可能性がある。",
        "Inference (unconfirmed): the user may prefer plain-language explanations.",
    ),
)
_CORRECTION = re.compile(
    r"^(?:訂正|いや|違う|違います|間違(?:い|えました)|実は|やっぱり)"
    r"|^(?:actually|correction|sorry,? i meant|no,)\b",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"たぶん|多分|おそらく|恐らく|かもしれ|かも$|気がする|と思う|と思います|だろう|はず|らしい"
    r"|うろ覚え|確か|\b(?:maybe|perhaps|probably|possibly|i think|i guess|i believe|might"
    r"|not sure|not certain|if i remember|i suppose|likely)\b",
    re.IGNORECASE,
)
_LEADING_HEDGE = re.compile(
    r"^(?:たぶん|多分|おそらく|恐らく|確か|(?:maybe|perhaps|probably|possibly|i think(?: that)?"
    r"|i guess|i believe|i suppose)\b)[\s、,]*",
    re.IGNORECASE,
)
_TRAILING_HEDGE = re.compile(
    r"(?:と思(?:う|います|いました)|かもしれません?|かも|でしょう|だろう|はず(?:です)?|気がする|らしい)$"
    r"|[\s,]+(?:i think|i guess|maybe|probably|perhaps)$",
    re.IGNORECASE,
)
_QUESTION = re.compile(
    r"でしょうか$|ますか|ですか|教えて|知りたい|"
    r"^(?:what|how|why|when|where|who|which|can you|could you|would you|do you|does|is|are|"
    r"will|should)\b",
    re.IGNORECASE,
)
_HYPOTHETICAL = re.compile(
    r"(?<!か)もし(?!れ)|仮に|例えば|たとえば"
    r"|\b(?:if|suppose|supposing|imagine|for example|e\.g\.)(?:\b|\s)",
    re.IGNORECASE,
)
_QUOTE = re.compile(r"[「」『』\"“”]")
_REFERENT = re.compile(
    r"^(?:それ|これ|あれ|その|この|あの|彼|彼女|彼ら|何か|something|it|that|this|them|him|her"
    r"|they|those|these|one|things?)$",
    re.IGNORECASE,
)

_BOUNDARY = re.compile(r"[。！？!?；;\n]+|\.+(?=\s|$)")

_SECRET_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bsk-(?:proj-)?[A-Za-z0-9_-]{16,}",
        r"\bAIza[A-Za-z0-9_-]{20,}",
        r"\b(?:ghp_|gho_|github_pat_|glpat-)[A-Za-z0-9_-]{16,}",
        r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
        r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bbearer\s+[A-Za-z0-9._~+/=-]{12,}",
        r"password|passwd|passphrase|api[ _-]?key|secret[ _-]?key|access[ _-]?token"
        r"|auth[ _-]?token|private key|credentials?\b|\btokens?\s*(?:is|=|:)",
        r"パスワード|トークン|暗証番号|秘密鍵|秘密のキー|APIキー|アクセストークン|シークレット|認証情報|PINコード",
        # Long opaque strings (keys, digests) and card/ID-like digit runs.
        r"(?<![A-Za-z0-9])(?=[A-Za-z0-9+/_=-]*\d)(?=[A-Za-z0-9+/_=-]*[A-Za-z])"
        r"[A-Za-z0-9+/_=-]{32,}(?![A-Za-z0-9])",
        r"(?<!\d)(?:\d[ -]?){12,19}(?!\d)",
    )
)


def _looks_secret(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SECRET_PATTERNS)


def looks_credential(text: str) -> bool:
    """Public name for the same heuristic, reused by the chat auto-memory sensitivity floor."""
    return _looks_secret(text)


def _digest(value: str) -> str:
    folded = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return hashlib.sha256(folded.encode()).hexdigest()[:10]


def _sentences(text: str) -> list[tuple[int, int, bool]]:
    """Return trimmed sentence spans and whether the terminator was a question."""
    spans: list[tuple[int, int, bool]] = []
    start = 0
    pieces = [(m.start(), m.end(), m.group()) for m in _BOUNDARY.finditer(text)]
    pieces.append((len(text), len(text), ""))
    for stop, next_start, terminator in pieces:
        raw = text[start:stop]
        left = start + len(raw) - len(raw.lstrip())
        right = start + len(raw.rstrip())
        if right > left:
            spans.append((left, right, "?" in terminator or "？" in terminator))
        start = next_start
    return spans


class GeneralCandidateExtractor:
    """Offline pattern extractor for user, project and self memory proposals."""

    def __init__(
        self,
        *,
        max_chars: int = MAX_TURN_CHARS,
        max_candidates: int = MAX_CANDIDATES,
        explicit: CandidateExtractor | None = None,
    ) -> None:
        for value in (max_chars, max_candidates):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("Detection bounds must be positive integers")
        self.max_chars = max_chars
        self.max_candidates = max_candidates
        self._explicit = explicit or ExplicitExtractor()

    def extract(self, evidence: Evidence) -> Sequence[ExtractedCandidate]:
        if not isinstance(evidence, ConversationEvidence):
            return self._explicit.extract(evidence)  # Typed Self events keep their own path.
        report = self.detect(evidence)  # Applies the role, size and secret gates first.
        if any(
            item.reason in (SkipReason.NOT_USER_TURN, SkipReason.OVERSIZE, SkipReason.CREDENTIAL)
            for item in report.skipped
        ):
            return ()
        labelled = self._explicit.extract(evidence)  # Labelled "Remember topic: ..." turns.
        return labelled or report.candidates

    def detect(self, evidence: ConversationEvidence) -> DetectionReport:
        """Return candidates plus the reasons and offsets of everything skipped."""
        if not isinstance(evidence, ConversationEvidence) or not isinstance(evidence.text, str):
            raise TypeError("Detection requires conversation evidence")
        text = evidence.text
        if evidence.role != "user":
            return DetectionReport((), (SkippedSpan(SkipReason.NOT_USER_TURN, 0, len(text)),))
        if len(text) > self.max_chars:
            return DetectionReport((), (SkippedSpan(SkipReason.OVERSIZE, 0, len(text)),))
        if _looks_secret(text):
            # The whole turn is dropped: neighbouring text may name or locate the secret.
            return DetectionReport((), (SkippedSpan(SkipReason.CREDENTIAL, 0, len(text)),))
        candidates: list[ExtractedCandidate] = []
        skipped: list[SkippedSpan] = []
        seen: set[tuple[MemoryCategory, str, str]] = set()
        sentences = _sentences(text)
        for index, (start, end, asked) in enumerate(sentences):
            if index >= MAX_SENTENCES or len(candidates) >= self.max_candidates:
                skipped.append(SkippedSpan(SkipReason.LIMIT, start, len(text)))
                break
            result = self._classify(text[start:end], asked)
            if isinstance(result, SkipReason):
                skipped.append(SkippedSpan(result, start, end))
                continue
            identity = (result.category, result.topic, " ".join(result.content.casefold().split()))
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(
                ExtractedCandidate(
                    category=result.category,
                    topic=result.topic,
                    content=result.content,
                    source=f"{evidence.source}:chars:{start}-{end}",
                    origin=result.origin,
                    importance=result.importance,
                    confidence=result.confidence,
                    tags=result.tags,
                    project=result.project,
                )
            )
        return DetectionReport(tuple(candidates), tuple(skipped))

    def _classify(self, sentence: str, asked: bool) -> _Proposal | SkipReason:
        if len(sentence) > MAX_SENTENCE_CHARS:
            return SkipReason.TOO_LONG
        if asked or _QUESTION.search(sentence):
            return SkipReason.QUESTION
        if _HYPOTHETICAL.search(sentence):
            return SkipReason.HYPOTHETICAL
        correction = _CORRECTION.search(sentence) is not None
        hedged = _HEDGE.search(sentence) is not None
        # Match the clause itself; hedges and correction cues do not change the claim.
        body = _CORRECTION.sub("", sentence, count=1).lstrip(" 、,:：")
        body = _LEADING_HEDGE.sub("", body, count=1)
        body = _TRAILING_HEDGE.sub("", body, count=1).rstrip()
        project = _PROJECT_MARKER.search(sentence)
        if project is None and _QUOTE.search(sentence):
            return SkipReason.QUOTED
        extra = (("correction-signal",) if correction else ()) + (("hedged",) if hedged else ())

        def finish(
            category: MemoryCategory, topic: str, importance: float,
            tags: tuple[str, ...] = (), name: str | None = None,
        ) -> _Proposal:
            return _Proposal(
                category, topic, sentence, MemoryOrigin.USER_EXPLICIT,
                min(EXPLICIT_CONFIDENCE, HEDGED_CONFIDENCE) if hedged else EXPLICIT_CONFIDENCE,
                importance * 0.6 if hedged else importance,
                tuple(dict.fromkeys((*tags, *extra))), name,
            )

        if (_FUTURE.search(sentence) and _REQUEST.search(sentence)) or (
            _RESPONSE_NOUN.search(sentence) and _RESPONSE_REQUEST.search(sentence)
        ):
            return finish(
                MemoryCategory.SELF, f"self-instruction-{_digest(sentence)}", 0.7,
                ("candidate-kind:instruction",),
            )
        if project is not None:
            name = (project.group(1) or project.group(2)) if project.lastindex else None
            for rule in _PROJECT_RULES:
                matched = self._match(rule, body)
                if matched is SkipReason.AMBIGUOUS:
                    return matched
                if matched is not None:
                    return finish(rule.category, matched, rule.importance, name=name)
            return SkipReason.NO_PATTERN
        if _QUOTE.search(sentence):
            return SkipReason.QUOTED
        for rule in _USER_RULES:
            matched = self._match(rule, body)
            if matched is SkipReason.AMBIGUOUS:
                return matched
            if matched is not None:
                return finish(rule.category, matched, rule.importance)
        for slot, pattern, japanese, english in _INFERENCE_RULES:
            if pattern.search(sentence):
                cjk = re.search(r"[぀-ヿ一-鿿]", sentence) is not None
                content = f"{japanese if cjk else english} Evidence: {sentence}"
                return _Proposal(
                    MemoryCategory.SELF, f"self-inferred-{slot}", content,
                    MemoryOrigin.AI_INFERENCE, INFERENCE_CONFIDENCE, 0.3,
                    tuple(dict.fromkeys(("inference", f"inferred:{slot}", *extra))),
                )
        return SkipReason.NO_PATTERN

    @staticmethod
    def _match(rule: _Rule, body: str) -> str | SkipReason | None:
        for pattern in rule.patterns:
            found = pattern.search(body)
            if found is None:
                continue
            groups = [value.strip() for value in found.groups() if value]
            if any(_REFERENT.fullmatch(value) for value in groups):
                return SkipReason.AMBIGUOUS
            if rule.keyed and groups:
                return f"{rule.slot}-{_digest(groups[0])}"
            return rule.slot
        return None
