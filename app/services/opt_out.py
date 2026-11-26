"""
SMS opt-out / opt-in detection: a line-for-line port of the n8n SMS handler's "Opt-out Detector" Code node
(detector_version patch-2026-09-06-v14-veto-scope-footer-strip). Compliance logic: every arm, guard and version note
mirrors the workflow; change it only together with the workflow.

`detect(text, lead_opted_out)` returns what the node returns: is_opt_out, is_opt_in, is_reaction_echo.
The flow that acts on it (persist with public.process_opt_out, send the fixed confirmation, never let an agent
answer an opted-out lead) is in app/services/chat_service.py.

JavaScript regexes without the `u` flag treat \\w, \\b, \\d and \\W as ASCII; they are compiled with re.ASCII here.
The `u`-flag ones (\\p{P}, \\p{Extended_Pictographic}) use the explicit classes _PUNCT and _PICTO below.
"""
import re
import sys
import unicodedata
from dataclasses import dataclass

DETECTOR_VERSION = "patch-2026-09-06-v14-veto-scope-footer-strip (gateway port)"

# \p{P}: every Unicode punctuation character, as a regex class body.
_PUNCT = "".join(re.escape(chr(c)) for c in range(min(sys.maxunicode, 0x2FFFF) + 1)
                 if unicodedata.category(chr(c)).startswith("P"))
# \p{Extended_Pictographic} (approximation: the emoji and symbol blocks it covers).
_PICTO = (r"©®‼⁉™ℹ↔-↙↩↪⌚⌛⌨⏏⏩-⏳"
          r"⏸-⏺Ⓜ▪▫▶◀◻-◾☀-➿⤴⤵⬅-⬇⬛⬜"
          r"⭐⭕〰〽㊗㊙\U0001F000-\U0001FAFF")
_FILLER = rf"\s{_PUNCT}​-‏⁠︎️"

A = re.ASCII


def _fold_stop_keywords(value: str) -> str:
    """optout-stop-diacritics-20260912-v1: fold only STOP/STOPALL tokens (accented letters to plain)."""
    def fold(m: re.Match) -> str:
        token = m.group(0)
        folded = re.sub(r"[̀-ͯ]", "", unicodedata.normalize("NFD", token))
        return folded if re.fullmatch(r"(?:STOP|STOPALL)", folded, re.I) else token
    return re.sub(r"(?:[^\W\d_]|[̀-ͯ])+", fold, value or "")


_OWN_FOOTER = re.compile(r"\b(?:REPLY|TEXT|TXT)\s+STOP\s+TO\s+OPT[\s-]?OUT\b\.?", A)

_REACTION_ECHO = re.compile(
    rf"^[{_FILLER}]*(?:REMOVED\s+(?:AN?\s+)?\S{{1,30}}(?:\s+\S{{1,30}}){{0,2}}\s+FROM"
    r"|(?:LIKED|LOVED|EMPHASIZED|LAUGHED\s+AT|DISLIKED|QUESTIONED|REACTED(?:\s+\S{1,30})?)(?:\s+TO)?"
    rf"|[{_PICTO}][︎️​-‏⁠]*\s*(?:TO|A|на)"
    r"|le\s+gust[oóa]|le\s+encant[oóa]|no\s+le\s+gust[oóa]|se\s+ri[oó]\s+de|le\s+dio\s+risa"
    r"|enfatizó|destacó|exclamó\s+por|preguntó|reaccionó\s+con\s+\S{1,30}\s+a"
    r"|eliminó\s+\S{1,30}\s+de|se\s+quit[oó]\s+\S{1,30}\s+de|a\s+aim[ée]|a\s+ador[ée]|curtiu"
    r"|gostou\s+de|реакция\s+\S{1,30}\s+на"
    r"|отреагировал\S*\s+\S{1,30}\s+на"
    r"|用\S{1,8}回应了"
    r"|του/της\s+αρέσει\s+πολύ\s+το"
    r"|ضحك\(ت\)\s+على|وضع\(ت\)\s+\S{1,20}\s+على"
    r"|a\s+ajouté\s+(?:un|une)\s+\S{1,40}(?:\s+\S{1,20}){0,3}\s+à"
    r"|a\s+retiré\s+(?:un|une)\s+\S{1,40}(?:\s+\S{1,20}){0,3}\s+de)"
    rf"\s*[\"“«][\s\S]*[\"”»][{_FILLER}]*$",
    re.I,
)

