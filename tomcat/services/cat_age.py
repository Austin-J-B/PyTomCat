"""Age estimates shared by cached and live cat profiles."""

from datetime import date, datetime, timezone


def parse_profile_date(value):
    """Parse dates in the formats used by CatDatabase."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def format_age_value(raw, last_seen=None) -> str:
    """Convert various birthday/age representations into friendly text.

    - If provided an integer/float: interpret as years (with <1 treated as months).
    - If provided a date string: compute age at the last sighting, if known.
    - Otherwise return the original text.
    """
    if raw in (None, ""):
        return ""

    def _months_to_text(months: int) -> str:
        if months <= 0:
            return "Less than 1 month"
        if months == 1:
            return "1 month"
        return f"{months} months"

    def _years_to_text(years: int) -> str:
        if years == 1:
            return "1 year"
        return f"{years} years"

    #Numeric values: treat as approximate years
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value < 1:
            months = max(1, int(round(value * 12)))
            return _months_to_text(months)
        years = int(round(value))
        years = max(1, years)
        return _years_to_text(years)

    text = str(raw).strip()
    if not text:
        return ""

    parsed = parse_profile_date(text)
    if parsed is None:
        return text

    today = datetime.now(timezone.utc).date()
    seen = parse_profile_date(last_seen)
    today = min(today, seen) if seen else today
    if parsed > today:
        return text

    #Compute months difference
    months_total = (today.year - parsed.year) * 12 + (today.month - parsed.month)
    if today.day < parsed.day:
        months_total -= 1

    if months_total <= 0:
        return "Less than 1 month"
    if months_total < 12:
        return _months_to_text(months_total)

    years = months_total // 12
    return _years_to_text(years)



def age_estimate_label(birthday, last_seen):
    """Explain when a dated estimate applies, avoiding misleading labels."""
    birth = parse_profile_date(birthday)
    seen = parse_profile_date(last_seen)
    today = datetime.now(timezone.utc).date()
    if birth and seen and birth <= seen <= today:
        return "Age Estimate (at last sighting)"
    return "Age Estimate"
