"""Independent signal for the customer_support persona: check every checkable statement in a draft email against
the company's own data (price_sheet.csv + policies.json), plus the persona's authority and style rules.

Nothing here knows the practice texts. It works from general patterns:
  1. find figures in the text (money, percentages, durations), written as digits or words ("nineteen dollars",
     "two weeks", "a fifth off", "$0.049 thousand");
  2. type each figure by the cue words in its own clause (price, loyalty/annual discount, refund window, trial,
     first-response time, loyalty tenure) and link it to the nearest plan name (or the customer's plan from context);
  3. compare it with the value the data allows, and record what was said, what the data says, and where.
Non-numeric rules (stacking, eligibility, card requirements, feature promises, exceptions, guarantees, hype) are
phrase-level checks with negation handling ("discounts can't be combined" is a correct statement, not a violation).

Every result is a Claim with a character span, so a reviewer can see exactly which words triggered which rule.
"""
import re
from dataclasses import dataclass, field, asdict

VERIFIED, CONTRADICTED, REVIEW = "verified", "contradicted", "review"

# ---------------------------------------------------------------------------------------------------------------
# Number reading

UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90}
SCALES = {"hundred": 100, "thousand": 1_000, "k": 1_000, "million": 1_000_000, "m": 1_000_000, "mm": 1_000_000,
          "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000}
ARTICLES = {"a": 1, "an": 1, "one": 1, "single": 1}

WORD = r"(?:" + "|".join(sorted(list(UNITS) + list(TENS), key=len, reverse=True)) + r")"
WORDNUM = rf"{WORD}(?:(?:[\s-]+|\s+and\s+)(?:{WORD}|hundred|thousand))*"
DIGITS = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+"
NUM = rf"(?:{DIGITS}|{WORDNUM})"
SCALE = r"(?:\s*(?:thousand|million|billion|bn|mm|k|m|b)\b)?"


def words_to_number(text):
    """'forty-nine' -> 49, 'one hundred and twenty' -> 120. Returns None if it is not a number phrase."""
    total, current, seen = 0, 0, False
    for tok in re.split(r"[\s-]+", text.lower().strip()):
        if tok in ("", "and"):
            continue
        if tok in UNITS:
            current += UNITS[tok]
        elif tok in TENS:
            current += TENS[tok]
        elif tok == "hundred":
            current = max(current, 1) * 100
        elif tok == "thousand":
            total += max(current, 1) * 1000
            current = 0
        else:
            return None
        seen = True
    return total + current if seen else None


def parse_number(raw, scale=None):
    raw = raw.strip()
    if re.fullmatch(DIGITS, raw):
        value = float(raw.replace(",", ""))
    else:
        value = words_to_number(raw)
        if value is None:
            return None
        value = float(value)
    if scale:
        s = scale.strip().lower()
        value *= SCALES.get(s, 1)
    return value


def fmt_money(v):
    return f"${v:,.2f}".replace(".00", "")


def fmt_num(v):
    return str(int(v)) if abs(v - round(v)) < 1e-9 else f"{v:g}"


def close(a, b, tol=0.005):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------------------------------------------
# Text structure

@dataclass
class Claim:
    kind: str
    label: str
    span: list
    text: str
    verdict: str
    message: str
    plan: str = None
    stated: str = None
    expected: str = None
    source: str = None
    level: str = None  # BLOCK / HOLD_FOR_REVIEW / None, filled from verdict by the caller

    def to_dict(self):
        return asdict(self)


@dataclass
class Sentence:
    start: int
    end: int
    text: str
    clauses: list = field(default_factory=list)  # (start, end) absolute offsets


SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])|\n+")
# Clause boundaries: punctuation and coordinators. Figures take their *fact type* from their own clause.
CLAUSE_SPLIT = re.compile(r"[;:,]|\s[-–—]\s|\b(?:and|or|but|while|whereas|plus|with|although|though|however)\b", re.I)


def sentences(text):
    out, pos = [], 0
    for part in SENT_SPLIT.split(text):
        if part is None:
            continue
        start = text.find(part, pos)
        if start < 0:
            continue
        end = start + len(part)
        pos = end
        if not part.strip():
            continue
        s = Sentence(start, end, part)
        cpos = 0
        for m in CLAUSE_SPLIT.finditer(part):
            s.clauses.append((start + cpos, start + m.start()))
            cpos = m.end()
        s.clauses.append((start + cpos, end))
        out.append(s)
    return out


def clause_of(sent, idx):
    for a, b in sent.clauses:
        if a <= idx < b or (idx == b and b == sent.end):
            return a, b
    return sent.start, sent.end


# ---------------------------------------------------------------------------------------------------------------
# Vocabulary

PLAN_NAMES = ["Starter", "Pro", "Business", "Enterprise"]
SELF_SERVE = ["Starter", "Pro", "Business"]
PLAN_RE = re.compile(
    r"\b(starter|pro|business|enterprise)\s+(?:plan|tier|subscription|package|account|customers?|users?|level)s?\b"
    r"|\b(Starter|Pro|Business|Enterprise)\b(?!\s+(?:day|days|hours?|email|address|partner|owner|reasons?|needs?|days?)\b)"
)
ALL_PLANS_RE = re.compile(
    r"\b(?:all|every|each|any)\s+(?:of\s+(?:our|the)\s+)?(?:self[\s-]?serve\s+|paid\s+|standard\s+)?(?:plans?|tiers?|subscriptions?|packages?)\b"
    r"|\b(?:plans?|tiers?)\s+(?:across\s+the\s+board|alike)\b|\bregardless\s+of\s+(?:the\s+)?plan\b", re.I)
YOUR_PLAN_RE = re.compile(r"\byour\s+(?:current\s+)?(?:plan|subscription|account|tier|package)\b", re.I)

NEGATION = re.compile(r"\b(?:not|no|never|cannot|can't|cant|won't|don't|doesn't|isn't|aren't|without|neither|nor|unable)\b|n't\b", re.I)

CUES = {
    "loyalty": re.compile(r"\b(?:loyal\w*|tenure|long[\s-]?(?:time|standing)|been\s+with\s+us|customer\s+(?:for|of)\s+(?:more|over|at\s+least|\d|a|one|two|three)|anniversary)\b", re.I),
    "annual": re.compile(r"\b(?:annual(?:ly)?|yearly|per\s+year|a\s+year|each\s+year|every\s+year|/\s*(?:yr|year)|12[\s-]month\s+(?:plan|billing|commitment)|year[\s-]long|prepa(?:y|id)\s+(?:for\s+)?(?:the\s+)?year)\b", re.I),
    "monthly": re.compile(r"\b(?:monthly|month[\s-]to[\s-]month|per\s+month|a\s+month|each\s+month|every\s+month|/\s*mo(?:nth)?)\b", re.I),
    "refund": re.compile(r"\b(?:refund\w*|money[\s-]back|reimburs\w*|cancel\w*\s+(?:and|for)\s+(?:a\s+)?(?:full\s+)?(?:refund|money))\b", re.I),
    "trial": re.compile(r"\b(?:trial\w*|try\s+(?:it|\w+)\s+(?:out\s+)?(?:for\s+)?free|free\s+for\s+(?:the\s+first|\d|a|one|two|three|fourteen|thirty))\b", re.I),
    "response": re.compile(r"\b(?:respon\w*|reply|replies|get\s+back|hear\s+(?:back|from)|first\s+contact|turnaround|SLA|support\s+(?:team|ticket)s?\s+(?:will|within|answer)|answer\w*\s+(?:within|in))\b", re.I),
    "discount": re.compile(r"\b(?:discount\w*|off\b|sav(?:e|es|ing|ings)\b|reduc\w*|cheaper|less\b|markdown|promo\w*|coupon|deal)\b", re.I),
    "price": re.compile(r"\b(?:cost\w*|price\w*|pricing|priced|fee|charge\w*|bill\w*|pay|runs|is|are|at|only|just)\b", re.I),
    "card": re.compile(r"\b(?:credit\s+card|card|payment\s+(?:details|method|info\w*))\b", re.I),
    "stack": re.compile(r"\b(?:stack\w*|combin\w*|together|on\s+top\s+of|in\s+addition\s+to|plus|both|double[\s-]?up|layer\w*|add(?:ed)?\s+to)\b", re.I),
}