_OPT_IN = [re.compile(p, A) for p in (
    r"^\W*(START|UNSTOP|SUBSCRIBE|RESUBSCRIBE|RESUME)\W*$",
    r"^(?:RE-?)?SUBSCRIBE\s+ME(\s+(BACK|UP|AGAIN))?(\s+PLEASE)?[.!?\s]*$",
    r"^START\s+ME(\s+(BACK|UP|AGAIN))?(\s+PLEASE)?[.!?\s]*$",
    r"^OPT\s+(ME\s+)?BACK\s+IN[.!?\s]*$",
    r"^(PUT|ADD)\s+ME\s+BACK(\s+(ON|IN)(\s+(THE\s+)?LIST)?)?[.!?\s]*$",
    r"^(?:OK\s+)?(?:U|YOU|Y'?ALL|YOU ALL)?\s*CAN\s+(?:TEXT|MESSAGE|CONTACT)\s+ME\s+AGAIN[.!?\s]*$",
    r"^RESUME\s+(?:TEXTS?|MESSAGES?|CONTACT)[.!?\s]*$",
    r"^I\s+WANT\s+TO\s+(OPT\s+BACK\s+IN|(?:RE-?)?SUBSCRIBE|START\s+(BACK|AGAIN))[.!?\s]*$",
)]
_CONVERSATIONAL_OPT_IN = re.compile(r"^(?:TEXT|MESSAGE|CONTACT)\s+ME(\s+(BACK|AGAIN))?(\s+PLEASE)?[.!?\s]*$", A)

_OPT_OUT_EXACT = ["STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "QUIT", "END", "OPT OUT", "OPTOUT", "OPT-OUT", "REMOVE",
                  "NOT INTERESTED", "WRONG NUMBER"]
_WHOLE_MESSAGE_EXACT = {"STOP", "CANCEL", "QUIT", "END", "NOT INTERESTED", "REMOVE"}
_STOP_TYPOS = ["STO", "STIP", "STOPE", "SOTP", "TSOP", "DTOP"]

