"""Flag columns holding personal / confidential data, by column name and by sampled value formats."""
import re

# (category, logical-name regex). Matched against the prefix-stripped logical name and the raw name.
NAME_RULES = [
    ("PAN", r"^pan(no|num|number|card)?$|panno|pancard"),
    ("Aadhaar", r"aadh?aa?r|^uid(no)?$|uidai"),
    ("Mobile/phone", r"mobile|^mob(no)?$|phone|^ph(no)?$|contactno|cellno|whatsapp"),
    ("Email", r"e?mail"),
    ("Name", r"^(first|last|middle|full|client|cust|customer|nominee|father|mother|spouse|holder|account|acc|"
             r"person|user|emp|employee|guardian|contact|beneficiary)_?name$|^fname$|^lname$"),
    ("Address", r"addr|address|street|pincode|^pin$|zipcode|^zip$"),
    ("Date of birth", r"^dob$|birth|^bdate$"),
    ("Bank account", r"bankacc|bank_?ac|^acc(ount)?_?no$|^acno$|^accno$|accountnumber|^bankac"),
    ("IFSC/MICR", r"ifsc|micr|swift"),
    ("Card", r"card_?no|cardnum|creditcard|debitcard|^cvv$"),
    ("Credential", r"pass(word|wd)?$|^pwd$|^pin_?code$|^otp$|secret|token|apikey|api_key|hash$|salt$"),
    ("Tax/ID number", r"^gst(in|no)?$|passport|^ssn$|voterid|drivinglic|^tin$"),
    ("Demat/DP ID", r"^(bo|dp)_?id$|^boid$|demat_?(ac|acc|no)"),
    ("Income/financial", r"salary|income|networth|annualinc"),
    ("IP address", r"^ip(addr|address)?$|ipaddress"),
]
NAME_RULES = [(cat, re.compile(rx)) for cat, rx in NAME_RULES]
# A bare 'name' column is personal data only in tables about people.
PERSON_TABLE = re.compile(r"client|cust|user|employee|emp|nominee|holder|person|contact|investor|guardian|"
                          r"beneficiar|dealer|broker|agent|kyc|member|account")

# Value formats checked on sampled values.
VALUE_RULES = [
    ("PAN", re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")),
    ("Aadhaar", re.compile(r"^[2-9][0-9]{3}\s?[0-9]{4}\s?[0-9]{4}$")),
    ("Email", re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")),
    ("Mobile/phone", re.compile(r"^(\+?91[\s-]?)?[6-9][0-9]{9}$")),
    ("IFSC/MICR", re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")),
    ("Card", re.compile(r"^[0-9]{13,19}$")),
    ("IP address", re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")),
]


def by_name(*names):
    for n in names:
        n = (n or "").lower()
        for cat, rx in NAME_RULES:
            if n and rx.search(n):
                return cat
    return None


def _luhn(s):
    digits = [int(d) for d in s][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return total % 10 == 0


def by_values(values, min_ratio=0.6):
    """Category whose format at least min_ratio of the non-empty sampled values match."""
    vals = [str(v).strip() for v in values if v is not None and str(v).strip()]
    if len(vals) < 3:
        return None
    for cat, rx in VALUE_RULES:
        hits = [v for v in vals if rx.match(v)]
        if cat == "Card":
            hits = [v for v in hits if _luhn(v)]
        if len(hits) / len(vals) >= min_ratio:
            return cat
    return None


def shape(value, keep=0):
    """Format of a value without its content: 'ABCPX1234X' -> 'AAAAA9999A'."""
    s = str(value)
    out = []
    for ch in s[: 40]:
        out.append("A" if ch.isupper() else "a" if ch.isalpha() else "9" if ch.isdigit() else ch)
    return "".join(out) + ("…" if len(s) > 40 else "")


def classify(column, logical, values, cfg, table=""):
    """Return the sensitivity category for a column, or None."""
    name = column.lower()
    if name in cfg.sensitive_exclude or logical in cfg.sensitive_exclude:
        return None
    if name in cfg.sensitive_include or logical in cfg.sensitive_include:
        return "Marked sensitive in config"
    if logical in ("name", "fullname") and PERSON_TABLE.search((table or "").lower()):
        return "Name"
    return by_name(logical, name) or by_values(values or [])
