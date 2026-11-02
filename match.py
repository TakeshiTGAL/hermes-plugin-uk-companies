"""Exact name comparison and one typed company query.

Equal text after case and spacing are folded is a match. Symbols stay in the
text, so a dropped ampersand is a different name. A compatibility character
stays as written. Anything else stays unconfirmed.

A typed query may carry a trailing semicolon, a trailing note, or an official
company link. The link is read on this machine. It is not requested.
"""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import re
import unicodedata
import urllib.parse

verdict_exact_ja = "一致"
verdict_possible_ja = "同名の可能性・未確認"
no_key_ja = "鍵が無いので調べていない。"

_COMPANY_NUMBER = re.compile(r"^(?:[A-Z]{2}\d{6}|[A-Z]\d{7}|\d{6,8})$")
_POSTCODE = re.compile(r"^[A-Z]{1,2}\d[A-Z\d]? \d[A-Z]{2}$")
_CHARITY_NUMBER = re.compile(r"^\d{1,8}$")
_SUFFIX = re.compile(r"^\d{1,4}$")


def norm(value: object) -> str:
    text = "" if value is None else str(value)
    text = text.lower()
    text = re.sub(r"\s+", " ", text).strip()
    return text


def compare(left: object, right: object) -> str:
    """Return exact, possible, or unchecked."""
    a = norm(left)
    b = norm(right)
    if not a or not b:
        return "unchecked"
    if a == b:
        return "exact"
    return "possible"


_HONORIFICS = frozenset({
    "mr", "mrs", "ms", "miss", "mx", "dr", "prof", "sir", "dame",
    "lord", "lady", "madam", "professor", "hon",
})
_CORPORATE = frozenset({
    "limited", "ltd", "plc", "llp", "llc", "inc", "gmbh", "company",
    "corp", "corporation", "partnership",
})


def _honorific(token: str) -> bool:
    return token.lower().rstrip(".") in _HONORIFICS


def _corporate(text: str) -> bool:
    tokens = {token.lower().rstrip(".") for token in text.split() if token}
    return bool(tokens & _CORPORATE)


def _person_parts(value: object) -> tuple[str, list[str]] | None:
    """Return (surname, forename tokens) for a Companies House person line.

    The officer list writes surname, forenames, and an optional title.
    The disqualified-officer index writes forenames then surname.
    A corporate name stays out of this split.
    """
    raw = "" if value is None else str(value)
    text = re.sub(r"\s+", " ", raw).strip()
    pieces = [piece.strip() for piece in text.split(",") if piece.strip()]
    if len(pieces) < 2 or _corporate(pieces[1]):
        return None
    surname = [token for token in pieces[0].split() if token and not _honorific(token)]
    forenames = [token for token in pieces[1].split() if token and not _honorific(token)]
    if not surname or not forenames:
        return None
    return " ".join(surname), forenames