_SOFT_SOURCES = [
    r"^(?:\W*)NOT INTERESTED(?:[.!?]+|\s*$)",
    r"\bREMOVE\s+(?:THIS|THE)\s+(?:NUMBER|PHONE\s+NUMBER|CONTACT\s+(?:INFO(?:RMATION)?|DETAILS?))(?=$|[.!?,;]|\s+(?:FROM|OFF)\s+(?:YOUR|THIS|THE)\s+(?:(?:CONTACT|CUSTOMER|MAILING|MARKETING|CALL|TEXT|SMS|DISTRIBUTION)\s+(?:LIST|RECORDS|DATABASE|SYSTEM)|SYSTEM|CRM|LIST|RECORDS|DATABASE|CONTACTS)\b)",
    r"\bPLEASE STOP\b(?!\s+(?:BY|OVER|IN|AT|ON|AROUND|HERE)\b)",
    r"\bSTOP IT\b",
    r"\bSTOP NOW\b",
    r"\bI WANT (?:IT|THIS|THESE|THE TEXTS?) TO STOP\b",
    r"\bSTOP TEXTING\b",
    r"\bSTOP MESSAGING\b",
    r"\bSTOP WRITING\b",
    r"\bSTOP CONTACTING\b",
    r"\bSTOP CALLING\b",
    r"\bSTOP SENDING\b",
    r"\bSTOP REACHING OUT\b",
    r"\bSTOP BOTHERING\b",
    r"\bDO\s*NOT\s*CALL\s*(LIST|REGISTRY)\b",
    r"\bDNC\s*(LIST|REGISTRY)\b",
    r"\bDON'?T TEXT\b",
    r"\bDON'?T MESSAGE\b",
    r"\bDON'?T CONTACT\b",
    r"\bDO NOT CONTACT\b",
    r"\bDO NOT TEXT\b",
    r"\bDO NOT MESSAGE\b",
    r"\b(?:DON['’]?T|DO\s+NOT|NEVER)\s+(?:EVER\s+|AGAIN\s+)?(?:CONTACT|TEXT|MESSAGE)\s+(?:ME|US)\b(?!\s+BACK\b)",
    r"\bTAKE ME OFF\b(?![-\s]?ROAD)",
    r"\bREMOVE ME\b",
    r"\bREMOVE MY\b",
    r"\bREMOVE\s+US\b",
    r"\bTAKE\s+US\s+(?:THE\s+)?(?:F+[UC*#@!]*K?(?:ING?)?\s+)?OFF\b(?![-\s]?ROAD)",
    r"\b(?:DELETE|LOO?SE|REMOVE)\s+MY\s+(?:F\w+\s+)?(?:(?:NUMBER|PHONE|CONTACT)(?:\s+NUMBER)?\b|#)",
    r"\bLEAVE\s+ME\s+(?:THE\s+)?(?:F+[UC*#@!]*K?(?:ING?)?\s+)?ALONE\b",
    r"\bNO MORE TEXT",
    r"\bNO MORE MESSAGE",
    r"\bNO MORE SMS",
    r"\bI('M| AM) NOT INTERESTED\b",
    r"\bNO NOT INTERESTED\b",
    r"\bNOT INTERESTED AT THIS TIME\b",
    r"\bWRONG PERSON\b",
    r"\bWRONG\s*#",
    r"\bWRONG\s*(?:NO\.?|NUMBER|PERSON)\b",
    r"\bYOU HAVE THE WRONG NUMBER\b",
    r"\bFUCK OFF\b",
    r"\bFUCK YOU\b",
    r"\bFUCK YOUR",
    r"\bGO AWAY\b",
    r"\bPISS OFF\b",
    r"\bSTFU\b",
    r"\bGO FUCK\b",
    r"\b(?:I|I['’]?VE|I\s+HAVE|WE|WE['’]?VE|WE\s+HAVE)\s+(?:ALREADY\s+|PREVIOUSLY\s+)?OPTED\s+OUT\b(?!\s+OF\s+(?:THE\s+|MY\s+|YOUR\s+|AN?\s+)?(?:EXTENDED\s+)?(?:WARRANTY|GAP|COVERAGE|PLAN|INSURANCE|PROTECTION|PROGRAM)\b)",
    r"\bPURGE MY (NUMBER|PHONE|CONTACT|INFO)\b",
    r"\b(GET|TAKE) ME OFF (YOUR|THIS) (LIST|APPEALS|CONTACT)\b",
    r"\bOFF YOUR APPEALS\b",
    r"\bOFF YOUR LIST\b",
    r"\bOPT ME OUT\b",
    r"\bQUIT (TEXTING|MESSAGING|CONTACTING|EMAILING|SENDING|BOTHERING)\b",
    r"\bTAKE MY NAME OFF\b",
    r"\b(BE )?REMOVED? FROM (THIS|YOUR|THE)\b",
    r"\bNO LONGER WISH\b",
    r"\bWISH TO BE CONTACTED\b",
    r"\bWISH TO (RECEIVE|GET) (THESE|ANY|MORE)\b",
    r"\bDON'?T WANT (ANY ?MORE|THESE|MORE OF|TO GET|TO RECEIVE)\b",
    r"\bDO NOT WANT (ANY ?MORE|THESE|MORE OF|TO GET|TO RECEIVE)\b",
    r"\bNO MORE OF THESE\b",
    r"\bSTOP (THESE|THE) (TEXTS?|MESSAGES?)\b",
]
_SOFT = [re.compile(s, A) for s in _SOFT_SOURCES]
# Only statements of disinterest may ever be overridden by an interest signal (codex-review-fixes-v1 [A]).
_DISINTEREST_SOURCES = {
    r"^(?:\W*)NOT INTERESTED(?:[.!?]+|\s*$)",
    r"\bI('M| AM) NOT INTERESTED\b",
    r"\bNO NOT INTERESTED\b",
    r"\bNOT INTERESTED AT THIS TIME\b",
}