OFFER_RE = re.compile(
    r"\b(?:you\s+(?:qualify|are\s+eligible|'re\s+eligible|get|'ll\s+get|will\s+get|can\s+get|receive|'ll\s+receive|will\s+receive|can\s+have|now\s+have|have\s+earned|earned|are\s+entitled)|"
    r"you're\s+eligible|your\s+(?:\d+\s*%\s+|\w+\s+)?(?:loyalty\s+)?discount|(?:we|i)\s*(?:'ll|will|can|have|'ve|am\s+going\s+to)\s+(?:apply|applied|give|add|offer|extend)|"
    r"applies\s+to\s+(?:you|your)|apply\s+(?:it|the\s+discount|a\s+discount)\s+to\s+your|for\s+you\b)", re.I)

DURATION_UNIT = r"(?:business\s+days?|calendar\s+days?|days?|weeks?|months?|years?|hours?|hrs?|h\b|fortnight)"


# ---------------------------------------------------------------------------------------------------------------
# Figure extraction

@dataclass
class Figure:
    kind: str           # money | percent | duration
    value: float
    unit: str           # usd | pct | days | hours | months
    start: int
    end: int
    text: str
    period: str = None  # money: month | year | None
    open_ended: bool = False  # durations like "any time", "first year"


MONEY_PATTERNS = [
    re.compile(rf"(?:US\$|\$|USD\s?)\s?(?P<num>{DIGITS})(?P<scale>{SCALE})", re.I),
    re.compile(rf"\b(?P<num>{NUM})(?P<scale>{SCALE})\s*(?:US\s+)?(?:dollars?|bucks|usd)\b(?:\s+and\s+(?P<cents>{NUM})\s+cents?)?", re.I),
]
PERIOD_RE = re.compile(
    r"^\s*(?:(?P<m>(?:per|a|/|each|every|an?)\s*(?:month|mo)\b|/\s*mo\b|monthly\b|a\s+month|per\s+user\s+per\s+month|/user/mo)"
    r"|(?P<y>(?:per|a|/|each|every)\s*(?:year|yr|annum)\b|/\s*yr\b|annually\b|yearly\b|a\s+year))", re.I)
PERCENT_PATTERNS = [
    re.compile(rf"\b(?P<num>{NUM})\s*(?:%|percent\b|per\s*cent\b|pct\b)", re.I),
    re.compile(r"(?P<num>\d+(?:\.\d+)?)\s*%"),
]
FRACTIONS = {"half": 50, "a half": 50, "one half": 50, "a third": 100 / 3, "one third": 100 / 3, "a quarter": 25,
             "one quarter": 25, "a fifth": 20, "one fifth": 20, "one-fifth": 20, "a tenth": 10, "one tenth": 10}
FRACTION_RE = re.compile(r"\b(?P<frac>(?:a|one)[\s-](?:half|third|quarter|fifth|tenth)|half)\s+(?:off|price|the\s+price|discount)\b", re.I)
DURATION_RE = re.compile(rf"\b(?P<num>{NUM}|a|an|one|single|first)[\s-]+(?:(?:full|whole|entire|complete|calendar|additional|extra|more)[\s-]+)?(?P<unit>{DURATION_UNIT})\b", re.I)
OPEN_ENDED_RE = re.compile(
    r"\b(?:any\s*time|at\s+any\s+(?:time|point)|whenever|no\s+matter\s+when|for\s+life|lifetime|indefinitely|forever|no\s+time\s+limit|"
    r"(?:within|during|in)\s+(?:the\s+)?(?:first|whole|entire|full)\s+(?:year|12\s+months|twelve\s+months)|"
    r"(?:within|during|in)\s+(?:the\s+)?first\s+(?:\w+\s+)?(?:months|quarter))\b", re.I)


def unit_of(word):
    w = word.lower()
    if w.startswith("hour") or w.startswith("hr") or w == "h":
        return "hours", 1
    if w.startswith("business day") or w.startswith("calendar day") or w.startswith("day"):
        return "days", 1
    if w.startswith("week"):
        return "days", 7
    if w == "fortnight":
        return "days", 14
    if w.startswith("month"):
        return "months", 1
    if w.startswith("year"):
        return "months", 12
    return None, 1


def extract_figures(text):
    figs, taken = [], []

    def free(a, b):
        return all(b <= x or a >= y for x, y in taken)

    for pat in MONEY_PATTERNS:
        for m in pat.finditer(text):
            if not free(m.start(), m.end()):
                continue
            value = parse_number(m.group("num"), m.group("scale"))
            if value is None:
                continue
            cents = m.groupdict().get("cents")
            if cents:
                c = parse_number(cents)
                value += (c or 0) / 100
            pm = PERIOD_RE.match(text[m.end():m.end() + 30])
            period = None
            end = m.end()
            if pm:
                period = "month" if pm.group("m") else "year"
                end = m.end() + pm.end()
            figs.append(Figure("money", value, "usd", m.start(), end, text[m.start():end], period=period))
            taken.append((m.start(), end))
    for pat in PERCENT_PATTERNS:
        for m in pat.finditer(text):
            if not free(m.start(), m.end()):
                continue
            value = parse_number(m.group("num"))
            if value is None:
                continue
            figs.append(Figure("percent", value, "pct", m.start(), m.end(), text[m.start():m.end()]))
            taken.append((m.start(), m.end()))
    for m in FRACTION_RE.finditer(text):
        if free(m.start(), m.end()):
            frac = re.sub(r"[\s-]+", " ", m.group("frac").lower())
            value = FRACTIONS.get(frac, FRACTIONS.get(frac.replace(" ", "-")))
            if value:
                figs.append(Figure("percent", value, "pct", m.start(), m.end(), text[m.start():m.end()]))
                taken.append((m.start(), m.end()))
    for m in DURATION_RE.finditer(text):
        if not free(m.start(), m.end()):
            continue
        raw = m.group("num").lower()
        unit, mult = unit_of(m.group("unit"))
        if unit is None:
            continue
        if raw in ARTICLES or raw == "first":
            n = 1.0
        else:
            n = parse_number(raw)
            if n is None:
                continue
        if m.group("unit").lower() == "fortnight":
            n = 1.0
        figs.append(Figure("duration", n * mult, unit, m.start(), m.end(), text[m.start():m.end()]))
        taken.append((m.start(), m.end()))
    for m in OPEN_ENDED_RE.finditer(text):
        if free(m.start(), m.end()):
            figs.append(Figure("duration", float("inf"), "days", m.start(), m.end(), text[m.start():m.end()], open_ended=True))
            taken.append((m.start(), m.end()))
    return sorted(figs, key=lambda f: f.start)


# ---------------------------------------------------------------------------------------------------------------
# The checker