def _query_word(token: str) -> str:
    letters = [ch for ch in token if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        return token[:1].upper() + token[1:].lower()
    return token


def person_query(value: object) -> str:
    """The disqualified-officer search string for one officer name.

    The officer list stores the surname in capitals. The search that returns
    the person uses forenames, then the surname with one capital.
    """
    found = _person_parts(value)
    if found:
        surname, forenames = found
        surname_text = " ".join(_query_word(token) for token in surname.split())
        return f"{' '.join(forenames)} {surname_text}"
    raw = "" if value is None else str(value)
    tokens = [token for token in re.sub(r"\s+", " ", raw).strip().split(" ") if token and not _honorific(token)]
    return " ".join(tokens)


def person_key(value: object) -> str:
    found = _person_parts(value)
    if found:
        surname, forenames = found
        return norm(f"{' '.join(forenames)} {surname}")
    return norm(" ".join(
        token for token in norm(value).split(" ") if token and not _honorific(token)
    ))


def person_compare(left: object, right: object) -> str:
    """Exact when the person name is the same after order and title are folded."""
    a = person_key(left)
    b = person_key(right)
    if not a or not b:
        return "unchecked"
    if a == b:
        return "exact"
    return "possible"


def shares_surname_and_first(officer_name: object, title: object) -> bool:
    """True when both lines share a surname and a first forename.

    The officer line is surname, then forenames. The index line is forenames, then surname.
    A line that is not a person stays false, so a fuzzy title is left out.
    """
    found = _person_parts(officer_name)
    if not found:
        return False
    surname, forenames = found
    first = norm(forenames[0])
    sur = [norm(token) for token in surname.split()]
    raw = re.sub(r"\s+", " ", "" if title is None else str(title)).strip()
    if "," in raw:
        other = _person_parts(title)
        if not other:
            return False
        other_sur, other_firsts = other
        return [norm(token) for token in other_sur.split()] == sur and norm(other_firsts[0]) == first
    tokens = [norm(token) for token in raw.split(" ") if token and not _honorific(token)]
    if len(tokens) < 1 + len(sur):
        return False
    return tokens[0] == first and tokens[-len(sur):] == sur


def _folded_person(value: object) -> str:
    """Drop accents and treat a hyphen as a space. Exact comparison does not use this."""
    raw = "" if value is None else str(value)
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.replace("-", " ")
    return re.sub(r"\s+", " ", raw).strip()


def shares_surname(officer_name: object, title: object) -> bool:
    """True when both lines share a surname after accents and hyphens are folded.

    The officer line is surname, then forenames. A line that is not a person stays false.
    """
    found = _person_parts(_folded_person(officer_name))
    if not found:
        return False
    surname, _forenames = found
    sur = [norm(token) for token in surname.split()]
    folded = _folded_person(title)
    if "," in folded:
        other = _person_parts(folded)
        if not other:
            return False
        other_sur, _other_firsts = other
        return [norm(token) for token in other_sur.split()] == sur
    tokens = [norm(token) for token in folded.split(" ") if token and not _honorific(token)]
    if len(tokens) < len(sur):
        return False
    return tokens[-len(sur):] == sur


def verdict(kind: str) -> tuple[str, str]:
    if kind == "exact":
        return "exact", verdict_exact_ja
    if kind == "possible":
        return "possible", verdict_possible_ja
    return "unchecked", ""


def company_number(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = re.sub(r"\s+", "", text).upper()
    if _COMPANY_NUMBER.fullmatch(text):
        if text.isdigit():
            return text.zfill(8)
        return text
    return None


def postcode(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value))
    text = re.sub(r"\s+", "", text).upper()
    if len(text) < 5:
        return None
    candidate = text[:-3] + " " + text[-3:]
    if _POSTCODE.fullmatch(candidate):
        return candidate
    return None


def charity_number(value: object) -> str | None:
    text = unicodedata.normalize("NFKC", "" if value is None else str(value)).strip()
    if _CHARITY_NUMBER.fullmatch(text):
        return text
    return None


def charity_suffix(value: object) -> str | None:
    if value is None or value == "":
        return "0"
    text = unicodedata.normalize("NFKC", str(value)).strip()
    if _SUFFIX.fullmatch(text):
        return str(int(text))
    return None


def plain_text(value: object, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFKC", value).strip()
    if not text or len(text) > limit:
        return None
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        return None
    if "\uffff" in text:
        return None
    return text


def _strip_edges(text: str) -> str:
    while True:
        nxt = text.strip().rstrip(";").strip()
        if nxt == text:
            return text
        text = nxt


def typed_text(value: object, limit: int = 160) -> str | None:
    """plain_text, then drop trailing semicolons. The limit applies after that."""
    text = plain_text(value, limit + 8)
    if text is None:
        return None
    text = _strip_edges(text)
    if not text or len(text) > limit:
        return None
    return text


_OFFICIAL_HOSTS = frozenset({
    "find-and-update.company-information.service.gov.uk",
    "api.company-information.service.gov.uk",
})
_COMPANY_PATH = re.compile(r"^/company/([^/]+)(?:/.*)?$")


LINK_REASONS = frozenset({
    "http",
    "scheme",
    "userinfo",
    "other_host",
    "not_company_path",
    "bad_path",
    "port",
    "encoded",
    "not_a_number",
})

_REFUSAL = {
    "http": "An http address was refused, so nothing was sent. Pass the https address or the company name or number.",
    "scheme": "That address scheme was refused, so nothing was sent. Pass an https address or the company name or number.",
    "userinfo": "An address with a user name was refused, so nothing was sent. Pass the company name or number.",
    "other_host": "That host was refused, so nothing was sent. Pass a company name or number.",
    "not_company_path": "That page is not a /company/ link, so nothing was sent. Pass a company name or number.",
    "bad_path": "That path was refused, so nothing was sent. Pass a company name or number.",
    "port": "That port was refused, so nothing was sent. Pass a company name or number.",
    "encoded": "That company number link was refused, so nothing was sent. Pass the company number itself.",
    "not_a_number": "That link has no company number, so nothing was sent. Pass a company name or number.",
    "empty": "Pass a company name or number.",
    "too_long": "Pass a company name or number.",
    "not_text": "Pass a company name or number.",
    "controls": "Pass a company name or number.",
}

_NUMBER_FORM = "The company number is not in the expected form."
_SEARCH_LINK = "A company link is not a search name, so nothing was sent. Call uk_check with that link."


def refusal_message(reason: str) -> str:
    return _REFUSAL.get(reason, _REFUSAL["not_text"])


def _url_attempt(text: str) -> str | None:
    if "://" in text:
        return text
    lower = text.lower()
    for host in _OFFICIAL_HOSTS:
        if lower.startswith(host + "/"):
            return "https://" + text
    return None


def _link_reason(text: str) -> tuple[str | None, str]:
    """Return (company number, reason). Reason is empty when the number is kept."""
    try:
        parts = urllib.parse.urlsplit(text)
    except ValueError:
        return None, "bad_path"
    if parts.username or parts.password:
        return None, "userinfo"
    if parts.scheme != "https":
        return None, "http" if parts.scheme == "http" else "scheme"
    if parts.port not in (None, 443):
        return None, "port"
    host = (parts.hostname or "").lower().rstrip(".")
    if host not in _OFFICIAL_HOSTS:
        return None, "other_host"
    if ".." in parts.path.split("/"):
        return None, "bad_path"
    found = _COMPANY_PATH.fullmatch(parts.path)
    if not found:
        return None, "not_company_path"
    segment = found.group(1)
    if "%" in segment or "\\" in segment:
        return None, "encoded"
    number = company_number(segment)
    if not number:
        return None, "not_a_number"
    return number, ""


def classify_query(value: object) -> tuple[str | None, str]:
    """Return (accepted text, reason). Reason is empty when the text is kept.

    The rows are test_query_input_table. A link refusal names http, a user
    name, another host, or the path. A name that is empty or too long stays
    a plain request for a company name or number.
    """
    if not isinstance(value, str):
        return None, "not_text"
    text = plain_text(value, 300)
    if text is None:
        raw = unicodedata.normalize("NFKC", value).strip()
        if not raw:
            return None, "empty"
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw) or "\uffff" in raw:
            return None, "controls"
        return None, "too_long"
    text = _strip_edges(text)
    if not text:
        return None, "empty"
    attempt = _url_attempt(text)
    if attempt is not None:
        return _link_reason(attempt)
    if " #" in text:
        left = text.partition(" #")[0].strip()
        number = company_number(left)
        if number:
            return number, ""
    if len(text) > 160:
        return None, "too_long"
    return text, ""


def prepared_query(value: object) -> str | None:
    """One typed company query. The rows are test_query_input_table."""
    text, _reason = classify_query(value)
    return text


def number_argument(value: object) -> tuple[str | None, str]:
    """A company number, or a refusal message when the text cannot be one."""
    text, reason = classify_query(value)
    if text is None:
        if reason in LINK_REASONS:
            return None, refusal_message(reason)
        return None, _NUMBER_FORM
    number = company_number(text)
    if not number:
        return None, _NUMBER_FORM
    return number, ""


def search_name(value: object) -> tuple[str | None, str]:
    """A Companies House search name. A link is refused and nothing is sent."""
    text, reason = classify_query(value)
    if text is None:
        if reason in LINK_REASONS:
            return None, refusal_message(reason)
        return None, "Pass name as a string."
    if isinstance(value, str):
        raw = plain_text(value, 300)
        if raw and _url_attempt(_strip_edges(raw)):
            return None, _SEARCH_LINK
    return text, ""


def user_company_number(value: object) -> str | None:
    """A company number typed by a person, including a pasted official link."""
    if isinstance(value, str):
        number, _message = number_argument(value)
        return number
    return company_number(value)


def loose_token(value: object, limit: int, accept):
    """Trim one typed token, then accept it. A trailing ' # note' drops only when accept holds."""
    if isinstance(value, str):
        text = typed_text(value, limit)
        if text is None:
            return None
        if " #" in text:
            left = text.partition(" #")[0].strip()
            accepted = accept(left)
            if accepted:
                return accepted
        return accept(text)
    return accept(value)
