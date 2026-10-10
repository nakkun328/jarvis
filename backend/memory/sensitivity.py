"""Deterministic floor of "never remember this" for automatic chat memory.

A best-effort filter, not a guarantee: it is fixed Japanese/English patterns over NFKC-folded
text. It is applied to the owner's message before anything is sent to a model and again to every
extracted fact and quote. A hit means the whole item (or turn) is skipped and nothing is stored.
The categories are documented in docs/chat-auto-memory.md. It never returns or logs the text.
"""

import re
import unicodedata

from backend.memory.candidate_detection import looks_credential

# The owner marked this as private or asked not to be remembered: the WHOLE turn is skipped.
_PRIVATE_MARKERS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"内緒|ないしょ|内密|秘密|ひみつ|オフレコ|ここだけの話|ここだけの秘密|誰にも言わないで",
        r"覚え(ないで|なくて(?:も)?(?:いい|良い|大丈夫)|ちゃダメ|なくていい)",
        r"(記憶|メモ|保存|記録|保管)(しないで|しなくて(?:も)?(?:いい|良い|大丈夫)|は不要)",
        r"プライベート|非公開",
        r"\b(?:off the record|confidential|keep (?:this|it) (?:private|secret)"
        r"|do not (?:remember|save|store|record)|don'?t (?:remember|save|store|record)"
        r"|private|secret)\b",
    )
)

_SENSITIVE = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        # Contact data and precise addresses.
        r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+",
        r"(?<!\d)0\d{1,4}[-ー\s]?\d{1,4}[-ー\s]?\d{3,4}(?!\d)",
        r"(?<!\d)\+\d{1,3}[-\s]?\d{1,4}[-\s]?\d{2,4}",
        r"〒\s*\d{3}-?\d{4}|(?<!\d)\d{3}-\d{4}(?!\d)",
        r"(?:都|道|府|県).{0,12}(?:市|区|町|村).{0,24}(?:\d+\s*(?:丁目|番|-)|号室)",
        r"(?:マンション|アパート|ハイツ|コーポ).{0,20}\d+\s*号室",
        r"\b\d{1,5}\s+\w+(?:\s\w+)?\s+(?:street|st|avenue|ave|road|rd|lane|drive|blvd)\b",
        # Card, bank and government identifiers.
        r"マイナンバー|個人番号|運転免許|免許証|パスポート|旅券|保険証|被保険者|年金番号|在留カード",
        r"クレジット\s*カード|カード番号|口座番号|銀行口座|口座|振込先|セキュリティコード",
        r"social security|\bssn\b|passport|credit card|debit card|bank account|\biban\b"
        r"|routing number|driver'?s licen[cs]e",
        # Health and medical.
        r"病気|病院|病歴|診断|診察|症状|服薬|通院|治療|手術|入院|持病|処方|薬を飲|医療|医師|"
        r"カウンセリング|うつ病|鬱|精神(?:科|疾患|的な不調)|発達障害|障害者|障がい|依存症|"
        r"癌|がん\b|ガン\b|糖尿|高血圧|妊娠|不妊|認知症|感染症|アレルギー|ADHD|ＡＤＨＤ|"
        r"\b(?:disease|diagnos\w*|symptoms?|medications?|therapy|therapist|depress\w*|cancer"
        r"|pregnan\w*|disorders?|allerg\w*|illness|surgery|mental health|disabilit\w*"
        r"|prescription|hospital)\b",
        # Sexual life and orientation.
        r"性的|セックス|性行為|性癖|ポルノ|アダルト|風俗|恋愛対象|性自認|性指向|ゲイ|レズビアン|"
        r"バイセクシャル|ＬＧＢＴ|LGBT|トランスジェンダー|性別違和|"
        r"\b(?:sexual\w*|porn\w*|erotic\w*|fetish\w*|gay|lesbian|bisexual|transgender|sex)\b",
        # Political, religious and similar beliefs; ethnicity.
        r"支持政党|政党|自民党|立憲民主|共産党|公明党|維新|投票先|投票し|政治的|思想|右翼|左翼|"
        r"宗教|信仰|信者|創価|統一教会|神道|仏教徒|キリスト教|イスラム|ムスリム|クリスチャン|"
        r"人種|民族|部落|在日",
        r"\b(?:political\w*|vote[ds]? for|religio\w*|christian\w*|muslim\w*|buddhis\w*|jewish"
        r"|atheis\w*|church|mosque|ethnic\w*|race)\b",
        # Minors' personal details.
        r"息子|娘|子供|子ども|こども|お子さん|未成年|幼児|赤ちゃん|乳児|小学生|中学生|高校生|"
        r"園児|児童|生徒",
        r"\bmy (?:son|daughter|child|children|kid|kids|baby|toddler)\b|\bminors?\b",
        # Money, criminal record.
        r"年収|月収|給与|給料|貯金|借金|負債|ローン|資産|"
        r"\b(?:income|salary|debt|mortgage)\b|逮捕|前科|犯罪歴",
    )
)

_URL = re.compile(r"https?://", re.IGNORECASE)
_CODE_FENCE = "```"
MAX_PASTED_LINES = 12


def fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def has_private_marker(text: str) -> bool:
    """The owner asked for this not to be remembered or called it private."""
    folded = fold(text)
    return any(pattern.search(folded) for pattern in _PRIVATE_MARKERS)


def is_sensitive(text: str) -> bool:
    """Whether the text looks like credentials or personal data that must not be stored."""
    folded = fold(text)
    return looks_credential(folded) or any(pattern.search(folded) for pattern in _SENSITIVE)


def looks_pasted(text: str) -> bool:
    """Whether the message looks like pasted external content (code, several links, a long block)
    rather than the owner's own words. Chat has no flag for this, so it is judged by shape."""
    folded = fold(text)
    return (
        _CODE_FENCE in folded
        or len(_URL.findall(folded)) >= 2
        or folded.count("\n") + 1 > MAX_PASTED_LINES
    )
