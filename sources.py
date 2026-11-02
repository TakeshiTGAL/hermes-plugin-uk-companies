"""Official-register reads. Shapes that were not seen live are documented outside this repo."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import re
import urllib.parse
from datetime import date

if __package__:
    from . import client, match
    from .client import (
        CHARITY_HOST,
        CHARITY_KEY,
        CH_HOST,
        GAZETTE_HOST,
        LAND_HOST,
        ApiError,
        Deadline,
        basic_headers,
        charity_headers,
        ensure_time,
        json_headers,
        land_headers,
        read_secret,
        request_json,
    )
    from .match import (
        charity_number,
        charity_suffix,
        company_number,
        compare,
        no_key_ja,
        norm,
        person_compare,
        person_query,
        shares_surname,
        shares_surname_and_first,
        loose_token,
        plain_text,
        postcode,
        typed_text,
        verdict,
    )
else:
    import client
    import match
    from client import (
        CHARITY_HOST,
        CHARITY_KEY,
        CH_HOST,
        GAZETTE_HOST,
        LAND_HOST,
        ApiError,
        Deadline,
        basic_headers,
        charity_headers,
        ensure_time,
        json_headers,
        land_headers,
        read_secret,
        request_json,
    )
    from match import (
        charity_number,
        charity_suffix,
        company_number,
        compare,
        no_key_ja,
        norm,
        person_compare,
        person_query,
        shares_surname,
        shares_surname_and_first,
        loose_token,
        plain_text,
        postcode,
        typed_text,
        verdict,
    )

FIND = "https://find-and-update.company-information.service.gov.uk"
PAGE_SIZE = 50
SEARCH_PAGE = 20
MAX_PAGES = 2
MAX_DISQUALIFY = 20
MAX_DISQUALIFY_PROFILES = 3
LAND_LIMIT = 10
_GAZETTE_HOST = "www.thegazette.co.uk"
_LR_HOST = "landregistry.data.gov.uk"
_NOTICE = re.compile("^https://" + re.escape(_GAZETTE_HOST) + "/notice/[A-Za-z0-9-]+$")
_OFFICER_ID = re.compile(r"^/disqualified-officers/natural/([A-Za-z0-9_-]{1,80})$")
_APPOINTMENT = re.compile(r"^/company/([A-Z0-9]{6,8})/appointments/([A-Za-z0-9_-]{1,80})$")
_OFFICER_APPTS = re.compile(r"^/officers/([A-Za-z0-9_-]{1,80})/appointments$")
_PSC_KIND = (
    "individual|individual-beneficial-owner|"
    "corporate-entity|corporate-entity-beneficial-owner|"
    "legal-person|legal-person-beneficial-owner|"
    "super-secure|super-secure-beneficial-owner"
)
_PSC_SELF = re.compile(
    r"^/company/([A-Z0-9]{6,8})/persons-with-significant-control/(?:"
    + _PSC_KIND
    + r")/([A-Za-z0-9_-]{1,80})$"
)
_TX = re.compile("^http://" + re.escape(_LR_HOST) + r"/data/ppi/transaction/([0-9A-Fa-f-]{36})/current$")
_CH_LINK_HOSTS = frozenset({
    "api.company-information.service.gov.uk",
    "find-and-update.company-information.service.gov.uk",
})
_CH_COMPANY = re.compile(
    r"^/company/([A-Z0-9]{6,8})(/officers|/persons-with-significant-control|/filing-history)?$"
)


def _total(payload: dict) -> int | None:
    for key in ("total_results", "total_count"):
        if key in payload:
            try:
                return int(payload[key])
            except (TypeError, ValueError):
                return None
    return None


def _complete(rows: list, total: int | None) -> bool:
    return total is not None and total == len(rows)


def _note(rows: list, total: int | None, zero_sentence: str) -> tuple[bool, str]:
    """Say whether this page is the whole list.

    A total of 0 beside real rows, or any other total that disagrees with the
    page, is incomplete. The rows stay in the result and the note says why.
    """
    if total is None:
        return False, "The response did not include a total, so later rows were not read."
    if total == 0 and rows:
        return False, "The response said the total was 0 and still included rows, so the list was not treated as complete."
    if total > len(rows):
        return False, "Later rows were not read."
    if total < len(rows):
        return False, "The response total was lower than the rows on the page, so the list was not treated as complete."
    if total == 0:
        return True, zero_sentence
    return True, ""


def block(status: str, reason: str, rows: list | None = None, **extra: object) -> dict:
    body_rows = list(rows or [])
    out = {
        "status": status,
        "reason": reason,
        "rows": body_rows,
        "rows_read": len(body_rows),
    }
    out.update(extra)
    return out


def _ch_path(path: str) -> None:
    if path in {"/search/companies", "/search/disqualified-officers"}:
        return
    company = _CH_COMPANY.fullmatch(path)
    if company and company_number(company.group(1)):
        return
    if _OFFICER_ID.fullmatch(path):
        return
    raise ApiError("bad_input", "That Companies House path is not called.", client.NEXT["bad_input"])


def ch_get(path: str, query: dict | None, deadline: Deadline | None) -> dict:
    _ch_path(path)
    key = read_secret(client.CH_KEY)
    if not key:
        raise ApiError(
            "no_key",
            "Set COMPANIES_HOUSE_API_KEY in the active profile .env. " + no_key_ja,
            client.NEXT["no_key"],
        )
    url = f"https://{CH_HOST}{path}"
    if query:
        url += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v is not None})
    _status, payload = request_json(url, basic_headers(key), deadline)
    if not isinstance(payload, dict):
        raise ApiError("bad_shape", "Companies House returned JSON that is not an object.", client.NEXT["bad_shape"])
    return payload


def _paged(path: str, deadline: Deadline | None) -> tuple[list[dict], int | None]:
    rows: list[dict] = []
    total: int | None = None
    for page in range(MAX_PAGES):
        ensure_time(deadline)
        payload = ch_get(
            path,
            {"items_per_page": PAGE_SIZE, "start_index": page * PAGE_SIZE},
            deadline,
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise ApiError("bad_shape", "Companies House omitted the items list.", client.NEXT["bad_shape"])
        rows.extend(item for item in items if isinstance(item, dict))
        total = _total(payload)
        if total is not None and len(rows) >= total:
            break
        if len(items) < PAGE_SIZE:
            break
    return rows, total


def _address(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    parts = []
    for key in (
        "premises",
        "address_line_1",
        "address_line_2",
        "locality",
        "region",
        "postal_code",
        "country",
        "po_box",
    ):
        piece = value.get(key)
        if isinstance(piece, str) and piece.strip():
            parts.append(piece.strip())
    return ", ".join(parts)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _official_path(value: object) -> str:
    """Relative path on the Companies House API host. Other hosts are ignored."""
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or ".." in text:
        return ""
    parts = urllib.parse.urlsplit(text)
    if parts.scheme or parts.netloc:
        if (parts.hostname or "").lower() not in _CH_LINK_HOSTS:
            return ""
        path = parts.path
    elif text.startswith("/"):
        path = text.split("?", 1)[0]
    else:
        return ""
    if not path.startswith("/") or ".." in path:
        return ""
    return path


def _identity_fields(item: dict, number: str, path: str) -> dict:
    """Appointment id and officer id come from the public officer list.

    links.self is one appointment. links.officer.appointments is the officer.
    A PSC list uses links.self on the documented PSC notification paths.
    A 200 body with these fields has not been seen live.
    """
    links = item.get("links") if isinstance(item.get("links"), dict) else {}
    self_path = _official_path(links.get("self"))
    if "/officers" in path:
        appointment = ""
        found = _APPOINTMENT.fullmatch(self_path)
        if found and found.group(1) == number:
            appointment = found.group(2)
        officer = links.get("officer") if isinstance(links.get("officer"), dict) else {}
        officer_id = ""
        found_officer = _OFFICER_APPTS.fullmatch(_official_path(officer.get("appointments")))
        if found_officer:
            officer_id = found_officer.group(1)
        return {
            "appointment_id": appointment,
            "appointment_reason": "" if appointment else "The officer row did not include links.self for this company appointment.",
            "officer_id": officer_id,
            "officer_id_reason": "" if officer_id else "The officer row did not include links.officer.appointments.",
        }
    psc_id = ""
    found_psc = _PSC_SELF.fullmatch(self_path)
    if found_psc and found_psc.group(1) == number:
        psc_id = found_psc.group(2)
    return {
        "psc_id": psc_id,
        "psc_reason": "" if psc_id else "The PSC row did not include links.self for this company.",
    }


def search_companies(name: str, deadline: Deadline | None) -> dict:
    text = typed_text(name, 160)
    if text is None:
        raise ApiError("bad_input", "The company name is empty or too long.", client.NEXT["bad_input"])
    payload = ch_get("/search/companies", {"q": text, "items_per_page": SEARCH_PAGE, "start_index": 0}, deadline)
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiError("bad_shape", "Company search omitted the items list.", client.NEXT["bad_shape"])
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        number = company_number(item.get("company_number"))
        rows.append({
            "title": _text(item.get("title")),
            "company_number": number or "",
            "company_status": _text(item.get("company_status")),
            "address": _text(item.get("address_snippet")) or _address(item.get("address")),
            "official_url": f"{FIND}/company/{number}" if number else "",
            "url_reason": "" if number else "The search row did not include a company number.",
        })
    total = _total(payload)
    full, note = _note(rows, total, "Companies House search returned 0 companies for this name. A zero count leaves the question open.")
    return {
        "query": text,
        "rows": rows,
        "rows_read": len(rows),
        "total_reported": total,
        "fully_read": full,
        "note": note,
    }


def fetch_profile(number: str, deadline: Deadline | None) -> dict:
    checked = company_number(number)
    if not checked:
        raise ApiError("bad_input", "The company number is not in the expected form.", client.NEXT["bad_input"])
    try:
        payload = ch_get(f"/company/{checked}", None, deadline)
    except ApiError as exc:
        if exc.status == 404:
            raise ApiError("upstream", "Companies House returned 404 for this number.", client.NEXT["not_found"], 404) from None
        raise
    if not _text(payload.get("company_name")):
        raise ApiError("bad_shape", "The company profile did not include a company name.", client.NEXT["bad_shape"])
    office = payload.get("registered_office_address")
    raw_postal = ""
    code = ""
    if isinstance(office, dict):
        raw = office.get("postal_code")
        if isinstance(raw, str):
            raw_postal = raw.strip()
        code = postcode(raw) or ""
    if code:
        postcode_reason = ""
    elif raw_postal:
        postcode_reason = "The registered-office postcode was not a UK postcode, so Price Paid was not queried."
    else:
        postcode_reason = "The profile did not include a registered-office postcode."
    return {
        "company_number": checked,
        "company_name": _text(payload.get("company_name")),
        "company_status": _text(payload.get("company_status")),
        "date_of_creation": _text(payload.get("date_of_creation")),
        "type": _text(payload.get("type")),
        "registered_office": _address(office),
        "postcode": code,
        "postcode_reason": postcode_reason,
        "sic_codes": [item for item in payload.get("sic_codes") or [] if isinstance(item, str)],
        "official_url": f"{FIND}/company/{checked}",
        **_profile_compliance(payload, checked),
    }


def _people(path: str, deadline: Deadline | None, zero_sentence: str) -> dict:
    checked_path = path
    rows_raw, total = _paged(checked_path, deadline)
    number = path.split("/")[2]
    rows = []
    for item in rows_raw:
        month, year = _birth_parts(item.get("date_of_birth"))
        natures = item.get("natures_of_control")
        rows.append({
            "name": _text(item.get("name")),
            "role": _text(item.get("officer_role")) or _text(item.get("kind")),
            "appointed_on": _text(item.get("appointed_on")) or _text(item.get("notified_on")),
            "ended_on": _text(item.get("resigned_on")) or _text(item.get("ceased_on")),
            "nationality": _text(item.get("nationality")),
            "country_of_residence": _text(item.get("country_of_residence")),
            "birth_month": month,
            "birth_year": year,
            "natures_of_control": [piece for piece in natures if isinstance(piece, str)] if isinstance(natures, list) else [],
            "address": _address(item.get("address")),
            "official_url": f"{FIND}{path}",
            **_identity_fields(item, number, path),
        })
    full, note = _note(rows, total, zero_sentence)
    return {
        "company_number": number,
        "rows": rows,
        "rows_read": len(rows),
        "total_reported": total,
        "fully_read": full,
        "note": note,
        "official_url": f"{FIND}{path}",
    }


def fetch_officers(number: str, deadline: Deadline | None) -> dict:
    checked = company_number(number)
    if not checked:
        raise ApiError("bad_input", "The company number is not in the expected form.", client.NEXT["bad_input"])
    return _people(
        f"/company/{checked}/officers",
        deadline,
        "The officers response listed 0 officers.",
    )


def fetch_psc(number: str, deadline: Deadline | None) -> dict:
    checked = company_number(number)
    if not checked:
        raise ApiError("bad_input", "The company number is not in the expected form.", client.NEXT["bad_input"])
    return _people(
        f"/company/{checked}/persons-with-significant-control",
        deadline,
        "The persons with significant control response listed 0 people.",
    )


def fetch_filings(number: str, deadline: Deadline | None) -> dict:
    checked = company_number(number)
    if not checked:
        raise ApiError("bad_input", "The company number is not in the expected form.", client.NEXT["bad_input"])
    payload = ch_get(
        f"/company/{checked}/filing-history",
        {"items_per_page": PAGE_SIZE, "start_index": 0},
        deadline,
    )
    items = payload.get("items")
    if not isinstance(items, list):
        raise ApiError("bad_shape", "Filing history omitted the items list.", client.NEXT["bad_shape"])
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append({
            "transaction_id": _text(item.get("transaction_id")),
            "category": _text(item.get("category")),
            "date": _text(item.get("date")),
            "description": _text(item.get("description")),
            "type": _text(item.get("type")),
            "official_url": f"{FIND}/company/{checked}/filing-history",
        })
    total = _total(payload)
    full, note = _note(rows, total, "The filing-history response listed 0 filings.")
    if len(items) > len(rows):
        full = False
        note = "Some filing rows were not objects, so the page was not fully used."
    return {
        "company_number": checked,
        "rows": rows,
        "rows_read": len(rows),
        "total_reported": total,
        "fully_read": full,
        "note": note,
        "official_url": f"{FIND}/company/{checked}/filing-history",
    }


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _profile_compliance(payload: dict, number: str) -> dict:
    """Copy filing flags from the company profile. Field names are from the public companyProfile document."""
    accounts = payload.get("accounts") if isinstance(payload.get("accounts"), dict) else {}
    nxt = accounts.get("next_accounts") if isinstance(accounts.get("next_accounts"), dict) else {}
    overdue = _as_bool(nxt.get("overdue"))
    overdue_reason = ""
    if overdue is None:
        overdue = _as_bool(accounts.get("overdue"))
        if overdue is None:
            overdue_reason = "The profile did not include accounts.next_accounts.overdue."
    statement = payload.get("confirmation_statement") if isinstance(payload.get("confirmation_statement"), dict) else {}
    confirm = _as_bool(statement.get("overdue"))
    confirm_reason = "" if confirm is not None else "The profile did not include confirmation_statement.overdue."
    links = payload.get("links") if isinstance(payload.get("links"), dict) else {}
    raw_charges = links.get("charges")
    charges_url = ""
    if isinstance(raw_charges, str) and raw_charges.strip():
        path = urllib.parse.urlsplit(raw_charges.strip()).path or raw_charges.strip()
        if re.fullmatch(rf"/company/{re.escape(number)}/charges", path):
            charges_url = f"{FIND}/company/{number}/charges"
    raw_flag = _as_bool(payload.get("has_charges"))
    has_charges: bool | str | None = raw_flag
    deprecated = "The company profile document marks has_charges as deprecated and says to use links.charges."
    if charges_url and raw_flag is False:
        has_charges = True
        has_reason = deprecated + " This profile set has_charges to false and included links.charges. The sheet records the charges link and does not request the charges list."
    elif charges_url and raw_flag is True:
        has_charges = True
        has_reason = deprecated + " This profile set has_charges to true and included links.charges. The sheet records the charges link and does not request the charges list."
    elif charges_url:
        has_charges = True
        has_reason = deprecated + " This profile omitted has_charges and included links.charges. The sheet records the charges link and does not request the charges list."
    elif raw_flag is True:
        has_charges = ""
        has_reason = deprecated + " This profile set has_charges to true and omitted links.charges, so has_charges stays blank."
    elif raw_flag is False:
        has_reason = "The profile set has_charges to false and omitted links.charges."
    else:
        has_charges = ""
        has_reason = "The profile did not include links.charges or has_charges."
    return {
        "accounts_overdue": overdue if overdue is not None else "",
        "accounts_overdue_reason": overdue_reason,
        "confirmation_statement_overdue": confirm if confirm is not None else "",
        "confirmation_statement_overdue_reason": confirm_reason,
        "has_charges": has_charges if has_charges is not None else "",
        "has_charges_reason": has_reason,
        "charges_url": charges_url,
        "charges_url_reason": "" if charges_url else "The profile did not include links.charges.",
    }


_BIRTH_TEXT = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})(?:-(?P<day>\d{2})"
    r"(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?)?$"
)
_DAY_TEXT = re.compile(
    r"^(?P<day>\d{4}-\d{2}-\d{2})(?:[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


def _birth_parts(value: object) -> tuple[object, object]:
    if isinstance(value, dict):
        month = value.get("month")
        year = value.get("year")
        return (
            month if isinstance(month, int) and not isinstance(month, bool) else "",
            year if isinstance(year, int) and not isinstance(year, bool) else "",
        )
    if isinstance(value, str):
        found = _BIRTH_TEXT.fullmatch(value.strip())
        if found:
            month = int(found.group("month"))
            year = int(found.group("year"))
            if not 1 <= month <= 12:
                return "", ""
            day = found.group("day")
            if day is not None:
                try:
                    date(year, month, int(day))
                except ValueError:
                    return "", ""
            return month, year
    return "", ""


def _disqualification_orders(detail: dict) -> list[dict]:
    raw = detail.get("disqualifications")
    orders = []
    if not isinstance(raw, list):
        return orders
    for item in raw:
        if not isinstance(item, dict):
            continue
        names = []
        raw_names = item.get("company_names")
        if isinstance(raw_names, str):
            raw_names = [raw_names]
        if isinstance(raw_names, list):
            for name in raw_names:
                if isinstance(name, str) and name.strip() and name.strip() not in names:
                    names.append(name.strip())
        orders.append({
            "company_names": names,
            "disqualified_from": _text(item.get("disqualified_from")),
            "disqualified_until": _text(item.get("disqualified_until")),
        })
    return orders


def _person_fields(search_item: dict, detail: dict | None, company_reason: str) -> dict:
    month, year = _birth_parts(search_item.get("date_of_birth"))
    orders: list[dict] = []
    if detail is not None:
        detail_month, detail_year = _birth_parts(detail.get("date_of_birth"))
        if detail_month != "" or detail_year != "":
            month, year = detail_month, detail_year
        orders = _disqualification_orders(detail)
    names: list[str] = []
    for order in orders:
        for name in order["company_names"]:
            if name not in names:
                names.append(name)
    return {
        "birth_month": month,
        "birth_year": year,
        "birth_reason": "" if month != "" and year != "" else "The disqualified record did not include a month and year of birth.",
        "company_names": names,
        "company_names_reason": "" if names else company_reason,
        "disqualifications": orders,
        "permissions_to_act": _permission_rows(detail),
    }


def _officer_id(item: dict) -> str:
    links = item.get("links")
    if not isinstance(links, dict):
        return ""
    self_link = links.get("self")
    if not isinstance(self_link, str):
        return ""
    path = urllib.parse.urlsplit(self_link).path or self_link
    match_id = _OFFICER_ID.fullmatch(path)
    return match_id.group(1) if match_id else ""


def _permission_rows(detail: dict | None) -> list[dict]:
    if not isinstance(detail, dict):
        return []
    raw = detail.get("permissions_to_act")
    if not isinstance(raw, list):
        return []
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append({
            "company_names": _string_list(item.get("company_names")),
            "granted_on": _text(item.get("granted_on")),
            "expires_on": _text(item.get("expires_on")),
        })
    return rows


def _string_list(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in names:
            names.append(item.strip())
    return names


def _calendar_date(value: object) -> date | None:
    text = _text(value)
    found = _DAY_TEXT.fullmatch(text)
    if not found:
        return None
    try:
        return date.fromisoformat(found.group("day"))
    except ValueError:
        return None


def _order_force(order: dict, today: date) -> str:
    """Map one order onto the public start and end dates.

    naturalDisqualification says disqualified_from is the date the
    disqualification starts and disqualified_until is the date it ends.
    The search document does not limit results to orders still in force.
    The end date itself is the day the order ends, so that day stays possible.
    """
    start = _calendar_date(order.get("disqualified_from"))
    end = _calendar_date(order.get("disqualified_until"))
    if start is None or end is None:
        return "undated"
    if today < start:
        return "not_started"
    if today >= end:
        return "ended"
    return "in_force"


def _names_this_company(names: list[str], company_name: str) -> bool:
    def fold(value: object) -> str:
        text = norm(value)
        if text.endswith(" ltd"):
            return text[:-4] + " limited"
        return text

    key = fold(company_name)
    if not key:
        return False
    return any(fold(item) == key for item in names)


def _birth_matches_officer(people: list[dict] | None, queried_name: str, month: object, year: object) -> bool:
    if month == "" or year == "":
        return False
    for person in people or []:
        if not isinstance(person, dict):
            continue
        if person_compare(person.get("name"), queried_name) != "exact":
            continue
        if person.get("birth_month") == month and person.get("birth_year") == year:
            return True
    return False


def _matching_officers(people: list[dict] | None, queried_name: str, month: object, year: object) -> list[dict]:
    found = []
    if month == "" or year == "":
        return found
    for person in people or []:
        if not isinstance(person, dict):
            continue
        if person_compare(person.get("name"), queried_name) != "exact":
            continue
        if person.get("birth_month") != month or person.get("birth_year") != year:
            continue
        found.append({
            "name": _text(person.get("name")),
            "birth_month": month,
            "birth_year": year,
            "appointed_on": _text(person.get("appointed_on")),
            "ended_on": _text(person.get("ended_on")),
            "officer_id": _text(person.get("officer_id")),
        })
    return found


def _permission_blocks(item: dict, company_name: str, today: date) -> bool:
    if not _names_this_company(_string_list(item.get("company_names")), company_name):
        return False
    granted = _calendar_date(item.get("granted_on"))
    if granted is not None and today < granted:
        return False
    expires = _calendar_date(item.get("expires_on"))
    if expires is None:
        return True
    return today <= expires


def _named_births(people: list[dict] | None, queried_name: str) -> list[tuple]:
    found = []
    for person in people or []:
        if not isinstance(person, dict):
            continue
        if person_compare(person.get("name"), queried_name) != "exact":
            continue
        month = person.get("birth_month")
        year = person.get("birth_year")
        if month in ("", None) or year in ("", None):
            continue
        pair = (month, year)
        if pair not in found:
            found.append(pair)
    return found


def _item_birth(item: dict) -> tuple:
    return _birth_parts(item.get("date_of_birth"))


def _birth_conflict(people: list[dict] | None, queried_name: str, item: dict) -> bool:
    month, year = _item_birth(item)
    if month == "" or year == "":
        return False
    births = _named_births(people, queried_name)
    if not births:
        return False
    return (month, year) not in births


def _birth_agrees(people: list[dict] | None, queried_name: str, item: dict) -> bool:
    month, year = _item_birth(item)
    if month == "" or year == "":
        return False
    return (month, year) in _named_births(people, queried_name)


def _gap_reason(people: list[dict] | None, queried_name: str, row: dict) -> str:
    month = row.get("birth_month")
    year = row.get("birth_year")
    if month == "" or year == "":
        return (
            "The disqualified record did not include a month and year of birth, "
            "so the row stays possible."
        )
    named = False
    births = []
    for person in people or []:
        if not isinstance(person, dict) or person_compare(person.get("name"), queried_name) != "exact":
            continue
        named = True
        bmonth = person.get("birth_month")
        byear = person.get("birth_year")
        if bmonth not in ("", None) and byear not in ("", None):
            births.append((bmonth, byear))
    if not named:
        return "No officer of this company has this name, so the row stays possible."
    if not births:
        return (
            "No officer of this company with this name included a month and year of birth, "
            "so this row cannot be compared and stays possible."
        )
    return (
        "The birth month and year on this row do not match an officer of this company "
        "with this name, so the row stays possible."
    )


def classify_ban(
    people: list[dict] | None,
    queried_name: str,
    company_name: str,
    row: dict,
    today: date,
) -> dict:
    """Return exact only for a ban that is still in force for one officer of this company.

    Input kinds are the rows of the ban table in tests/test_uk_companies.py.
    """
    matched = _matching_officers(people, queried_name, row.get("birth_month"), row.get("birth_year"))
    raw_orders = row.get("disqualifications")
    if not isinstance(raw_orders, list):
        raw_orders = []
    orders = []
    covering: set[str] = set()
    for item in raw_orders:
        if not isinstance(item, dict):
            continue
        names = _string_list(item.get("company_names"))
        force = _order_force(item, today)
        names_company = _names_this_company(names, company_name)
        orders.append({
            "company_names": names,
            "disqualified_from": _text(item.get("disqualified_from")),
            "disqualified_until": _text(item.get("disqualified_until")),
            "force": force,
            "names_this_company": names_company,
        })
        if names_company:
            covering.add(force)
    raw_permissions = row.get("permissions_to_act")
    if not isinstance(raw_permissions, list):
        raw_permissions = []
    permissions = []
    blocked = False
    for item in raw_permissions:
        if not isinstance(item, dict):
            continue
        blocks = _permission_blocks(item, company_name, today)
        permissions.append({
            "company_names": _string_list(item.get("company_names")),
            "granted_on": _text(item.get("granted_on")),
            "expires_on": _text(item.get("expires_on")),
            "blocks": blocks,
        })
        if blocks:
            blocked = True
    if not matched:
        reason = _gap_reason(people, queried_name, row)
        kind = "possible"
    elif not covering:
        if not any(order["company_names"] for order in orders):
            reason = "The order did not include a company name, so the row stays possible."
        else:
            reason = "No order names this company, so the row stays possible."
        kind = "possible"
    elif "in_force" not in covering:
        kind = "possible"
        if covering == {"ended"}:
            reason = (
                "The order that names this company has disqualified_until on or before today, "
                "so the row stays possible."
            )
        elif covering == {"not_started"}:
            reason = (
                "The order that names this company has disqualified_from after today, "
                "so the row stays possible."
            )
        elif covering == {"undated"}:
            reason = (
                "The order that names this company did not include both disqualified_from and "
                "disqualified_until as YYYY-MM-DD, so the row stays possible."
            )
        else:
            reason = "No order that names this company is in force on today's date, so the row stays possible."
    elif blocked:
        kind = "possible"
        reason = (
            "A permission to act names this company and still covers today, "
            "so the row stays possible."
        )
    elif len(matched) > 1 and (
        len({_text(item.get("officer_id")) for item in matched}) != 1
        or not _text(matched[0].get("officer_id"))
    ):
        kind = "possible"
        reason = (
            "More than one officer shares this name and birth month and year "
            "without one shared officer id, so the row stays possible."
        )
    else:
        kind = "exact"
        reason = ""
    return {
        "match": kind,
        "identity_reason": reason,
        "matched_officers": matched,
        "orders": orders,
        "permissions": permissions,
    }


def _page_kind(dicts: list, total: int | None, full: bool) -> str:
    """Name one disqualified-officer search page before a row is built.

    Input kinds are the rows of DISQUALIFIED_PAGE_TABLE.
    total_zero_with_rows: the total is 0 and the page still lists rows.
    complete_zero: the page is complete and empty.
    incomplete: the total is missing or disagrees with the page.
    complete_hits: the total equals the rows on the page and the page is not empty.
    """
    if total == 0 and dicts:
        return "total_zero_with_rows"
    if full and not dicts:
        return "complete_zero"
    if not full:
        return "incomplete"
    return "complete_hits"


def _status_row(queried_name: str, title: str, note: str, official_url: str = "") -> dict:
    """A search page that did not confirm a person.

    A complete zero, a total of 0 beside listed rows, and an incomplete page
    share this shape. match stays unchecked. A zero count is not a clearance.
    url_reason is set only when official_url is empty.
    """
    code, label = verdict("unchecked")
    return {
        "queried_name": queried_name,
        "title": title,
        "match": code,
        "match_ja": label,
        "official_url": official_url,
        "url_reason": "" if official_url else note,
        "reason": note,
    }


def fetch_disqualified(
    names: list[str],
    deadline: Deadline | None,
    names_complete: bool,
    people: list[dict] | None = None,
    company_name: str = "",
) -> dict:
    checked = [plain_text(name, 160) for name in names]
    usable = []
    for name in checked:
        if name and name not in usable:
            usable.append(name)
    if not usable:
        if names_complete:
            return block(
                "read",
                "There was no officer name to check. This is not a finding that anyone is clear.",
                [],
                fully_read=True,
            )
        return block(
            "not_read",
            "Officer names were not fully read, so disqualification was not checked.",
            [],
            fully_read=False,
        )
    if len(usable) > MAX_DISQUALIFY:
        usable = usable[:MAX_DISQUALIFY]
        capped = True
    else:
        capped = False
    rows = []
    profiles = 0
    page_gap = False
    profile_gap = False
    birth_unread = False
    zero_clear = False
    dropped = 0
    for name in usable:
        ensure_time(deadline)
        query_name = person_query(name) or name
        payload = ch_get(
            "/search/disqualified-officers",
            {"q": query_name, "items_per_page": SEARCH_PAGE, "start_index": 0},
            deadline,
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise ApiError("bad_shape", "Disqualified-officer search omitted the items list.", client.NEXT["bad_shape"])
        dicts = [item for item in items if isinstance(item, dict)]
        total = _total(payload)
        full, note = _note(
            dicts,
            total,
            "Disqualified-officer search for this exact name returned 0. That is not a finding that the person is clear.",
        )
        if not full:
            page_gap = True
        page = _page_kind(dicts, total, full)
        if page == "total_zero_with_rows":
            for item in dicts:
                rows.append(_status_row(name, _text(item.get("title")), note))
            continue
        if page == "complete_zero":
            zero_clear = True
            rows.append(_status_row(name, "", note, f"{FIND}/search/disqualified-officers"))
            continue
        exact = []
        possible = []
        for item in dicts:
            kind = person_compare(item.get("title"), name)
            code, label = verdict(kind)
            row = {
                "queried_name": name,
                "title": _text(item.get("title")),
                "match": code,
                "match_ja": label,
                "official_url": "",
                "url_reason": "",
            }
            if kind == "exact":
                if _birth_conflict(people, name, item):
                    dropped += 1
                    continue
                officer = _officer_id(item)
                month, year = _birth_parts(item.get("date_of_birth"))
                birth_ok = _birth_matches_officer(people, name, month, year)
                detail = None
                if officer and birth_ok and profiles < MAX_DISQUALIFY_PROFILES:
                    ensure_time(deadline)
                    detail = ch_get(f"/disqualified-officers/natural/{officer}", None, deadline)
                    profiles += 1
                    row["official_url"] = f"{FIND}/disqualified-officers/natural/{officer}"
                    row["description"] = _text(detail.get("description")) or _text(item.get("description"))
                    company_reason = "The disqualified profile did not include company names."
                elif officer and birth_ok:
                    profile_gap = True
                    row["official_url"] = f"{FIND}/disqualified-officers/natural/{officer}"
                    row["url_reason"] = "The profile was not requested because this call already read 3 disqualified profiles."
                    row["description"] = _text(item.get("description"))
                    company_reason = "The profile was not requested because this call already read 3 disqualified profiles."
                elif officer and (month == "" or year == ""):
                    birth_unread = True
                    row["official_url"] = f"{FIND}/disqualified-officers/natural/{officer}"
                    row["url_reason"] = "The search row did not include a month and year of birth, so no disqualified profile was requested."
                    row["description"] = _text(item.get("description"))
                    company_reason = row["url_reason"]
                elif officer and not _named_births(people, name):
                    row["official_url"] = f"{FIND}/disqualified-officers/natural/{officer}"
                    row["url_reason"] = (
                        "No officer with this name included a month and year of birth, "
                        "so no disqualified profile was requested."
                    )
                    row["description"] = _text(item.get("description"))
                    company_reason = row["url_reason"]
                elif officer:
                    row["official_url"] = f"{FIND}/disqualified-officers/natural/{officer}"
                    row["url_reason"] = "The search-row birth month and year do not match this officer, so no disqualified profile was requested."
                    row["description"] = _text(item.get("description"))
                    company_reason = row["url_reason"]
                else:
                    row["url_reason"] = "The search row did not include a natural-officer id on the official host."
                    row["description"] = _text(item.get("description"))
                    company_reason = "The search row did not include a natural-officer id on the official host."
                row.update(_person_fields(item, detail, company_reason))
                decision = classify_ban(people, name, company_name, row, date.today())
                row["disqualifications"] = decision["orders"]
                row["permissions_to_act"] = decision["permissions"]
                row["matched_officers"] = decision["matched_officers"]
                if decision["match"] == "exact":
                    exact.append(row)
                else:
                    code, label = verdict("possible")
                    row["match"] = code
                    row["match_ja"] = label
                    if detail is None and birth_ok:
                        row["identity_reason"] = row["url_reason"] or decision["identity_reason"]
                    else:
                        row["identity_reason"] = decision["identity_reason"]
                    possible.append(row)
            elif kind == "possible":
                if _birth_conflict(people, name, item):
                    dropped += 1
                    continue
                close = shares_surname_and_first(name, item.get("title"))
                by_birth = shares_surname(name, item.get("title")) and _birth_agrees(people, name, item)
                if not close and not by_birth:
                    dropped += 1
                    continue
                if by_birth and not close:
                    held = (
                        "The surname and the birth month and year match this officer. "
                        "The title is not an exact match, so no disqualified profile was requested."
                    )
                else:
                    held = "The title is not an exact match, so no disqualified profile was requested."
                row["url_reason"] = held
                row["description"] = _text(item.get("description"))
                row.update(_person_fields(item, None, held))
                possible.append(row)
        if page == "incomplete":
            rows.append(_status_row(name, "", note))
        rows.extend(exact)
        rows.extend(possible)
    reason = ""
    if capped or not names_complete or page_gap:
        reason = "Some officer names were not fully checked against the disqualified-officer search."
    elif profile_gap:
        reason = "Some disqualified profiles whose birth month and year match an officer were not read, because this call stops at 3 profiles."
    elif birth_unread:
        reason = "An exact title had no birth month and year on the search row, so that profile was not read."
    elif zero_clear:
        reason = "Disqualified-officer search returned 0 for a checked name. That is not a finding that the person is clear."
    if dropped:
        note = (
            f"Dropped {dropped} search rows. "
            "A row is dropped when the surname and first name do not both match the officer, "
            "or when the birth month and year are present on both sides and differ. "
            "That drop is not a finding that the officer is clear."
        )
        reason = f"{reason} {note}".strip()
    return block(
        "read",
        reason,
        rows,
        fully_read=not capped and names_complete and not page_gap and not profile_gap and not birth_unread,
    )


def _entries(payload: dict) -> tuple[list[dict], int | None]:
    raw = payload.get("entry")
    if raw is None:
        rows: list[dict] = []
    elif isinstance(raw, dict):
        rows = [raw]
    elif isinstance(raw, list):
        rows = [item for item in raw if isinstance(item, dict)]
    else:
        raise ApiError("bad_shape", "The Gazette feed entry was not a list.", client.NEXT["bad_shape"])
    total_raw = payload.get("f:total")
    total: int | None
    if total_raw is None:
        total = None
    else:
        try:
            total = int(str(total_raw))
        except ValueError:
            total = None
    return rows, total


def _notice_url(entry: dict) -> str:
    links = entry.get("link")
    if isinstance(links, dict):
        links = [links]
    if not isinstance(links, list):
        return ""
    for item in links:
        if not isinstance(item, dict):
            continue
        href = item.get("@href") or item.get("href") or ""
        if isinstance(href, str) and _NOTICE.fullmatch(href):
            return href
    return ""


def _gazette_category(entry: dict) -> str:
    raw = entry.get("category")
    if isinstance(raw, list):
        raw = raw[0] if raw else None
    if isinstance(raw, dict):
        return _text(raw.get("@term") or raw.get("term"))
    return _text(raw)


def _excerpt(value: object) -> str:
    if not isinstance(value, str):
        return ""
    plain = re.sub(r"<[^>]*>", " ", value)
    plain = re.sub(r"\s+", " ", plain).strip()
    if len(plain) > 240:
        return plain[:240].rstrip() + "..."
    return plain


def _published_before(published: object, incorporated: object) -> bool:
    notice = _calendar_date(published)
    created = _calendar_date(incorporated)
    if notice is None or created is None:
        return False
    return notice < created


# One row of test_gazette_text_table is one input kind.
# The Gazette search guide uses a double quote as the exact-phrase mark.
# A name that already contains one is not sent. On 2026-11-05 a phrase with
# one unescaped quote came back HTTP 200 with f:total 0, and a phrase that
# split the quote came back with a large total.
# A backslash is the escape mark. On 2026-11-05 the phrase for PAGE \ PARK
# LIMITED came back HTTP 200 with a body that is not JSON. The same name
# with each backslash written twice came back JSON, f:total 0, and
# searchTerms held that registered name. gazette_wire does that doubling.
# Title comparison uses the registered name.
GAZETTE_SKIP = {
    "empty": (
        "The Gazette query is empty.",
        "The registered name was empty, so it was not sent to the insolvency feed.",
    ),
    "too_long": (
        "The Gazette query is too long.",
        "The registered name was too long, so it was not sent to the insolvency feed.",
    ),
    "double_quote": (
        "The Gazette query contains a double quote.",
        "The registered name contains a double quote, so it was not sent to the insolvency feed.",
    ),
}


def gazette_text(name: object) -> tuple[str, str]:
    """Return ("send", registered name) or (skip kind, "").

    Input kinds are the rows of test_gazette_text_table.
    The registered name is what notice titles are compared with.
    gazette_wire builds the phrase body. The caller adds the phrase marks.
    """
    if not isinstance(name, str) or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        return "empty", ""
    text = typed_text(name, 160)
    if text is None:
        if len(name.strip()) > 160:
            return "too_long", ""
        return "empty", ""
    if '"' in text:
        return "double_quote", ""
    return "send", text


def gazette_wire(text: str) -> str:
    """Double each backslash so the Gazette phrase comes back as JSON."""
    return text.replace("\\", "\\\\")


def fetch_gazette(name: str, deadline: Deadline | None, incorporated: str = "") -> dict:
    kind, text = gazette_text(name)
    if kind != "send":
        message, step = GAZETTE_SKIP[kind]
        raise ApiError("bad_input", message, step)
    url = f"https://{GAZETTE_HOST}/insolvency/notice/data.json?" + urllib.parse.urlencode(
        {"text": f'"{gazette_wire(text)}"'}
    )
    _status, payload = request_json(url, json_headers(), deadline)
    if not isinstance(payload, dict):
        raise ApiError("bad_shape", "The Gazette feed was not a JSON object.", client.NEXT["bad_shape"])
    entries, total = _entries(payload)
    rows = []
    for entry in entries:
        kind = compare(entry.get("title"), text)
        published = _text(entry.get("published"))
        match_reason = ""
        if kind == "exact" and _published_before(published, incorporated):
            kind = "possible"
            match_reason = "The notice date is before this company's incorporation date, so the row stays possible."
        code, label = verdict(kind)
        url_value = _notice_url(entry)
        category = _gazette_category(entry)
        excerpt = _excerpt(entry.get("content"))
        rows.append({
            "title": _text(entry.get("title")),
            "published": published,
            "notice_code": _text(entry.get("f:notice-code")),
            "category": category,
            "category_reason": "" if category else "The feed entry did not include a category term.",
            "excerpt": excerpt,
            "excerpt_reason": "" if excerpt else "The feed entry did not include content.",
            "match": code,
            "match_ja": label,
            "match_reason": match_reason,
            "official_url": url_value,
            "url_reason": "" if url_value else "The feed entry did not include an https notice URL on www.thegazette.co.uk.",
            "date_reason": "" if published else "The feed entry did not include published.",
        })
    full, note = _note(
        rows,
        total,
        "The insolvency feed returned 0 notices for this text. That is not a finding of no insolvency notice.",
    )
    if total is not None and total > len(rows):
        full = False
        note = "The feed total is higher than the page that was read, so later notices were not read."
    if total == 0 and rows:
        unchecked, blank = verdict("unchecked")
        for row in rows:
            row["match"] = unchecked
            row["match_ja"] = blank
            row["match_reason"] = ""
    return {
        "query": text,
        "rows": rows,
        "rows_read": len(rows),
        "total_reported": total,
        "fully_read": full,
        "note": note,
        "official_url": "https://www.thegazette.co.uk/insolvency/notice",
    }


def _charity_items(payload: object) -> list[dict]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in ("items", "charities"):
            if isinstance(payload.get(key), list):
                return [item for item in payload[key] if isinstance(item, dict)]
        if any(key in payload for key in ("charity_name", "charityName")):
            return [payload]
    raise ApiError("bad_shape", "The charity response was not a list of records.", client.NEXT["bad_shape"])


def _pick(row: dict, *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return ""


def _json_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _present_int(item: dict, key: str, camel: str) -> tuple[object, str]:
    for name in (key, camel):
        if name not in item:
            continue
        value = item.get(name)
        if value is None:
            return "", f"The charity details returned {key} as null."
        parsed = _json_int(value)
        if parsed is None:
            return "", f"The charity details {key} was not an integer."
        return parsed, ""
    return "", f"The charity details did not include {key}."


def _present_bool(item: dict, key: str, camel: str) -> tuple[object, str]:
    for name in (key, camel):
        if name not in item:
            continue
        value = item.get(name)
        if isinstance(value, bool):
            return value, ""
        if value is None:
            return "", f"The charity details returned {key} as null."
        return "", f"The charity details {key} was not true or false."
    return "", f"The charity details did not include {key}."


def _trustee_names(item: dict) -> tuple[list[str], str]:
    key = ""
    for name in ("trustee_names", "trusteeNames"):
        if name in item:
            key = name
            break
    if not key:
        return [], "The charity details did not include trustee names."
    raw = item.get(key)
    if raw is None:
        return [], "The charity details returned trustee names as null."
    names: list[str] = []
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str) and entry.strip():
                text = entry.strip()
            elif isinstance(entry, dict):
                text = _pick(entry, "trustee_name", "trusteeName", "name")
            else:
                text = ""
            if text and text not in names:
                names.append(text)
    if not names:
        return [], "The charity details included trustee names and none could be copied."
    return names, ""


def _charity_record(item: dict, number: str, suffix: str) -> dict:
    title = _pick(item, "charity_name", "charityName")
    income, income_reason = _present_int(item, "latest_income", "latestIncome")
    spend, spend_reason = _present_int(item, "latest_expenditure", "latestExpenditure")
    insolvent, insolvent_reason = _present_bool(item, "insolvent", "insolvent")
    admin, admin_reason = _present_bool(item, "in_administration", "inAdministration")
    charity_type = _pick(item, "charity_type", "charityType")
    trustees, trustee_reason = _trustee_names(item)
    return {
        "charity_name": title,
        "charity_number": number,
        "suffix": suffix,
        "reg_status": _pick(item, "reg_status", "regStatus", "charity_status"),
        "charity_type": charity_type,
        "latest_income": income,
        "latest_expenditure": spend,
        "insolvent": insolvent,
        "in_administration": admin,
        "trustee_names": trustees,
        "official_url": _charity_public(number, suffix),
        "income_reason": income_reason,
        "expenditure_reason": spend_reason,
        "insolvent_reason": insolvent_reason,
        "administration_reason": admin_reason,
        "trustee_reason": trustee_reason,
        "type_reason": "" if charity_type else "The charity details did not include charity_type.",
    }


def _charity_public(number: str, suffix: str) -> str:
    if charity_number(number) and charity_suffix(suffix):
        return (
            "https://register-of-charities.charitycommission.gov.uk/charity-search/-/charity-details/"
            f"{number}/{suffix}"
        )
    return ""


def _charity_key(deadline: Deadline | None) -> str:
    ensure_time(deadline)
    key = read_secret(CHARITY_KEY)
    if not key:
        raise ApiError(
            "no_key",
            "Set CHARITY_COMMISSION_API_KEY in the active profile .env. " + no_key_ja,
            client.NEXT["no_key"],
        )
    return key


def _charity_zero(text: str) -> dict:
    return {
        "query": text,
        "rows": [],
        "rows_read": 0,
        "fully_read": True,
        "note": "The charity name search returned 0 rows. A zero count leaves the question open.",
    }


def fetch_charity_name(name: str, deadline: Deadline | None) -> dict:
    text = typed_text(name, 160)
    if text is None:
        raise ApiError("bad_input", "The charity name is empty or too long.", client.NEXT["bad_input"])
    key = _charity_key(deadline)
    segment = urllib.parse.quote(text, safe="")
    url = f"https://{CHARITY_HOST}/register/api/searchCharityName/{segment}"
    try:
        _status, payload = request_json(url, charity_headers(key), deadline)
    except ApiError as exc:
        if exc.status == 404:
            return _charity_zero(text)
        raise
    documented_array = isinstance(payload, list)
    dropped = 0
    if documented_array:
        dropped = sum(1 for item in payload if not isinstance(item, dict))
        items = [item for item in payload if isinstance(item, dict)]
    else:
        items = _charity_items(payload)
    rows = []
    exact_rows = []
    for item in items:
        title = _pick(item, "charity_name", "charityName")
        number = charity_number(_pick(item, "reg_charity_number", "regCharityNumber", "registered_charity_number"))
        suffix = charity_suffix(_pick(item, "group_subsid_suffix", "groupSubsidSuffix") or "0") or "0"
        kind = compare(title, text)
        code, label = verdict(kind)
        row = {
            "charity_name": title,
            "charity_number": number or "",
            "suffix": suffix,
            "reg_status": _pick(item, "reg_status", "regStatus"),
            "match": code,
            "match_ja": label,
            "official_url": _charity_public(number, suffix) if number else "",
            "url_reason": "" if number else "The charity row did not include a registration number.",
        }
        rows.append(row)
        if kind == "exact" and number:
            exact_rows.append(row)
    if len(exact_rows) == 1:
        detail = fetch_charity_number(exact_rows[0]["charity_number"], exact_rows[0]["suffix"], deadline)
        exact_rows[0]["detail_status"] = detail.get("reg_status", "")
        exact_rows[0]["detail_reason"] = detail.get("note", "")
        for key in (
            "charity_type",
            "latest_income",
            "latest_expenditure",
            "insolvent",
            "in_administration",
            "trustee_names",
            "income_reason",
            "expenditure_reason",
            "insolvent_reason",
            "administration_reason",
            "trustee_reason",
            "type_reason",
        ):
            if key in detail:
                exact_rows[0][key] = detail[key]
    elif len(exact_rows) > 1:
        held = "More than one charity row matches this name, so charity details were not requested."
        for row in exact_rows:
            row["detail_reason"] = held
    if not documented_array:
        full = False
        note = (
            "The published charity name search returns one array and has no page parameter. "
            "This body was an object, so the rows are not the full result."
        )
    elif dropped:
        full = False
        note = "Some charity rows were not objects, so the response was not fully used."
    elif not rows:
        full = True
        note = "The charity name search returned 0 rows. A zero count leaves the question open."
    else:
        full = True
        note = ""
    return {"query": text, "rows": rows, "rows_read": len(rows), "fully_read": full, "note": note}


def fetch_charity_number(number: str, suffix: str | None, deadline: Deadline | None) -> dict:
    checked = loose_token(number, 24, charity_number)
    checked_suffix = loose_token(suffix, 8, charity_suffix)
    if not checked or checked_suffix is None:
        raise ApiError("bad_input", "The charity number or suffix is not in the expected form.", client.NEXT["bad_input"])
    key = _charity_key(deadline)
    url = f"https://{CHARITY_HOST}/register/api/allcharitydetailsV2/{checked}/{checked_suffix}"
    try:
        _status, payload = request_json(url, charity_headers(key), deadline)
    except ApiError as exc:
        if exc.status == 404:
            raise ApiError("upstream", "Charity details returned 404 for this number.", client.NEXT["not_found"], 404) from None
        raise
    items = _charity_items(payload)
    if not items:
        raise ApiError("bad_shape", "Charity details did not include a record.", client.NEXT["bad_shape"])
    record = _charity_record(items[0], checked, checked_suffix)
    record["match"] = "exact"
    record["match_ja"] = verdict("exact")[1]
    return {
        **record,
        "rows": [dict(record)],
        "rows_read": 1,
        "fully_read": True,
        "note": "" if record["charity_name"] else "The charity details response did not include a name.",
    }


def _land_query_url(sparql: str) -> str:
    return urllib.parse.urlunsplit((
        "https",
        LAND_HOST,
        "/landregistry/query",
        urllib.parse.urlencode({"query": sparql, "output": "json"}),
        "",
    ))


def _tx_url(value: str) -> str:
    """SPARQL DESCRIBE for one transaction.

    The linked-data page `/data/ppi/transaction/{uuid}/current` returns 403 from
    nginx. DESCRIBE of that same URI on `/landregistry/query` returns 200.
    """
    found = _TX.fullmatch(value or "")
    if not found:
        return ""
    uri = f"http://{_LR_HOST}/data/ppi/transaction/{found.group(1)}/current"
    query = f"DESCRIBE <{uri}>"
    return _land_query_url(query)


def _tail(value: str) -> str:
    if not value:
        return ""
    return value.rstrip("/").rsplit("/", 1)[-1]


LAND_ATTRIBUTION = (
    "Contains HM Land Registry data © Crown copyright and database right 2021. "
    "This data is licensed under the Open Government Licence v3.0."
)
LAND_ADDRESS_LIMIT = (
    "Address fields in a Price Paid row (postcode, paon, saon, street, town) are for personal or non-commercial use, "
    "or for displaying residential property price information."
)
LAND_OWNERSHIP = (
    "Price Paid is the price recorded for a sale at the postcode. "
    "It does not show that a company owns the property."
)


def _land_notice(sentence: str) -> str:
    parts = [sentence, LAND_OWNERSHIP, LAND_ATTRIBUTION, LAND_ADDRESS_LIMIT]
    return " ".join(part for part in parts if part)


def fetch_land(code: str, deadline: Deadline | None) -> dict:
    checked = loose_token(code, 40, postcode)
    if not checked or '"' in checked or "\\" in checked:
        raise ApiError("bad_input", "The postcode is not a UK postcode with the outward and inward parts.", client.NEXT["bad_input"])
    query = f"""
