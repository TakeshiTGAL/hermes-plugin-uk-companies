"""One company sheet, split by official source."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

if __package__:
    from . import client
    from .client import CHARITY_KEY, Deadline, host_counts, read_secret
    from .match import (
        charity_number,
        charity_suffix,
        company_number,
        compare,
        no_key_ja,
        number_argument,
        refusal_message,
        search_name,
        classify_query,
        verdict,
    )
    from . import sources
else:
    import client
    import sources
    from client import CHARITY_KEY, Deadline, host_counts, read_secret
    from match import (
        charity_number,
        charity_suffix,
        company_number,
        compare,
        no_key_ja,
        number_argument,
        refusal_message,
        search_name,
        classify_query,
        verdict,
    )


def _counts() -> dict:
    return host_counts()


def _not_read(reason: str) -> dict:
    return sources.block("not_read", reason, [])


def _error_block(exc: client.ApiError) -> dict:
    return sources.block("error", exc.message, [], error_type=exc.error_type, next_step=exc.next_step)


def _no_key_ch() -> dict:
    return _not_read("Set COMPANIES_HOUSE_API_KEY in the active profile .env. " + no_key_ja)


def _no_key_charity() -> dict:
    return _not_read("Set CHARITY_COMMISSION_API_KEY in the active profile .env. " + no_key_ja)


def _vat() -> dict:
    return _vat_excluded()


def _stopped(reason: str) -> dict:
    return {
        "companies_house": _not_read(reason),
        "disqualified": _not_read(reason),
        "gazette": _not_read(reason),
        "charity": _not_read(reason),
        "vat": _vat(),
        "land": _not_read(reason),
    }


def _disq_prefix(sources_map: dict) -> tuple[int, str]:
    block = sources_map.get("disqualified") if isinstance(sources_map, dict) else None
    rows = block.get("rows") if isinstance(block, dict) else None
    exact = 0
    if isinstance(rows, list):
        exact = sum(1 for row in rows if isinstance(row, dict) and row.get("match") == "exact")
    noun = "row is" if exact == 1 else "rows are"
    summary = (
        f"{exact} exact disqualification {noun} in sources.disqualified. "
        "This count is not a clearance. Read that block before deciding."
    )
    return exact, summary


def _base(query: str, resolved: bool, company: dict | None, candidates: list, sources_map: dict, ok: bool, message: str, next_step: str = "") -> dict:
    before = {}
    exact, summary = _disq_prefix(sources_map)
    return {
        "disqualified_exact_count": exact,
        "disqualified_summary": summary,
        "ok": ok,
        "query": query,
        "resolved": resolved,
        "message": message,
        "next_step": next_step,
        "candidates": candidates,
        "company": company,
        "sources": sources_map,
        "requests": _counts(),
        "limits": {
            "per_request_seconds": client.REQUEST_TIMEOUT_SECONDS,
            "uk_check_seconds": client.CALL_DEADLINE_SECONDS,
            "companies_house_requests_per_5_minutes": client.CH_LIMIT,
        },
        "_before": before,
    }


def _audit_counts(node: object) -> None:
    if isinstance(node, dict):
        if isinstance(node.get("rows"), list) and "rows_read" in node:
            if node["rows_read"] != len(node["rows"]):
                node["rows_read"] = len(node["rows"])
                node["status"] = "error"
                node["reason"] = "The row count did not match the rows, so this source was not treated as read."
        for value in node.values():
            _audit_counts(value)
    elif isinstance(node, list):
        for item in node:
            _audit_counts(item)


def _vat_excluded() -> dict:
    body = sources.vat_refusal()
    return sources.block(
        "excluded",
        body["message"],
        [],
        next_step=body["next_step"],
        official_url=body["official_url"],
        checked=False,
    )


def _drop_private(payload: dict) -> dict:
    payload.pop("_before", None)
    if payload.get("ok") is False and isinstance(payload.get("sources"), dict):
        payload["sources"]["vat"] = _vat_excluded()
    _audit_counts(payload.get("sources"))
    return payload


def _gazette(name: str, deadline: Deadline, incorporated: str = "") -> dict:
    try:
        found = sources.fetch_gazette(name, deadline, incorporated)
    except client.ApiError as exc:
        if exc.error_type == "bad_input":
            block = _not_read("The insolvency feed was not queried. " + exc.message)
            if exc.next_step:
                block["next_step"] = exc.next_step
            return block
        return _error_block(exc)
    return sources.block("read", found["note"], found["rows"], fully_read=found["fully_read"], total_reported=found["total_reported"])


def _charity_for_name(name: str, deadline: Deadline) -> dict:
    try:
        if not read_secret(CHARITY_KEY):
            return _no_key_charity()
        found = sources.fetch_charity_name(name, deadline)
    except client.ApiError as exc:
        if exc.error_type == "no_key":
            return _no_key_charity()
        if exc.error_type == "bad_input":
            return _not_read("The charity register was not queried. " + exc.message)
        return _error_block(exc)
    return sources.block("read", found["note"], found["rows"], fully_read=found["fully_read"])


def _candidate(row: dict, query: str) -> dict:
    code, label = verdict(compare(row.get("title"), query))
    return {
        "company_number": row.get("company_number", ""),
        "title": row.get("title", ""),
        "company_status": row.get("company_status", ""),
        "official_url": row.get("official_url", ""),
        "match": code,
        "match_ja": label,
    }


def _stop_candidates(query: str, candidates: list, reason: str, *, fully_read: bool, total_reported: object) -> dict:
    stopped = _stopped(reason)
    stopped["companies_house"] = sources.block(
        "read",
        reason,
        candidates,
        fully_read=fully_read,
        total_reported=total_reported,
    )
    return _drop_private(_base(
        query,
        False,
        None,
        candidates,
        stopped,
        True,
        reason,
        "Call uk_check again with one company number from the candidates array.",
    ))


def _mark_profile_parts(block: dict) -> None:
    failed = []
    for row in block.get("rows") or []:
        if isinstance(row, dict) and row.get("status") == "error" and row.get("part"):
            failed.append(row)
    if not failed:
        return
    names = []
    for row in failed:
        part = str(row["part"])
        if part not in names:
            names.append(part)
    block["status"] = "error"
    block["reason"] = "The company profile was read. " + ", ".join(names) + " returned an error."
    for row in failed:
        if row.get("next_step"):
            block["next_step"] = row["next_step"]
            break


_RESOLVED_MESSAGE = (
    "One company was resolved. Each source is separate. "
    "A disqualification row is exact only when the title, the birth month, the birth year, and this company name agree. "
    "LTD and LIMITED are the same ending on that company name. "
    "A notice row is exact only when its title agrees with the registered name and the notice date is not before incorporation. "
    "A symbol one name has and the other lacks stays possible."
)

# status read and fully_read false. not_read and excluded stay off this list.
# part is empty for the block itself. A nested companies_house row uses part.
_UNREAD_SOURCES = (
    {"key": "disqualified", "part": "", "label": "disqualified-officer search"},
    {"key": "gazette", "part": "", "label": "insolvency feed"},
    {"key": "charity", "part": "", "label": "charity register"},
    {"key": "land", "part": "", "label": "Price Paid list"},
    {"key": "companies_house", "part": "", "label": "company profile"},
    {"key": "companies_house", "part": "officers", "label": "officer list"},
    {"key": "companies_house", "part": "psc", "label": "persons with significant control list"},
    {"key": "companies_house", "part": "filings", "label": "filing history"},
)


def _block_open(block: object) -> bool:
    return isinstance(block, dict) and block.get("status") == "read" and block.get("fully_read") is False


def _unfinished_labels(sources_map: dict) -> list[str]:
    labels = []
    house = sources_map.get("companies_house")
    rows = house.get("rows") if isinstance(house, dict) else None
    for spec in _UNREAD_SOURCES:
        if spec["part"]:
            if not isinstance(rows, list):
                continue
            for item in rows:
                if isinstance(item, dict) and item.get("part") == spec["part"] and _block_open(item):
                    labels.append(spec["label"])
                    break
            continue
        if _block_open(sources_map.get(spec["key"])):
            labels.append(spec["label"])
    return labels


def _unread_sentence(labels: list[str]) -> str:
    named = [f"the {label}" for label in labels]
    if len(named) == 1:
        subject = named[0]
        verb = "was"
        which = "that block"
    elif len(named) == 2:
        subject = f"{named[0]} and {named[1]}"
        verb = "were"
        which = "those blocks"
    else:
        subject = ", ".join(named[:-1]) + ", and " + named[-1]
        verb = "were"
        which = "those blocks"
    opened = subject[:1].upper() + subject[1:]
    return (
        f"{opened} {verb} not fully read. "
        f"Read {which} before treating a short list as the whole register."
    )


def _named_skips(sources_map: dict) -> list[str]:
    """next_step values on not_read blocks. A missing key stays off this list."""
    found = []
    for block in sources_map.values():
        if not isinstance(block, dict) or block.get("status") != "not_read":
            continue
        step = block.get("next_step")
        if isinstance(step, str) and step:
            found.append(step)
    return found


def _sheet_outcome(sources_map: dict) -> tuple[bool, str, str]:
    """Pick ok, message, and next_step for one resolved company.

    Input kinds are the rows of test_resolved_sheet_outcome_table.
    An error block wins. A read block with fully_read false names itself
    in next_step and leaves ok true. A missing fully_read flag does not.
    A not_read block is named only when that block carries next_step.
    """
    for block in sources_map.values():
        if isinstance(block, dict) and block.get("status") == "error":
            step = block.get("next_step") or "Read the source block whose status is error."
            return False, "One company was resolved. A source returned an error.", str(step)
    parts = []
    labels = _unfinished_labels(sources_map)
    if labels:
        parts.append(_unread_sentence(labels))
    parts.extend(_named_skips(sources_map))
    if not parts:
        return True, _RESOLVED_MESSAGE, ""
    return True, _RESOLVED_MESSAGE, " ".join(parts)


def _land_for_code(code: str, deadline: Deadline, reason: str = "") -> dict:
    if not code:
        return _not_read(reason or "The company profile did not include a registered-office postcode, so Price Paid was not queried.")
    try:
        found = sources.fetch_land(code, deadline)
    except client.ApiError as exc:
        return _error_block(exc)
    return sources.block("read", found["note"], found["rows"], fully_read=found["fully_read"], postcode=found["postcode"])


def _skipped_name_sources() -> dict:
    """Gazette and charity run only after a registered name is known."""
    return {
        "disqualified": _not_read("No company was resolved, so officer names were not checked."),
        "gazette": _not_read("The registered name was not read, so the insolvency feed was not queried."),
        "charity": _not_read("The registered name was not read, so the charity register was not queried."),
        "vat": _vat(),
        "land": _not_read("No registered-office postcode was read, so Price Paid was not queried."),
    }


def _unresolved_name(query: str, ch_block: dict, message: str, next_step: str) -> dict:
    """Return a sheet that has not resolved one company.

    The typed query is not sent to The Gazette or the Charity Commission.
    Input kinds are the rows of test_outbound_name_table.
    """
    sources_map = {"companies_house": ch_block, **_skipped_name_sources()}
    for block in sources_map.values():
        if isinstance(block, dict) and block.get("status") == "error":
            step = str(block.get("next_step") or "Read the source block whose status is error.")
            return _drop_private(_base(
                query, False, None, [], sources_map, False, "A source returned an error.", step,
            ))
    return _drop_private(_base(query, False, None, [], sources_map, False, message, next_step))


def _one_company(query: str, profile: dict, deadline: Deadline) -> dict:
    number = profile["company_number"]
    try:
        officers = sources.fetch_officers(number, deadline)
        officer_block = sources.block("read", officers["note"], officers["rows"], fully_read=officers["fully_read"], total_reported=officers["total_reported"], official_url=officers["official_url"])
    except client.ApiError as exc:
        officers = None
        officer_block = _error_block(exc)
    try:
        people = sources.fetch_psc(number, deadline)
        psc_block = sources.block("read", people["note"], people["rows"], fully_read=people["fully_read"], total_reported=people["total_reported"], official_url=people["official_url"])
    except client.ApiError as exc:
        psc_block = _error_block(exc)
    try:
        filings = sources.fetch_filings(number, deadline)
        filing_block = sources.block("read", filings["note"], filings["rows"], fully_read=filings["fully_read"], total_reported=filings["total_reported"], official_url=filings["official_url"])
    except client.ApiError as exc:
        filing_block = _error_block(exc)
    if officers is None:
        disq = _not_read("Officers were not read, so disqualification was not checked.")
    else:
        people_rows = [row for row in officers["rows"] if row.get("name")]
        try:
            disq = sources.fetch_disqualified(
                [row["name"] for row in people_rows],
                deadline,
                bool(officers["fully_read"]),
                people_rows,
                profile["company_name"],
            )
        except client.ApiError as exc:
            disq = _error_block(exc)
    try:
        gazette = _gazette(profile["company_name"], deadline, profile.get("date_of_creation") or "")
    except client.ApiError as exc:
        gazette = _error_block(exc)
    try:
        charity = _charity_for_name(profile["company_name"], deadline)
    except client.ApiError as exc:
        charity = _error_block(exc)
    land = _land_for_code(profile.get("postcode") or "", deadline, profile.get("postcode_reason") or "")
    profile_row = {
        "company_number": profile["company_number"],
        "company_name": profile["company_name"],
        "company_status": profile["company_status"],
        "date_of_creation": profile["date_of_creation"],
        "type": profile["type"],
        "registered_office": profile["registered_office"],
        "postcode": profile["postcode"],
        "sic_codes": profile["sic_codes"],
        "official_url": profile["official_url"],
        "accounts_overdue": profile["accounts_overdue"],
        "accounts_overdue_reason": profile["accounts_overdue_reason"],
        "confirmation_statement_overdue": profile["confirmation_statement_overdue"],
        "confirmation_statement_overdue_reason": profile["confirmation_statement_overdue_reason"],
        "has_charges": profile["has_charges"],
        "has_charges_reason": profile["has_charges_reason"],
        "charges_url": profile["charges_url"],
        "charges_url_reason": profile["charges_url_reason"],
        "status_reason": "" if profile["company_status"] else "The profile did not include company_status.",
        "postcode_reason": profile["postcode_reason"] if "postcode_reason" in profile else (
            "" if profile["postcode"] else "The profile did not include a registered-office postcode."
        ),
    }
    ch = sources.block(
        "read",
        "",
        [profile_row, {"part": "officers", **officer_block}, {"part": "psc", **psc_block}, {"part": "filings", **filing_block}],
        official_url=profile["official_url"],
    )
    _mark_profile_parts(ch)
    # rows_read is 4 parts, which matches the list above. Officer row counts live on the nested blocks.
    sources_map = {
        "companies_house": ch,
        "disqualified": disq,
        "gazette": gazette,
        "charity": charity,
        "vat": _vat(),
        "land": land,
    }
    ok, message, step = _sheet_outcome(sources_map)
    return _drop_private(_base(
        query,
        True,
        profile_row,
        [],
        sources_map,
        ok,
        message,
        step,
    ))


def uk_check(args: dict) -> dict:
    client.clear_host_counts()
    if not isinstance(args, dict) or set(args) - {"query"}:
        return client.failed("bad_input", "Pass query as a string.", client.NEXT["bad_input"])
    query, reason = classify_query(args.get("query"))
    if query is None:
        return client.failed("bad_input", refusal_message(reason), client.NEXT["bad_input"])
    deadline = Deadline()
    number = company_number(query)
    if number:
        try:
            if not read_secret(client.CH_KEY):
                return _unresolved_name(
                    query,
                    _no_key_ch(),
                    "The company number was not looked up because the Companies House key is unset.",
                    client.NEXT["no_key"],
                )
            profile = sources.fetch_profile(number, deadline)
        except client.ApiError as exc:
            if exc.error_type == "no_key":
                return _unresolved_name(query, _no_key_ch(), exc.message, exc.next_step)
            stopped = _stopped("The company was not resolved, so this source was not looked up.")
            stopped["companies_house"] = _error_block(exc)
            return _drop_private(_base(query, False, None, [], stopped, False, exc.message, exc.next_step))
        return _one_company(query, profile, deadline)
    try:
        if not read_secret(client.CH_KEY):
            return _unresolved_name(
                query,
                _no_key_ch(),
                "Companies House was not searched because the key is unset. "
                "The registered name was not read, so The Gazette and the charity register were not queried.",
                client.NEXT["no_key"],
            )
        found = sources.search_companies(query, deadline)
    except client.ApiError as exc:
        if exc.error_type == "no_key":
            return _unresolved_name(query, _no_key_ch(), exc.message, exc.next_step)
        return _drop_private(_base(query, False, None, [], _stopped(exc.message), False, exc.message, exc.next_step))
    rows = found["rows"]
    if found["total_reported"] == 0 and rows:
        note = found["note"]
        candidates = [_candidate(row, query) for row in rows]
        stopped = _stopped(note)
        stopped["companies_house"] = sources.block(
            "read",
            note,
            candidates,
            fully_read=False,
            total_reported=0,
        )
        return _drop_private(_base(
            query,
            False,
            None,
            candidates,
            stopped,
            False,
            note,
            "Pass a company number from the rows if you already have one.",
        ))
    if found["fully_read"] and found["total_reported"] == 0:
        return _unresolved_name(
            query,
            sources.block("read", found["note"], [], fully_read=True, total_reported=0),
            found["note"],
            "Pass a company number if you already have one.",
        )
    one = found["fully_read"] and found["total_reported"] == 1 and len(rows) == 1
    if one and not rows[0]["company_number"]:
        return _drop_private(_base(
            query, False, None, [], _stopped("The only search row had no company number."),
            False, "The only search row had no company number.", client.NEXT["bad_shape"],
        ))
    if one and compare(query, rows[0]["title"]) == "exact":
        only = rows[0]
        try:
            profile = sources.fetch_profile(only["company_number"], deadline)
        except client.ApiError as exc:
            return _drop_private(_base(query, False, None, [], _stopped(exc.message), False, exc.message, exc.next_step))
        if compare(query, profile["company_name"]) != "exact":
            code, label = verdict(compare(profile["company_name"], query))
            candidate = {
                "company_number": profile["company_number"],
                "title": profile["company_name"],
                "company_status": profile["company_status"],
                "official_url": profile["official_url"],
                "match": code,
                "match_ja": label,
            }
            return _stop_candidates(
                query,
                [candidate],
                "The registered name is not an exact match for the query, so other sources were not looked up.",
                fully_read=True,
                total_reported=1,
            )
        body = _one_company(query, profile, deadline)
        if body.get("company"):
            code, label = verdict("exact")
            body["company"]["match"] = code
            body["company"]["match_ja"] = label
        return body
    if one:
        reason = "The only search hit is not an exact match for the query, so other sources were not looked up."
    else:
        reason = found["note"] or "More than one company matched, so other sources were not looked up."
    candidates = [_candidate(row, query) for row in rows]
    return _stop_candidates(
        query,
        candidates,
        reason,
        fully_read=found["fully_read"],
        total_reported=found["total_reported"],
    )


def uk_search(args: dict) -> dict:
    client.clear_host_counts()
    if not isinstance(args, dict) or set(args) - {"name"}:
        return client.failed("bad_input", "Pass name as a string.", client.NEXT["bad_input"])
    name, message = search_name(args.get("name"))
    if name is None:
        return client.failed("bad_input", message, client.NEXT["bad_input"])
    try:
        found = sources.search_companies(name, Deadline())
    except client.ApiError as exc:
        return client.from_error(exc)
    total = found["total_reported"]
    rows = found["rows"]
    # A full first page is a successful search. ok is false when the total is
    # missing, zero, or smaller than the rows on the page.
    ok = bool(rows) and isinstance(total, int) and total >= len(rows) and total > 0
    body = {"ok": ok, "message": found["note"], **found, "requests": _counts()}
    if not ok:
        if total == 0 and not rows:
            body["next_step"] = "Pass a company number if you already have one."
        else:
            body["next_step"] = "The company list was not complete. Pass a company number if you already have one."
    return body


def _number_tool(args: dict, fn) -> dict:
    client.clear_host_counts()
    if not isinstance(args, dict) or set(args) - {"company_number"}:
        return client.failed("bad_input", "Pass company_number.", client.NEXT["bad_input"])
    raw_number = args.get("company_number")
    if isinstance(raw_number, str):
        number, message = number_argument(raw_number)
    else:
        number, message = company_number(raw_number), ""
    if not number:
        return client.failed("bad_input", message or "The company number is not in the expected form.", client.NEXT["bad_input"])
    try:
        found = fn(number, Deadline())
    except client.ApiError as exc:
        return client.from_error(exc)
    return {"ok": True, "message": found.get("note", ""), **found, "requests": _counts()}


def uk_officers(args: dict) -> dict:
    return _number_tool(args, sources.fetch_officers)


def uk_psc(args: dict) -> dict:
    return _number_tool(args, sources.fetch_psc)


def uk_filings(args: dict) -> dict:
    return _number_tool(args, sources.fetch_filings)


def uk_charity(args: dict) -> dict:
    client.clear_host_counts()
    if not isinstance(args, dict) or set(args) - {"name", "number", "suffix"}:
        return client.failed("bad_input", "Pass name or number.", client.NEXT["bad_input"])
    try:
        if args.get("number"):
            found = sources.fetch_charity_number(str(args.get("number")), args.get("suffix"), Deadline())
        elif args.get("name"):
            found = sources.fetch_charity_name(str(args.get("name")), Deadline())
        else:
            return client.failed("bad_input", "Pass name or number.", client.NEXT["bad_input"])
    except client.ApiError as exc:
        extra = {}
        if exc.error_type == "no_key":
            extra["reason_ja"] = no_key_ja
        return client.from_error(exc, **extra)
    return {"ok": True, "message": found.get("note", ""), **found, "requests": _counts()}


def uk_vat(args: dict) -> dict:
    client.clear_host_counts()
    if args is None:
        args = {}
    if not isinstance(args, dict) or set(args) - set():
        return client.failed("bad_input", "uk_vat takes no arguments.", client.NEXT["bad_input"])
    body = sources.vat_refusal()
    body["requests"] = _counts()
    code, label = verdict("unchecked")
    body["match"] = code
    body["match_ja"] = label
    return body


def uk_land(args: dict) -> dict:
    client.clear_host_counts()
    if not isinstance(args, dict) or set(args) - {"postcode"}:
        return client.failed("bad_input", "Pass postcode.", client.NEXT["bad_input"])
    try:
        found = sources.fetch_land(str(args.get("postcode") or ""), Deadline())
    except client.ApiError as exc:
        return client.from_error(exc)
    return {"ok": True, "message": found["note"], **found, "requests": _counts()}