_NEGATED_CTIA = (r"\b(?:DON['’]?T|DO\s+NOT|NEVER|WON['’]?T|CAN['’]?T|CANNOT|DIDN['’]?T|WOULDN['’]?T"
                 r"|SHOULDN['’]?T)\s+(?:(?:WANT|WISH|PLAN|INTEND|NEED|MEAN)\s+TO\s+)?"
                 r"(?:(?:OPT\s?-?\s?OUT|OPTOUT|UNSUBSCRIBE|STOPALL)\s*,?\s+(?:OR|AND|NOR)\s+)*")
_NEGATION_GUARDED_EXACT = {"OPT OUT", "OPTOUT", "OPT-OUT", "UNSUBSCRIBE", "STOPALL"}

_CONTEXTUAL_DO_NOT_TEXT = re.compile(
    r"\b(?:DON['’]?T|DO\s+NOT)\s+(?:EVER\s+)?TEXT\s+(?:ME\s+|US\s+)?(?:THE\s+|MY\s+|ANY\s+|THOSE\s+|THESE\s+|YOUR\s+)?"
    r"(?:PAYOFF|PAY\s+OFF|NUMBERS|FIGURES|PRIVATE|PRIVACY|SECURE|SENSITIVE)\b", A)
_NOT_CONTEXTUAL = re.compile(r"\b(STOP|UNSUBSCRIBE|REMOVE|NO MORE|LEAVE ME ALONE)\b", A)
_EXPLICIT_STOP = [re.compile(p, A) for p in (
    r"\b(?:STOP|STOPALL|UNSUBSCRIBE|OPTOUT|OPT ?- ?OUT)\b",
    r"\b(?:REMOVE ME|TAKE ME OFF|TAKE MY NAME OFF|OFF YOUR LIST|CONTACT LIST)\b",
    r"\b(?:DO NOT|DON'?T) (?:TEXT|CONTACT|CALL|MESSAGE)\b",
    r"\bWRONG\s*(?:NO\.?|NUMBER|PERSON)\b",
)]
_CONTRASTIVE = re.compile(r"\b(?:BUT|HOWEVER|THOUGH|ALTHOUGH)\b([^.!?]{0,130})", A)
_TAIL_NEGATED = re.compile(r"\b(?:NOT|N'T|NO|NEVER|DONT|DON'?T)\b", A)
_TAIL_INTEREST = re.compile(
    r"\b(?:CURIOUS|WORTH|VALUE|ESTIMATE|APPRAIS|HOW MUCH|OFFER|PRICE|NUMBERS?|WOULD LIKE TO KNOW|LIKE TO KNOW|UPDATE|INTERESTED)\b", A)
_VALUE_QUESTION = [re.compile(p, A) for p in (
    r"\bWHAT (?:IS|'S) IT WORTH\b",
    r"\bWHAT (?:WOULD|COULD) YOU (?:GIVE|PAY|OFFER)\b",
    r"\bCURIOUS\b[^.!?]{0,70}\b(?:VALUE|WORTH)\b",
    r"\bJUST CURIOUS\b",
    r"\b(?:GIVE|GET|SEE) ME (?:MY|THE|A) (?:VALUE|NUMBER|OFFER|ESTIMATE)\b",
    r"\b(?:TRADE ?-? ?IN|APPRAISAL) VALUE\b",
)]
_DONT_TEXT_FAMILY = re.compile(r"^(?:DON['’]?T|DO\s+NOT)\s+(?:EVER\s+|AGAIN\s+)?TEXT\b", A)
_BARE_AFFIRMATIVE = re.compile(
    r"^(?:Y|YE|YES|YEAH|YEP|YUP|SURE|OK|OKAY|ABSOLUTELY|DEFINITELY|CORRECT|AFFIRMATIVE|SOUNDS\s+GOOD|SI|SÍ)[.!?\s]*$")