class Checker:
    def __init__(self, sot):
        pol = sot["policies"]
        self.pol = pol
        self.sheet = {row["plan"]: row for row in sot["price_sheet"]}
        self.monthly = {p: float(r["monthly_price_usd"]) for p, r in self.sheet.items()}
        self.annual_pm = {p: float(r["annual_price_per_month_usd"]) for p, r in self.sheet.items()}
        self.response_h = {p: float(r["first_response_hours"]) for p, r in self.sheet.items()}
        self.trial_days = {p: float(r["free_trial_days"]) for p, r in self.sheet.items()}
        d = pol["discounts"]
        self.annual_pct = float(d["annual_billing"]["percent"])
        self.annual_plans = d["annual_billing"]["applies_to"]
        self.loyal_pct = float(d["loyalty"]["percent"])
        self.loyal_plans = d["loyalty"]["applies_to"]
        self.loyal_min = float(d["loyalty"]["min_tenure_months"])
        self.stacking = bool(d["stacking_allowed"])
        self.refund_monthly = float(pol["refunds"]["monthly_plans_days"])
        self.refund_annual = float(pol["refunds"]["annual_plans_days"])
        self.refund_scope = pol["refunds"].get("scope", "")
        self.policy_trial = float(pol["free_trial"]["days"])
        self.trial_plans = pol["free_trial"]["plans"]
        self.card_required = bool(pol["free_trial"]["card_required"])
        self.can_promise = bool(pol.get("agent_authority", {}).get("can_promise_product_features", False))
        self.can_extra_discount = bool(pol.get("agent_authority", {}).get("can_offer_discounts_beyond_policy", False))

    # ---- helpers -------------------------------------------------------------------------------------------
    @staticmethod
    def plans_in(text, offset=0):
        out = []
        for m in PLAN_RE.finditer(text):
            name = (m.group(1) or m.group(2)).capitalize()
            out.append((offset + m.start(), name))
        return out

    def plans_for(self, sent, clause, fig_start, context, prev_plan):
        """Which plan(s) a figure talks about: same clause (nearest), then the sentence, then the previous sentence,
        then 'your plan' / the customer's plan from context. 'all/every plan' means every self-serve plan."""
        a, b = clause
        ctext = sent.text[a - sent.start:b - sent.start]
        if ALL_PLANS_RE.search(ctext):
            return list(SELF_SERVE), "all plans"
        in_clause = self.plans_in(ctext, a)
        if in_clause:
            return [min(in_clause, key=lambda p: abs(p[0] - fig_start))[1]], "named"
        if ALL_PLANS_RE.search(sent.text):
            return list(SELF_SERVE), "all plans"
        in_sent = self.plans_in(sent.text, sent.start)
        if in_sent:
            before = [p for p in in_sent if p[0] <= fig_start]
            pick = max(before, key=lambda p: p[0]) if before else min(in_sent, key=lambda p: p[0])
            return [pick[1]], "named"
        if prev_plan:
            return [prev_plan], "previous sentence"
        cplan = str(context.get("plan", "")).strip().capitalize() if context else ""
        if cplan in self.sheet or cplan == "Enterprise":
            return [cplan], "customer's plan"
        return [], "none"

    @staticmethod
    def local(sent, fig, before=60, after=40):
        a = max(sent.start, fig.start - before)
        b = min(sent.end, fig.end + after)
        return a, b

    @staticmethod
    def nearest_cue(text_abs, sent, clause, fig, names):
        """Return the cue name (from names) whose match is nearest to the figure, preferring its own clause."""
        best = None
        for scope in (clause, (sent.start, sent.end)):
            a, b = scope
            seg = text_abs[a:b]
            for name in names:
                for m in CUES[name].finditer(seg):
                    pos = a + m.start()
                    dist = 0 if fig.start <= pos <= fig.end else min(abs(pos - fig.end), abs(pos + len(m.group(0)) - fig.start))
                    if best is None or dist < best[0]:
                        best = (dist, name)
            if best:
                return best[1]
        return None

    @staticmethod
    def negated(seg):
        return bool(NEGATION.search(seg))

    # ---- main --------------------------------------------------------------------------------------------
    def check(self, text, context):
        context = context or {}
        claims = []
        sents = sentences(text)
        figs = extract_figures(text)
        prev_plan = None
        for sent in sents:
            sfigs = [f for f in figs if sent.start <= f.start < sent.end]
            for fig in sfigs:
                clause = clause_of(sent, fig.start)
                claims.extend(self.check_figure(text, sent, clause, fig, context, prev_plan, sfigs))
            claims.extend(self.check_rules(text, sent, context, sfigs))
            named = self.plans_in(sent.text, sent.start)
            if named:
                prev_plan = named[-1][1]
        return dedupe(claims)

    # ---- figures ----------------------------------------------------------------------------------------
    def check_figure(self, text, sent, clause, fig, context, prev_plan, sfigs):
        ctext = text[clause[0]:clause[1]]
        stext = sent.text
        if fig.kind == "money":
            return self.check_money(text, sent, clause, fig, context, prev_plan)
        if fig.kind == "percent":
            return self.check_percent(text, sent, clause, fig, context, prev_plan)
        if fig.kind == "duration":
            cue = self.nearest_cue(text, sent, clause, fig, ["refund", "trial", "response", "loyalty"])
            if fig.unit == "hours" and cue not in ("response", "refund", "trial"):
                cue = "response" if CUES["response"].search(stext) else cue
            if cue == "refund":
                return self.check_refund(text, sent, clause, fig, context)
            if cue == "trial":
                return self.check_trial(text, sent, clause, fig, context, prev_plan)
            if cue == "response":
                return self.check_response(text, sent, clause, fig, context, prev_plan)
            if cue == "loyalty" and fig.unit == "months":
                return self.check_tenure(text, sent, clause, fig, context)
        return []

    def check_money(self, text, sent, clause, fig, context, prev_plan):
        ctext = text[clause[0]:clause[1]]
        stext = sent.text
        plans, how = self.plans_for(sent, clause, fig.start, context, prev_plan)
        win = text[max(sent.start, fig.start - 50):min(sent.end, fig.end + 50)]
        annual_cue = bool(CUES["annual"].search(stext)) or fig.period == "year"
        saving_cue = bool(re.search(r"\b(?:save|saves|saving|savings|off|discount\w*|less|cheaper|reduc\w*)\b", text[max(sent.start, fig.start - 30):min(sent.end, fig.end + 25)], re.I))
        loyalty_cue = bool(CUES["loyalty"].search(stext))
        if "Enterprise" in plans:
            return [Claim("price_enterprise", "Enterprise price", [fig.start, fig.end], fig.text, CONTRADICTED,
                          f"The email quotes {fig.text} for Enterprise, but Enterprise pricing is custom (contact sales).",
                          plan="Enterprise", stated=fig.text, expected="custom pricing via sales",
                          source="policies.json › enterprise.pricing")]
        if not plans:
            plans = list(SELF_SERVE)
            how = "any plan"
        results = []
        for plan in plans:
            if plan not in self.monthly:
                continue
            m, a = self.monthly[plan], self.annual_pm[plan]
            allowed = {}
            if saving_cue:
                allowed.update({
                    "monthly saving with annual billing": m - a, "yearly saving with annual billing": 12 * (m - a),
                })
                if plan in self.loyal_plans:
                    allowed.update({"monthly loyalty saving": m * self.loyal_pct / 100,
                                    "yearly loyalty saving": 12 * m * self.loyal_pct / 100})
            if fig.period == "year":
                allowed.update({"annual billing, per year": 12 * a, "monthly billing, per year": 12 * m})
            else:
                allowed["monthly price"] = m
                if annual_cue:
                    allowed["annual billing, per month"] = a
                    allowed["annual billing, per year"] = 12 * a
                if loyalty_cue and plan in self.loyal_plans:
                    allowed["monthly price with loyalty discount"] = m * (1 - self.loyal_pct / 100)
                if saving_cue or annual_cue or loyalty_cue:
                    allowed["monthly price"] = m
            stacked = {"stacked annual + loyalty price": a * (1 - self.loyal_pct / 100),
                       "stacked annual + loyalty price per year": 12 * a * (1 - self.loyal_pct / 100)}
            match = next((k for k, v in allowed.items() if close(fig.value, v, 0.011)), None)
            src = f"price_sheet.csv › {plan}"
            if match:
                results.append(Claim("price", f"{plan} {match}", [fig.start, fig.end], fig.text, VERIFIED,
                                     f"{fig.text} matches the {plan} {match} ({fmt_money(allowed[match])}).",
                                     plan=plan, stated=fig.text, expected=fmt_money(allowed[match]), source=src))
                continue
            st = next((k for k, v in stacked.items() if close(fig.value, v, 0.011)), None)
            if st:
                results.append(Claim("stacking", f"{plan} {st}", [fig.start, fig.end], fig.text, CONTRADICTED,
                                     f"{fig.text} is only reachable by stacking the annual and loyalty discounts, which policy forbids.",
                                     plan=plan, stated=fig.text, expected="discounts cannot be stacked",
                                     source="policies.json › discounts.stacking_allowed"))
                continue
            if fig.period != "year" and not annual_cue and close(fig.value, a, 0.011):
                results.append(Claim("price", f"{plan} price", [fig.start, fig.end], fig.text, REVIEW,
                                     f"{fig.text} is the {plan} price with annual billing, but the sentence doesn't say it is billed annually (monthly billing is {fmt_money(m)}).",
                                     plan=plan, stated=fig.text, expected=f"{fmt_money(m)}/month, or {fmt_money(a)}/month billed annually", source=src))
                continue
            if saving_cue and fig.period != "year" and not any(close(fig.value, v, 0.011) for v in (m, a)):
                # A computed saving we cannot reproduce from the price sheet: a human should check the arithmetic.
                results.append(Claim("price_saving", f"{plan} saving", [fig.start, fig.end], fig.text, REVIEW,
                                     f"{fig.text} looks like a computed saving for {plan} that doesn't match the price sheet "
                                     f"(annual saving {fmt_money(m - a)}/month or {fmt_money(12 * (m - a))}/year).",
                                     plan=plan, stated=fig.text, expected=f"{fmt_money(m - a)}/month saved with annual billing", source=src))
                continue
            exp = f"{fmt_money(m)}/month" + (f" or {fmt_money(a)}/month billed annually ({fmt_money(12 * a)}/year)" if annual_cue or fig.period == "year" else "")
            results.append(Claim("price", f"{plan} price", [fig.start, fig.end], fig.text, CONTRADICTED,
                                 f"The email says {plan} costs {fig.text}; the price sheet says {exp}.",
                                 plan=plan, stated=fig.text, expected=exp, source=f"{src} › monthly_price_usd / annual_price_per_month_usd"))
        if how == "any plan":
            ok = [c for c in results if c.verdict == VERIFIED]
            if ok:
                return ok[:1]
            if results:
                c = results[0]
                return [Claim("price", "Price (no plan named)", [fig.start, fig.end], fig.text, CONTRADICTED,
                              f"{fig.text} doesn't match any plan's price (Starter {fmt_money(self.monthly['Starter'])}, Pro {fmt_money(self.monthly['Pro'])}, "
                              f"Business {fmt_money(self.monthly['Business'])} per month).",
                              stated=fig.text, expected="a price from the price sheet", source="price_sheet.csv")]
        if len(plans) > 1:  # "every plan costs $X": all must hold
            bad = [c for c in results if c.verdict != VERIFIED]
            return bad[:1] if bad else results[:1]
        return results

    def check_percent(self, text, sent, clause, fig, context, prev_plan):
        ctext = text[clause[0]:clause[1]]
        stext = sent.text
        span = [fig.start, fig.end]
        cue = self.nearest_cue(text, sent, clause, fig, ["loyalty", "annual"])
        has_loyal = bool(CUES["loyalty"].search(stext))
        has_annual = bool(CUES["annual"].search(stext))
        discount_ctx = bool(CUES["discount"].search(stext)) or has_loyal or has_annual
        if not discount_ctx:
            return []
        combined = {self.loyal_pct + self.annual_pct, round(100 - (100 - self.loyal_pct) * (100 - self.annual_pct) / 100, 2)}
        if has_loyal and has_annual and (CUES["stack"].search(stext) or any(close(fig.value, c, 0.5) for c in combined)):
            if any(close(fig.value, c, 0.5) for c in combined) or not (close(fig.value, self.loyal_pct) or close(fig.value, self.annual_pct)):
                return [Claim("stacking", "Combined discount", span, fig.text, CONTRADICTED,
                              f"{fig.text} combines the loyalty ({fmt_num(self.loyal_pct)}%) and annual ({fmt_num(self.annual_pct)}%) discounts; policy says discounts cannot be stacked.",
                              stated=fig.text, expected=f"one discount only: {fmt_num(self.loyal_pct)}% loyalty or {fmt_num(self.annual_pct)}% annual",
                              source="policies.json › discounts.stacking_allowed = false")]
        if cue == "loyalty":
            if close(fig.value, self.loyal_pct):
                return [Claim("discount_loyalty_pct", "Loyalty discount", span, fig.text, VERIFIED,
                              f"Loyalty discount is {fmt_num(self.loyal_pct)}%, as stated.", stated=fig.text,
                              expected=f"{fmt_num(self.loyal_pct)}%", source="policies.json › discounts.loyalty.percent")]
            return [Claim("discount_loyalty_pct", "Loyalty discount", span, fig.text, CONTRADICTED,
                          f"The email says the loyalty discount is {fig.text}; policy sets it at {fmt_num(self.loyal_pct)}%.",
                          stated=fig.text, expected=f"{fmt_num(self.loyal_pct)}%", source="policies.json › discounts.loyalty.percent")]
        if cue == "annual":
            plans, how = self.plans_for(sent, clause, fig.start, context, prev_plan)
            if close(fig.value, self.annual_pct):
                bad_plans = [p for p in plans if p not in self.annual_plans] if how == "named" else []
                if bad_plans:
                    return [Claim("discount_annual_pct", "Annual billing discount", span, fig.text, CONTRADICTED,
                                  f"The annual-billing discount applies to {', '.join(self.annual_plans)}, not {', '.join(bad_plans)}.",
                                  plan=bad_plans[0], stated=f"{fig.text} on {bad_plans[0]}", expected=", ".join(self.annual_plans),
                                  source="policies.json › discounts.annual_billing.applies_to")]
                return [Claim("discount_annual_pct", "Annual billing discount", span, fig.text, VERIFIED,
                              f"Annual billing discount is {fmt_num(self.annual_pct)}% on {', '.join(self.annual_plans)}, as stated.",
                              stated=fig.text, expected=f"{fmt_num(self.annual_pct)}%", source="policies.json › discounts.annual_billing.percent")]
            return [Claim("discount_annual_pct", "Annual billing discount", span, fig.text, CONTRADICTED,
                          f"The email says annual billing saves {fig.text}; policy sets the annual discount at {fmt_num(self.annual_pct)}%.",
                          stated=fig.text, expected=f"{fmt_num(self.annual_pct)}%", source="policies.json › discounts.annual_billing.percent")]
        # A discount percentage with no named discount type: it must be one of the policy discounts.
        if any(close(fig.value, v) for v in (self.loyal_pct, self.annual_pct)):
            return [Claim("discount_pct", "Discount", span, fig.text, VERIFIED,
                          f"{fig.text} matches a policy discount ({fmt_num(self.loyal_pct)}% loyalty or {fmt_num(self.annual_pct)}% annual billing).",
                          stated=fig.text, expected=f"{fmt_num(self.loyal_pct)}% or {fmt_num(self.annual_pct)}%", source="policies.json › discounts")]
        if CUES["discount"].search(text[max(sent.start, fig.start - 40):min(sent.end, fig.end + 40)]):
            return [Claim("discount_pct", "Discount beyond policy", span, fig.text, CONTRADICTED,
                          f"No policy discount is {fig.text}. Policy offers {fmt_num(self.loyal_pct)}% loyalty or {fmt_num(self.annual_pct)}% annual billing, "
                          f"and agents can't offer discounts beyond policy.", stated=fig.text,
                          expected=f"{fmt_num(self.loyal_pct)}% or {fmt_num(self.annual_pct)}%",
                          source="policies.json › discounts, agent_authority.can_offer_discounts_beyond_policy = false")]
        return []

    def check_refund(self, text, sent, clause, fig, context):
        span = [fig.start, fig.end]
        days = fig.value * (30 if fig.unit == "months" else 1) if fig.unit != "hours" else fig.value / 24
        # Refund *processing* time ("your refund will arrive in 5 business days") is not the refund window.
        stext = sent.text
        if re.search(r"\b(?:process\w*|arriv\w*|appear\w*|post\w*|land\w*|show\s+up|reach\w*|back\s+(?:on|to)\s+your|issued|clear\w*|take[sn]?)\b", stext, re.I) \
                and not re.search(r"\b(?:request|eligib\w*|within\s+\S+\s+\S+\s+of|of\s+(?:your\s+)?(?:first\s+)?(?:payment|purchase|signing|subscrib\w*)|after\s+(?:purchase|payment|signing|you\s+(?:buy|subscribe|sign)))\b", stext, re.I):
            return []
        # Which billing type? The figure's own clause first ("monthly plans ... 30 days; annual plans ... 14 days"), then the sentence.
        ctext = text[clause[0]:clause[1]]
        annual = monthly = False
        for scope in (ctext, stext):
            a = bool(CUES["annual"].search(scope))
            mo = bool(re.search(r"\bmonthly\b|month[\s-]to[\s-]month|per\s+month", scope, re.I))
            if a != mo:
                annual, monthly = a, mo
                break
        if annual:
            expected, label, src = [self.refund_annual], "annual plans", "policies.json › refunds.annual_plans_days"
        elif monthly:
            expected, label, src = [self.refund_monthly], "monthly plans", "policies.json › refunds.monthly_plans_days"
        else:
            expected, label, src = [self.refund_monthly, self.refund_annual], "monthly or annual plans", "policies.json › refunds"
        exp_txt = " / ".join(f"{fmt_num(e)} days" for e in expected)
        if fig.open_ended:
            return [Claim("refund_window", f"Refund window ({label})", span, fig.text, CONTRADICTED,
                          f"The email promises a refund '{fig.text}'; refunds for {label} are limited to {exp_txt} after the {self.refund_scope}.",
                          stated=fig.text, expected=exp_txt, source=src)]
        if any(close(days, e, 0.5) for e in expected):
            return [Claim("refund_window", f"Refund window ({label})", span, fig.text, VERIFIED,
                          f"Refund window for {label} is {exp_txt}, as stated.", stated=fig.text, expected=exp_txt, source=src)]
        return [Claim("refund_window", f"Refund window ({label})", span, fig.text, CONTRADICTED,
                      f"The email gives a {fig.text} refund window; policy allows {exp_txt} for {label}.",
                      stated=fig.text, expected=exp_txt, source=src)]

    def check_trial(self, text, sent, clause, fig, context, prev_plan):
        span = [fig.start, fig.end]
        days = fig.value * 30 if fig.unit == "months" else fig.value
        if fig.unit == "hours":
            days = fig.value / 24
        plans, how = self.plans_for(sent, clause, fig.start, context, prev_plan)
        named = [p for p in plans if how in ("named", "all plans")]
        no_trial = [p for p in named if p not in self.trial_plans]
        if no_trial:
            return [Claim("trial_days", "Free trial eligibility", span, fig.text, CONTRADICTED,
                          f"The email offers a free trial on {', '.join(no_trial)}; free trials are only on {', '.join(self.trial_plans)}.",
                          plan=no_trial[0], stated=f"trial on {', '.join(no_trial)}", expected=f"trial only on {', '.join(self.trial_plans)}",
                          source="policies.json › free_trial.plans; price_sheet.csv › free_trial_days")]
        if fig.open_ended or not close(days, self.policy_trial, 0.5):
            return [Claim("trial_days", "Free trial length", span, fig.text, CONTRADICTED,
                          f"The email says the free trial is {fig.text}; it is {fmt_num(self.policy_trial)} days.",
                          stated=fig.text, expected=f"{fmt_num(self.policy_trial)} days", source="policies.json › free_trial.days")]
        return [Claim("trial_days", "Free trial length", span, fig.text, VERIFIED,
                      f"Free trial is {fmt_num(self.policy_trial)} days on {', '.join(self.trial_plans)}, as stated.",
                      stated=fig.text, expected=f"{fmt_num(self.policy_trial)} days", source="policies.json › free_trial.days")]

    def check_response(self, text, sent, clause, fig, context, prev_plan):
        span = [fig.start, fig.end]
        hours = fig.value * 24 if fig.unit == "days" else (fig.value * 24 * 30 if fig.unit == "months" else fig.value)
        if fig.open_ended:
            return []
        ctext = text[clause[0]:clause[1]]
        if re.search(r"\bI(?:'ll|\s+will|\s+am\s+going\s+to)\b[^.]{0,40}\b(?:get\s+back|follow\s+up|update|reply|respond|email|call|check)", sent.text) \
                and not re.search(r"\bfirst\s+response|response\s+time|SLA\b", sent.text, re.I):
            return []  # an agent's own follow-up, not the plan's first-response time
        plans, how = self.plans_for(sent, clause, fig.start, context, prev_plan)
        plans = [p for p in plans if p in self.response_h]
        if not plans:
            if any(close(hours, v, 0.01) for v in self.response_h.values()):
                return [Claim("response_hours", "First-response time", span, fig.text, VERIFIED,
                              f"{fig.text} matches a plan's first-response time.", stated=fig.text,
                              expected=", ".join(f"{p} {fmt_num(v)}h" for p, v in self.response_h.items()), source="price_sheet.csv › first_response_hours")]
            return [Claim("response_hours", "First-response time", span, fig.text, REVIEW,
                          f"{fig.text} doesn't match any plan's first-response time and no plan is named.", stated=fig.text,
                          expected=", ".join(f"{p} {fmt_num(v)}h" for p, v in self.response_h.items()), source="price_sheet.csv › first_response_hours")]
        bad = [p for p in plans if not close(hours, self.response_h[p], 0.01)]
        if bad:
            p = bad[0]
            return [Claim("response_hours", f"{p} first-response time", span, fig.text, CONTRADICTED,
                          f"The email promises a first response within {fig.text} on {p}; {p} gets {fmt_num(self.response_h[p])} hours.",
                          plan=p, stated=fig.text, expected=f"{fmt_num(self.response_h[p])} hours", source=f"price_sheet.csv › {p} › first_response_hours")]
        p = plans[0]
        return [Claim("response_hours", f"{p} first-response time", span, fig.text, VERIFIED,
                      f"{p} first-response time is {fmt_num(self.response_h[p])} hours, as stated.",
                      plan=p, stated=fig.text, expected=f"{fmt_num(self.response_h[p])} hours", source=f"price_sheet.csv › {p} › first_response_hours")]

    def check_tenure(self, text, sent, clause, fig, context):
        span = [fig.start, fig.end]
        months = fig.value
        stext = sent.text
        before = text[max(sent.start, fig.start - 25):fig.start].lower()
        at_least = bool(re.search(r"(?:more\s+than|over|at\s+least|minimum\s+of|after|beyond|\+)\s*$", before)) or text[fig.end:fig.end + 1] == "+"
        if close(months, self.loyal_min, 0.01):
            return [Claim("loyalty_tenure", "Loyalty tenure requirement", span, fig.text, VERIFIED,
                          f"Loyalty discount requires {fmt_num(self.loyal_min)} months' tenure, as stated.", stated=fig.text,
                          expected=f"{fmt_num(self.loyal_min)} months", source="policies.json › discounts.loyalty.min_tenure_months")]
        tenure = context.get("customer_tenure_months") if context else None
        second_person = bool(re.search(r"\b(?:you|your)\b", stext, re.I))
        if months < self.loyal_min:
            return [Claim("loyalty_tenure", "Loyalty tenure requirement", span, fig.text, CONTRADICTED,
                          f"The email implies {fig.text} is enough for the loyalty discount; it requires {fmt_num(self.loyal_min)} months.",
                          stated=fig.text, expected=f"{fmt_num(self.loyal_min)} months", source="policies.json › discounts.loyalty.min_tenure_months")]
        if second_person and isinstance(tenure, (int, float)):
            approx = bool(re.search(r"(?:almost|nearly|about|around|roughly|close\s+to|approximately)\s*$", before))
            if (at_least and tenure >= months) or close(tenure, months, 3.01 if approx else 1.01):
                return [Claim("loyalty_tenure", "Customer tenure", span, fig.text, VERIFIED,
                              f"Customer tenure is {fmt_num(tenure)} months, consistent with '{fig.text}' and above the {fmt_num(self.loyal_min)}-month minimum.",
                              stated=fig.text, expected=f"{fmt_num(tenure)} months (context)", source="request context › customer_tenure_months")]
            return [Claim("loyalty_tenure", "Customer tenure", span, fig.text, CONTRADICTED,
                          f"The email says the customer has {fig.text}; their record shows {fmt_num(tenure)} months.",
                          stated=fig.text, expected=f"{fmt_num(tenure)} months (context)", source="request context › customer_tenure_months")]
        if not second_person:
            return [Claim("loyalty_tenure", "Loyalty tenure requirement", span, fig.text, CONTRADICTED,
                          f"The email states a {fig.text} loyalty requirement; policy requires {fmt_num(self.loyal_min)} months.",
                          stated=fig.text, expected=f"{fmt_num(self.loyal_min)} months", source="policies.json › discounts.loyalty.min_tenure_months")]
        return []

    # ---- phrase-level rules -------------------------------------------------------------------------------
    def check_rules(self, text, sent, context, sfigs):
        out = []
        st = sent.text
        s0 = sent.start
        low = st.lower()

        # Stacking: two discount types joined by a combining word, unless negated.
        has_loyal = CUES["loyalty"].search(st)
        has_annual = CUES["annual"].search(st)
        stack = CUES["stack"].search(st)
        mentions_two = (has_loyal and has_annual) or re.search(r"\b(?:both|two|multiple|all)\s+(?:of\s+(?:the|our)\s+)?discounts\b|\bdiscounts?\s+(?:stack|combine)", low)
        if stack and mentions_two and re.search(r"discount|off|saving", low):
            neg = self.negated(st)
            if neg:
                out.append(Claim("stacking", "Discount stacking", [s0 + stack.start(), s0 + stack.end()], stack.group(0), VERIFIED,
                                 "Correctly says discounts can't be combined.", stated="no stacking", expected="no stacking",
                                 source="policies.json › discounts.stacking_allowed = false"))
            elif not self.stacking:
                out.append(Claim("stacking", "Discount stacking", [s0 + stack.start(), s0 + stack.end()], stack.group(0), CONTRADICTED,
                                 "The email lets the customer combine the loyalty and annual-billing discounts; policy says discounts cannot be stacked.",
                                 stated="discounts combined", expected="one discount at a time", source="policies.json › discounts.stacking_allowed = false"))

        # Loyalty eligibility: an offer of the loyalty discount to *this* customer must meet plan + tenure.
        if has_loyal and re.search(r"discount|off|%|percent|saving", low) and OFFER_RE.search(st) and not re.search(r"\b(?:not|n't|no longer|isn't|aren't)\b[^.]{0,30}\b(?:eligible|qualify)", low):
            plan = str(context.get("plan", "")).strip().capitalize()
            tenure = context.get("customer_tenure_months")
            named = [p for _, p in self.plans_in(st, s0)]
            target_plan = named[0] if named else plan
            problems = []
            if target_plan and target_plan not in self.loyal_plans:
                problems.append(f"{target_plan} is not eligible (loyalty applies to {', '.join(self.loyal_plans)})")
            if plan and named and plan != named[0] and plan not in self.loyal_plans:
                problems.append(f"the customer is on {plan}, which is not eligible")
            if isinstance(tenure, (int, float)) and tenure < self.loyal_min:
                problems.append(f"the customer has {fmt_num(tenure)} months' tenure, below the {fmt_num(self.loyal_min)}-month minimum")
            m = has_loyal
            if problems:
                out.append(Claim("discount_eligibility", "Loyalty discount eligibility", [s0 + m.start(), s0 + m.end()], m.group(0), CONTRADICTED,
                                 "The email offers the loyalty discount, but " + "; ".join(problems) + ".",
                                 plan=target_plan or None, stated="customer qualifies", expected=f"{', '.join(self.loyal_plans)} with ≥{fmt_num(self.loyal_min)} months",
                                 source="policies.json › discounts.loyalty + request context"))
            elif not isinstance(tenure, (int, float)) or not plan:
                out.append(Claim("discount_eligibility", "Loyalty discount eligibility", [s0 + m.start(), s0 + m.end()], m.group(0), REVIEW,
                                 "The email offers the loyalty discount, but the customer's plan or tenure wasn't supplied, so eligibility can't be confirmed.",
                                 stated="customer qualifies", expected=f"{', '.join(self.loyal_plans)} with ≥{fmt_num(self.loyal_min)} months",
                                 source="policies.json › discounts.loyalty"))
            else:
                out.append(Claim("discount_eligibility", "Loyalty discount eligibility", [s0 + m.start(), s0 + m.end()], m.group(0), VERIFIED,
                                 f"Customer is on {plan} with {fmt_num(tenure)} months' tenure: eligible for the loyalty discount.",
                                 plan=plan, stated="customer qualifies", expected=f"{', '.join(self.loyal_plans)} with ≥{fmt_num(self.loyal_min)} months",
                                 source="policies.json › discounts.loyalty + request context"))

        # Loyalty discount said to apply to a non-eligible plan ("loyalty discount on Starter").
        if has_loyal and re.search(r"discount", low):
            for pos, p in self.plans_in(st, s0):
                if p not in self.loyal_plans and not self.negated(st) and not any(c.kind == "discount_eligibility" and c.verdict == CONTRADICTED for c in out):
                    out.append(Claim("discount_eligibility", "Loyalty discount plans", [pos, pos + len(p)], p, CONTRADICTED,
                                     f"The loyalty discount applies only to {', '.join(self.loyal_plans)}, not {p}.",
                                     plan=p, stated=f"loyalty discount on {p}", expected=", ".join(self.loyal_plans),
                                     source="policies.json › discounts.loyalty.applies_to"))
                    break

        # Free trial without a figure: plan eligibility and card requirement.
        trial = CUES["trial"].search(st)
        if trial and "free" in low or (trial and "trial" in low):
            if not any(f.start >= s0 and f.start < sent.end and f.kind == "duration" for f in sfigs):
                named = [(pos, p) for pos, p in self.plans_in(st, s0)]
                if ALL_PLANS_RE.search(st):
                    named = [(s0, p) for p in SELF_SERVE]
                for pos, p in named:
                    if p not in self.trial_plans and not self.negated(st):
                        out.append(Claim("trial_days", "Free trial eligibility", [s0 + trial.start(), s0 + trial.end()], trial.group(0), CONTRADICTED,
                                         f"The email offers a free trial on {p}; free trials are only on {', '.join(self.trial_plans)}.",
                                         plan=p, stated=f"trial on {p}", expected=", ".join(self.trial_plans), source="policies.json › free_trial.plans"))
                        break
            card = CUES["card"].search(st)
            if card:
                says_required = bool(re.search(r"\b(?:requir\w*|need\w*|must|ask\s+for|have\s+to|mandatory)\b", low))
                neg = bool(re.search(r"(?:\b(?:no|not|without|never)\b|n't\b)[^.]{0,25}\b(?:card|requir|need)|\bno\s+(?:credit\s+)?card\b|card[^.]{0,20}(?:\bnot\b|n't\b)", low))
                claims_required = says_required and not neg
                verdict = CONTRADICTED if claims_required != self.card_required else VERIFIED
                out.append(Claim("trial_card", "Card required for trial", [s0 + card.start(), s0 + card.end()], card.group(0), verdict,
                                 ("The email says a card is required for the trial; " if claims_required else "The email says no card is needed; ")
                                 + ("policy agrees." if verdict == VERIFIED else f"policy says card_required = {str(self.card_required).lower()}."),
                                 stated="card required" if claims_required else "no card required",
                                 expected="card required" if self.card_required else "no card required", source="policies.json › free_trial.card_required"))

        # Enterprise: anything other than "custom / contact sales" about Enterprise pricing.
        if re.search(r"\bEnterprise\b", st) and re.search(r"\b(?:price|pricing|cost|costs|priced)\b", low):
            if re.search(r"\bcustom\b|\bcontact\s+(?:our\s+)?sales\b|\bsales\s+team\b|\bquote\b", low):
                m = re.search(r"\bEnterprise\b", st)
                out.append(Claim("price_enterprise", "Enterprise pricing", [s0 + m.start(), s0 + m.end()], m.group(0), VERIFIED,
                                 "Enterprise pricing is custom via sales, as stated.", plan="Enterprise", stated="custom", expected="custom (contact sales)",
                                 source="policies.json › enterprise"))

        # Refund scope: refunds apply to the first payment only.
        refund = CUES["refund"].search(st)
        if refund and re.search(r"\b(?:every|all|any)\s+(?:of\s+your\s+)?(?:payments?|charges?|invoices?|months?|renewals?)\b|\bpast\s+(?:payments|charges|months)\b|\bprorat\w*\b|\bunused\s+(?:time|months|portion)\b", low) and not self.negated(st):
            out.append(Claim("refund_scope", "Refund scope", [s0 + refund.start(), s0 + refund.end()], refund.group(0), CONTRADICTED,
                             f"The email offers refunds beyond the {self.refund_scope}; policy limits refunds to the {self.refund_scope}.",
                             stated="refund beyond first payment", expected=f"{self.refund_scope} only", source="policies.json › refunds.scope"))

        out.extend(self.check_authority(text, sent))
        out.extend(self.check_style(text, sent))
        return out

    # ---- authority (rule 3): promises an agent can't make --------------------------------------------------
    FEATURE_PROMISE = re.compile(
        r"\b(?:(?:i|we)(?:'ll|\s+will|\s+are\s+going\s+to|'re\s+going\s+to|\s+promise(?:\s+to)?|\s+guarantee|\s+can\s+guarantee)|i'll\s+make\s+sure|i\s+will\s+make\s+sure|we'll\s+make\s+sure|"
        r"(?:engineering|the\s+(?:dev|engineering|product)\s+team|our\s+(?:developers|engineers|product\s+team)|product)\s+(?:will|is\s+going\s+to|'ll|plans\s+to|promises))"
        r"[^.!?]{0,80}?\b(?:ship\w*|release\w*|launch\w*|build\w*|add\w*|implement\w*|deliver\w*|roll\w*\s+out|develop\w*|have\s+it\s+(?:ready|done|live)|get\s+(?:it|this|that)\s+(?:built|added|fixed|shipped|done)|fix\w*|support)\b", re.I)
    FEATURE_NOUN = re.compile(r"\b(?:feature|integration|functionality|capabilit\w*|export|import|api|endpoint|dashboard|option|setting|module|plugin|add[\s-]?on|update|version|release|fix|bug|roadmap|sso|support\s+for)\b", re.I)
    ENGINEERING = re.compile(r"\b(?:engineering|engineers|developers|dev\s+team|product\s+team|roadmap)\b", re.I)
    DATE = re.compile(r"\b(?:next\s+(?:week|month|quarter|year|sprint|release)|by\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next|the\s+end\s+of\s+(?:the\s+)?(?:week|month|quarter|year)|end\s+of\s+(?:the\s+)?(?:week|month|quarter|year)|q[1-4]|january|february|march|april|may|june|july|august|september|october|november|december|\d)|"
                      r"(?:this|next)\s+(?:week|month|quarter)|in\s+(?:the\s+)?(?:next\s+)?(?:\d+|a|two|three|few|couple\s+of)\s+(?:days|weeks|months)|q[1-4]\s*(?:20\d\d)?|before\s+(?:your\s+renewal|the\s+end))\b", re.I)
    EXCEPTION = re.compile(
        r"\b(?:make\s+an?\s+exception|as\s+an?\s+exception|one[\s-]time\s+exception|just\s+this\s+once|waive\w*|bend\s+the\s+rules?|special\s+(?:case|exception|arrangement)|off\s+the\s+record|"
        r"override|outside\s+(?:of\s+)?(?:the\s+|our\s+)?(?:policy|refund\s+window|trial)|extend\w*\s+(?:your|the)\s+(?:free\s+)?(?:trial|refund\s+window|deadline)|"
        r"(?:free|complimentary|bonus)\s+(?:upgrade|month|months|credit|seats?)|on\s+the\s+house|comp(?:ed|ing)?\s+(?:you|your)|credit\s+your\s+account|"
        r"(?:extra|additional|special|exclusive|personal)\s+(?:\d+\s*%\s+)?discount|(?:i|we)(?:'ll|\s+will|\s+can)\s+(?:give|offer)\s+you\s+(?:a|an)\s+(?:\w+\s+)?discount)\b", re.I)
    PROMISE_WORD = re.compile(r"\b(?:i|we)\s+(?:promise|guarantee|swear)\b|\bpromise\s+(?:you|that)\b|\byou\s+have\s+my\s+word\b", re.I)

    def check_authority(self, text, sent):
        out = []
        st, s0 = sent.text, sent.start
        if self.can_promise:
            return out
        fp = self.FEATURE_PROMISE.search(st)
        if fp and (self.FEATURE_NOUN.search(st) or self.ENGINEERING.search(st)) and not re.search(r"\b(?:can't|cannot|won't|unable|not\s+able)\b", st[:fp.end()], re.I):
            out.append(Claim("commitment", "Product/feature promise", [s0 + fp.start(), s0 + fp.end()], fp.group(0), REVIEW,
                             "The email promises product work (a feature, fix or release). Agents can't promise product features; a manager must confirm with product first.",
                             stated="promise of product work", expected="no feature promises without manager sign-off",
                             source="policies.json › agent_authority.can_promise_product_features = false; business rule 3"))
        elif self.ENGINEERING.search(st) and self.DATE.search(st) and not self.negated(st):
            m = self.ENGINEERING.search(st)
            out.append(Claim("commitment", "Delivery date promise", [s0 + m.start(), s0 + m.end()], m.group(0), REVIEW,
                             "The email ties engineering/product work to a date. Delivery dates are commitments a manager must approve.",
                             stated="dated product commitment", expected="no delivery dates without manager sign-off", source="business rule 3"))
        elif self.FEATURE_NOUN.search(st) and self.DATE.search(st) and re.search(r"\b(?:will|'ll|going\s+to)\b", st, re.I) \
                and re.search(r"\b(?:ship\w*|releas\w*|launch\w*|available|live|ready|roll\w*\s+out|deliver\w*|coming)\b", st, re.I) and not self.negated(st):
            m = self.DATE.search(st)
            out.append(Claim("commitment", "Delivery date promise", [s0 + m.start(), s0 + m.end()], m.group(0), REVIEW,
                             "The email promises when a feature will ship. Delivery dates are commitments a manager must approve.",
                             stated="feature delivery date", expected="no delivery dates without manager sign-off", source="business rule 3"))
        ex = self.EXCEPTION.search(st)
        if ex and not re.search(r"\b(?:can't|cannot|won't|unable|not\s+able|no)\b[^.]{0,20}" + re.escape(ex.group(0).split()[0]), st, re.I):
            out.append(Claim("exception", "Policy exception", [s0 + ex.start(), s0 + ex.end()], ex.group(0), REVIEW,
                             "The email offers an exception or goodwill concession outside standard policy. Exceptions need a manager's approval.",
                             stated=ex.group(0), expected="standard policy unless a manager approves",
                             source="policies.json › agent_authority.can_offer_discounts_beyond_policy = false; business rule 3"))
        pw = self.PROMISE_WORD.search(st)
        if pw and not out and not re.search(r"\b(?:can't|cannot|won't|not)\s+(?:promise|guarantee)\b", st, re.I):
            out.append(Claim("commitment", "Explicit promise", [s0 + pw.start(), s0 + pw.end()], pw.group(0), REVIEW,
                             "The email makes an explicit promise or guarantee. Promises the company may not be able to keep need a manager's review.",
                             stated=pw.group(0), expected="no guarantees without manager sign-off", source="business rules 3 and 4"))
        return out

    # ---- style (rule 4): hype, guarantees, flattery --------------------------------------------------------
    HYPE = re.compile(r"\b(?:amazing|incredible|unbelievable|life[\s-]changing|revolutionary|mind[\s-]blowing|unbeatable|insane(?:ly)?|"
                      r"once[\s-]in[\s-]a[\s-]lifetime|best\s+\w*\s*(?:ever|deal|value|decision|choice|plan\s+(?:ever|on\s+the\s+market))|greatest|"
                      r"you(?:'ll|\s+will|\s+are\s+going\s+to)\s+(?:absolutely\s+)?(?:love|adore)|act\s+now|don't\s+miss|no[\s-]brainer|"
                      r"100\s*%\s+(?:satisf\w*|guaranteed)|risk[\s-]free)\b", re.I)
    GUARANTEE = re.compile(r"\bguarantee[ds]?\b", re.I)
    FLATTERY = re.compile(r"\b(?:great\s+question|excellent\s+(?:question|point)|you'?re\s+(?:absolutely|totally|completely|100%)\s+right|you\s+are\s+(?:absolutely|totally|completely)\s+right|"
                          r"brilliant|genius|as\s+(?:you\s+)?(?:said|rightly\s+(?:said|pointed\s+out))|you'?re\s+right|you\s+are\s+right|good\s+point|what\s+a\s+(?:great|smart))\b", re.I)

    def check_style(self, text, sent):
        out = []
        st, s0 = sent.text, sent.start
        g = self.GUARANTEE.search(st)
        if g and not re.search(r"\b(?:can't|cannot|won't|not|no|unable\s+to)\b[^.]{0,20}\bguarantee", st, re.I):
            out.append(Claim("tone", "Guarantee", [s0 + g.start(), s0 + g.end()], g.group(0), REVIEW,
                             "The email gives a guarantee. Business rule 4: guarantees need a manager's review even when every number is right.",
                             stated=g.group(0), expected="no guarantees", source="business rule 4"))
        hype = list(self.HYPE.finditer(st))
        bangs = len(re.findall(r"!{2,}", st))
        caps = len([w for w in re.findall(r"\b[A-Z]{4,}\b", st) if w not in ("GDPR", "CCPA", "HIPAA", "HTTPS", "USD", "SAML", "OAUTH", "JSON")])
        if hype or bangs or caps >= 2:
            first = hype[0] if hype else (re.search(r"!{2,}", st) or re.search(r"\b[A-Z]{4,}\b", st))
            signals = [f"'{h.group(0)}'" for h in hype[:3]] + (["repeated exclamation marks"] if bangs else []) + ([f"{caps} all-caps words"] if caps >= 2 else [])
            if len(hype) + bangs + (caps >= 2) >= 1 and (hype or bangs >= 1 and caps >= 1 or caps >= 2 and bangs):
                out.append(Claim("tone", "Promotional language", [s0 + first.start(), s0 + first.end()], first.group(0), REVIEW,
                                 "Over-the-top promotional language (" + ", ".join(signals) + "). Business rule 4: hype needs a manager's review even when the facts are right.",
                                 stated=", ".join(signals), expected="neutral, factual tone", source="business rule 4"))
        flat = list(self.FLATTERY.finditer(st))
        if len(flat) >= 2 or (flat and re.search(r"absolutely|totally|completely|100%|brilliant|genius", flat[0].group(0), re.I)):
            out.append(Claim("tone", "Flattery / over-agreement", [s0 + flat[0].start(), s0 + flat[0].end()], flat[0].group(0), REVIEW,
                             "The email flatters or over-agrees with the customer. Business rule 4: flattery and agreeing with a customer's claim need review.",
                             stated=", ".join(f"'{f.group(0)}'" for f in flat[:3]), expected="independent, factual reply", source="business rule 4"))
        return out


def dedupe(claims):
    """One claim per span and kind; when a span is judged twice, the strictest verdict wins."""
    rank = {VERIFIED: 0, REVIEW: 1, CONTRADICTED: 2}
    best = {}
    for c in claims:
        key = (c.kind if c.kind not in ("price", "stacking", "price_saving") else "money", tuple(c.span) if c.span else None)
        if key not in best or rank[c.verdict] > rank[best[key].verdict]:
            best[key] = c
    out = sorted(best.values(), key=lambda c: (c.span or [10 ** 9])[0])
    for i, c in enumerate(out, 1):
        c.level = {CONTRADICTED: "BLOCK", REVIEW: "HOLD_FOR_REVIEW"}.get(c.verdict)
    return out


def check(text, context, sot):
    return Checker(sot).check(text, context)
