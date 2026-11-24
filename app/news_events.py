"""Pure helpers for the news pipeline: query building, relevance, local scoring, hard-event
classification and recency/source weighted aggregation. No I/O, no heavy dependencies.

Headlines are untrusted text: everything here only pattern-matches on them.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone

# --- query ---------------------------------------------------------------------------------


def build_news_query(symbol: str, suffix: str | None = " stock", name: str | None = None) -> str:
    """Google News query for a ticker: ``"NVDA" stock``. The suffix is stripped and re-joined
    with one space in code, so an env parser that eats the leading space cannot glue the words
    together (``NVDAstock``). The ticker is quoted so Google matches it as a phrase. Word-like tickers (ON, ARM, MU, NOW) are
    searched by company name instead (``name``): ``"ON" stock`` returns noise."""
    q = f'"{(name or symbol).strip()}"' if name else f'"{symbol.strip().upper()}"'
    suffix = (suffix or "").strip()
    return f"{q} {suffix}" if suffix else q


def query_name_for(symbol: str) -> str | None:
    """Company name to search for when the ticker alone is ambiguous, else None."""
    al = aliases_for(symbol)
    return al[0] if symbol.upper() in _NAME_REQUIRED and al else None


_URL_SUFFIX_RE = re.compile(r"(\{symbol\})(?:\+|%20|%2B)stock\b", re.I)


def normalise_rss_template(template: str) -> str:
    """Old templates hard-coded ``{symbol}+stock``; the suffix is now added in code, so drop
    that part to avoid querying ``"NVDA" stock stock``."""
    return _URL_SUFFIX_RE.sub(r"\1", template)


# --- company names / relevance -------------------------------------------------------------

# Lower-case aliases matched as whole words/phrases. Built-in for the watchlist universes; the
# yfinance summary registers more at runtime (register_name). Unknown symbols: ticker only.
COMPANY_NAMES: dict[str, tuple[str, ...]] = {
    "AAPL": ("apple",), "MSFT": ("microsoft",), "NVDA": ("nvidia",), "AMZN": ("amazon",),
    "GOOGL": ("alphabet", "google"), "GOOG": ("alphabet", "google"), "META": ("meta platforms", "facebook", "instagram"),
    "NFLX": ("netflix",), "ORCL": ("oracle",), "CRM": ("salesforce",), "ADBE": ("adobe",),
    "NOW": ("servicenow",), "INTU": ("intuit",), "PANW": ("palo alto networks",),
    "CRWD": ("crowdstrike",), "FTNT": ("fortinet",), "ZS": ("zscaler",), "DDOG": ("datadog",),
    "SNOW": ("snowflake",), "NET": ("cloudflare",), "PLTR": ("palantir",), "SHOP": ("shopify",),
    "UBER": ("uber",), "ANET": ("arista",), "DELL": ("dell technologies", "dell"),
    "HPE": ("hewlett packard enterprise", "hpe"), "CSCO": ("cisco",), "IBM": ("ibm", "international business machines"),
    "ACN": ("accenture",), "MDB": ("mongodb",), "TEAM": ("atlassian",), "WDAY": ("workday",),
    "AMD": ("advanced micro devices", "amd"), "AVGO": ("broadcom",),
    "TSM": ("tsmc", "taiwan semiconductor"), "ASML": ("asml",), "QCOM": ("qualcomm",),
    "TXN": ("texas instruments",), "INTC": ("intel",), "MU": ("micron",),
    "AMAT": ("applied materials",), "LRCX": ("lam research",), "KLAC": ("kla corp", "kla corporation", "kla"),
    "ADI": ("analog devices",), "MRVL": ("marvell",), "NXPI": ("nxp semiconductors", "nxp"),
    "MCHP": ("microchip technology", "microchip"), "ON": ("onsemi", "on semiconductor"),
    "MPWR": ("monolithic power",), "SWKS": ("skyworks",), "QRVO": ("qorvo",),
    "ARM": ("arm holdings",), "SMCI": ("super micro computer", "supermicro", "super micro"),
    "ALAB": ("astera labs",), "CRDO": ("credo technology", "credo"), "COHR": ("coherent",),
    "ENTG": ("entegris",), "TER": ("teradyne",), "ONTO": ("onto innovation",),
    "FORM": ("formfactor",), "LSCC": ("lattice semiconductor", "lattice"), "RMBS": ("rambus",),
    "GFS": ("globalfoundries",), "STM": ("stmicroelectronics", "stmicro"), "UMC": ("united microelectronics", "umc"),
    "ASX": ("ase technology", "ase industrial"), "SNPS": ("synopsys",), "CDNS": ("cadence design", "cadence"),
    # ETFs: broad ones are mentioned by index name more often than by ticker.
    "SPY": ("spdr s&p 500", "s&p 500"), "SPYM": ("spdr portfolio s&p 500", "s&p 500"),
    "QQQ": ("invesco qqq", "nasdaq-100", "nasdaq 100"), "QQQM": ("invesco nasdaq 100", "nasdaq-100", "nasdaq 100"),
    "IWM": ("russell 2000", "ishares russell 2000"), "DIA": ("dow jones industrial",),
    "XLK": ("technology select sector",), "XLF": ("financial select sector",),
    "XLE": ("energy select sector",), "XLV": ("health care select sector",),
    "XLY": ("consumer discretionary select sector",), "IEFA": ("ishares core msci eafe",),
    "IAU": ("ishares gold trust",), "TLT": ("ishares 20+ year treasury", "20-year treasury"),
}

# Tickers that are also ordinary words / very common abbreviations. A bare uppercase match is
# NOT enough for these: the company name (or an exchange-qualified ticker) must appear.
_NAME_REQUIRED = frozenset({
    "ON", "ARM", "MU", "NOW", "ALAB", "A", "ALL", "IT", "KEY", "F", "T", "V", "C", "BIG", "FAST",
    "GOLD", "LOW", "MAR", "TEAM", "NET", "FORM", "SNOW", "UBER", "META", "TER", "ADI", "ZS",
    "DELL", "COHR", "CRDO", "ASX", "STM", "UMC"})
AMBIGUOUS_TICKERS = _NAME_REQUIRED

_runtime_names: dict[str, tuple[str, ...]] = {}
_NAME_SUFFIXES = re.compile(
    r"[,.]?\s+(?:incorporated|inc|corporation|corp|company|co|holdings?|holding|ltd|limited|plc|"
    r"n\.?v|s\.?a|ag|se|group|class [abc]|common stock|ads|adr)\.?(?=\s|$|,)", re.I)


def derive_aliases(full_name: str | None) -> tuple[str, ...]:
    """'Applied Materials, Inc.' -> ('applied materials',). Names under 4 chars are dropped."""
    if not full_name:
        return ()
    name = str(full_name).strip()
    prev = None
    while prev != name:
        prev = name
        name = _NAME_SUFFIXES.sub("", name).strip(" ,.")
    name = name.lower()
    return (name,) if len(name) >= 4 else ()


def register_name(symbol: str, full_name: str | None) -> None:
    """Remember a company name learned at runtime (yfinance). Built-in aliases win."""
    sym = symbol.upper()
    if sym in COMPANY_NAMES or sym in _runtime_names:
        return
    aliases = derive_aliases(full_name)
    if aliases:
        _runtime_names[sym] = aliases


def aliases_for(symbol: str) -> tuple[str, ...]:
    sym = symbol.upper()
    return COMPANY_NAMES.get(sym) or _runtime_names.get(sym) or ()


_alias_re_cache: dict[str, re.Pattern | None] = {}
_ticker_re_cache: dict[str, re.Pattern] = {}


def _alias_re(symbol: str) -> re.Pattern | None:
    sym = symbol.upper()
    if sym not in _alias_re_cache:
        al = aliases_for(sym)
        _alias_re_cache[sym] = (re.compile(
            r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in al) + r")(?:'s|’s)?(?![a-z0-9])", re.I)
            if al else None)
    return _alias_re_cache[sym]


def _ticker_re(symbol: str) -> re.Pattern:
    sym = symbol.upper()
    if sym not in _ticker_re_cache:
        e = re.escape(sym)
        # Case-sensitive whole word, e.g. "NVDA", "$NVDA", "(NASDAQ: NVDA)".
        _ticker_re_cache[sym] = re.compile(rf"(?<![A-Za-z0-9.]){e}(?![A-Za-z0-9])")
    return _ticker_re_cache[sym]


_EXCH_RE_T = r"(?:NASDAQ|NYSE|NYSEARCA|AMEX|OTC|TSX|LSE)\s*[:\-]\s*"


def _text_mentions(symbol: str, text: str) -> bool:
    sym = symbol.upper()
    if not text:
        return False
    rx = _alias_re(sym)
    if rx is not None and rx.search(text):
        return True
    if sym in _NAME_REQUIRED:
        # Only an exchange-qualified or cashtag ticker counts without the company name.
        return bool(re.search(rf"(?:{_EXCH_RE_T}|\$){re.escape(sym)}(?![A-Za-z0-9])", text))
    return bool(_ticker_re(sym).search(text))


def relevance(symbol: str, title: str, summary: str = "", use_summary: bool = False) -> float:
    """1.0 when the headline names the company/ticker, else 0. With ``use_summary`` a mention
    only in the summary counts 0.5 (off by default: roundup summaries name every stock).

    Tickers match as whole, upper-case words; word-like tickers (ON, ARM, MU, NOW, ALAB ...)
    need the company name."""
    if _text_mentions(symbol, title or ""):
        return 1.0
    if not use_summary:
        return 0.0
    s = (summary or "").strip()
    if s and s.lower() != (title or "").strip().lower() and _text_mentions(symbol, s):
        return 0.5
    return 0.0


# --- time ----------------------------------------------------------------------------------


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def item_time(item: dict) -> datetime | None:
    return parse_ts(item.get("published_at")) or parse_ts(item.get("fetched_at"))


def age_hours(item: dict, now: datetime) -> float | None:
    t = item_time(item)
    return None if t is None else max(0.0, (now - t).total_seconds() / 3600.0)


def fmt_age(hours: float | None) -> str:
    if hours is None:
        return "age unknown"
    if hours < 1:
        return f"{int(hours * 60)}m ago"
    if hours < 48:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


# --- source quality ------------------------------------------------------------------------

_SOURCE_TIERS: tuple[tuple[float, tuple[str, ...]], ...] = (
    (1.0, ("reuters", "bloomberg", "wall street journal", "wsj", "financial times", "cnbc",
           "associated press", "ap news", "barron", "investor's business daily", "investors business daily")),
    (0.85, ("marketwatch", "yahoo finance", "the information", "new york times", "washington post",
            "fortune", "forbes", "business insider", "axios", "techcrunch", "the verge")),
    (0.7, ("benzinga", "seeking alpha", "thestreet", "tipranks", "investing.com", "morningstar",
           "globenewswire", "business wire", "pr newswire", "prnewswire", "accesswire", "nasdaq.com")),
    (0.5, ("motley fool", "zacks", "investorplace", "simply wall st", "stocktwits", "gurufocus")),
    (0.35, ("insider monkey", "marketbeat", "tradingview", "msn", "benzinga india", "invezz",
            "24/7 wall st", "coincentral")),
)


def source_weight(source: str | None) -> float:
    s = (source or "").lower()
    if not s:
        return 0.5
    for weight, names in _SOURCE_TIERS:
        if any(n in s for n in names):
            return weight
    return 0.5


# --- local lexicon scorer ------------------------------------------------------------------

_POS = (
    (r"\bbeats?\b|\btops?\b (?:estimates|expectations|forecasts)|\bbetter[- ]than[- ]expected\b|\bsurpass", 1.3),
    (r"\braises?\b|\braised\b|\blifts?\b|\bhikes?\b|\bboosts?\b", 0.8),
    (r"\bupgrad(?:e|es|ed|ing)\b|\boutperform|\boverweight\b|\bbuy rating\b|\bprice target (?:raised|hiked|boosted)", 1.2),
    (r"\brecord (?:revenue|sales|profit|earnings|high|quarter|results|demand)", 1.2),
    (r"\bsurg(?:e|es|ed|ing)\b|\bsoar(?:s|ed|ing)?\b|\bjump(?:s|ed|ing)?\b|\brall(?:y|ies|ied)\b|\bclimb(?:s|ed|ing)?\b|"
     r"\bspik(?:e|es|ed)\b|\bskyrocket|\bpop(?:s|ped)?\b", 1.0),
    (r"\bgains?\b|\bgained\b|\brises?\b|\brose\b|\bup \d|\bhigher\b|\bstrong(?:er)?\b|\brobust\b|\bbullish\b|\boptimis", 0.6),
    (r"\bapprov(?:al|ed|es)\b|\bwins?\b|\bwon\b|\bawarded?\b|\bpartnership\b|\bcontract\b|\bexpands?\b|\bbreakthrough\b|"
     r"\bbuyback\b|\brepurchase\b|\bdividend (?:increase|hike)", 0.8),
    (r"\bgrowth\b|\bdemand\b|\bboom\b|\bmomentum\b|\bacceler", 0.4),
    (r"\b(?:trad(?:es|ing)|is|are|stock|shares?)\s+(?:sharply |slightly )?up\b|\bup today\b", 0.6),
)
_NEG = (
    (r"\bmiss(?:es|ed)?\b|\bfalls? short\b|\bfell short\b|\bworse[- ]than[- ]expected\b|\bdisappoint", 1.3),
    (r"\bcuts?\b|\blowers?\b|\bslash(?:es|ed)?\b|\btrims?\b|\breduces?\b|\bwithdraws?\b", 0.9),
    (r"\bdowngrad(?:e|es|ed|ing)\b|\bunderperform|\bunderweight\b|\bsell rating\b|\bprice target (?:cut|lowered|slashed)", 1.3),
    (r"\bplunge[sd]?\b|\bplummet|\btumbl(?:e|es|ed|ing)\b|\bslump(?:s|ed|ing)?\b|\bcrash(?:es|ed|ing)?\b|\bsink(?:s|ing)?\b|"
     r"\bsank\b|\btank(?:s|ed|ing)?\b|\bsell[- ]?off\b|\bslid(?:e|es|ing)\b|\bslid\b|\bslips?\b|\bslipp(?:ed|ing)\b|\bdives?\b|\bdove\b", 1.2),
    (r"\bfalls?\b|\bfell\b|\bdrops?\b|\bdropped\b|\bdeclines?\b|\bdeclined\b|\bdown \d|\blower\b|\bweak(?:er|ness)?\b|\bslows?\b|"
     r"\bbearish\b|\bpessimis|\bloss(?:es)?\b", 0.7),
    (r"\bwarns?\b|\bwarning\b|\bprobe\b|\binvestigat|\blawsuit|\bsued?\b|\bsues\b|\bfraud\b|\brecall\b|\blayoffs?\b|"
     r"\bbankrupt|\bdelist|\bdilut|\bgoing concern\b|\bsubpoena", 1.1),
    (r"\b(?:trad(?:es|ing)|is|are|stock|shares?)\s+(?:sharply |slightly )?down\b|\bdown today\b|\bworst (?:week|day|month)\b", 0.8),
    (r"\bconcerns?\b|\bfears?\b|\brisks?\b|\bworr(?:y|ies|ied)\b|\btariffs?\b|\bban(?:s|ned)?\b|\brestrict|\bdelays?\b|"
     r"\bhalts?\b|\bscrutiny\b|\bheadwinds?\b|\bglut\b|\bbubble\b", 0.6),
)
_POS_RX = tuple((re.compile(p, re.I), w) for p, w in _POS)
_NEG_RX = tuple((re.compile(p, re.I), w) for p, w in _NEG)
_NEGATION_RX = re.compile(r"\b(?:no|not|never|avoids?|averts?|dismiss(?:es|ed)?|denies|rules? out)\b\W+(?:\w+\W+){0,2}$", re.I)


_RESOLVED_SPAN_RX = re.compile(
    r"\b(?:drops?|dropped|ends?|ended|closes|closed|abandons?|scraps?)\s+(?:the |its |an? |all )?(?:\w+ ){0,2}"
    r"(?:investigation|probe|inquiry|lawsuit|suit|case|charges?)\b(?: (?:into|against|of)\b)?|\bdismissal of\b", re.I)


def local_score(title: str, summary: str = "") -> tuple[float, str]:
    """Deterministic finance-lexicon score in [-0.8, 0.8] and a label. Pure Python, no models.
    A match preceded by a negation within three words ("no lawsuit") is ignored."""
    text = (title or "").strip()
    if not text:
        return 0.0, "neutral"
    text = _RESOLVED_SPAN_RX.sub(" ", text)          # "SEC drops investigation" is not bad news
    total = 0.0
    for sign, table in ((1.0, _POS_RX), (-1.0, _NEG_RX)):
        for rx, weight in table:
            for m in rx.finditer(text):
                if _NEGATION_RX.search(text[:m.start()]):
                    continue
                total += sign * weight
                break                      # one hit per pattern group: repeats don't stack
    score = max(-0.8, min(0.8, math.tanh(0.55 * total)))
    score = round(score, 3)
    label = "bullish" if score >= 0.15 else "bearish" if score <= -0.15 else "neutral"
    return score, label


# --- hard events ---------------------------------------------------------------------------

# Law-firm "investigation on behalf of investors" press releases are noise, not events.
_NOISE_RX = re.compile(
    r"on behalf of (?:\w+\s+){0,2}(?:investors|shareholders|stockholders)|investors who (?:lost|suffered|purchased|bought)|"
    r"encourages? (?:\w+\s+){0,2}investors|\bllp\b|law (?:firm|offices?)|pomerantz|rosen law|bragar|levi\s*&\s*korsinsky|"
    r"faruqi|glancy|schall|kessler topaz|gross law|bronstein|howard g\. smith|johnson fistel|class period|"
    r"lead plaintiff|investor alert|shareholder alert", re.I)

# (event, label, severity polarity, regex). "neg" events veto new entries; "pos" are informational.
_EVENTS: tuple[tuple[str, str, str, re.Pattern], ...] = tuple(
    (e, lab, pol, re.compile(rx, re.I)) for e, lab, pol, rx in (
        ("downgrade", "analyst downgrade", "neg",
         r"\bdowngrad(?:e|es|ed|ing)\b(?!.{0,25}\bupgrad)|\bcut(?:s|ting)? (?:to|rating to) (?:sell|underperform|neutral|hold|underweight|reduce)\b|"
         r"\bslashes? (?:its )?rating\b"),
        ("guidance_cut", "guidance cut / warning", "neg",
         r"\b(?:cuts?|lowers?|slash(?:es)?|reduces?|trims?|slashed|lowered|cut)\s+(?:its |their |the |full[- ]year |fy\d* |annual |quarterly |q[1-4] |revenue |sales |profit |earnings |"
         r"second[- ]half |current[- ]quarter )*(?:guidance|outlook|forecasts?|projections?)\b|"
         r"\bprofit warning\b|\b(?:revenue|sales|earnings|profit) warning\b|\bwarns? (?:of|on|about|that)\b.{0,60}\b(?:sales|revenue|profit|earnings|demand|guidance|results)\b|"
         r"\bwithdraws? (?:its |full[- ]year |annual )*(?:guidance|outlook|forecast)|\bguidance (?:below|disappoint|cut|miss)|"
         r"\b(?:weak|soft|disappointing|downbeat|lackluster|below[- ]consensus) (?:guidance|outlook)\b|\bshares? (?:plunge|tumble|sink|fall|slump)s? on (?:weak|soft|disappointing|downbeat)"),
        ("offering", "share offering / dilution", "neg",
         r"\b(?:public|secondary|common stock|equity|stock|share|registered direct|underwritten|follow[- ]on|direct) offering\b|"
         r"\bconvertible (?:senior |subordinated )?notes\b(?!.{0,30}\b(?:repurchase|redeem|redemption))|\bat[- ]the[- ]market (?:offering|program)\b|"
         r"\bdilutive\b|\bdilution\b(?!\s+risks?)|\bprices? (?:upsized )?(?:public )?offering\b"),
        ("investigation", "SEC/DOJ investigation or lawsuit", "neg",
         r"\b(?:sec|doj|ftc|fbi|finra|cftc|justice department|department of justice|attorney general|regulators?|prosecutors?|congress)\b.{0,50}"
         r"\b(?:investigat|probe|subpoena|charg|sues?|sued|lawsuit|inquiry|fines?|fined|settle|indict|antitrust)|"
         r"\b(?:investigat\w+|probe|subpoena\w*|indict\w*)\b.{0,30}\b(?:into|of|against)\b|\bunder (?:federal |sec |doj )?(?:investigation|scrutiny)\b|"
         r"\b(?:faces?|hit with|slapped with|named in|defendant in)\b.{0,25}\b(?:lawsuit|class[- ]action|suit)\b|\bsecurities fraud\b|\bantitrust (?:suit|lawsuit|case|trial)\b|"
         r"\b(?:sued|sues|suing)\b|\bwhistleblower\b|\bfraud (?:charges|allegations)\b|\brestate(?:s|d|ment)\b|\bmaterial weakness\b"),
        ("fda_reject", "FDA rejection / trial failure", "neg",
         r"\bcomplete response letter\b|\bfda\b.{0,25}\b(?:reject|declin|refus|den(?:y|ies|ied)|delays?|crl|clinical hold)|\bclinical hold\b|"
         r"\b(?:trial|study|phase \w+)\b.{0,25}\b(?:fails?|failed|misses?|missed|halted)\b|\bfails? to meet (?:its )?primary endpoint\b"),
        ("bankruptcy", "bankruptcy / going concern", "neg",
         r"\bchapter (?:7|11)\b|\bbankruptcy\b|\bbankrupt\b|\bgoing[- ]concern\b|\binsolven|\bfil(?:es|ed|ing) for (?:creditor )?protection\b|\brestructuring support agreement\b|\bdefault(?:s|ed)? on\b"),
        ("delisting", "delisting notice", "neg",
         r"\bdelist(?:s|ed|ing)?\b|\b(?:nasdaq|nyse)\b.{0,30}\b(?:deficiency|non-?compliance|notice|minimum bid)\b|\bminimum bid price\b|\bnoncompliance\b"),
        ("upgrade", "analyst upgrade", "pos",
         r"\bupgrad(?:e|es|ed|ing)\b(?!.{0,25}\bdowngrad)|\bprice target (?:raised|hiked|boosted)|\braises? (?:its )?price target"),
        ("guidance_raise", "guidance raised", "pos",
         r"\b(?:raises?|lifts?|hikes?|boosts?|raised|lifted)\s+(?:its |their |the |full[- ]year |fy\d* |annual |quarterly |q[1-4] |revenue |sales |profit |earnings )*(?:guidance|outlook|forecasts?)\b"),
        ("buyback", "buyback", "pos", r"\b(?:share )?(?:buyback|repurchase program|repurchase authorization)\b"),
        ("fda_approval", "FDA approval", "pos", r"\bfda (?:approv|clears?|clearance|grants?)"),
    ))
_ACQ_TARGET_GENERIC = re.compile(
    r"\b(?:to be|being|agrees? to be|agreed to be|will be|set to be)\s+(?:acquired|bought|purchased|taken private)\b|"
    r"\breceives?\s+(?:an? )?(?:unsolicited |buyout |takeover |acquisition )+(?:offer|bid|proposal)|"
    r"\bdefinitive (?:merger )?agreement to be acquired\b|\bmerger agreement\b.{0,30}\bacquired by\b|\bacquired by\b|"
    r"\bagrees? to (?:a )?(?:sale|merge)\b|\bsells? itself\b|\bgoing private\b|\bstrategic alternatives\b",
    re.I)
_ACQ_VERB_RX = re.compile(r"\b(?:to acquire|acquires?|acquiring|(?:agrees?|agreed|plans?|set) to buy|bid for|"
                          r"offer for|takeover of|acquisition of|to purchase)\b", re.I)
# "<Acquirer> makes a takeover offer for <Target>": the target is named AFTER for/of.
_ACQ_OFFER_RX = re.compile(r"\b(?:take[- ]private|buyout|takeover)\s+(?:offer|bid|deal|proposal|talks)\b.{0,40}?\b(?:for|of)\b", re.I)
_STAKE_RX = re.compile(r"\b(?:stake|investment|invest(?:s|ing)?|minority|shares? in|funding)\b", re.I)

NEGATIVE_EVENTS = frozenset({"downgrade", "guidance_cut", "offering", "investigation", "fda_reject",
                             "bankruptcy", "acquisition_target", "delisting"})
POSITIVE_EVENTS = frozenset({"upgrade", "guidance_raise", "buyback", "fda_approval"})
EVENT_LABELS = {e: lab for e, lab, _p, _r in _EVENTS}
EVENT_LABELS["acquisition_target"] = "acquisition target (deal pending)"
LLM_EVENT_ENUM = ["none", *sorted(NEGATIVE_EVENTS), *sorted(POSITIVE_EVENTS)]


def event_label(event: str | None) -> str:
    return EVENT_LABELS.get(event or "", str(event or ""))


def is_negative_event(event: str | None) -> bool:
    return event in NEGATIVE_EVENTS


_CLAUSE_SPLIT = re.compile(r"\s*[,;]\s*|\s+as\s+|\s+while\s+", re.I)
_REVERSAL_RX = re.compile(r"\b(?:revers(?:e|es|ed|ing)|reiterat\w*|despite|shrugs? off|rescind\w*|walks? back|"
                          r"retract\w*|withdraws? (?:its )?downgrade)\b", re.I)
_RESOLVED_RX = re.compile(
    r"\b(?:wins?|won|secures?)\s+(?:the )?(?:dismissal|case|ruling|appeal)\b|\bdismissal of\b|\bdismiss(?:es|ed)\b|"
    r"\b(?:drops?|dropped|ends?|ended|closes|closed|abandons?|scraps?)\s+(?:the |its |an? |all )?(?:\w+ ){0,2}"
    r"(?:investigation|probe|inquiry|lawsuit|suit|case|charges?)\b|"
    r"\b(?:clears?|cleared|clearing|exonerat\w+|acquit\w*)\b|\bno (?:fine|penalty|charges?|action)\b|"
    r"\bthrows? out\b|\bsettles?\b|\bsettlement\b", re.I)
_OPPOSITE_RX = re.compile(r"\b(?:upgrad|rais|boost|lift|reiterat|outperform|overweight|initiat|buy\b)", re.I)
_SUES_RX = re.compile(r"\bsues\b", re.I)


def _event_matches(event: str, rx: re.Pattern, t: str, symbol: str | None) -> bool:
    """Keyword rule ``rx`` on headline ``t`` with context guards: reversals, resolutions, and
    binding an event to the clause that names ``symbol``."""
    if event == "downgrade":
        clauses = [c for c in _CLAUSE_SPLIT.split(t) if c and c.strip()]
        hits = [bool(rx.search(c)) and not _REVERSAL_RX.search(c) for c in clauses]
        if not symbol:
            return any(hits)
        for i, c in enumerate(clauses):
            if not _text_mentions(symbol, c):
                continue
            if hits[i]:
                return True
            # short list-like neighbour ("Nvidia falls as Citi downgrades"): same story
            if len(c.split()) <= 3 and not _OPPOSITE_RX.search(c) and ((i > 0 and hits[i - 1]) or (i + 1 < len(clauses) and hits[i + 1])):
                return True
        return False
    if event == "investigation":
        if _RESOLVED_RX.search(t):
            return False
        text = t
        if symbol:
            m = _SUES_RX.search(text)
            if m and _text_mentions(symbol, text[:m.start()]):
                text = text[:m.start()] + "XX" + text[m.end():]     # the company is the plaintiff
        return bool(rx.search(text))
    return bool(rx.search(t))


def classify_event(title: str, symbol: str | None = None) -> str | None:
    """Keyword rules for hard events. Returns an event key or None. Negative events are checked
    first so a headline that mixes both ("upgrade ... despite lawsuit") is treated cautiously.
    ``symbol`` lets M&A rules tell the TARGET from the ACQUIRER."""
    t = (title or "").strip()
    if not t:
        return None
    if _NOISE_RX.search(t):
        return None
    # Ordered: acquisition target before the generic list (a deal headline is the main event).
    if symbol and _is_acq_target(t, symbol):
        return "acquisition_target"
    found_pos = None
    for event, _lab, pol, rx in _EVENTS:
        if _event_matches(event, rx, t, symbol):
            if pol == "neg":
                return event
            found_pos = found_pos or event
    return found_pos


def _is_acq_target(title: str, symbol: str) -> bool:
    mo = _ACQ_OFFER_RX.search(title)
    if mo and not _ACQ_TARGET_GENERIC.search(title):
        # the symbol must be the thing being bid for, not the bidder
        return (_text_mentions(symbol, title[mo.end():mo.end() + 45])
                and not _text_mentions(symbol, title[:mo.start()]))
    if _ACQ_TARGET_GENERIC.search(title):
        # "X to be acquired" style: the subject must be our company (not the acquirer).
        m = re.search(r"\bacquired by\b", title, re.I)
        if m:
            return _text_mentions(symbol, title[:m.start()]) or not _text_mentions(symbol, title[m.end():])
        return _text_mentions(symbol, title)
    # "<Acquirer> to acquire <Target>": we are the target if the verb precedes our name and our
    # name does not precede the verb.
    m = _ACQ_VERB_RX.search(title)
    if not m or _text_mentions(symbol, title[:m.start()]):
        return False
    if not re.search(r"[A-Za-z]{2}", title[:m.start()]):
        return False                                   # no acquirer subject ("To Buy Nvidia ...")
    after = title[m.end():m.end() + 45]               # the target follows the verb directly
    if _STAKE_RX.search(after[:25]):
        return False                                   # "to buy a $5B stake in X" is not a takeover
    return _text_mentions(symbol, after)


# --- aggregation ---------------------------------------------------------------------------

WEBULL_TAG_SCORE = {"POSITIVE": 0.5, "NEGATIVE": -0.5, "NEUTRAL": 0.0}
SCORER_FACTOR = {"llm": 1.0, "local": 0.7, "webull": 0.8}


def item_score(item: dict, webull_fallback: bool = True) -> tuple[float | None, str | None]:
    """(score, scorer) for a stored headline, or (None, None) while unscored. scorer is
    'llm' | 'local' | 'filter' (not about the company) | 'webull' (tag only)."""
    if item.get("llm_score") is not None:
        scorer = item.get("scorer") or "llm"          # rows scored before the scorer column are LLM
        return float(item["llm_score"]), (scorer if scorer in ("local", "filter") else "llm")
    tag = item.get("webull_sentiment")
    if webull_fallback and tag in WEBULL_TAG_SCORE:
        return WEBULL_TAG_SCORE[tag], "webull"
    return None, None


def aggregate(symbol: str, items: list[dict], *, now: datetime, lookback_hours: float,
              half_life_hours: float = 24.0, shrink: float = 1.0,
              webull_fallback: bool = True) -> dict:
    """Weighted mean sentiment for one symbol.

    weight = recency (half-life) x source tier x relevance x scorer confidence. Headlines that
    are stale, not about the company, or rated irrelevant by the LLM are ignored and not
    counted. ``n`` is the number of relevant headlines that carry a score."""
    num = den = 0.0
    n = pending = 0
    by_scorer: dict[str, int] = {}
    n_relevant = 0
    for it in items:
        rel = relevance(symbol, it.get("title") or "", it.get("summary") or "")
        if rel <= 0:
            continue
        age = age_hours(it, now)
        if age is not None and age > lookback_hours:
            continue
        score, scorer = item_score(it, webull_fallback)
        if scorer == "filter":
            continue
        n_relevant += 1
        if score is None:
            pending += 1
            continue
        llm_rel = it.get("llm_relevance")
        if scorer == "llm" and llm_rel is not None:
            rel *= max(0.0, min(1.0, float(llm_rel)))
        if rel <= 0:
            n_relevant -= 1
            continue
        recency = 0.5 ** ((age or 0.0) / max(1.0, half_life_hours))
        w = recency * source_weight(it.get("source")) * rel * SCORER_FACTOR.get(scorer or "", 0.7)
        num += w * score
        den += w
        n += 1
        by_scorer[scorer] = by_scorer.get(scorer, 0) + 1
    if n == 0:
        return {"score": None, "n": 0, "n_relevant": n_relevant, "pending": pending,
                "by_scorer": by_scorer, "weight": 0.0, "state": "unavailable"}
    return {"score": round(num / (den + shrink), 3), "n": n, "n_relevant": n_relevant,
            "pending": pending, "by_scorer": by_scorer, "weight": round(den, 3), "state": "available"}


LLM_MIN_RELEVANCE = 0.5


def effective_event(item: dict, symbol: str, title: str | None = None) -> str | None:
    """The hard event to act on for a stored headline. LLM-scored rows trust the LLM label (and
    only when it judged the headline relevant), so it can correct keyword false positives;
    keyword/unscored rows use the stored keyword event, then a fresh keyword pass."""
    title = title if title is not None else (item.get("title") or "")
    if item.get("scorer") == "llm":
        rel = item.get("llm_relevance")
        if rel is not None and float(rel) < LLM_MIN_RELEVANCE:
            return None
        ev = item.get("llm_event")
        return ev if ev in NEGATIVE_EVENTS or ev in POSITIVE_EVENTS else None
    for cand in (item.get("event_type"), item.get("llm_event"), classify_event(title, symbol)):
        if cand in NEGATIVE_EVENTS or cand in POSITIVE_EVENTS:
            return cand
    return None


def find_negative_event(symbol: str, items: list[dict], *, now: datetime, hours: float) -> dict | None:
    """Newest relevant headline inside ``hours`` carrying a negative hard event (the stored
    keyword event, the LLM label, or a fresh keyword pass). Returns a dict or None."""
    best = None
    for it in items:
        age = age_hours(it, now)
        if age is None or age > hours:
            continue
        title = it.get("title") or ""
        if relevance(symbol, title, it.get("summary") or "") <= 0:
            continue
        event = effective_event(it, symbol, title)
        if not is_negative_event(event):
            continue
        if best is None or age < best["age_hours"]:
            best = {"event": event, "title": title, "source": it.get("source") or "unknown source",
                    "age_hours": age, "news_id": it.get("news_id")}
    return best