_LEADING_CTIA = re.compile(r"^\W*(STOP|STOPALL|UNSUBSCRIBE|OPT\s?-?OUT|OPTOUT)\b(?!\s+(?:BY|OVER|IN|AT|ON|AROUND|HERE)\b)", A)
_POLITE_TAIL = r"(?:\s*[,.]*\s*(?:PLEASE|PLZ|PLS|THANKS?|THANK\s+YOU|TY))?\W*$"
_TRAILING_CTIA = re.compile(r"\b(?:STOP|STOPALL|UNSUBSCRIBE|OPT\s?-?OUT|OPTOUT)" + _POLITE_TAIL, A)
_TRAILING_NEGATED = re.compile(
    r"\b(?:DON['’]?T|DO\s+NOT|NEVER|WON['’]?T|CAN['’]?T|CANNOT|DIDN['’]?T|WOULDN['’]?T|SHOULDN['’]?T)"
    r"\s+(?:(?:WANT|WISH|PLAN|INTEND|NEED|MEAN)\s+TO\s+)?(?:STOP|STOPALL|UNSUBSCRIBE|OPT\s?-?OUT|OPTOUT)" + _POLITE_TAIL, A)
_TRAILING_NOUN_STOP = re.compile(r"\b(?:BUS|GAS|ONE|REST|TRUCK|PIT|FIRST|LAST|NEXT|QUICK|SHORT|FULL)\s+STOP" + _POLITE_TAIL, A)
_BARE_SYMBOL = re.compile(rf"^[{_FILLER}]*(?:[\U0001F6D1⛔\U0001F6AB\U0001F515][{_FILLER}]*)+$")
_DIGIT_TYPO_STOP = re.compile(r"^\W*(?:[0-9]TOP|S[0-9]OP|ST[0-9]P|STO[0-9])\W*$", A)
_LETTER_TYPO_STOP = {"STO", "STIP", "STOPE", "SOTP", "TSOP", "DTOP", "STOO", "SROP", "SYOP", "STOL"}
_BARE_OUT = [
    re.compile(r"^[^\w\"“”«»?]*OUT[^\w\"“”«»?]*$", A),
    re.compile(r"^\W*(?:COUNT\s+ME|LEAVE\s+ME|TAKE\s+ME)\s+OUT(?:\s+OF\s+(?:THIS|THESE|IT|THE\s+LIST))?" + _POLITE_TAIL, A),
]
_STOP_SPAM = re.compile(
    r"\b(?:DON['’]?T|DO\s+NOT|PLEASE\s+STOP|STOP)\s+(?:SEND(?:ING)?\s+)?(?:ME\s+|US\s+)?SPAM\s+(?:TEXTS?|MESSAGES?|SMS)\b", A)
_WRONG_NUMBER = [re.compile(p, A) for p in (r"\bWRONG\s*#", r"\bWRONG\s*(?:NO\.?|NUMBER|PERSON)\b", r"\b(IDK|I DON'?T KNOW)\s+WHO\b")]


@dataclass(frozen=True)
class Detection:
    is_opt_out: bool
    is_opt_in: bool
    is_reaction_echo: bool
    text: str  # the uppercased text the arms read (our own footer stripped)
    own_footer_stripped: bool