PREFIX lrppi: <http://landregistry.data.gov.uk/def/ppi/>
PREFIX lrcommon: <http://landregistry.data.gov.uk/def/common/>
SELECT ?item ?price ?date ?paon ?saon ?street ?town ?postcode ?propertyType ?estateType
WHERE {{
  ?item lrppi:propertyAddress ?addr .
  ?item lrppi:pricePaid ?price .
  ?item lrppi:transactionDate ?date .
  ?addr lrcommon:postcode "{checked}" .
  OPTIONAL {{ ?addr lrcommon:paon ?paon }}
  OPTIONAL {{ ?addr lrcommon:saon ?saon }}
  OPTIONAL {{ ?addr lrcommon:street ?street }}
  OPTIONAL {{ ?addr lrcommon:town ?town }}
  OPTIONAL {{ ?item lrppi:propertyType ?propertyType }}
  OPTIONAL {{ ?item lrppi:estateType ?estateType }}
}}
ORDER BY DESC(?date)
LIMIT {LAND_LIMIT}
""".strip()
    url = _land_query_url(query)
    _status, payload = request_json(url, land_headers(), deadline)
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), dict):
        raise ApiError("bad_shape", "Price Paid SPARQL did not return a results object.", client.NEXT["bad_shape"])
    bindings = payload["results"].get("bindings")
    if not isinstance(bindings, list):
        raise ApiError("bad_shape", "Price Paid SPARQL did not return bindings.", client.NEXT["bad_shape"])
    rows = []
    for item in bindings:
        if not isinstance(item, dict):
            continue
        def cell(name: str, item: dict = item) -> str:
            raw = item.get(name)
            if isinstance(raw, dict) and isinstance(raw.get("value"), str):
                return raw["value"]
            return ""
        uri = cell("item")
        official = _tx_url(uri)
        note = "Price Paid is the price recorded for that sale at this postcode. It does not show that a company owns the property."
        if official:
            note += " The linked-data transaction page returns 403, so official_url is the SPARQL DESCRIBE for that transaction."
        note += " " + LAND_ATTRIBUTION + " " + LAND_ADDRESS_LIMIT
        rows.append({
            "price_paid": cell("price"),
            "transaction_date": cell("date"),
            "paon": cell("paon"),
            "saon": cell("saon"),
            "street": cell("street"),
            "town": cell("town"),
            "postcode": cell("postcode") or checked,
            "property_type": _tail(cell("propertyType")),
            "estate_type": _tail(cell("estateType")),
            "official_url": official,
            "url_reason": "" if official else "The transaction URI was not a Price Paid http URI, so no SPARQL DESCRIBE was built.",
            "note": note,
        })
    capped = len(rows) >= LAND_LIMIT
    if capped:
        sentence = "The query kept the 10 newest sales. Older sales were not read."
    elif not rows:
        sentence = "The Price Paid query returned 0 rows for this postcode. A zero count leaves the question open."
    else:
        sentence = ""
    note = _land_notice(sentence)
    return {
        "postcode": checked,
        "rows": rows,
        "rows_read": len(rows),
        "fully_read": not capped,
        "note": note,
        "official_url": "https://landregistry.data.gov.uk/app/doc/ppd",
    }


def vat_refusal() -> dict:
    return {
        "ok": True,
        "status": "excluded",
        "checked": False,
        "requests": 0,
        "message": (
            "HMRC Check a UK VAT number API 2.0 requires an application that takes about two weeks "
            "and OAuth 2.0 client credentials. This plugin does not call HMRC and does not send a VAT number."
        ),
        "next_step": "Use HMRC's own service if you need a VAT check. This plugin will not apply for HMRC access.",
        "official_url": "https://developer.service.hmrc.gov.uk/api-documentation/docs/api/service/vat-registered-companies-api/2.0",
    }


def _watch_text(row: dict, kind: str) -> str:
    name = _text(row.get("name"))
    if kind == "officer":
        role = _text(row.get("role")) or "role-missing"
        appointed = _text(row.get("appointed_on")) or "appointed-missing"
        parts = [name, role, appointed]
        if _text(row.get("appointment_id")):
            parts.append("appointment=" + _text(row.get("appointment_id")))
        if _text(row.get("officer_id")):
            parts.append("officer=" + _text(row.get("officer_id")))
        return " | ".join(parts)
    role = _text(row.get("role")) or "unknown-kind"
    natures = sorted(_text(item) for item in row.get("natures_of_control") or [] if _text(item))
    nature = ", ".join(natures) if natures else "no-nature-listed"
    parts = [name, role, nature]
    if _text(row.get("psc_id")):
        parts.append("psc=" + _text(row.get("psc_id")))
    return " | ".join(parts)


def active_watch_lines(rows: list, kind: str) -> list[str] | None:
    """One line per active appointment or PSC record.

    Input kinds are the rows of test_watch_identity_table.
    A name alone is not an identity. links.self is one appointment or one PSC
    notification. Rows that share no id stay as a count, so two active people
    with the same name remain two. An active row with no name returns None.
    """
    id_key = "appointment_id" if kind == "officer" else "psc_id"
    identified: dict[str, str] = {}
    anonymous: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or _text(row.get("ended_on")):
            continue
        if not _text(row.get("name")):
            return None
        line = _watch_text(row, kind)
        ident = _text(row.get(id_key))
        if ident:
            identified.setdefault(ident, line)
        else:
            anonymous.append(line)
    counts: dict[str, int] = {}
    for line in anonymous:
        counts[line] = counts.get(line, 0) + 1
    lines = [f"1 | {line}" for line in identified.values()]
    for line, count in counts.items():
        lines.append(f"{count} | {line}")
    return sorted(lines)


def read_for_watch(number: str, deadline: Deadline | None) -> dict:
    checked = company_number(number)
    if not checked:
        raise ApiError("bad_input", "The company number is not in the expected form.", client.NEXT["bad_input"])
    profile = fetch_profile(checked, deadline)
    if not profile.get("company_status"):
        return {
            "ok": False,
            "error_type": "partial",
            "message": "The profile did not include company_status, so the snapshot was kept.",
        }
    officers = fetch_officers(checked, deadline)
    people = fetch_psc(checked, deadline)
    if not officers["fully_read"] or not people["fully_read"]:
        return {
            "ok": False,
            "error_type": "partial",
            "message": "Officers or persons with significant control were not fully read, so the snapshot was kept.",
        }
    officer_lines = active_watch_lines(officers["rows"], "officer")
    psc_lines = active_watch_lines(people["rows"], "psc")
    if officer_lines is None or psc_lines is None:
        return {
            "ok": False,
            "error_type": "partial",
            "message": "An active officer or PSC row had no name, so the snapshot was kept.",
        }
    return {
        "ok": True,
        "company_number": checked,
        "status": profile["company_status"],
        "officers": officer_lines,
        "psc": psc_lines,
    }