def detect(message: str, lead_opted_out: bool = False) -> Detection:
    """The n8n Opt-out Detector's verdict for the customer's latest message. `lead_opted_out`: the lead's status or
    lifecycle_stage is opted_out (enables the bare-affirmative and conversational opt-in arms)."""
    inbound = _fold_stop_keywords((message or "").strip().upper())
    text = _OWN_FOOTER.sub(" ", inbound).strip()
    stripped = text != inbound
    if _REACTION_ECHO.search(text):
        return Detection(False, False, True, text, stripped)

    is_opt_in = any(p.search(text) for p in _OPT_IN)

    def exact(word: str) -> bool:
        escaped = re.escape(word)
        if word in _WHOLE_MESSAGE_EXACT:
            return re.search(rf"^\W*{escaped}\W*$", text, A) is not None
        if word in _NEGATION_GUARDED_EXACT and re.search(_NEGATED_CTIA + escaped + r"\b", text, A):
            return False
        return re.search(rf"\b{escaped}\b", text, A) is not None

    is_exact = any(exact(w) for w in _OPT_OUT_EXACT)
    is_stop_typo = any(re.search(rf"^{t}[.!?\s]*$", text, A) or re.search(rf"\b{t}\b", text, A) for t in _STOP_TYPOS)

    contextual_do_not_text = bool(_CONTEXTUAL_DO_NOT_TEXT.search(text)) and not _NOT_CONTEXTUAL.search(text)
    explicit_stop = any(p.search(text) for p in _EXPLICIT_STOP)
    contrastive = _CONTRASTIVE.search(text)
    tail = contrastive.group(1) if contrastive else ""
    contrastive_interest = bool(contrastive) and bool(_TAIL_INTEREST.search(tail)) and not _TAIL_NEGATED.search(tail)
    value_question = any(p.search(text) for p in _VALUE_QUESTION)
    soft_hits = [p for p in _SOFT if p.search(text)]
    disinterest_only = bool(soft_hits) and all(p.pattern in _DISINTEREST_SOURCES for p in soft_hits)
    soft_overridden = disinterest_only and not explicit_stop and (contrastive_interest or value_question)
    after_veto = [p for p in soft_hits
                  if not (contextual_do_not_text and all(_DONT_TEXT_FAMILY.search(m.group(0)) for m in p.finditer(text)))]
    is_soft = bool(after_veto) and not soft_overridden

    leading = bool(_LEADING_CTIA.search(text))
    trailing = (bool(_TRAILING_CTIA.search(text)) and not _TRAILING_NEGATED.search(text)
                and not _TRAILING_NOUN_STOP.search(text))
    bare_symbol = bool(_BARE_SYMBOL.match(text))
    letters = re.sub(r"[^A-Za-z0-9]", "", text)
    normalized_exact = letters in {"STOP", "STOPALL", "UNSUBSCRIBE", "END", "QUIT", "CANCEL", "OPTOUT", "REMOVE"}
    collapsed = re.sub(r"(.)\1+", r"\1", letters)
    collapsed_exact = collapsed in {"STOP", "STOPAL", "UNSUBSCRIBE", "QUIT", "CANCEL", "OPTOUT", "REMOVE"}
    digit_typo = bool(_DIGIT_TYPO_STOP.search(text))
    letter_typo = letters in _LETTER_TYPO_STOP
    bare_out = any(p.search(text) for p in _BARE_OUT)
    repeated_stop = re.fullmatch(r"(?:STOP){2,}", letters) is not None
    stop_spam = bool(_STOP_SPAM.search(text))

    is_opt_out = (repeated_stop or stop_spam or is_exact or normalized_exact or collapsed_exact or leading or trailing
                  or is_stop_typo or is_soft or bare_symbol or digit_typo or letter_typo or bare_out)
    final_opt_in = (is_opt_in or ((bool(_BARE_AFFIRMATIVE.search(text)) or bool(_CONVERSATIONAL_OPT_IN.search(text)))
                                  and lead_opted_out)) and not is_opt_out
    return Detection(is_opt_out, final_opt_in, False, text, stripped)


OPT_OUT_CONFIRMATION = "You've been unsubscribed and won't receive further messages."
WRONG_NUMBER_CONFIRMATION = "Sorry about that - wrong number. You've been unsubscribed and won't receive further messages."


def confirmation_text(message: str) -> str:
    """n8n "Build Opt-Out Confirmation" (opt-out branch): fixed text, never written by a model."""
    text = (message or "").strip().upper()
    return WRONG_NUMBER_CONFIRMATION if any(p.search(text) for p in _WRONG_NUMBER) else OPT_OUT_CONFIRMATION
