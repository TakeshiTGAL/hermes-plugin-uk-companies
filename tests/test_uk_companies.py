"""Sheet, watch, HTTP, and cron behavior for uk-companies."""
import ast
import json
import os
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pytest

import client
import cli
import handlers
import match
import prefix
import public_cron
import sheet
import sources
import watch
from safe_write import WriteRefused, write_bytes
from tests.state import STATE

ROOT = Path(__file__).resolve().parents[1]
CH = client.CH_HOST
KEY = "companies-house-test-key"
CHARITY_KEY = "charity-commission-test-key"
NUMBER = "12345678"
NAME = "ACKOR CONTRACTING LIMITED"


class Router:
    def __init__(self):
        self.calls = []
        self.routes = {}

    def send(self, method, url, headers, timeout):
        parsed = urllib.parse.urlsplit(url)
        self.calls.append({"method": method, "url": url, "path": parsed.path, "headers": dict(headers), "timeout": timeout})
        spec = self.routes.get(parsed.path)
        if spec is None:
            status, body, final = 500, {"error": "no fixture"}, url
        elif callable(spec):
            status, body, final = spec(url, headers)
        else:
            status, body, final = spec
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return client.HttpResult(status, raw, final or url)


def use(router, clock):
    client.configure(transport=router, clock=clock, sleep=clock.sleep)
    STATE["values"][client.CH_KEY] = KEY


def profile():
    return {
        "company_name": NAME,
        "company_number": NUMBER,
        "company_status": "active",
        "date_of_creation": "2020-01-01",
        "type": "ltd",
        "registered_office_address": {"address_line_1": "1 High Street", "postal_code": "EC1V 3AP", "locality": "London"},
        "sic_codes": ["41202"],
    }


def people(kind):
    row = {
        "name": "CORK, Martin",
        "appointed_on": "2020-01-01",
        "nationality": "British",
        "date_of_birth": {"month": 1, "year": 1970},
    }
    if kind == "officers":
        row["officer_role"] = "director"
    else:
        row["kind"] = "individual-person-with-significant-control"
        row["natures_of_control"] = ["ownership-of-shares-25-to-50-percent"]
        row["notified_on"] = "2020-01-01"
    return {"total_results": 1, "items": [row]}


def filings():
    return {
        "total_count": 1,
        "items": [{
            "transaction_id": "MzA",
            "category": "confirmation-statement",
            "date": "2024-01-01",
            "description": "confirmation-statement",
            "type": "CS01",
        }],
    }


def gazette(title=NAME):
    return {
        "f:total": "1",
        "entry": [{
            "title": title,
            "published": "2026-11-02T18:31:54",
            "f:notice-code": "2450",
            "category": {"@term": "Petitions to Wind Up (Companies)"},
            "content": "<div><p>In the Birmingham District Registry CR-2026-BHM-000279 In the matter of …</p></div>",
            "link": [
                {"@href": "https://www.thegazette.co.uk/notice/5243319"},
                {"@href": "https://www.thegazette.co.uk/notice/5243319/data.json"},
            ],
        }],
    }


def land():
    return {
        "results": {
            "bindings": [{
                "item": {"type": "uri", "value": "http://landregistry.data.gov.uk/data/ppi/transaction/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/current"},
                "price": {"value": "542000"},
                "date": {"value": "2026-09-03"},
                "paon": {"value": "ORCHARD BUILDING, 25"},
                "street": {"value": "PEAR TREE STREET"},
                "postcode": {"value": "EC1V 3AP"},
                "propertyType": {"value": "http://landregistry.data.gov.uk/def/common/flat-maisonette"},
                "estateType": {"value": "http://landregistry.data.gov.uk/def/common/leasehold"},
            }]
        }
    }


def sheet_router():
    router = Router()
    router.routes = {
        "/company/" + NUMBER: (200, profile(), None),
        "/company/" + NUMBER + "/officers": (200, people("officers"), None),
        "/company/" + NUMBER + "/persons-with-significant-control": (200, people("psc"), None),
        "/company/" + NUMBER + "/filing-history": (200, filings(), None),
        "/search/disqualified-officers": (200, {"total_results": 0, "items": []}, None),
        "/insolvency/notice/data.json": (200, gazette(), None),
        "/landregistry/query": (200, land(), None),
    }
    return router


def invoke(name, args):
    return json.loads(handlers.invoke(name, args))


def paths(router):
    return [call["path"] for call in router.calls]


def walk_counts(node):
    if isinstance(node, dict):
        if isinstance(node.get("rows"), list) and "rows_read" in node:
            assert node["rows_read"] == len(node["rows"])
        for value in node.values():
            walk_counts(value)
    elif isinstance(node, list):
        for item in node:
            walk_counts(item)


def test_one_company_sheet_splits_sources(runtime):
    router = sheet_router()
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["ok"] is True and body["resolved"] is True
    assert body["next_step"] == ""
    assert body["message"] == sheet._RESOLVED_MESSAGE
    assert body["company"]["company_status"] == "active"
    assert body["company"]["company_name"] == NAME
    gazette_calls = [call for call in router.calls if call["path"] == "/insolvency/notice/data.json"]
    assert len(gazette_calls) == 1
    gazette_text = urllib.parse.parse_qs(urllib.parse.urlsplit(gazette_calls[0]["url"]).query)["text"][0]
    assert gazette_text == f'"{NAME}"'
    assert NUMBER not in gazette_text
    gazette_row = body["sources"]["gazette"]["rows"][0]
    assert gazette_row["match"] == "exact"
    assert gazette_row["match_ja"] == match.verdict_exact_ja
    assert gazette_row["official_url"] == "https://www.thegazette.co.uk/notice/5243319"
    assert gazette_row["published"] == "2026-11-02T18:31:54"
    assert gazette_row["notice_code"] == "2450"
    assert gazette_row["category"] == "Petitions to Wind Up (Companies)"
    assert gazette_row["category_reason"] == ""
    assert "Birmingham District Registry" in gazette_row["excerpt"]
    assert "<" not in gazette_row["excerpt"]
    assert gazette_row["excerpt_reason"] == ""
    land_row = body["sources"]["land"]["rows"][0]
    assert land_row["official_url"].startswith("https://landregistry.data.gov.uk/landregistry/query?")
    assert "DESCRIBE" in urllib.parse.unquote(land_row["official_url"])
    assert "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee" in land_row["official_url"]
    assert "/data/ppi/transaction/" not in urllib.parse.urlsplit(land_row["official_url"]).path
    assert "does not show that a company owns" in land_row["note"]
    assert "returns 403" in land_row["note"]
    assert "SPARQL DESCRIBE" in land_row["note"]
    assert "Contains HM Land Registry data © Crown copyright and database right 2021." in land_row["note"]
    assert "personal or non-commercial use" in land_row["note"]
    assert "Contains HM Land Registry data © Crown copyright and database right 2021." in body["sources"]["land"]["reason"]
    assert "older pages" not in body["sources"]["land"]["reason"].lower()
    assert "Exact title matches are marked exact." not in body["message"]
    assert body["sources"]["vat"]["status"] == "excluded"
    assert body["sources"]["vat"]["rows"] == []
    assert body["sources"]["vat"]["checked"] is False
    assert match.no_key_ja in body["sources"]["charity"]["reason"]
    assert "not a finding that the person is clear" in body["sources"]["disqualified"]["rows"][0]["reason"]
    assert "CORK, Martin" in json.dumps(body["sources"]["companies_house"])
    walk_counts(body)
    assert all("hmrc" not in call["url"] and "address.json" not in call["url"] for call in router.calls)
    assert KEY not in json.dumps(body)
    basic = client.basic_headers(KEY)["Authorization"].split(" ", 1)[1]
    assert basic not in json.dumps(body)


def test_partial_page_does_not_deny(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {
        "total_results": 50,
        "items": [{"title": "ACME LTD", "company_number": NUMBER, "company_status": "active"}],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": "ACME LTD"})
    assert body["resolved"] is False
    assert len(router.calls) == 1
    assert "Later rows were not read." in body["message"]
    assert "no company" not in body["message"].lower()
    assert body["sources"]["gazette"]["status"] == "not_read"
    assert body["ok"] is True
    assert body["sources"]["vat"]["status"] == "excluded"
    assert body["sources"]["vat"]["rows"] == []
    assert body["sources"]["vat"]["checked"] is False


def test_status_word_is_copied_not_inferred(runtime):
    router = sheet_router()
    router.routes["/company/" + NUMBER] = (200, {**profile(), "company_status": "foo-status"}, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["company"]["company_status"] == "foo-status"
    assert "insolvent" not in json.dumps(body["company"])


def test_single_search_hit_must_match_the_name(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {
        "total_results": 1,
        "items": [{"title": "ACME WIDGETS PLC", "company_number": NUMBER, "company_status": "active"}],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": "ACME"})
    assert body["ok"] is True and body["resolved"] is False
    assert body["company"] is None
    assert body["candidates"][0]["title"] == "ACME WIDGETS PLC"
    assert body["candidates"][0]["company_number"] == NUMBER
    assert body["candidates"][0]["match"] == "possible"
    assert body["candidates"][0]["match_ja"] == match.verdict_possible_ja
    assert paths(router) == ["/search/companies"]
    assert body["sources"]["gazette"]["status"] == "not_read"
    assert "One company was resolved" not in body["message"]


def test_exact_name_resolves_and_marks_match(runtime):
    router = sheet_router()
    router.routes["/search/companies"] = (200, {
        "total_results": 1,
        "items": [{"title": NAME, "company_number": NUMBER, "company_status": "active"}],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NAME})
    assert body["ok"] is True and body["resolved"] is True
    assert body["company"]["company_name"] == NAME
    assert body["company"]["match"] == "exact"
    assert body["company"]["match_ja"] == match.verdict_exact_ja
    assert "/company/" + NUMBER in paths(router)


def test_profile_name_mismatch_does_not_fan_out(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {
        "total_results": 1,
        "items": [{"title": "ACME LTD", "company_number": NUMBER, "company_status": "active"}],
    }, None)
    router.routes["/company/" + NUMBER] = (200, {**profile(), "company_name": "ACME HOLDINGS PLC"}, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": "ACME LTD"})
    assert body["resolved"] is False
    assert body["candidates"][0]["title"] == "ACME HOLDINGS PLC"
    assert body["candidates"][0]["match"] == "possible"
    assert paths(router) == ["/search/companies", "/company/" + NUMBER]
    assert body["sources"]["gazette"]["status"] == "not_read"


def test_source_error_is_not_sheet_success(runtime):
    router = sheet_router()
    router.routes["/company/" + NUMBER + "/officers"] = (401, {"error": "no"}, None)
    router.routes["/insolvency/notice/data.json"] = (500, {"error": "no"}, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["resolved"] is True
    assert body["ok"] is False
    assert body["next_step"]
    house = body["sources"]["companies_house"]
    assert house["status"] == "error"
    assert house["reason"]
    officers = [row for row in house["rows"] if row.get("part") == "officers"][0]
    assert officers["status"] == "error"
    assert officers["error_type"] == "auth"
    assert body["sources"]["gazette"]["status"] == "error"
    assert "HTTP 500" in body["sources"]["gazette"]["reason"]
    assert body["sources"]["disqualified"]["status"] == "not_read"


def test_multiple_candidates_stop(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {
        "total_results": 2,
        "items": [
            {"title": "ACME LTD", "company_number": "11111111", "company_status": "active"},
            {"title": "ACME TWO LTD", "company_number": "22222222", "company_status": "dissolved"},
        ],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": "ACME"})
    assert body["ok"] is True and body["resolved"] is False
    assert [row["company_number"] for row in body["candidates"]] == ["11111111", "22222222"]
    assert paths(router) == ["/search/companies"]
    assert "Call uk_check again" in body["next_step"]
    assert body["sources"]["vat"]["status"] == "excluded"
    assert body["sources"]["vat"]["rows"] == []


def _hosts(router):
    found = []
    for call in router.calls:
        parsed = urllib.parse.urlsplit(call["url"])
        found.append(parsed.hostname + parsed.path)
    return found


def test_outbound_name_table(runtime):
    """Every input kind for what leaves this machine, and what ok means.

    uk_check sends Gazette and the charity register the registered name only.
    uk_search and uk_charity send the name the caller passed to that one source.
    """
    hidden = {"title": "HIDDEN LTD", "company_number": "12345678", "company_status": "active"}
    cases = []

    def add(label, fn):
        cases.append((label, fn))

    def check_quiet(body, router):
        assert body["ok"] is False and body["resolved"] is False
        assert body["next_step"]
        assert router.calls == []
        assert body["sources"]["gazette"]["status"] == "not_read"
        assert "registered name was not read" in body["sources"]["gazette"]["reason"]
        assert "registered name was not read" in body["sources"]["charity"]["reason"]
        assert "thegazette.co.uk" not in json.dumps(_hosts(router))
        assert "charitycommission" not in json.dumps(_hosts(router))

    def keyless_number():
        router = Router()
        client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
        body = invoke("uk_check", {"query": NUMBER})
        check_quiet(body, router)
        assert "not looked up" in body["message"]
        assert match.no_key_ja in body["sources"]["companies_house"]["reason"]

    def keyless_name():
        router = Router()
        router.routes["/insolvency/notice/data.json"] = (200, gazette("TYPED NAME LTD"), None)
        segment = urllib.parse.quote("TYPED NAME LTD", safe="")
        router.routes["/register/api/searchCharityName/" + segment] = (200, [], None)
        client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
        body = invoke("uk_check", {"query": "TYPED NAME LTD"})
        check_quiet(body, router)
        assert "were not queried" in body["message"]
        assert "TYPED NAME LTD" not in json.dumps(_hosts(router))
        assert match.no_key_ja in body["sources"]["companies_house"]["reason"]

    def keyless_name_charity_key():
        router = Router()
        router.routes["/insolvency/notice/data.json"] = (200, gazette("TYPED NAME LTD"), None)
        segment = urllib.parse.quote("TYPED NAME LTD", safe="")
        router.routes["/register/api/searchCharityName/" + segment] = (200, [
            {"charity_name": "TYPED NAME LTD", "reg_charity_number": 202918, "group_subsid_suffix": 0},
        ], None)
        client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
        STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
        body = invoke("uk_check", {"query": "TYPED NAME LTD"})
        check_quiet(body, router)
        assert "searchCharityName" not in json.dumps(router.calls)

    def zero_empty():
        router = Router()
        router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)
        router.routes["/insolvency/notice/data.json"] = (200, gazette("MISSING NAME LTD"), None)
        use(router, runtime)
        body = invoke("uk_check", {"query": "MISSING NAME LTD"})
        assert body["ok"] is False and body["resolved"] is False
        assert paths(router) == ["/search/companies"]
        assert "leaves the question open" in body["message"]
        assert body["next_step"] == "Pass a company number if you already have one."
        assert body["sources"]["gazette"]["status"] == "not_read"
        assert "no company is registered" not in json.dumps(body).lower()
        assert "Gazette rows are in the gazette block." not in body["message"]

    def contradict_zero():
        router = Router()
        router.routes["/search/companies"] = (200, {"total_results": 0, "items": [hidden]}, None)
        router.routes["/insolvency/notice/data.json"] = (200, gazette("MISSING NAME LTD"), None)
        use(router, runtime)
        body = invoke("uk_check", {"query": "MISSING NAME LTD"})
        assert body["ok"] is False and body["resolved"] is False
        assert paths(router) == ["/search/companies"]
        assert "12345678" in json.dumps(body["sources"]["companies_house"])
        assert body["sources"]["companies_house"]["fully_read"] is False
        assert body["sources"]["companies_house"]["total_reported"] == 0
        assert "total was 0" in body["message"]
        assert body["message"]
        assert body["sources"]["companies_house"]["reason"]
        assert "Pass a company number from the rows" in body["next_step"]
        assert body["sources"]["gazette"]["status"] == "not_read"

    def total_below_rows():
        router = Router()
        router.routes["/search/companies"] = (200, {
            "total_results": 1,
            "items": [hidden, {"title": "OTHER LTD", "company_number": "87654321", "company_status": "active"}],
        }, None)
        use(router, runtime)
        body = invoke("uk_check", {"query": "HIDDEN"})
        assert body["resolved"] is False
        assert paths(router) == ["/search/companies"]
        assert "12345678" in json.dumps(body["candidates"])
        assert "87654321" in json.dumps(body["candidates"])
        assert body["sources"]["companies_house"]["fully_read"] is False
        assert "lower than the rows" in body["message"]
        assert body["sources"]["gazette"]["status"] == "not_read"

    def multi():
        router = Router()
        router.routes["/search/companies"] = (200, {
            "total_results": 2,
            "items": [
                {"title": "ACME LTD", "company_number": "11111111", "company_status": "active"},
                {"title": "ACME TWO LTD", "company_number": "22222222", "company_status": "active"},
            ],
        }, None)
        router.routes["/insolvency/notice/data.json"] = (200, gazette("ACME LTD"), None)
        use(router, runtime)
        body = invoke("uk_check", {"query": "ACME"})
        assert body["ok"] is True and body["resolved"] is False
        assert paths(router) == ["/search/companies"]
        assert body["sources"]["gazette"]["status"] == "not_read"
        assert "Call uk_check again" in body["next_step"]

    def search_zero():
        router = Router()
        router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)
        use(router, runtime)
        body = invoke("uk_search", {"name": "MISSING NAME LTD"})
        assert body["ok"] is False
        assert "0 companies" in body["message"]
        assert body["next_step"] == "Pass a company number if you already have one."
        assert body["fully_read"] is True

    def search_no_total():
        router = Router()
        router.routes["/search/companies"] = (200, {"items": [hidden]}, None)
        use(router, runtime)
        body = invoke("uk_search", {"name": "HIDDEN LTD"})
        assert body["ok"] is False
        assert body["fully_read"] is False
        assert "did not include a total" in body["message"]
        assert body["next_step"]
        assert body["rows"][0]["company_number"] == "12345678"

    def search_contradict():
        router = Router()
        router.routes["/search/companies"] = (200, {"total_results": 0, "items": [hidden]}, None)
        use(router, runtime)
        body = invoke("uk_search", {"name": "HIDDEN LTD"})
        assert body["ok"] is False
        assert body["fully_read"] is False
        assert "total was 0" in body["message"]
        assert body["rows"][0]["company_number"] == "12345678"
        assert body["next_step"]

    def search_one():
        router = Router()
        router.routes["/search/companies"] = (200, {"total_results": 1, "items": [hidden]}, None)
        use(router, runtime)
        body = invoke("uk_search", {"name": "HIDDEN LTD"})
        assert body["ok"] is True
        assert body["fully_read"] is True
        assert body["rows"][0]["company_number"] == "12345678"
        assert "next_step" not in body

    def search_partial():
        router = Router()
        router.routes["/search/companies"] = (200, {"total_results": 50, "items": [hidden]}, None)
        use(router, runtime)
        body = invoke("uk_search", {"name": "HIDDEN LTD"})
        assert body["ok"] is True
        assert body["fully_read"] is False
        assert "Later rows were not read." in body["message"]
        assert body["rows"][0]["company_number"] == "12345678"
        assert "next_step" not in body

    def charity_name_is_the_callers_name():
        router = Router()
        router.routes["/register/api/searchCharityName/OXFAM"] = (200, [], None)
        client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
        STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
        body = invoke("uk_charity", {"name": "OXFAM"})
        assert body["ok"] is True
        assert paths(router) == ["/register/api/searchCharityName/OXFAM"]

    add("keyless_number", keyless_number)
    add("keyless_name", keyless_name)
    add("keyless_name_charity_key", keyless_name_charity_key)
    add("zero_empty", zero_empty)
    add("contradict_zero", contradict_zero)
    add("total_below_rows", total_below_rows)
    add("multi", multi)
    add("search_zero", search_zero)
    add("search_no_total", search_no_total)
    add("search_contradict", search_contradict)
    add("search_one", search_one)
    add("search_partial", search_partial)
    add("charity_name_is_the_callers_name", charity_name_is_the_callers_name)
    failed = []
    for label, fn in cases:
        client.reset_runtime()
        client.configure(clock=runtime, sleep=runtime.sleep)
        STATE["values"].clear()
        STATE["calls"].clear()
        try:
            fn()
        except AssertionError as exc:
            failed.append(label + ": " + str(exc))
    assert not failed, "\n".join(failed)


def test_contradictory_totals_keep_rows_and_do_not_confirm(runtime):
    router = Router()
    router.routes["/company/" + NUMBER + "/officers"] = (200, {
        "total_results": 0,
        "items": [{"name": "CORK, Martin", "officer_role": "director"}],
    }, None)
    router.routes["/company/" + NUMBER + "/persons-with-significant-control"] = (200, {
        "total_results": 0,
        "items": [{"name": "CORK, Martin", "kind": "individual-person-with-significant-control"}],
    }, None)
    router.routes["/company/" + NUMBER + "/filing-history"] = (200, {
        "total_count": 0,
        "items": [{"transaction_id": "MzA", "category": "accounts", "date": "2024-01-01", "type": "AA"}],
    }, None)
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 0,
        "items": [{"title": "CORK, Martin", "links": {"self": "/disqualified-officers/natural/abc123"}}],
    }, None)
    notice = gazette("FOLDERPAGE LIMITED")
    notice["f:total"] = "0"
    router.routes["/insolvency/notice/data.json"] = (200, notice, None)
    use(router, runtime)
    officers = sources.fetch_officers(NUMBER, client.Deadline())
    assert officers["fully_read"] is False
    assert officers["rows"][0]["name"] == "CORK, Martin"
    assert "total was 0" in officers["note"]
    people = sources.fetch_psc(NUMBER, client.Deadline())
    assert people["fully_read"] is False
    assert people["rows"][0]["name"] == "CORK, Martin"
    assert "total was 0" in people["note"]
    filings_found = sources.fetch_filings(NUMBER, client.Deadline())
    assert filings_found["fully_read"] is False
    assert filings_found["rows"][0]["transaction_id"] == "MzA"
    assert "total was 0" in filings_found["note"]
    client._last_at.clear()
    banned = sources.fetch_disqualified(["CORK, Martin"], client.Deadline(), True)
    assert banned["fully_read"] is False
    assert "CORK, Martin" in json.dumps(banned["rows"])
    assert all(row.get("match") != "exact" for row in banned["rows"])
    assert "/disqualified-officers/natural/abc123" not in paths(router)
    client._last_at.clear()
    feed = sources.fetch_gazette("FOLDERPAGE LIMITED", client.Deadline())
    assert feed["fully_read"] is False
    assert feed["rows"][0]["title"] == "FOLDERPAGE LIMITED"
    assert feed["rows"][0]["match"] == "unchecked"
    assert "total was 0" in feed["note"]


def _person(name, appointed="2020-01-01", ended="", appointment="", officer="", role="director", natures=None, psc=""):
    row = {
        "name": name,
        "role": role,
        "appointed_on": appointed,
        "ended_on": ended,
        "appointment_id": appointment,
        "officer_id": officer,
        "natures_of_control": natures or [],
        "psc_id": psc,
    }
    return row


WATCH_LINES = [
    ("two-same-name", "officer", [_person("SMITH, John"), _person("SMITH, John")], ["2 | SMITH, John | director | 2020-01-01"]),
    ("one-resigns", "officer", [_person("SMITH, John"), _person("SMITH, John", ended="2026-11-01")], ["1 | SMITH, John | director | 2020-01-01"]),
    ("two-appointments", "officer", [
        _person("SMITH, John", appointment="aaa", officer="p1"),
        _person("SMITH, John", appointed="2019-05-01", appointment="bbb", officer="p2"),
    ], [
        "1 | SMITH, John | director | 2019-05-01 | appointment=bbb | officer=p2",
        "1 | SMITH, John | director | 2020-01-01 | appointment=aaa | officer=p1",
    ]),
    ("duplicate-appointment", "officer", [
        _person("SMITH, John", appointment="aaa", officer="p1"),
        _person("SMITH, John", appointment="aaa", officer="p1"),
    ], ["1 | SMITH, John | director | 2020-01-01 | appointment=aaa | officer=p1"]),
    ("role-resigns", "officer", [
        _person("SMITH, John", appointment="dir", officer="p1", role="director"),
        _person("SMITH, John", appointment="sec", officer="p1", role="secretary", ended="2026-11-01"),
    ], ["1 | SMITH, John | director | 2020-01-01 | appointment=dir | officer=p1"]),
    ("psc-natures", "psc", [
        _person("SMITH, John", role="individual-person-with-significant-control", natures=["ownership-of-shares-25-to-50-percent"], psc="a1"),
        _person("SMITH, John", role="individual-person-with-significant-control", natures=["voting-rights-25-to-50-percent"], psc="a2"),
    ], [
        "1 | SMITH, John | individual-person-with-significant-control | ownership-of-shares-25-to-50-percent | psc=a1",
        "1 | SMITH, John | individual-person-with-significant-control | voting-rights-25-to-50-percent | psc=a2",
    ]),
    ("psc-duplicate-self", "psc", [
        _person("SMITH, John", role="individual-person-with-significant-control", natures=["ownership-of-shares-25-to-50-percent"], psc="a1"),
        _person("SMITH, John", role="individual-person-with-significant-control", natures=["ownership-of-shares-25-to-50-percent"], psc="a1"),
    ], ["1 | SMITH, John | individual-person-with-significant-control | ownership-of-shares-25-to-50-percent | psc=a1"]),
    ("psc-ceased", "psc", [
        _person("SMITH, John", role="individual-person-with-significant-control", natures=["ownership-of-shares-25-to-50-percent"], ended="2024-01-01"),
    ], []),
    ("no-name", "officer", [_person("")], None),
]


@pytest.mark.parametrize("case", WATCH_LINES, ids=[row[0] for row in WATCH_LINES])
def test_watch_identity_table(case):
    _name, kind, rows, expected = case
    assert sources.active_watch_lines(rows, kind) == expected


def test_watch_same_name_resignation_is_a_line(tmp_path, runtime):
    watch.set_data_dir(tmp_path)
    watch.add_number(NUMBER)
    state = {"phase": "two"}

    def officers(url, headers):
        if state["phase"] == "two":
            items = [
                {"name": "SMITH, John", "officer_role": "director", "appointed_on": "2020-01-01"},
                {"name": "SMITH, John", "officer_role": "director", "appointed_on": "2020-01-01"},
            ]
        else:
            items = [
                {"name": "SMITH, John", "officer_role": "director", "appointed_on": "2020-01-01"},
                {"name": "SMITH, John", "officer_role": "director", "appointed_on": "2020-01-01", "resigned_on": "2026-11-01"},
            ]
        return 200, {"items": items, "total_results": 2}, None

    router = sheet_router()
    router.routes["/company/" + NUMBER + "/officers"] = officers
    router.routes["/company/" + NUMBER + "/persons-with-significant-control"] = (200, {"items": [], "total_results": 0}, None)
    use(router, runtime)
    assert watch.run_watch(tmp_path) == ""
    saved = json.loads((tmp_path / "watch.json").read_text())["numbers"][NUMBER]["officers"]
    assert saved == ["2 | SMITH, John | director | 2020-01-01"]
    state["phase"] = "one"
    text = watch.run_watch(tmp_path)
    assert "2 | SMITH, John | director | 2020-01-01" in text
    assert "1 | SMITH, John | director | 2020-01-01" in text
    assert "officers removed:" in text
    assert "officers added:" in text
    assert "->" not in text


def test_charity_name_array_is_the_whole_result(runtime):
    router = Router()
    router.routes["/register/api/searchCharityName/OXFAM"] = (200, [
        {"charity_name": "OXFAM", "reg_charity_number": 202918, "group_subsid_suffix": 0, "reg_status": "R"},
        {"charity_name": "OTHER", "reg_charity_number": 1, "group_subsid_suffix": 0, "reg_status": "R"},
    ], None)
    router.routes["/register/api/allcharitydetailsV2/202918/0"] = (200, {
        "charity_name": "OXFAM",
        "reg_status": "R",
    }, None)
    client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    body = invoke("uk_charity", {"name": "OXFAM"})
    assert body["ok"] is True
    assert body["fully_read"] is True
    assert body["note"] == ""
    assert body["rows_read"] == 2
    wrapped = Router()
    wrapped.routes["/register/api/searchCharityName/OXFAM"] = (200, {"items": [
        {"charity_name": "OXFAM", "reg_charity_number": 202918, "group_subsid_suffix": 0, "reg_status": "R"},
    ]}, None)
    wrapped.routes["/register/api/allcharitydetailsV2/202918/0"] = (200, {"charity_name": "OXFAM", "reg_status": "R"}, None)
    client.configure(transport=wrapped, clock=runtime, sleep=runtime.sleep)
    client._last_at.clear()
    partial = invoke("uk_charity", {"name": "OXFAM"})
    assert partial["fully_read"] is False
    assert "one array" in partial["note"]


def test_fuzzy_gazette_title_stays_possible(runtime):
    router = Router()
    router.routes["/insolvency/notice/data.json"] = (200, gazette("FOLDERPAGE LIMITED"), None)
    client.configure(transport=router)
    found = sources.fetch_gazette("TESCO PLC", client.Deadline())
    assert found["rows"][0]["match"] == "possible"
    assert found["rows"][0]["match_ja"] == match.verdict_possible_ja
    assert found["rows"][0]["match_ja"] != match.verdict_exact_ja


def test_disqualified_zero_is_not_a_clearance(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {"total_results": 0, "items": []}, None)
    use(router, runtime)
    found = sources.fetch_disqualified(["CORK, Martin"], client.Deadline(), True)
    assert found["status"] == "read"
    assert "not a finding that the person is clear" in found["reason"]
    assert found["fully_read"] is True
    assert found["rows"][0]["match"] == "unchecked"
    assert found["rows"][0]["match_ja"] == ""
    assert "not a finding that the person is clear" in found["rows"][0]["reason"]


def test_land_query_keeps_newest(runtime):
    router = Router()
    router.routes["/landregistry/query"] = (200, land(), None)
    use(router, runtime)
    body = invoke("uk_land", {"postcode": "EC1V 3AP"})
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(router.calls[0]["url"]).query)["query"][0]
    assert "ORDER BY DESC(?date)" in query
    assert query.index("ORDER BY DESC(?date)") < query.index("LIMIT")
    assert body["rows"][0]["transaction_date"] == "2026-09-03"
    assert body["rows"][0]["price_paid"] == "542000"
    bindings = []
    for index in range(10):
        bindings.append({
            "item": {"value": "http://landregistry.data.gov.uk/data/ppi/transaction/" + f"aaaaaaaa-bbbb-cccc-dddd-{index:012d}" + "/current"},
            "price": {"value": "1"},
            "date": {"value": f"2020-01-{index + 1:02d}"},
        })
    router.routes["/landregistry/query"] = (200, {"results": {"bindings": bindings}}, None)
    capped = invoke("uk_land", {"postcode": "EC1V 3AP"})
    assert capped["fully_read"] is False
    assert "Older sales were not read." in capped["note"]
    assert "does not show that a company owns" in capped["note"]
    assert "later sales" not in capped["note"]
    assert "does not show that a company owns" in body["note"]


def test_disqualified_exact_profile_only(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 4,
        "items": [
            {"title": "Martin CORK", "date_of_birth": "1966-04-15T00:00:00", "links": {"self": "/disqualified-officers/natural/abc123"}},
            {
                "title": "Martin Alan CORK",
                "date_of_birth": "1966-04-15T00:00:00",
                "description": "Born on 15 April 1966 - Disqualified",
                "links": {"self": "/disqualified-officers/natural/should-not"},
            },
            {
                "title": "Martin Peter CORK",
                "date_of_birth": "1988-02-02T00:00:00",
                "description": "Born on 2 February 1988 - Disqualified",
                "links": {"self": "/disqualified-officers/natural/other-year"},
            },
            {
                "title": "Ernest Paul NORTON",
                "date_of_birth": "1960-01-01T00:00:00",
                "description": "Born on 1 January 1960 - Disqualified",
                "links": {"self": "/disqualified-officers/natural/norton"},
            },
        ],
    }, None)
    router.routes["/disqualified-officers/natural/abc123"] = (200, {
        "description": "a ban record",
        "date_of_birth": "1966-04-15T00:00:00",
        "disqualifications": [{
            "company_names": ["OTHER LTD"],
            "disqualified_from": "2015-01-01",
            "disqualified_until": "2027-01-01",
        }],
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(
        ["CORK, Martin, Mr."],
        client.Deadline(),
        True,
        [{"name": "CORK, Martin, Mr.", "birth_month": 4, "birth_year": 1966}],
        "OTHER LTD",
    )
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(router.calls[0]["url"]).query)["q"][0]
    assert sent == "Martin Cork"
    assert "/disqualified-officers/natural/abc123" in paths(router)
    assert "/disqualified-officers/natural/should-not" not in paths(router)
    exact = [row for row in found["rows"] if row["match"] == "exact"]
    possible = [row for row in found["rows"] if row["match"] == "possible"]
    assert exact and possible
    assert exact[0]["match_ja"] == match.verdict_exact_ja
    assert exact[0]["birth_month"] == 4
    assert exact[0]["birth_year"] == 1966
    assert exact[0]["company_names"] == ["OTHER LTD"]
    assert exact[0]["disqualifications"][0]["disqualified_until"] == "2027-01-01"
    assert possible[0]["match_ja"] == match.verdict_possible_ja
    assert possible[0]["birth_month"] == 4
    assert possible[0]["birth_year"] == 1966
    assert "did not include a month and year" not in possible[0]["birth_reason"]
    assert possible[0]["company_names"] == []
    assert "not an exact match" in possible[0]["company_names_reason"]
    assert possible[0]["title"] == "Martin Alan CORK"
    assert all(row.get("title") != "Ernest Paul NORTON" for row in found["rows"])
    assert all(row.get("title") != "Martin Peter CORK" for row in found["rows"])
    assert "Dropped 2" in found["reason"]
    assert "birth month and year" in found["reason"]
    client._last_at.clear()
    seen = len(router.calls)
    mismatched = sources.fetch_disqualified(
        ["CORK, Martin, Mr."],
        client.Deadline(),
        True,
        [{"name": "CORK, Martin, Mr.", "birth_month": 1, "birth_year": 1970}],
        NAME,
    )
    assert [row.get("title") for row in mismatched["rows"]] == []
    assert "Dropped 4" in mismatched["reason"]
    assert "birth month and year" in mismatched["reason"]
    assert "not a finding that the officer is clear" in mismatched["reason"]
    assert "/disqualified-officers/natural/abc123" not in paths(router)[seen:]
    assert all(row.get("match") != "exact" for row in mismatched["rows"])


def test_case_and_space_still_resolve(runtime):
    router = sheet_router()
    router.routes["/search/companies"] = (200, {
        "total_results": 1,
        "items": [{"title": NAME, "company_number": NUMBER, "company_status": "active"}],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": "ackor  contracting limited"})
    assert body["resolved"] is True
    assert body["company"]["match"] == "exact"
    assert body["company"]["company_name"] == NAME


def test_ampersand_name_does_not_resolve(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {
        "total_results": 1,
        "items": [{"title": "SMITH JONES LIMITED", "company_number": NUMBER, "company_status": "active"}],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": "SMITH & JONES LIMITED"})
    assert body["resolved"] is False
    assert body["ok"] is True
    assert body["candidates"][0]["title"] == "SMITH JONES LIMITED"
    assert body["candidates"][0]["match"] == "possible"
    assert body["candidates"][0]["match_ja"] == match.verdict_possible_ja
    assert "/company/" + NUMBER not in paths(router)
    assert body["sources"]["gazette"]["status"] == "not_read"
    assert body["sources"]["disqualified"]["status"] == "not_read"


def test_symbol_difference_stays_possible_on_gazette_and_charity(runtime):
    router = Router()
    router.routes["/insolvency/notice/data.json"] = (200, gazette("SMITH JONES LIMITED"), None)
    client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
    gazette_found = sources.fetch_gazette("SMITH & JONES LIMITED", client.Deadline())
    assert gazette_found["rows"][0]["match"] == "possible"
    text = "SMITH & JONES LIMITED"
    segment = urllib.parse.quote(text, safe="")
    path = "/register/api/searchCharityName/" + segment
    router.routes[path] = (200, [{
        "charity_name": "SMITH JONES LIMITED",
        "reg_charity_number": "202918",
        "group_subsid_suffix": 0,
        "reg_status": "R",
    }], None)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    charity_found = sources.fetch_charity_name(text, client.Deadline())
    assert charity_found["rows"][0]["match"] == "possible"
    assert "/register/api/allcharitydetailsV2/202918/0" not in paths(router)


def test_sheet_disqualification_needs_the_officer(runtime):
    router = sheet_router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 1,
        "items": [{"title": "Martin CORK", "date_of_birth": "1966-04-15T00:00:00", "links": {"self": "/disqualified-officers/natural/abc123"}}],
    }, None)
    router.routes["/disqualified-officers/natural/abc123"] = (200, {
        "description": "a ban record",
        "date_of_birth": "1966-04-15T00:00:00",
        "disqualifications": [{
            "company_names": ["OTHER LTD"],
            "disqualified_from": "2015-01-01",
            "disqualified_until": "2027-01-01",
        }],
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    banned = body["sources"]["disqualified"]
    assert banned["rows"] == []
    assert "Dropped 1" in banned["reason"]
    assert "birth month and year" in banned["reason"]
    assert "not a finding that the officer is clear" in banned["reason"]
    assert body["disqualified_exact_count"] == 0
    assert list(body)[0] == "disqualified_exact_count"
    assert "not a clearance" in body["disqualified_summary"]
    assert "/disqualified-officers/natural/abc123" not in paths(router)
    router.routes["/company/" + NUMBER] = (200, {**profile(), "company_name": "OTHER LTD"}, None)
    router.routes["/company/" + NUMBER + "/officers"] = (200, {
        "total_results": 1,
        "items": [{
            "name": "CORK, Martin, Mr.",
            "officer_role": "director",
            "date_of_birth": {"month": 4, "year": 1966},
        }],
    }, None)
    client._last_at.clear()
    confirmed = invoke("uk_check", {"query": NUMBER})
    confirmed_row = confirmed["sources"]["disqualified"]["rows"][0]
    assert confirmed_row["match"] == "exact"
    assert confirmed_row["match_ja"] == match.verdict_exact_ja
    assert confirmed["company"]["company_name"] == "OTHER LTD"
    assert confirmed["disqualified_exact_count"] == 1
    assert confirmed["disqualified_summary"].startswith("1 exact disqualification row is")


def test_exact_title_different_birth_is_left_out(runtime):
    items = [
        _dq_item("Martin CORK", f"19{50 + index}-03-01T00:00:00", f"b{index}")
        for index in range(5)
    ]
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 5,
        "items": items,
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(
        ["CORK, Martin"],
        client.Deadline(),
        True,
        [{"name": "CORK, Martin", "birth_month": 4, "birth_year": 1972, "officer_id": "o"}],
        "NORTHGLASS LIMITED",
    )
    assert found["rows"] == []
    assert found["fully_read"] is True
    assert "Dropped 5" in found["reason"]
    assert "birth month and year" in found["reason"]
    assert "not a finding that the officer is clear" in found["reason"]
    assert not any(path.startswith("/disqualified-officers/natural/") for path in paths(router))


def test_two_exact_charity_rows_say_why_details_were_skipped(runtime):
    charity_name = "ACME WIDGETS LIMITED"
    router = _sheet_named(charity_name)
    segment = urllib.parse.quote(charity_name, safe="")
    router.routes["/register/api/searchCharityName/" + segment] = (200, [
        {"charity_name": charity_name, "reg_charity_number": 202918, "group_subsid_suffix": 0, "reg_status": "R"},
        {"charity_name": charity_name, "reg_charity_number": 202919, "group_subsid_suffix": 0, "reg_status": "R"},
    ], None)
    use(router, runtime)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    body = invoke("uk_check", {"query": NUMBER})
    charity = body["sources"]["charity"]
    held = "More than one charity row matches this name, so charity details were not requested."
    assert body["ok"] is True
    assert charity["status"] == "read"
    assert charity["fully_read"] is True
    assert charity["rows_read"] == 2
    assert [row["detail_reason"] for row in charity["rows"]] == [held, held]
    assert not any("allcharitydetails" in call["path"] for call in router.calls)
    client._last_at.clear()
    named = invoke("uk_charity", {"name": charity_name})
    assert named["ok"] is True
    assert named["fully_read"] is True
    assert [row["detail_reason"] for row in named["rows"]] == [held, held]


def test_birth_true_is_not_a_month(runtime):
    router = Router()
    router.routes["/company/" + NUMBER + "/officers"] = (200, {
        "total_results": 1,
        "items": [{
            "name": "CORK, Martin",
            "officer_role": "director",
            "date_of_birth": {"month": True, "year": 1970},
        }],
    }, None)
    use(router, runtime)
    found = sources.fetch_officers(NUMBER, client.Deadline())
    assert found["rows"][0]["birth_month"] == ""
    assert found["rows"][0]["birth_year"] == 1970


def test_land_zero_does_not_claim_unread_pages(runtime):
    router = Router()
    router.routes["/landregistry/query"] = (200, {"results": {"bindings": []}}, None)
    use(router, runtime)
    body = invoke("uk_land", {"postcode": "EC1V 3AP"})
    assert body["ok"] is True
    assert body["fully_read"] is True
    assert body["rows"] == []
    assert "older pages" not in body["note"].lower()
    assert "Older sales" not in body["note"]
    assert "leaves the question open" in body["note"]
    assert "does not show that a company owns" in body["note"]
    assert "Contains HM Land Registry data © Crown copyright and database right 2021." in body["note"]
    assert "personal or non-commercial use" in body["note"]


def test_urllib_transport_discards_oversized_body(monkeypatch):
    import io
    import urllib.error

    payload = b"x" * (client.MAX_BYTES + 1)
    url = "https://landregistry.data.gov.uk/landregistry/query"

    class Huge:
        def __init__(self):
            self.status = 200
            self._sent = 0

        def geturl(self):
            return url

        def read(self, size):
            if self._sent >= len(payload):
                return b""
            chunk = payload[self._sent:self._sent + size]
            self._sent += len(chunk)
            return chunk

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Opener200:
        def open(self, request, timeout):
            return Huge()

    class Opener500:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(url, 500, "err", hdrs=None, fp=io.BytesIO(payload))

    monkeypatch.setattr(client, "build_opener", lambda: Opener200())
    with pytest.raises(client.ApiError) as caught:
        client.REAL_URLLIB_SEND(client.UrllibTransport(), "GET", url, {}, 3)
    assert caught.value.error_type == "too_large"
    assert "xxxx" not in caught.value.message
    monkeypatch.setattr(client, "build_opener", lambda: Opener500())
    with pytest.raises(client.ApiError) as caught:
        client.REAL_URLLIB_SEND(client.UrllibTransport(), "GET", url, {}, 3)
    assert caught.value.error_type == "too_large"
    assert "xxxx" not in caught.value.message


def test_number_404_does_not_claim_absence(runtime):
    router = Router()
    router.routes["/company/" + NUMBER] = (404, {"error": "missing"}, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    text = json.dumps(body)
    assert "Companies House returned 404 for this number." in text
    assert "does not exist" not in text.lower()
    assert "Try again later" not in body["next_step"]
    assert "404" in body["next_step"]
    assert body["sources"]["vat"]["status"] == "excluded"
    assert body["sources"]["vat"]["rows"] == []
    assert body["sources"]["vat"]["checked"] is False
    assert paths(router) == ["/company/" + NUMBER]


def test_vat_makes_no_request(runtime):
    router = Router()
    client.configure(transport=router)
    body = invoke("uk_vat", {})
    assert body["ok"] is True and body["checked"] is False and body["requests"] == {}
    assert router.calls == []
    assert "valid" not in body["message"].lower()


def test_requests_are_per_call(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)
    use(router, runtime)
    invoke("uk_search", {"name": "ACME LTD"})
    body = invoke("uk_search", {"name": "ACME TWO LTD"})
    assert body["requests"] == {CH: 1}
    assert len(router.calls) == 2


def test_unknown_args_and_hosts_rejected(runtime):
    router = Router()
    client.configure(transport=router)
    STATE["values"][client.CH_KEY] = KEY
    body = invoke("uk_check", {"query": "ACME LTD", "ch_limit": 9999})
    assert body["error_type"] == "bad_input"
    assert router.calls == []
    assert client.CH_LIMIT == 600
    for url in (
        "https://evil.example/search/companies",
        "https://169.254.169.254/latest",
        "https://192.0.2.1/landregistry/query",
        "http://landregistry.data.gov.uk/landregistry/query",
        "https://landregistry.data.gov.uk/data/ppi/address.json",
    ):
        with pytest.raises(client.ApiError) as caught:
            client.request_json(url, client.json_headers(), None)
        assert caught.value.error_type == "bad_input"
    assert router.calls == []


def test_error_kinds_distinct(runtime):
    seen = {}

    def one(status, body, final=None):
        router = Router()
        router.routes["/search/companies"] = (status, body, final)
        use(router, runtime)
        client._last_at.clear()
        return invoke("uk_search", {"name": "ACME LTD"})

    auth = one(401, {"error": "no"})
    permission = one(403, {"error": "no"})
    rate = one(429, {"error": "no"})
    upstream = one(500, {"error": "no"})
    html = one(200, b"<html>no</html>")
    redirect = one(302, b"", "https://evil.example/stolen")
    kinds = [auth, permission, rate, upstream, html, redirect]
    assert [item["error_type"] for item in kinds] == ["auth", "permission", "rate", "upstream", "bad_shape", "redirect"]
    steps = [item["next_step"] for item in kinds]
    assert len(set(steps)) == len(steps)
    assert KEY not in json.dumps(kinds)


def test_redirect_handler_and_empty_proxy(monkeypatch):
    assert client._NoRedirect().redirect_request(None, None, 302, "go", {}, "https://evil.example/") is None
    for name in ("http_proxy", "https_proxy", "ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "no_proxy", "NO_PROXY", "all_proxy", "ftp_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    opener = client.build_opener()
    names = [type(handler).__name__ for handler in opener.handlers]
    assert "_NoRedirect" in names
    assert "HTTPRedirectHandler" not in names
    assert "ProxyHandler" not in names


def test_local_cap_and_secret_reread(runtime):
    now = runtime.now
    client._ch_times[:] = [now] * 600
    with pytest.raises(client.ApiError) as caught:
        client._acquire(CH, None)
    assert caught.value.error_type == "local_rate"
    client._ch_times.clear()
    client._last_at.clear()
    router = Router()
    router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)
    client.configure(transport=router)
    STATE["values"][client.CH_KEY] = KEY
    invoke("uk_search", {"name": "ACME LTD"})
    STATE["values"][client.CH_KEY] = "second-house-test-key"
    invoke("uk_search", {"name": "ACME LTD"})
    assert STATE["calls"].count(client.CH_KEY) >= 2
    first = router.calls[0]["headers"]["Authorization"]
    second = router.calls[1]["headers"]["Authorization"]
    assert first != second
    assert KEY not in second


def test_unscoped_secret_is_not_a_missing_key(runtime):
    STATE["raise"] = "unscoped"
    body = invoke("uk_search", {"name": "ACME LTD"})
    text = json.dumps(body)
    assert body["error_type"] == "scope"
    assert "sentinel-unscoped-SECRETVALUE12" not in text
    STATE["raise"] = "boom"
    body = invoke("uk_search", {"name": "ACME LTD"})
    assert body["error_type"] == "scope"
    assert "sentinel-boom-SECRETVALUE12" not in json.dumps(body)


def test_short_key_is_not_sent(runtime):
    router = Router()
    client.configure(transport=router)
    STATE["values"][client.CH_KEY] = "short"
    body = invoke("uk_search", {"name": "ACME LTD"})
    assert body["error_type"] == "auth"
    assert router.calls == []
    assert "short" not in json.dumps(body)


def test_transport_exception_hides_the_key(runtime):
    class Boom:
        def send(self, method, url, headers, timeout):
            raise RuntimeError("leaked " + KEY)

    client.configure(transport=Boom())
    STATE["values"][client.CH_KEY] = KEY
    body = invoke("uk_search", {"name": "ACME LTD"})
    assert body["error_type"] == "down"
    assert KEY not in json.dumps(body)


def test_read_tools_do_not_write(tmp_path, runtime):
    marker = tmp_path / "marker.txt"
    marker.write_text("same")
    before = marker.stat().st_ino, marker.read_bytes()
    watch.set_data_dir(tmp_path)
    router = Router()
    client.configure(transport=router)
    invoke("uk_vat", {})
    invoke("uk_search", {"name": "ACME LTD"})
    assert not (tmp_path / "watch.json").exists()
    assert (marker.stat().st_ino, marker.read_bytes()) == before


def test_watch_baseline_then_one_diff_and_keeps_failure(tmp_path, runtime):
    watch.set_data_dir(tmp_path)
    added = watch.add_number(NUMBER)
    assert added["ok"] is True
    saved = json.loads((tmp_path / "watch.json").read_text())
    assert saved["numbers"][NUMBER] is None
    router = sheet_router()
    use(router, runtime)
    assert watch.run_watch(tmp_path) == ""
    stored = json.loads((tmp_path / "watch.json").read_text())["numbers"][NUMBER]
    assert stored["status"] == "active"
    assert stored["officers"] == ["1 | CORK, Martin | director | 2020-01-01"]
    router.routes["/company/" + NUMBER] = (200, {**profile(), "company_status": "liquidation"}, None)
    text = watch.run_watch(tmp_path)
    assert text == f"{NUMBER} status: active -> liquidation"
    router.routes["/company/" + NUMBER] = (500, {"error": "down"}, None)
    before = (tmp_path / "watch.json").read_bytes()
    failed = watch.run_watch(tmp_path)
    assert f"{NUMBER} status: unread (upstream), snapshot kept" in failed
    assert (tmp_path / "watch.json").read_bytes() == before


def test_partial_read_keeps_snapshot(tmp_path, runtime):
    watch.set_data_dir(tmp_path)
    watch.add_number(NUMBER)
    router = sheet_router()
    use(router, runtime)
    watch.run_watch(tmp_path)
    before = (tmp_path / "watch.json").read_bytes()
    router.routes["/company/" + NUMBER] = (200, {**profile(), "company_status": ""}, None)
    text = watch.run_watch(tmp_path)
    assert "unread (partial)" in text
    assert (tmp_path / "watch.json").read_bytes() == before


def test_watch_cap_corrupt_and_symlink(tmp_path):
    watch.set_data_dir(tmp_path)
    for index in range(20):
        assert watch.add_number(f"{index + 1:08d}")["ok"] is True
    refused = watch.add_number("00000021")
    assert refused["ok"] is False and "20" in refused["message"]
    raw = (tmp_path / "watch.json").read_bytes()
    (tmp_path / "watch.json").write_text("{")
    message = watch.run_watch(tmp_path)
    assert "not valid JSON" in message
    assert (tmp_path / "watch.json").read_text() == "{"
    (tmp_path / "watch.json").unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text(raw.decode())
    (tmp_path / "watch.json").symlink_to(target)
    blocked = watch.add_number(NUMBER)
    assert blocked["ok"] is False
    assert target.read_text() == raw.decode()


def test_safe_write_symlink_hardlink_dotdot(tmp_path):
    with pytest.raises(WriteRefused):
        write_bytes(tmp_path, "../escape.txt", b"no", create=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(WriteRefused):
        write_bytes(tmp_path, "alias/watch.json", b"no", create=True)
    write_bytes(tmp_path, "real.txt", b"one", create=True)
    link = tmp_path / "real.json"
    link.symlink_to(tmp_path / "real.txt")
    with pytest.raises(WriteRefused):
        write_bytes(tmp_path, "real.json", b"no", create=True)
    os.link(tmp_path / "real.txt", tmp_path / "hard.txt")
    with pytest.raises(WriteRefused):
        write_bytes(tmp_path, "real.txt", b"two", create=True, cut_hardlink=False)
    assert (tmp_path / "real.txt").read_bytes() == b"one"
    assert stat.S_ISREG((tmp_path / "real.txt").stat().st_mode)


def test_match_and_names():
    assert match.compare("TESCO", "TESCO PLC") == "possible"
    assert match.compare(NAME, NAME) == "exact"
    assert match.compare("SMITH & JONES LIMITED", "SMITH JONES LIMITED") == "possible"
    assert match.compare("SMITH & JONES LIMITED", "smith & jones limited") == "exact"
    assert match.compare("SMITH  &   JONES LIMITED", "SMITH & JONES LIMITED") == "exact"
    assert match.compare("SMITH-JONES LIMITED", "SMITH JONES LIMITED") == "possible"
    assert match.compare("O'BRIEN LIMITED", "OBRIEN LIMITED") == "possible"
    assert match.compare("ACME LTD.", "ACME LTD") == "possible"
    assert match.company_number("TESCO") is None
    assert match.company_number(" 00445790 ") == "00445790"
    assert match.postcode("ec1v3ap") == "EC1V 3AP"
    assert match.verdict("exact")[1] == match.verdict_exact_ja
    assert match.verdict("possible")[1] == match.verdict_possible_ja


def test_schedule_is_local_and_silent(tmp_path):
    calls = []

    def run(args):
        calls.append(list(args))
        if args[0] == "list":
            return public_cron.Cmd(0, "No scheduled jobs\n", "")
        return public_cron.Cmd(0, "Created job: abc123\n", "")

    code, text = cli.schedule_job(public_cron.Store(run), "telegram", home=tmp_path)
    assert code == 1 and calls == []
    assert "local only" in text
    code, text = cli.schedule_job(public_cron.Store(run), "local", home=tmp_path)
    assert code == 0
    created = calls[-1]
    assert created[0] == "create"
    assert "[SILENT]" in created[2]
    assert "say nothing" not in created[2].lower()
    assert "output nothing" not in created[2].lower()
    assert "--no-agent" in created and "local" in created and "uk-companies-watch.py" in created
    assert (tmp_path / "scripts" / "uk-companies-watch.py").is_file()
    code, again = cli.schedule_job(public_cron.Store(lambda args: public_cron.Cmd(0, "  abc123 [active]\n    Name: uk-companies-watch\n    Schedule: 0 8 * * *\n    Deliver: local\n", "")), "local", home=tmp_path)
    assert code == 0 and "already scheduled" in again
    assert "Created" not in again


def test_gone_sentence_and_profile_prefix(tmp_path):
    store = public_cron.Store(lambda args: public_cron.Cmd(0, "No scheduled jobs\n", ""))
    code, text = cli.job_status(store)
    assert code == 0
    assert text == "No cron job named uk-companies-watch was found."
    code, text = cli.unschedule_job(store, home=tmp_path)
    assert code == 0
    assert text == "No cron job named uk-companies-watch was found."
    prefix.remember_profile_name("harnessprof")

    def run(args):
        if args[0] == "list":
            return public_cron.Cmd(0, "No scheduled jobs\n", "")
        return public_cron.Cmd(0, "Created job: abc123\n", "")

    code, text = cli.schedule_job(public_cron.Store(run), "local", home=tmp_path)
    cron_lines = [line for line in text.splitlines() if "hermes" in line and "cron" in line]
    assert cron_lines
    assert all("-p harnessprof" in line for line in cron_lines)
    assert not any(line.startswith("hermes cron") for line in cron_lines)


def test_script_exits_when_plugin_missing(tmp_path):
    script = tmp_path / "uk-companies-watch.py"
    script.write_text(cli.SCRIPT_TEXT)
    compile(cli.SCRIPT_TEXT, "uk-companies-watch.py", "exec")
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path / "home")
    (tmp_path / "home").mkdir()
    done = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env, timeout=30)
    assert done.returncode == 1
    assert "not installed" in done.stderr


def test_no_hook_and_foreign_turn_unchanged():
    foreign = b"unrelated tool result: weather is fine\n"
    before = bytes(foreign)
    source = (ROOT / "__init__.py").read_text()
    assert "register_hook" not in source
    import importlib.util
    import types
    parent = "hermes_plugins"
    if parent not in sys.modules:
        namespace = types.ModuleType(parent)
        namespace.__path__ = []
        namespace.__package__ = parent
        sys.modules[parent] = namespace
    name = "hermes_plugins.uk_companies_testload"
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = name
    module.__path__ = [str(ROOT)]
    sys.modules[name] = module
    spec.loader.exec_module(module)

    class Ctx:
        def __init__(self):
            self.tools = []
            self.hooks = []
            self.profile_name = "default"

        def register_tool(self, **kwargs):
            self.tools.append(kwargs["name"])

        def register_cli_command(self, **kwargs):
            self.cli = kwargs["name"]

        def register_skill(self, *args, **kwargs):
            self.skill = args[0] if args else kwargs.get("name")

        def register_hook(self, *args, **kwargs):
            self.hooks.append((args, kwargs))

    ctx = Ctx()
    module.register(ctx)
    assert ctx.hooks == []
    assert foreign == before
    assert ctx.tools == [
        "uk_check", "uk_search", "uk_officers", "uk_psc", "uk_filings", "uk_charity", "uk_vat", "uk_land",
    ]


def test_unschedule_reports_remove_failure(tmp_path):
    def run(args):
        if args[0] == "list":
            return public_cron.Cmd(
                0,
                "  abc123 [active]\n    Name: uk-companies-watch\n    Schedule: 0 8 * * *\n    Deliver: local\n",
                "",
            )
        return public_cron.Cmd(1, "", "remove failed")

    code, text = cli.unschedule_job(public_cron.Store(run), home=tmp_path)
    assert code == 1
    assert "Removed cron job" not in text
    assert "abc123" in text
    assert "not removed" in text

    class Bare:
        def job_by_name(self, name):
            return {"id": "", "name": name}

        def remove_job(self, job_id):
            raise AssertionError(job_id)

    code, text = cli.unschedule_job(Bare())
    assert code == 1
    assert "Removed" not in text


def test_error_body_is_not_a_zero_count(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {"error": "partial", "items": [], "total_results": 0}, None)
    use(router, runtime)
    body = invoke("uk_search", {"name": "ACME LTD"})
    assert body["ok"] is False
    assert body["error_type"] == "upstream"
    assert "0 companies" not in body["message"]
    assert "error status" not in body["next_step"]
    assert "HTTP 200" in body["next_step"]
    router.routes["/search/companies"] = (200, {"errors": ["partial"], "items": [{"title": "ACME LTD"}], "company_name": "ACME LTD"}, None)
    client._last_at.clear()
    body = invoke("uk_search", {"name": "ACME LTD"})
    assert body["ok"] is False
    assert "ACME LTD" not in body["message"]
    router.routes["/company/" + NUMBER] = (200, {"error": "partial", "company_name": NAME, "items": []}, None)
    client._last_at.clear()
    body = invoke("uk_check", {"query": NUMBER})
    assert body["ok"] is False and body["resolved"] is False
    assert NAME not in json.dumps(body["company"])


def test_profile_keeps_overdue_and_charges(runtime):
    router = sheet_router()
    router.routes["/company/" + NUMBER] = (200, {
        **profile(),
        "accounts": {"next_accounts": {"overdue": True}},
        "confirmation_statement": {"overdue": False},
        "has_charges": False,
        "links": {"charges": f"/company/{NUMBER}/charges"},
    }, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    company = body["company"]
    assert company["accounts_overdue"] is True
    assert company["confirmation_statement_overdue"] is False
    assert company["has_charges"] is True
    assert "does not request the charges list" in company["has_charges_reason"]
    assert company["charges_url"] == f"https://find-and-update.company-information.service.gov.uk/company/{NUMBER}/charges"
    assert company["accounts_overdue_reason"] == ""
    router.routes["/company/" + NUMBER] = (200, {
        **profile(),
        "has_charges": True,
    }, None)
    client._last_at.clear()
    blank = invoke("uk_check", {"query": NUMBER})
    assert blank["company"]["has_charges"] == ""
    assert "stays blank" in blank["company"]["has_charges_reason"]
    router.routes["/company/" + NUMBER] = (200, {
        **profile(),
        "links": {"charges": f"/company/{NUMBER}/charges"},
    }, None)
    client._last_at.clear()
    linked = invoke("uk_check", {"query": NUMBER})
    assert linked["company"]["has_charges"] is True
    assert "omitted has_charges" in linked["company"]["has_charges_reason"]


def test_charity_detail_keeps_trustees_and_income(runtime):
    router = Router()
    router.routes["/register/api/allcharitydetailsV2/202918/0"] = (200, {
        "charity_name": "OXFAM",
        "reg_charity_number": 202918,
        "group_subsid_suffix": 0,
        "reg_status": "R",
        "charity_type": "Trust",
        "latest_income": 100,
        "latest_expenditure": 90,
        "insolvent": False,
        "in_administration": False,
        "trustee_names": [{"trustee_name": "A Person"}],
    }, None)
    client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    body = invoke("uk_charity", {"number": "202918", "suffix": "0"})
    row = body["rows"][0]
    assert body["ok"] is True
    assert row["reg_status"] == "R"
    assert row["latest_income"] == 100
    assert row["latest_expenditure"] == 90
    assert row["trustee_names"] == ["A Person"]
    assert row["insolvent"] is False
    assert row["trustee_reason"] == ""
    assert paths(router) == ["/register/api/allcharitydetailsV2/202918/0"]
    router.routes["/register/api/allcharitydetailsV2/202918/0"] = (404, {"error": "missing"}, None)
    client._last_at.clear()
    missing = invoke("uk_charity", {"number": "202918"})
    assert missing["ok"] is False
    assert "Try again later" not in missing["next_step"]
    assert "404" in missing["next_step"]


def test_unschedule_removes_the_script(tmp_path):
    def run(args):
        if args[0] == "list":
            return public_cron.Cmd(0, "No scheduled jobs\n", "")
        return public_cron.Cmd(0, "Created job: abc123\n", "")

    assert cli.schedule_job(public_cron.Store(run), "local", home=tmp_path)[0] == 0
    script = tmp_path / "scripts" / "uk-companies-watch.py"
    assert script.is_file()

    def run_remove(args):
        if args[0] == "list":
            return public_cron.Cmd(
                0,
                "  abc123 [active]\n    Name: uk-companies-watch\n    Schedule: 0 8 * * *\n    Deliver: local\n",
                "",
            )
        return public_cron.Cmd(0, "", "")

    code, text = cli.unschedule_job(public_cron.Store(run_remove), home=tmp_path)
    assert code == 0
    assert "Removed cron job abc123" in text
    assert "Removed scripts/uk-companies-watch.py." in text
    assert not script.exists()


def test_failed_schedule_removes_the_script(tmp_path):
    def run(args):
        if args[0] == "list":
            return public_cron.Cmd(0, "No scheduled jobs\n", "")
        raise RuntimeError("create failed")

    code, text = cli.schedule_job(public_cron.Store(run), "local", home=tmp_path)
    assert code == 1
    assert "create failed" in text
    assert "Removed scripts/uk-companies-watch.py." in text
    assert not (tmp_path / "scripts" / "uk-companies-watch.py").exists()


def test_skill_and_readme_mention_approval_text():
    skill = (ROOT / "skills" / "uk-companies" / "SKILL.md").read_text()
    readme = (ROOT / "README.md").read_text()
    schemas = (ROOT / "schemas.py").read_text()
    assert "This plugin does not set approval-card text." in skill
    assert "Before any approval card" not in skill
    body = skill.split("---", 2)[2]
    assert body.find("tool_search") < body.find("web_search")
    assert "This plugin does not set approval-card text." in readme
    assert "lists the company number and the morning job" not in readme
    assert "numbered list" not in readme
    assert "numbered list" not in schemas
    assert "candidates array" in readme
    assert 'hermes plugins install "file://$PWD" --enable' in readme
    assert "hermes plugins enable uk-companies" in readme
    assert "hermes uk-companies call uk_vat" in readme
    assert "hermes cron remove <id>" in readme
    assert "0 8 * * *" in readme
    assert "State a ban or a notice only when the row says exact." not in readme
    assert "State a ban or a notice only when the row says exact." not in skill
    assert "is a title match" not in readme
    assert "is a title match" not in skill
    assert "only when that row has a birth month" not in readme
    assert "State a ban only when that row's match is exact." not in readme
    assert "State a ban only when that row's match is exact." in skill
    assert "disqualified_until` is after today" in readme
    assert "disqualified_until` is after today" in skill
    assert "matched_officers" in readme
    assert "matched_officers" in skill
    assert "A compatibility character stays as written." in readme
    assert "A compatibility character stays as written." in skill
    assert "excluded` and no row on every sheet" in readme
    assert "excluded` and no row on every sheet" in skill
    assert "Gazette was not queried" in skill
    assert "receive the registered name only" in skill
    assert "An exact notice can sit in that block" not in skill
    assert "plugin-data/uk-companies/watch.json" in readme
    assert "hermes plugins remove" in readme
    assert "persons with significant control" in readme
    assert "The error names which refusal applied." in readme
    assert "Copy `category` and `excerpt`" not in readme
    assert "Copy `category` and `excerpt`" in skill
    assert "already enabled" in readme
    assert "before this company's incorporation date" in skill or "before the company's incorporation date" in skill
    assert "Contains HM Land Registry data © Crown copyright and database right 2021." in readme
    assert "personal or non-commercial use" in readme
    assert "official transaction URL" not in schemas
    assert "SPARQL DESCRIBE" in schemas
    assert "肩書の一致で" not in readme
    front = skill.split("---", 2)[1]
    description = front.split("description:", 1)[1].strip().strip('"')
    assert len(description) <= 223
    assert "requires_" not in front
    source = (ROOT / "__init__.py").read_text()
    assert "description=_SKILL_DESCRIPTION" in source
    assert description in source


def test_descriptions_use_ordinary_words():
    text = (ROOT / "schemas.py").read_text()
    for word in ("company", "officer", "charity", "postcode"):
        assert word in text.lower()
    tree = ast.parse(text)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            fields = {}
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and isinstance(value, ast.Constant) and isinstance(value.value, str):
                    fields[key.value] = value.value
            if fields.get("name", "").startswith("uk_") and "description" in fields:
                count += 1
                assert not fields["description"].startswith("Read-only")
                assert len(fields["description"]) <= 1024
    assert count == 8


def test_audit_count_mismatch_is_not_a_read():
    payload = {"sources": {"gazette": {"rows": [1], "rows_read": 9, "status": "read", "reason": ""}}}
    sheet._drop_private(payload)
    assert payload["sources"]["gazette"]["status"] == "error"
    assert payload["sources"]["gazette"]["rows_read"] == 1


def test_no_runtime_download_strings():
    banned = ("pip install", "uvx ", "npx ", "urlretrieve")
    for path in ROOT.glob("*.py"):
        text = path.read_text()
        for item in banned:
            assert item not in text


BAN_TODAY = date(2026, 11, 3)


def _officer(month=4, year=1966, ended="", officer_id="", appointed="2020-01-01"):
    return {
        "name": "CORK, Martin",
        "birth_month": month,
        "birth_year": year,
        "appointed_on": appointed,
        "ended_on": ended,
        "officer_id": officer_id,
    }


def _order(names, start, end):
    return {"company_names": names, "disqualified_from": start, "disqualified_until": end}


def _ban_row(orders, permissions=None, month=4, year=1966):
    return {
        "birth_month": month,
        "birth_year": year,
        "disqualifications": orders,
        "permissions_to_act": permissions or [],
    }


def _perm(names, granted, expires):
    return {"company_names": names, "granted_on": granted, "expires_on": expires}


# One row per disqualified-officer search page. Person decisions stay in BAN_TABLE.
DISQUALIFIED_PAGE_TABLE = [
    {
        "name": "complete-zero",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 0, "items": []},
        "status": "read",
        "fully_read": True,
        "matches": ["unchecked"],
        "block_reason": "not a finding that the person is clear",
        "row_reason": "not a finding that the person is clear",
        "profiles": 0,
        "url": "https://find-and-update.company-information.service.gov.uk/search/disqualified-officers",
    },
    {
        "name": "non-dict-ignored",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 0, "items": [1, "row"]},
        "status": "read",
        "fully_read": True,
        "matches": ["unchecked"],
        "block_reason": "not a finding that the person is clear",
        "row_reason": "not a finding that the person is clear",
        "profiles": 0,
        "url": "https://find-and-update.company-information.service.gov.uk/search/disqualified-officers",
    },
    {
        "name": "total-zero-with-rows",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 0, "items": [{"title": "Martin CORK", "links": {"self": "/disqualified-officers/natural/abc"}}]},
        "status": "read",
        "fully_read": False,
        "matches": ["unchecked"],
        "titles": ["Martin CORK"],
        "block_reason": "not fully checked",
        "row_reason": "total was 0",
        "profiles": 0,
        "url": "",
    },
    {
        "name": "later-unread",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 5, "items": []},
        "status": "read",
        "fully_read": False,
        "matches": ["unchecked"],
        "block_reason": "not fully checked",
        "row_reason": "Later rows were not read.",
        "profiles": 0,
        "url": "",
    },
    {
        "name": "total-missing",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"items": []},
        "status": "read",
        "fully_read": False,
        "matches": ["unchecked"],
        "block_reason": "not fully checked",
        "row_reason": "did not include a total",
        "profiles": 0,
        "url": "",
    },
    {
        "name": "total-lower",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 1, "items": [{"title": "Martin Alan CORK"}, {"title": "Martin CORK"}]},
        "status": "read",
        "fully_read": False,
        "matches": ["unchecked", "possible", "possible"],
        "block_reason": "not fully checked",
        "row_reason": "lower than the rows",
        "profiles": 0,
        "url": "",
    },
    {
        "name": "complete-possible",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 1, "items": [{"title": "Martin Alan CORK"}]},
        "status": "read",
        "fully_read": True,
        "matches": ["possible"],
        "block_reason": "",
        "profiles": 0,
    },
    {
        "name": "complete-dropped",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 1, "items": [{"title": "Ernest Paul NORTON"}]},
        "status": "read",
        "fully_read": True,
        "matches": [],
        "block_reason": "Dropped 1",
        "profiles": 0,
    },
    {
        "name": "no-names-complete",
        "names": [],
        "complete": True,
        "payload": None,
        "status": "read",
        "fully_read": True,
        "matches": [],
        "block_reason": "no officer name",
        "profiles": 0,
    },
    {
        "name": "no-names-gap",
        "names": [],
        "complete": False,
        "payload": None,
        "status": "not_read",
        "fully_read": False,
        "matches": [],
        "block_reason": "were not fully read",
        "profiles": 0,
    },
    {
        "name": "items-missing",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 0},
        "error": "items list",
    },
    {
        "name": "items-not-list",
        "names": ["CORK, Martin"],
        "complete": True,
        "payload": {"total_results": 0, "items": {}},
        "error": "items list",
    },
]


# One row per input kind for the disqualification decision. Today is 2026-11-03.
BAN_TABLE = [
    ("birth-missing", [], "OTHER LTD", _ban_row([], month="", year=""), "possible", "month and year of birth", []),
    ("no-officer", [], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "possible", "No officer of this company has this name", []),
    ("officer-name-differs", [{"name": "OTHER, Ann", "birth_month": 4, "birth_year": 1966}], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "possible", "No officer of this company has this name", []),
    ("birth-differs", [_officer(1, 1970)], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "possible", "do not match an officer", []),
    ("company-missing", [_officer()], "OTHER LTD", _ban_row([]), "possible", "did not include a company name", ["",]),
    ("ltd-versus-limited", [_officer()], "OTHER LIMITED", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "exact", "", [""]),
    ("until-past", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2020-01-01")]), "possible", "disqualified_until", [""]),
    ("in-force", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "exact", "", [""]),
    ("surname-title-datetime", [{
        "name": "CORK, Martin, Mr.",
        "birth_month": 4,
        "birth_year": 1966,
        "appointed_on": "2020-01-01",
        "ended_on": "",
        "officer_id": "",
    }], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01T00:00:00", "2027-01-01T00:00:00")]), "exact", "", [""]),
    ("until-today", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2026-11-03")]), "possible", "disqualified_until", [""]),
    ("from-future", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2026-11-04", "2030-01-01")]), "possible", "disqualified_from", [""]),
    ("until-missing", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "")]), "possible", "YYYY-MM-DD", [""]),
    ("from-missing", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "", "2027-01-01")]), "possible", "YYYY-MM-DD", [""]),
    ("bad-date", [_officer()], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015/01/01", "2027-01-01")]), "possible", "YYYY-MM-DD", [""]),
    ("resigned-birth-only", [_officer(1, 1970), _officer(ended="2024-01-01")], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "exact", "", ["2024-01-01"]),
    ("two-officers-same-birth", [_officer(), _officer(ended="2024-01-01")], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "possible", "shared officer id", ["", "2024-01-01"]),
    ("two-roles-one-officer-id", [_officer(officer_id="abc"), _officer(officer_id="abc", appointed="2021-06-01")], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "exact", "", ["", ""]),
    ("two-officer-ids", [_officer(officer_id="abc"), _officer(officer_id="def", ended="2024-01-01")], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "possible", "shared officer id", ["", "2024-01-01"]),
    ("company-only-on-expired", [_officer()], "OTHER LTD", _ban_row([
        _order(["OTHER LTD"], "2015-01-01", "2020-01-01"),
        _order(["OTHER CO"], "2015-01-01", "2027-01-01"),
    ]), "possible", "disqualified_until", [""]),
    ("company-on-current", [_officer()], "OTHER LTD", _ban_row([
        _order(["OTHER CO"], "2015-01-01", "2020-01-01"),
        _order(["OTHER LTD"], "2015-01-01", "2027-01-01"),
    ]), "exact", "", [""]),
    ("permission-current", [_officer()], "OTHER LTD", _ban_row(
        [_order(["OTHER LTD"], "2015-01-01", "2027-01-01")],
        [_perm(["OTHER LTD"], "2016-01-01", "2027-06-01")],
    ), "possible", "permission to act", [""]),
    ("permission-other-company", [_officer()], "OTHER LTD", _ban_row(
        [_order(["OTHER LTD"], "2015-01-01", "2027-01-01")],
        [_perm(["OTHER CO"], "2016-01-01", "2027-06-01")],
    ), "exact", "", [""]),
    ("permission-expired", [_officer()], "OTHER LTD", _ban_row(
        [_order(["OTHER LTD"], "2015-01-01", "2027-01-01")],
        [_perm(["OTHER LTD"], "2016-01-01", "2020-01-01")],
    ), "exact", "", [""]),
    ("permission-expires-today", [_officer()], "OTHER LTD", _ban_row(
        [_order(["OTHER LTD"], "2015-01-01", "2027-01-01")],
        [_perm(["OTHER LTD"], "2016-01-01", "2026-11-03")],
    ), "possible", "permission to act", [""]),
    ("permission-no-end", [_officer()], "OTHER LTD", _ban_row(
        [_order(["OTHER LTD"], "2015-01-01", "2027-01-01")],
        [_perm(["OTHER LTD"], "2016-01-01", "")],
    ), "possible", "permission to act", [""]),
    ("permission-not-started", [_officer()], "OTHER LTD", _ban_row(
        [_order(["OTHER LTD"], "2015-01-01", "2027-01-01")],
        [_perm(["OTHER LTD"], "2026-12-01", "2028-01-01")],
    ), "exact", "", [""]),
    ("company-roman-numeral", [_officer()], "ELIZABETH II LTD", _ban_row([_order(["ELIZABETH Ⅱ LTD"], "2015-01-01", "2027-01-01")]), "possible", "No order names this company", [""]),
    ("officer-case-and-space", [{"name": "cork,  martin", "birth_month": 4, "birth_year": 1966, "appointed_on": "", "ended_on": ""}], "OTHER LTD", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "exact", "", [""]),
    ("company-ampersand", [_officer()], "SMITH JONES LIMITED", _ban_row([_order(["SMITH & JONES LIMITED"], "2015-01-01", "2027-01-01")]), "possible", "No order names this company", [""]),
    ("ended-and-undated", [_officer()], "OTHER LTD", _ban_row([
        _order(["OTHER LTD"], "2015-01-01", "2020-01-01"),
        _order(["OTHER LTD"], "", "2027-01-01"),
    ]), "possible", "today's date", [""]),
    ("company-case-fold", [_officer()], "other  ltd", _ban_row([_order(["OTHER LTD"], "2015-01-01", "2027-01-01")]), "exact", "", [""]),
]


NAME_TABLE = [
    ("case", "ACME LTD", "acme ltd", "exact"),
    ("inner-space", "ACME  LTD", "acme ltd", "exact"),
    ("outer-space", "  ACME LTD  ", "acme ltd", "exact"),
    ("ampersand", "SMITH & JONES LIMITED", "SMITH JONES LIMITED", "possible"),
    ("roman-numeral", "ELIZABETH Ⅱ LTD", "ELIZABETH II LTD", "possible"),
    ("ligature", "ﬁrm LTD", "firm LTD", "possible"),
    ("empty-left", "", "ACME", "unchecked"),
    ("none-left", None, "ACME", "unchecked"),
    ("empty-right", "ACME", "", "unchecked"),
    ("hyphen", "ACME-LTD", "ACME LTD", "possible"),
    ("ltd-limited", "ACME LTD", "ACME LIMITED", "possible"),
]


HTTP_TABLE = [
    ("ch-401", "https://api.company-information.service.gov.uk/search/companies", 401, {"error": "no"}, "auth", "API key", "refused this key"),
    ("ch-403", "https://api.company-information.service.gov.uk/search/companies", 403, {"error": "no"}, "permission", "refused this key", "sent no API key"),
    ("ch-429", "https://api.company-information.service.gov.uk/search/companies", 429, {"error": "no"}, "rate", "Companies House", "sent no API key"),
    ("ch-200-error", "https://api.company-information.service.gov.uk/search/companies", 200, {"error": "partial", "items": []}, "upstream", "HTTP 200", "error status"),
    ("land-403", "https://landregistry.data.gov.uk/landregistry/query", 403, {"error": "no"}, "upstream", "sent no API key", "refused this key"),
    ("land-401", "https://landregistry.data.gov.uk/landregistry/query", 401, {"error": "no"}, "upstream", "sent no API key", "rejected the API key"),
    ("gazette-403", "https://www.thegazette.co.uk/insolvency/notice/data.json", 403, {"error": "no"}, "upstream", "sent no API key", "refused this key"),
    ("gazette-429", "https://www.thegazette.co.uk/insolvency/notice/data.json", 429, {"error": "no"}, "rate", "this host", "Companies House"),
    ("charity-403", "https://api.charitycommission.gov.uk/register/api/searchCharityName/OXFAM", 403, {"error": "no"}, "permission", "refused this key", "sent no API key"),
    ("charity-429", "https://api.charitycommission.gov.uk/register/api/searchCharityName/OXFAM", 429, {"error": "no"}, "rate", "this host", "Companies House"),
    ("land-500", "https://landregistry.data.gov.uk/landregistry/query", 500, {"error": "no"}, "upstream", "error status", "refused this key"),
    ("ch-404", "https://api.company-information.service.gov.uk/company/12345678", 404, {"error": "no"}, "upstream", "404", "Try again later"),
]


@pytest.mark.parametrize("case", DISQUALIFIED_PAGE_TABLE, ids=[row["name"] for row in DISQUALIFIED_PAGE_TABLE])
def test_disqualified_page_input_table(runtime, case):
    router = Router()
    if case.get("payload") is not None:
        router.routes["/search/disqualified-officers"] = (200, case["payload"], None)
    use(router, runtime)
    if case.get("error"):
        with pytest.raises(client.ApiError) as caught:
            sources.fetch_disqualified(case["names"], client.Deadline(), case["complete"])
        assert case["error"] in caught.value.message
        return
    found = sources.fetch_disqualified(case["names"], client.Deadline(), case["complete"])
    assert found["status"] == case["status"]
    assert found["fully_read"] is case["fully_read"]
    assert [row["match"] for row in found["rows"]] == case["matches"]
    for row in found["rows"]:
        assert "match" in row and "match_ja" in row
        assert row["match_ja"] == match.verdict(row["match"])[1]
    if "titles" in case:
        assert [row["title"] for row in found["rows"]] == case["titles"]
    if "url" in case:
        assert found["rows"][0]["official_url"] == case["url"]
        if case["url"]:
            assert found["rows"][0]["url_reason"] == ""
        else:
            assert case["row_reason"] in found["rows"][0]["url_reason"]
    if case.get("row_reason"):
        assert case["row_reason"] in found["rows"][0]["reason"]
    if case.get("block_reason"):
        assert case["block_reason"] in found["reason"]
    else:
        assert found["reason"] == ""
    profiles = [path for path in paths(router) if path.startswith("/disqualified-officers/natural/")]
    assert len(profiles) == case["profiles"]


@pytest.mark.parametrize("case", BAN_TABLE, ids=[row[0] for row in BAN_TABLE])
def test_ban_input_table(case):
    name, people, company, row, kind, reason, ended = case
    found = sources.classify_ban(people, "CORK, Martin", company, row, BAN_TODAY)
    assert found["match"] == kind
    if reason:
        assert reason in found["identity_reason"]
    else:
        assert found["identity_reason"] == ""
    assert [item["ended_on"] for item in found["matched_officers"]] == ended
    if kind == "exact":
        assert any(item["force"] == "in_force" and item["names_this_company"] for item in found["orders"])
        assert not any(item["blocks"] for item in found["permissions"])
        if len(found["matched_officers"]) > 1:
            ids = {item["officer_id"] for item in found["matched_officers"]}
            assert len(ids) == 1 and "" not in ids


@pytest.mark.parametrize("case", NAME_TABLE, ids=[row[0] for row in NAME_TABLE])
def test_legal_name_input_table(case):
    _name, left, right, kind = case
    assert match.compare(left, right) == kind


def test_identifier_fold_is_separate_from_legal_names():
    assert match.company_number("１２３４５６７８") == "12345678"
    assert match.compare("１２３４５６７８ LTD", "12345678 LTD") == "possible"
    assert match.company_number("445790") == "00445790"
    assert match.company_number("0445790") == "00445790"
    assert match.company_number("12345") is None
    assert match.person_query("CORK, Martin Alan, Mr.") == "Martin Alan Cork"
    assert match.person_compare("CORK, Martin Alan, Mr.", "Martin Alan CORK") == "exact"
    assert match.person_compare("Chol JON", "TAYLOR, Christopher Jon") == "possible"
    assert match.shares_surname_and_first("ALDEN, Peter James", "Peter James ALDEN")
    assert match.shares_surname_and_first("ALDEN, Peter James", "Ernest Paul NORTON") is False
    assert match.shares_surname("GARCIA, José", "Jose GARCIA")
    assert match.shares_surname("SMITH-JONES, Ann", "Ann SMITH JONES")
    assert match.shares_surname("ALDEN, James Peter", "Peter James ALDEN")
    assert match.shares_surname_and_first("CORK, Martin Alan, Mr.", "Martin Alexander CORK")
    assert match.shares_surname_and_first("HALE, Nora", "Claire HALE") is False
    assert match.shares_surname_and_first("VAN DER BERG, Jan", "Jan Peter Van Der Berg")
    assert match.shares_surname_and_first("ACME LIMITED", "ACME HOLDINGS LIMITED") is False
    assert match.person_query("OTHER LTD") == "OTHER LTD"
    assert sources._birth_parts("1907-08-09T00:00:00") == (8, 1907)
    assert sources._birth_parts("1904-05-06T00:00:00.000Z") == (5, 1904)
    assert sources._calendar_date("2031-04-18T00:00:00") == date(2031, 4, 18)


def test_name_order_is_exact(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 4,
        "items": [
            {
                "title": "Nora Claire HALE",
                "date_of_birth": "1972-04-18T00:00:00",
                "description": "Born on 18 April 1972",
                "links": {"self": "/disqualified-officers/natural/ExampleBanId000000000001"},
            },
            {
                "title": "Nora Jane HALE",
                "date_of_birth": "1972-04-18T00:00:00",
                "description": "Born on 18 April 1972",
                "links": {"self": "/disqualified-officers/natural/same-birth"},
            },
            {
                "title": "Nora Ellen HALE",
                "date_of_birth": "1988-01-02T00:00:00",
                "description": "Born on 2 January 1988",
                "links": {"self": "/disqualified-officers/natural/other-year"},
            },
            {
                "title": "Adam John NORTON",
                "date_of_birth": "1970-01-01T00:00:00",
                "description": "Born on 1 January 1970",
                "links": {"self": "/disqualified-officers/natural/adam"},
            },
        ],
    }, None)
    router.routes["/disqualified-officers/natural/ExampleBanId000000000001"] = (200, {
        "date_of_birth": "1972-04-18T00:00:00",
        "disqualifications": [{
            "company_names": ["NORTHGLASS LIMITED"],
            "disqualified_from": "2024-01-15",
            "disqualified_until": "2031-01-15",
        }],
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(
        ["HALE, Nora Claire, Ms."],
        client.Deadline(),
        True,
        [{
            "name": "HALE, Nora Claire, Ms.",
            "birth_month": 4,
            "birth_year": 1972,
            "appointed_on": "2019-03-01",
            "ended_on": "",
            "officer_id": "ExampleOfficerId000001",
        }],
        "NORTHGLASS LIMITED",
    )
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(router.calls[0]["url"]).query)["q"][0]
    assert sent == "Nora Claire Hale"
    exact = [row for row in found["rows"] if row["match"] == "exact"]
    assert len(exact) == 1
    assert exact[0]["title"] == "Nora Claire HALE"
    assert exact[0]["birth_month"] == 4
    assert exact[0]["birth_year"] == 1972
    assert exact[0]["matched_officers"][0]["name"] == "HALE, Nora Claire, Ms."
    assert "/disqualified-officers/natural/same-birth" not in paths(router)
    assert "/disqualified-officers/natural/other-year" not in paths(router)
    assert "/disqualified-officers/natural/adam" not in paths(router)
    assert all(row.get("title") != "Adam John NORTON" for row in found["rows"])
    assert all(row.get("title") != "Nora Ellen HALE" for row in found["rows"])
    assert "Dropped 2" in found["reason"]
    other = [row for row in found["rows"] if row.get("title") == "Nora Jane HALE"]
    assert other and other[0]["match"] == "possible"
    assert other[0]["birth_month"] == 4
    assert other[0]["birth_year"] == 1972
    assert "did not include a month and year" not in other[0]["birth_reason"]


def test_dir_flags_without_nofollow(monkeypatch):
    import safe_write
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    assert isinstance(safe_write._dir_flags(), int)
    assert isinstance(safe_write._create_flags(), int)


@pytest.mark.parametrize("case", HTTP_TABLE, ids=[row[0] for row in HTTP_TABLE])
def test_status_next_step_follows_the_host(runtime, case):
    _name, url, status, body, error_type, present, absent = case
    router = Router()
    router.routes[urllib.parse.urlsplit(url).path] = (status, body, None)
    client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
    with pytest.raises(client.ApiError) as caught:
        client.request_json(url, client.json_headers(), None)
    assert caught.value.error_type == error_type
    text = caught.value.message + " " + caught.value.next_step
    assert present in text
    assert absent not in text


def test_expired_ban_through_fetch_stays_possible(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 1,
        "items": [{"title": "Martin CORK", "date_of_birth": "1966-04-15T00:00:00", "links": {"self": "/disqualified-officers/natural/abc123"}}],
    }, None)
    router.routes["/disqualified-officers/natural/abc123"] = (200, {
        "date_of_birth": "1966-04-15T00:00:00",
        "disqualifications": [{
            "company_names": ["OTHER LTD"],
            "disqualified_from": "2015-01-01",
            "disqualified_until": "2020-01-01",
        }],
        "permissions_to_act": [{
            "company_names": ["OTHER CO"],
            "granted_on": "2016-01-01",
            "expires_on": "2017-01-01",
        }],
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(
        ["CORK, Martin"],
        client.Deadline(),
        True,
        [_officer()],
        "OTHER LTD",
    )
    row = found["rows"][0]
    assert row["match"] == "possible"
    assert row["match_ja"] == match.verdict_possible_ja
    assert "disqualified_until" in row["identity_reason"]
    assert row["disqualifications"][0]["force"] == "ended"
    assert row["matched_officers"][0]["name"] == "CORK, Martin"
    assert row["permissions_to_act"][0]["blocks"] is False


def test_corporate_disqualification_link_stays_possible(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 1,
        "items": [{"title": "OTHER LTD", "links": {"self": "/disqualified-officers/corporate/abc123"}}],
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(["OTHER LTD"], client.Deadline(), True, [_officer()], "OTHER LTD")
    row = found["rows"][0]
    assert row["match"] == "possible"
    assert "natural-officer" in row["url_reason"]
    assert "/disqualified-officers/corporate/abc123" not in paths(router)


def _dq_item(title, dob, pid):
    return {
        "title": title,
        "date_of_birth": dob,
        "description": "Born on x - Disqualified",
        "links": {"self": "/disqualified-officers/natural/" + pid},
    }


def test_fuzzy_page_drops_other_surnames(runtime):
    items = [_dq_item("Peter James ALDEN", "1971-03-09T00:00:00", "real")]
    items.append(_dq_item("Peter Alan ALDEN", "1971-03-09T00:00:00", "middle"))
    items.append(_dq_item("Peter Alan ALDEN", "1988-11-02T00:00:00", "other-year"))
    for index in range(18):
        items.append(_dq_item(f"Ernest Paul SURNAME{index}", "1950-01-01T00:00:00", f"n{index}"))
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 21,
        "items_per_page": 21,
        "start_index": 0,
        "items": items,
    }, None)
    router.routes["/disqualified-officers/natural/real"] = (200, {
        "date_of_birth": "1971-03-09T00:00:00",
        "disqualifications": [{
            "company_names": ["RIVERMILL FUELS LTD"],
            "disqualified_from": "2026-05-18",
            "disqualified_until": "2035-05-17",
        }],
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(
        ["ALDEN, Peter James"],
        client.Deadline(),
        True,
        [{"name": "ALDEN, Peter James", "birth_month": 3, "birth_year": 1971, "officer_id": "off"}],
        "RIVERMILL FUELS LIMITED",
    )
    titles = [row.get("title") for row in found["rows"]]
    assert titles == ["Peter James ALDEN", "Peter Alan ALDEN"]
    assert found["rows"][0]["match"] == "exact"
    assert found["rows"][0]["company_names"] == ["RIVERMILL FUELS LTD"]
    assert found["rows"][1]["match"] == "possible"
    assert found["fully_read"] is True
    assert "Dropped 19" in found["reason"]
    assert "/disqualified-officers/natural/middle" not in paths(router)
    assert len(json.dumps(found)) < 8000
    assert "not a finding that the officer is clear" in found["reason"]


def test_twenty_officer_fuzzy_pages_stay_under_model_cap(runtime):
    people = []
    for index in range(20):
        people.append({
            "name": f"PERSON{index}, Alice",
            "birth_month": 4,
            "birth_year": 1966,
            "officer_id": f"off{index}",
        })

    def search(url, _headers):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        surname = query.split()[-1]
        items = [_dq_item(query.upper(), "1966-04-15T00:00:00", "id-" + surname)]
        items.append(_dq_item(f"Alice Mae {surname.upper()}", "1966-04-15T00:00:00", "mid-" + surname))
        for junk in range(18):
            items.append(_dq_item(f"Ernest Paul OTHER{junk}", "1950-01-01T00:00:00", f"j-{surname}-{junk}"))
        return 200, {"total_results": 20, "items_per_page": 20, "start_index": 0, "items": items}, None

    router = Router()
    router.routes["/search/disqualified-officers"] = search
    for index in range(3):
        router.routes[f"/disqualified-officers/natural/id-Person{index}"] = (200, {
            "date_of_birth": "1966-04-15T00:00:00",
            "disqualifications": [{
                "company_names": ["OTHER CO"],
                "disqualified_from": "2015-01-01",
                "disqualified_until": "2027-01-01",
            }],
        }, None)
    use(router, runtime)
    found = sources.fetch_disqualified([person["name"] for person in people], client.Deadline(), True, people, "OTHER LTD")
    assert found["fully_read"] is False
    assert "stops at 3 profiles" in found["reason"]
    assert "Dropped 360" in found["reason"]
    assert len(found["rows"]) == 40
    assert len(json.dumps(found)) < 100000
    fourth = [row for row in found["rows"] if row.get("queried_name") == "PERSON3, Alice" and row.get("title") == "ALICE PERSON3"]
    assert fourth and fourth[0]["match"] == "possible"
    assert "already read 3" in fourth[0]["url_reason"]


def test_profile_cap_follows_birth_and_sets_fully_read(runtime):
    people = [
        {"name": "SMITH, John", "birth_month": 1, "birth_year": 1970, "officer_id": "a"},
        {"name": "JONES, David", "birth_month": 2, "birth_year": 1971, "officer_id": "b"},
        {"name": "BROWN, Peter", "birth_month": 3, "birth_year": 1972, "officer_id": "c"},
        {"name": "TAYLOR, Mark", "birth_month": 6, "birth_year": 1963, "officer_id": "d"},
    ]
    saved = {}

    def search(url, _headers):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
        if saved.get("mode") == "waste":
            births = {
                "John Smith": ("1950-01-01T00:00:00", "p1"),
                "David Jones": ("1951-02-01T00:00:00", "p2"),
                "Peter Brown": ("1952-03-01T00:00:00", "p3"),
                "Mark Taylor": ("1963-06-01T00:00:00", "p4"),
            }
        else:
            births = {
                "John Smith": ("1970-01-01T00:00:00", "p1"),
                "David Jones": ("1971-02-01T00:00:00", "p2"),
                "Peter Brown": ("1972-03-01T00:00:00", "p3"),
                "Mark Taylor": ("1963-06-01T00:00:00", "p4"),
            }
        dob, pid = births[query]
        return 200, {"total_results": 1, "items_per_page": 20, "start_index": 0, "items": [_dq_item(query.upper(), dob, pid)]}, None

    router = Router()
    router.routes["/search/disqualified-officers"] = search
    router.routes["/disqualified-officers/natural/p4"] = (200, {
        "date_of_birth": "1963-06-01",
        "disqualifications": [{
            "company_names": ["OTHER LTD"],
            "disqualified_from": "2015-01-01",
            "disqualified_until": "2027-01-01",
        }],
    }, None)
    for pid, dob in (("p1", "1970-01-01"), ("p2", "1971-02-01"), ("p3", "1972-03-01")):
        router.routes["/disqualified-officers/natural/" + pid] = (200, {
            "date_of_birth": dob,
            "disqualifications": [{
                "company_names": ["OTHER CO"],
                "disqualified_from": "2015-01-01",
                "disqualified_until": "2027-01-01",
            }],
        }, None)
    use(router, runtime)
    saved["mode"] = "waste"
    wasted = sources.fetch_disqualified([person["name"] for person in people], client.Deadline(), True, people, "OTHER LTD")
    taylor = [row for row in wasted["rows"] if row.get("queried_name") == "TAYLOR, Mark"]
    assert taylor and taylor[0]["match"] == "exact"
    assert wasted["fully_read"] is True
    assert paths(router).count("/disqualified-officers/natural/p4") == 1
    assert "/disqualified-officers/natural/p1" not in paths(router)
    client._last_at.clear()
    router.calls.clear()
    saved["mode"] = "spend"
    spent = sources.fetch_disqualified([person["name"] for person in people], client.Deadline(), True, people, "OTHER LTD")
    assert spent["fully_read"] is False
    assert "stops at 3 profiles" in spent["reason"]
    unread = [row for row in spent["rows"] if row.get("queried_name") == "TAYLOR, Mark"]
    assert unread and unread[0]["match"] == "possible"
    assert "already read 3" in unread[0]["url_reason"]
    assert "did not include a company name" not in unread[0]["identity_reason"]


def test_gazette_before_incorporation_stays_possible(runtime):
    router = Router()
    payload = gazette("ACME LTD")
    payload["entry"][0]["published"] = "2009-04-12T00:00:00"
    router.routes["/insolvency/notice/data.json"] = (200, payload, None)
    client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
    found = sources.fetch_gazette("ACME LTD", client.Deadline(), "2018-02-22")
    row = found["rows"][0]
    assert row["match"] == "possible"
    assert row["match_ja"] == match.verdict_possible_ja
    assert "incorporation" in row["match_reason"]
    later = gazette("ACME LTD")
    router.routes["/insolvency/notice/data.json"] = (200, later, None)
    client._last_at.clear()
    kept = sources.fetch_gazette("ACME LTD", client.Deadline(), "2018-02-22")
    assert kept["rows"][0]["match"] == "exact"
    assert kept["rows"][0]["match_reason"] == ""


def test_gazette_excerpt_and_missing_category(runtime):
    router = Router()
    payload = gazette("FOLDERPAGE LIMITED")
    payload["entry"][0]["category"] = [{"@term": "Appointment of Liquidators"}]
    payload["entry"][0]["content"] = "<p>" + ("word " * 80) + "</p>"
    router.routes["/insolvency/notice/data.json"] = (200, payload, None)
    client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
    found = sources.fetch_gazette("FOLDERPAGE LIMITED", client.Deadline())
    row = found["rows"][0]
    assert row["category"] == "Appointment of Liquidators"
    assert row["excerpt"].endswith("...")
    assert "<" not in row["excerpt"]
    assert len(row["excerpt"]) <= 243
    bare = gazette("FOLDERPAGE LIMITED")
    bare["entry"][0].pop("category", None)
    bare["entry"][0].pop("content", None)
    router.routes["/insolvency/notice/data.json"] = (200, bare, None)
    client._last_at.clear()
    missing = sources.fetch_gazette("FOLDERPAGE LIMITED", client.Deadline())
    assert missing["rows"][0]["category"] == ""
    assert "category term" in missing["rows"][0]["category_reason"]
    assert missing["rows"][0]["excerpt"] == ""
    assert "content" in missing["rows"][0]["excerpt_reason"]


_LONG_OFFICIAL = (
    "https://find-and-update.company-information.service.gov.uk/company/12345678/"
    + ("filing/" * 20)
)

# kind is number, name, or refuse. number rows resolve to one company number.
# name rows stay text. refuse rows call nothing.
QUERY_TABLE = [
    ("trailing-semicolon", "12345678;", "number", "12345678"),
    ("trailing-semicolons", "12345678;;", "number", "12345678"),
    ("fullwidth-semicolon", "12345678；", "number", "12345678"),
    ("semicolon-before-space", "  12345678 ; ", "number", "12345678"),
    ("trailing-comment", "12345678 # filed yesterday", "number", "12345678"),
    ("fullwidth-hash-comment", "12345678 ＃ note", "number", "12345678"),
    ("hash-without-space", "12345678#note", "name", "12345678#note"),
    ("hash-inside-name", "ACME # HOLDINGS LIMITED", "name", "ACME # HOLDINGS LIMITED"),
    ("name-semicolon", "ACME LTD;", "name", "ACME LTD"),
    ("name-internal-semicolon", "A; B LTD", "name", "A; B LTD"),
    ("japanese", "  株式会社テスト；", "name", "株式会社テスト"),
    ("fullwidth-digits", "１２３４５６７８", "number", "12345678"),
    ("lower-case-number", "sc123456", "number", "SC123456"),
    ("six-digits", "445790", "number", "00445790"),
    ("seven-digits", "0445790", "number", "00445790"),
    ("six-digits-semicolon", "445790;", "number", "00445790"),
    ("surrounding-space", "  12345678  ", "number", "12345678"),
    ("official-https", "https://find-and-update.company-information.service.gov.uk/company/12345678", "number", "12345678"),
    ("official-api-path", "https://api.company-information.service.gov.uk/company/12345678/officers?utm=1", "number", "12345678"),
    ("official-schemeless", "find-and-update.company-information.service.gov.uk/company/12345678", "number", "12345678"),
    ("long-official-url", _LONG_OFFICIAL, "number", "12345678"),
    ("http-link", "http://find-and-update.company-information.service.gov.uk/company/12345678", "refuse", ""),
    ("spaced-http", "  http://find-and-update.company-information.service.gov.uk/company/12345678 ; ", "refuse", ""),
    ("ftp-link", "ftp://find-and-update.company-information.service.gov.uk/company/12345678", "refuse", ""),
    ("userinfo-link", "https://user:pass@find-and-update.company-information.service.gov.uk/company/12345678", "refuse", ""),
    ("http-userinfo", "http://user:pass@find-and-update.company-information.service.gov.uk/company/12345678", "refuse", ""),
    ("other-host", "https://evil.example/company/12345678", "refuse", ""),
    ("official-search-page", "https://find-and-update.company-information.service.gov.uk/search?q=acme", "refuse", ""),
    ("dotdot-path", "https://find-and-update.company-information.service.gov.uk/company/12345678/../secret", "refuse", ""),
    ("odd-port", "https://find-and-update.company-information.service.gov.uk:8443/company/12345678", "refuse", ""),
    ("percent-number", "https://find-and-update.company-information.service.gov.uk/company/1234567%38", "refuse", ""),
    ("company-path-word", "https://find-and-update.company-information.service.gov.uk/company/ACME", "refuse", ""),
    ("too-long-name", "A" * 161, "refuse", ""),
    ("empty-semicolon", ";", "refuse", ""),
    ("non-string", None, "refuse", ""),
]

REFUSE_BITS = {
    "http-link": "http address was refused",
    "spaced-http": "http address was refused",
    "ftp-link": "address scheme was refused",
    "userinfo-link": "user name was refused",
    "http-userinfo": "user name was refused",
    "other-host": "host was refused",
    "official-search-page": "not a /company/ link",
    "dotdot-path": "path was refused",
    "odd-port": "port was refused",
    "percent-number": "company number link was refused",
    "company-path-word": "no company number",
    "too-long-name": "Pass a company name or number.",
    "empty-semicolon": "Pass a company name or number.",
    "non-string": "Pass a company name or number.",
}

POSTCODE_TABLE = [
    ("surrounding-space", "  ec1v 3ap ", "EC1V 3AP"),
    ("trailing-semicolon", "EC1V 3AP;", "EC1V 3AP"),
    ("fullwidth", "ＥＣ１Ｖ３ＡＰ", "EC1V 3AP"),
    ("trailing-comment", "EC1V 3AP # home", "EC1V 3AP"),
    ("lower-case", "ec1v3ap", "EC1V 3AP"),
]


def _number_routes(number):
    body = {**profile(), "company_number": number}
    return {
        "/company/" + number: (200, body, None),
        "/company/" + number + "/officers": (200, people("officers"), None),
        "/company/" + number + "/persons-with-significant-control": (200, people("psc"), None),
        "/company/" + number + "/filing-history": (200, filings(), None),
    }


@pytest.mark.parametrize("case", QUERY_TABLE, ids=[row[0] for row in QUERY_TABLE])
def test_query_input_table(runtime, case):
    label, raw, kind, expect = case
    found = match.prepared_query(raw)
    if kind == "refuse":
        assert found is None
        assert match.user_company_number(raw) is None
        router = Router()
        use(router, runtime)
        body = invoke("uk_check", {"query": raw})
        assert body["ok"] is False
        assert body["error_type"] == "bad_input"
        assert REFUSE_BITS[label] in body["message"]
        assert router.calls == []
        return
    if kind == "name":
        assert found == expect
        assert match.company_number(found) is None
        router = Router()
        router.routes["/insolvency/notice/data.json"] = (200, {"f:total": "0", "entry": []}, None)
        client.configure(transport=router, clock=runtime, sleep=runtime.sleep)
        STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
        body = invoke("uk_check", {"query": raw})
        assert router.calls == []
        assert body["ok"] is False and body["resolved"] is False
        assert body["query"] == expect
        assert "registered name was not read" in body["sources"]["gazette"]["reason"]
        assert "registered name was not read" in body["sources"]["charity"]["reason"]
        return
    assert match.user_company_number(raw) == expect
    assert match.company_number(found) == expect
    router = Router()
    router.routes.update(_number_routes(expect))
    router.routes["/search/disqualified-officers"] = (200, {"total_results": 0, "items": []}, None)
    router.routes["/insolvency/notice/data.json"] = (200, gazette(), None)
    router.routes["/landregistry/query"] = (200, land(), None)
    use(router, runtime)
    body = invoke("uk_check", {"query": raw})
    assert body["ok"] is True and body["resolved"] is True
    assert body["company"]["company_number"] == expect
    joined = "\n".join(call["url"] for call in router.calls)
    assert "find-and-update.company-information.service.gov.uk" not in joined
    assert "evil.example" not in joined
    assert "user:pass" not in joined
    assert "utm=" not in joined
    assert "/company/" + expect in paths(router)


LINK_TOOLS = [
    ("uk_search", "name"),
    ("uk_officers", "company_number"),
    ("uk_psc", "company_number"),
    ("uk_filings", "company_number"),
]

LINK_INPUTS = [
    ("http", "http://find-and-update.company-information.service.gov.uk/company/12345678", "http address was refused"),
    ("userinfo", "https://user:pass@find-and-update.company-information.service.gov.uk/company/12345678", "user name was refused"),
    ("other-host", "https://evil.example/company/12345678", "host was refused"),
]


@pytest.mark.parametrize("tool", LINK_TOOLS, ids=[row[0] for row in LINK_TOOLS])
@pytest.mark.parametrize("link", LINK_INPUTS, ids=[row[0] for row in LINK_INPUTS])
def test_refused_link_names_the_reason(runtime, tool, link):
    name, key = tool
    _label, raw, bit = link
    router = Router()
    use(router, runtime)
    body = invoke(name, {key: raw})
    assert body["ok"] is False
    assert bit in body["message"]
    assert router.calls == []


def test_watch_refused_link_names_the_reason(tmp_path):
    for raw, bit in ((row[1], row[2]) for row in LINK_INPUTS):
        added = watch.add_number(raw, directory=tmp_path)
        assert added["ok"] is False
        assert bit in added["message"]
        assert bit in added["next_step"]
    assert watch.list_numbers(directory=tmp_path)["numbers"] == []


@pytest.mark.parametrize("case", POSTCODE_TABLE, ids=[row[0] for row in POSTCODE_TABLE])
def test_postcode_input_table(runtime, case):
    _label, raw, expect = case
    router = Router()
    router.routes["/landregistry/query"] = (200, {"results": {"bindings": []}}, None)
    use(router, runtime)
    body = invoke("uk_land", {"postcode": raw})
    assert body["ok"] is True
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(router.calls[0]["url"]).query)["query"][0]
    assert f'"{expect}"' in query
    assert ";" not in query
    assert "#" not in query


def test_typed_edges_do_not_call_the_pasted_host(runtime, tmp_path):
    assert len(_LONG_OFFICIAL) > 160
    readme = (ROOT / "README.md").read_text()
    skill = (ROOT / "skills" / "uk-companies" / "SKILL.md").read_text()
    assert "The pasted address is not requested." in readme
    assert "貼ったアドレス自体は取りに行かない。" not in readme
    assert "reads the whole array from that operation" not in readme
    assert "A Charity Commission HTTP 200 for that route remains unseen on this machine" in readme
    assert "445790 and 0445790 returned 404" not in readme
    assert "does not request the pasted link." in skill

    router = Router()
    router.routes["/company/12345678/officers"] = (200, people("officers"), None)
    use(router, runtime)
    pasted = "https://api.company-information.service.gov.uk/company/12345678/officers?utm=1"
    body = invoke("uk_officers", {"company_number": pasted})
    assert body["ok"] is True
    assert paths(router) == ["/company/12345678/officers"]
    assert "utm=" not in router.calls[0]["url"]
    assert "find-and-update.company-information.service.gov.uk" not in router.calls[0]["url"]

    router.calls.clear()
    router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)
    client._last_at.clear()
    found = invoke("uk_search", {"name": "ACME LTD;"})
    assert found["ok"] is False
    assert "0 companies" in found["message"]
    assert found["next_step"]
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(router.calls[-1]["url"]).query)["q"][0]
    assert sent == "ACME LTD"
    refused = invoke("uk_search", {"name": "https://evil.example/company/12345678"})
    assert refused["ok"] is False
    assert refused["error_type"] == "bad_input"
    assert "host was refused" in refused["message"]
    assert all("evil.example" not in call["url"] for call in router.calls)
    client._last_at.clear()
    linked = invoke("uk_search", {"name": "https://find-and-update.company-information.service.gov.uk/company/12345678"})
    assert linked["ok"] is False
    assert "uk_check" in linked["message"]
    assert "find-and-update" not in " ".join(call["url"] for call in router.calls)

    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    router.routes["/register/api/searchCharityName/OXFAM"] = (200, [], None)
    router.routes["/register/api/allcharitydetailsV2/202918/0"] = (200, {
        "charity_name": "OXFAM",
        "reg_charity_number": 202918,
        "group_subsid_suffix": 0,
        "reg_status": "R",
    }, None)
    client._last_at.clear()
    named = invoke("uk_charity", {"name": "OXFAM;"})
    assert named["ok"] is True
    assert "/register/api/searchCharityName/OXFAM" in paths(router)
    client._last_at.clear()
    numbered = invoke("uk_charity", {"number": "202918 # england", "suffix": "0;"})
    assert numbered["ok"] is True
    assert "/register/api/allcharitydetailsV2/202918/0" in paths(router)

    added = watch.add_number(
        "https://find-and-update.company-information.service.gov.uk/company/12345678;",
        directory=tmp_path,
    )
    assert added["ok"] is True
    assert added["numbers"] == ["12345678"]
    removed = watch.remove_number("12345678 # old", directory=tmp_path)
    assert removed["ok"] is True
    assert removed["numbers"] == []


def test_deadline_stops_the_second_call(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)

    def jump(seconds):
        runtime.now += 120

    client.configure(transport=router, clock=runtime, sleep=jump)
    STATE["values"][client.CH_KEY] = KEY
    invoke("uk_search", {"name": "ACME LTD"})
    body = invoke("uk_search", {"name": "ACME TWO LTD"})
    assert body["error_type"] == "deadline"
    assert len(router.calls) == 1


def _closed_sources() -> dict:
    house = {
        "status": "read",
        "reason": "",
        "rows": [
            {"company_number": NUMBER},
            {"part": "officers", "status": "read", "fully_read": True, "rows": [], "rows_read": 0},
            {"part": "psc", "status": "read", "fully_read": True, "rows": [], "rows_read": 0},
            {"part": "filings", "status": "read", "fully_read": True, "rows": [], "rows_read": 0},
        ],
        "rows_read": 4,
    }
    return {
        "companies_house": house,
        "disqualified": {"status": "read", "fully_read": True, "reason": "", "rows": [], "rows_read": 0},
        "gazette": {"status": "read", "fully_read": True, "reason": "", "rows": [], "rows_read": 0},
        "charity": {"status": "not_read", "reason": "no key", "rows": [], "rows_read": 0},
        "vat": {"status": "excluded", "reason": "", "rows": [], "rows_read": 0},
        "land": {"status": "read", "fully_read": True, "reason": "", "rows": [], "rows_read": 0},
    }


def _open_block() -> dict:
    return {"status": "read", "fully_read": False, "reason": "later rows", "rows": [], "rows_read": 0}


# One row per input kind for a resolved sheet. The label is the noun next_step must name.
# An empty label means next_step stays empty. error_step is set when ok must be false.
RESOLVED_OUTCOME_TABLE = [
    {"name": "all-read", "label": "", "error_step": ""},
    {"name": "disqualified-open", "key": "disqualified", "label": "disqualified-officer search"},
    {"name": "gazette-open", "key": "gazette", "label": "insolvency feed"},
    {"name": "charity-open", "key": "charity", "label": "charity register"},
    {"name": "land-open", "key": "land", "label": "Price Paid list"},
    {"name": "profile-open", "key": "companies_house", "label": "company profile", "on": "block"},
    {"name": "officers-open", "part": "officers", "label": "officer list"},
    {"name": "psc-open", "part": "psc", "label": "persons with significant control list"},
    {"name": "filings-open", "part": "filings", "label": "filing history"},
    {"name": "flag-missing", "key": "gazette", "label": "", "drop_flag": True},
    {"name": "not-read-flag", "key": "charity", "label": "", "status": "not_read"},
    {"name": "excluded-flag", "key": "vat", "label": "", "status": "excluded"},
    {"name": "gazette-and-disqualified", "keys": ("gazette", "disqualified"), "label": "insolvency feed"},
    {"name": "three-open", "keys": ("gazette", "land", "disqualified"), "label": "disqualified-officer search"},
    {"name": "error-wins", "key": "gazette", "label": "", "error_step": "Read the source block whose status is error."},
    {"name": "error-beats-open", "key": "gazette", "also": "disqualified", "label": "", "error_step": "Read the source block whose status is error."},
    {
        "name": "gazette-quote-skip",
        "key": "gazette",
        "label": "",
        "status": "not_read",
        "skip_step": sources.GAZETTE_SKIP["double_quote"][1],
    },
    {
        "name": "quote-skip-and-disqualified",
        "key": "gazette",
        "also_open": "disqualified",
        "label": "disqualified-officer search",
        "status": "not_read",
        "skip_step": sources.GAZETTE_SKIP["double_quote"][1],
    },
]


def _outcome_sources(case: dict) -> dict:
    sources_map = _closed_sources()
    if case.get("error_step") and case.get("key"):
        sources_map[case["key"]] = {
            "status": "error",
            "reason": "The upstream returned HTTP 500.",
            "next_step": case["error_step"],
            "rows": [],
            "rows_read": 0,
            "fully_read": False,
        }
        if case.get("also"):
            sources_map[case["also"]] = _open_block()
        return sources_map
    if case.get("part"):
        for row in sources_map["companies_house"]["rows"]:
            if row.get("part") == case["part"]:
                row["fully_read"] = False
        return sources_map
    keys = case.get("keys") or ((case["key"],) if case.get("key") else ())
    for key in keys:
        if case.get("on") == "block" and key == "companies_house":
            sources_map[key]["fully_read"] = False
            sources_map[key]["status"] = "read"
            continue
        block = _open_block()
        if case.get("drop_flag"):
            block.pop("fully_read")
        if case.get("status"):
            block["status"] = case["status"]
        if case.get("skip_step"):
            block["next_step"] = case["skip_step"]
        sources_map[key] = block
    if case.get("also_open"):
        sources_map[case["also_open"]] = _open_block()
    return sources_map


@pytest.mark.parametrize("case", RESOLVED_OUTCOME_TABLE, ids=[row["name"] for row in RESOLVED_OUTCOME_TABLE])
def test_resolved_sheet_outcome_table(case):
    ok, message, step = sheet._sheet_outcome(_outcome_sources(case))
    if case.get("error_step"):
        assert ok is False
        assert message == "One company was resolved. A source returned an error."
        assert step == case["error_step"]
        assert "not fully read" not in step
        return
    assert ok is True
    assert message == sheet._RESOLVED_MESSAGE
    label = case["label"]
    skip = case.get("skip_step") or ""
    if skip:
        assert skip in step
    if not label and not skip:
        assert step == ""
        return
    if not label:
        assert "not fully read" not in step
        return
    assert label in step
    assert "not fully read" in step
    if case["name"] == "gazette-and-disqualified":
        assert "disqualified-officer search" in step
        assert " and " in step
    if case["name"] == "three-open":
        assert "insolvency feed" in step
        assert "Price Paid list" in step
        assert ", and " in step


def test_sheet_calls_do_not_carry_host_counts(runtime):
    router = Router()
    router.routes["/search/companies"] = (200, {"total_results": 0, "items": []}, None)
    router.routes["/landregistry/query"] = (200, land(), None)
    use(router, runtime)
    first = sheet.uk_search({"name": "ACME LTD"})
    vat = sheet.uk_vat({})
    assert first["requests"] == {CH: 1}
    assert vat["checked"] is False
    assert vat["requests"] == {}
    paid = sheet.uk_land({"postcode": "EC1V 3AP"})
    assert paid["requests"] == {client.LAND_HOST: 1}
    second = sheet.uk_search({"name": "ACME TWO LTD"})
    assert second["requests"] == {CH: 1}
    wrapped = invoke("uk_vat", {})
    assert wrapped["requests"] == {}
    assert client.host_counts() == {}


def test_clear_host_counts_keeps_the_rate_window(runtime):
    client._ch_times[:] = [runtime.now] * client.CH_LIMIT
    client._host_counts[CH] = 4
    client.clear_host_counts()
    assert client.host_counts() == {}
    assert len(client._ch_times) == client.CH_LIMIT
    with pytest.raises(client.ApiError) as caught:
        client._acquire(CH, None)
    assert caught.value.error_type == "local_rate"


def test_invoke_check_then_vat_counts_only_this_call(runtime):
    router = sheet_router()
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["next_step"] == ""
    assert sum(body["requests"].values()) == len(router.calls)
    assert client.host_counts()[CH] >= 1
    vat = invoke("uk_vat", {})
    assert vat["requests"] == {}
    assert client.host_counts() == {}
    assert len(client._ch_times) == body["requests"][CH]


def test_open_sources_are_named_on_the_sheet(runtime):
    router = sheet_router()
    gaz = gazette()
    gaz["f:total"] = "50"
    router.routes["/insolvency/notice/data.json"] = (200, gaz, None)
    router.routes["/search/disqualified-officers"] = (200, {"total_results": 5, "items": []}, None)
    officers = people("officers")
    officers["total_results"] = 200
    router.routes["/company/" + NUMBER + "/officers"] = (200, officers, None)
    psc = people("psc")
    psc["total_results"] = 200
    router.routes["/company/" + NUMBER + "/persons-with-significant-control"] = (200, psc, None)
    filed = filings()
    filed["total_count"] = 200
    router.routes["/company/" + NUMBER + "/filing-history"] = (200, filed, None)
    paid = land()
    paid["results"]["bindings"] = paid["results"]["bindings"] * 10
    router.routes["/landregistry/query"] = (200, paid, None)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    segment = urllib.parse.quote(NAME, safe="")
    router.routes["/register/api/searchCharityName/" + segment] = (200, {"charities": []}, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["ok"] is True and body["resolved"] is True
    assert body["message"] == sheet._RESOLVED_MESSAGE
    for label in (
        "disqualified-officer search",
        "insolvency feed",
        "charity register",
        "Price Paid list",
        "officer list",
        "persons with significant control list",
        "filing history",
    ):
        assert label in body["next_step"]
    assert "company profile" not in body["next_step"]
    assert body["sources"]["companies_house"].get("fully_read") is None
    assert "not fully read" in body["next_step"]


def _sheet_named(name, postal="EC1V 3AP"):
    router = sheet_router()
    body = profile()
    body["company_name"] = name
    body["registered_office_address"] = {
        "address_line_1": "1 High Street",
        "postal_code": postal,
        "locality": "London",
    }
    router.routes["/company/" + NUMBER] = (200, body, None)
    router.routes["/insolvency/notice/data.json"] = (200, gazette(name), None)
    return router


def test_charity_name_404_is_zero_rows(runtime):
    router = _sheet_named(NAME)
    segment = urllib.parse.quote(NAME, safe="")
    router.routes["/register/api/searchCharityName/" + segment] = (404, {"error": "missing"}, None)
    use(router, runtime)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    body = invoke("uk_check", {"query": NUMBER})
    charity = body["sources"]["charity"]
    assert body["ok"] is True and body["resolved"] is True
    assert charity["status"] == "read"
    assert charity["fully_read"] is True
    assert charity["rows"] == []
    assert "zero count leaves the question open" in charity["reason"]
    assert "404" not in body["next_step"]
    assert "Check the identifier" not in body["next_step"]
    assert "/register/api/allcharitydetailsV2/" not in json.dumps(paths(router))
    client._last_at.clear()
    named = invoke("uk_charity", {"name": NAME})
    assert named["ok"] is True
    assert named["rows"] == []
    assert named["fully_read"] is True
    assert "zero count leaves the question open" in named["message"]
    client._last_at.clear()
    router.routes["/register/api/allcharitydetailsV2/202918/0"] = (404, {"error": "missing"}, None)
    missing = invoke("uk_charity", {"number": "202918"})
    assert missing["ok"] is False
    assert "404" in missing["next_step"]


# One row is one Gazette text input. send keeps the registered name.
# gazette_wire doubles each backslash, and the caller adds the phrase marks.
# A double quote is the phrase mark, so that name is not sent.
# empty and too_long are not sent either.
GAZETTE_TEXT_TABLE = [
    {"name": "letters", "text": "TESCO PLC", "kind": "send", "body": "TESCO PLC"},
    {"name": "slash", "text": "24/7 CARE LIMITED", "kind": "send", "body": "24/7 CARE LIMITED"},
    {"name": "question", "text": "WHY? LIMITED", "kind": "send", "body": "WHY? LIMITED"},
    {"name": "hash", "text": "HASH# LIMITED", "kind": "send", "body": "HASH# LIMITED"},
    {"name": "ampersand", "text": "SMITH & JONES LIMITED", "kind": "send", "body": "SMITH & JONES LIMITED"},
    {"name": "accent", "text": "JOSÉ LTD", "kind": "send", "body": "JOSÉ LTD"},
    {"name": "and-word", "text": "FOO AND BAR LIMITED", "kind": "send", "body": "FOO AND BAR LIMITED"},
    {"name": "backslash", "text": "PAGE \\ PARK LIMITED", "kind": "send", "body": "PAGE \\ PARK LIMITED", "wire": "PAGE \\\\ PARK LIMITED"},
    {"name": "backslash-end", "text": "ACME\\", "kind": "send", "body": "ACME\\", "wire": "ACME\\\\"},
    {"name": "backslash-pair", "text": "A\\\\B LIMITED", "kind": "send", "body": "A\\\\B LIMITED", "wire": "A\\\\\\\\B LIMITED"},
    {"name": "fullwidth-backslash", "text": "PAGE＼PARK LIMITED", "kind": "send", "body": "PAGE\\PARK LIMITED", "wire": "PAGE\\\\PARK LIMITED"},
    {"name": "inner-quote", "text": 'ACME "NORTH" LIMITED', "kind": "double_quote"},
    {"name": "edge-quote", "text": '"ACME LIMITED"', "kind": "double_quote"},
    {"name": "only-quote", "text": '"', "kind": "double_quote"},
    {"name": "spaces", "text": "   ", "kind": "empty"},
    {"name": "blank", "text": "", "kind": "empty"},
    {"name": "not-text", "text": None, "kind": "empty"},
    {"name": "control", "text": "ACME\nLTD", "kind": "empty"},
    {"name": "too-long", "text": "A" * 161, "kind": "too_long"},
    {"name": "too-long-quote", "text": '"' + ("A" * 161), "kind": "too_long"},
]


@pytest.mark.parametrize("case", GAZETTE_TEXT_TABLE, ids=[row["name"] for row in GAZETTE_TEXT_TABLE])
def test_gazette_text_table(case):
    kind, body = sources.gazette_text(case["text"])
    assert kind == case["kind"]
    if kind == "send":
        assert body == case["body"]
        assert '"' not in body
        assert sources.gazette_wire(body) == case.get("wire", body)
        assert sources.gazette_wire(body).count("\\") % 2 == 0
        return
    assert body == ""
    message, step = sources.GAZETTE_SKIP[kind]
    assert message
    assert "insolvency feed" in step
    if kind == "double_quote":
        assert "double quote" in message
        assert "double quote" in step


def test_registered_name_symbols_stay_on_the_sheet(runtime):
    slash = "24/7 CARE LIMITED"
    router = _sheet_named(slash)
    segment = urllib.parse.quote(slash, safe="")
    assert "/" not in segment
    router.routes["/register/api/searchCharityName/" + segment] = (200, [], None)
    use(router, runtime)
    STATE["values"][client.CHARITY_KEY] = CHARITY_KEY
    body = invoke("uk_check", {"query": NUMBER})
    assert body["ok"] is True
    assert body["sources"]["charity"]["status"] == "read"
    assert "argument format" not in body["message"]
    assert segment in paths(router)[0] or any(segment in call["path"] for call in router.calls)
    gazette_calls = [call for call in router.calls if call["path"] == "/insolvency/notice/data.json"]
    assert len(gazette_calls) == 1
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(gazette_calls[0]["url"]).query)["text"][0]
    assert sent == f'"{slash}"'
    client._last_at.clear()
    asked = "WHY? LIMITED"
    asked_router = _sheet_named(asked)
    asked_segment = urllib.parse.quote(asked, safe="")
    asked_router.routes["/register/api/searchCharityName/" + asked_segment] = (404, {"error": "missing"}, None)
    client.configure(transport=asked_router, clock=runtime, sleep=runtime.sleep)
    asked_body = invoke("uk_check", {"query": NUMBER})
    assert asked_body["ok"] is True
    assert asked_body["sources"]["charity"]["status"] == "read"
    assert "zero count leaves the question open" in asked_body["sources"]["charity"]["reason"]
    client._last_at.clear()
    quoted = 'ACME "NORTH" LIMITED'
    quoted_router = _sheet_named(quoted)
    client.configure(transport=quoted_router, clock=runtime, sleep=runtime.sleep)
    STATE["values"].pop(client.CHARITY_KEY, None)
    quoted_body = invoke("uk_check", {"query": NUMBER})
    assert quoted_body["ok"] is True
    assert quoted_body["sources"]["gazette"]["status"] == "not_read"
    assert "double quote" in quoted_body["sources"]["gazette"]["reason"]
    assert sources.GAZETTE_SKIP["double_quote"][1] in quoted_body["next_step"]
    assert "0 notices" not in quoted_body["sources"]["gazette"]["reason"]
    assert not any(call["path"] == "/insolvency/notice/data.json" for call in quoted_router.calls)


def test_backslash_name_is_queried_as_an_escaped_phrase(runtime):
    name = "PAGE \\ PARK LIMITED"
    router = _sheet_named(name)
    router.routes["/insolvency/notice/data.json"] = (200, {"f:total": "0", "entry": []}, None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    gazette_block = body["sources"]["gazette"]
    assert body["ok"] is True and body["resolved"] is True
    assert gazette_block["status"] == "read"
    assert gazette_block["rows"] == []
    assert gazette_block["fully_read"] is True
    assert "0 notices" in gazette_block["reason"]
    assert "not a finding" in gazette_block["reason"]
    assert "Try the call once more" not in body["next_step"]
    calls = [call for call in router.calls if call["path"] == "/insolvency/notice/data.json"]
    assert len(calls) == 1
    assert "\\" not in calls[0]["url"]
    sent = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[0]["url"]).query)["text"][0]
    assert sent == '"PAGE \\\\ PARK LIMITED"'
    client._last_at.clear()
    matched = _sheet_named(name)
    use(matched, runtime)
    matched_body = invoke("uk_check", {"query": NUMBER})
    row = matched_body["sources"]["gazette"]["rows"][0]
    assert matched_body["ok"] is True
    assert row["title"] == name
    assert row["match"] == "exact"
    wire_calls = [call for call in matched.calls if call["path"] == "/insolvency/notice/data.json"]
    wire = urllib.parse.parse_qs(urllib.parse.urlsplit(wire_calls[0]["url"]).query)["text"][0]
    assert wire == '"PAGE \\\\ PARK LIMITED"'


def test_gazette_non_json_still_fails_the_sheet(runtime):
    router = _sheet_named(NAME)
    router.routes["/insolvency/notice/data.json"] = (200, b"not-json", None)
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["ok"] is False
    assert body["sources"]["gazette"]["status"] == "error"
    assert "not JSON" in body["sources"]["gazette"]["reason"]
    assert "Try the call once more" in body["next_step"]


def test_spelling_with_the_same_birth_stays(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 3,
        "items": [
            _dq_item("Jose GARCIA", "1971-04-02T00:00:00", "garcia"),
            _dq_item("Ann SMITH JONES", "1980-03-04T00:00:00", "hyphen"),
            _dq_item("Peter James ALDEN", "1975-05-06T00:00:00", "order"),
        ],
    }, None)
    use(router, runtime)
    jose = sources.fetch_disqualified(
        ["GARCIA, José"],
        client.Deadline(),
        True,
        [{"name": "GARCIA, José", "birth_month": 4, "birth_year": 1971, "officer_id": "g"}],
        "NORTHGLASS LIMITED",
    )
    assert [row.get("title") for row in jose["rows"]] == ["Jose GARCIA"]
    assert jose["rows"][0]["match"] == "possible"
    assert "surname and the birth month" in jose["rows"][0]["url_reason"]
    assert "/disqualified-officers/natural/garcia" not in paths(router)
    client._last_at.clear()
    hyphen = sources.fetch_disqualified(
        ["SMITH-JONES, Ann"],
        client.Deadline(),
        True,
        [{"name": "SMITH-JONES, Ann", "birth_month": 3, "birth_year": 1980, "officer_id": "h"}],
        "NORTHGLASS LIMITED",
    )
    assert [row.get("title") for row in hyphen["rows"]] == ["Ann SMITH JONES"]
    assert hyphen["rows"][0]["match"] == "possible"
    client._last_at.clear()
    ordered = sources.fetch_disqualified(
        ["ALDEN, James Peter"],
        client.Deadline(),
        True,
        [{"name": "ALDEN, James Peter", "birth_month": 5, "birth_year": 1975, "officer_id": "s"}],
        "NORTHGLASS LIMITED",
    )
    assert [row.get("title") for row in ordered["rows"]] == ["Peter James ALDEN"]
    assert ordered["rows"][0]["match"] == "possible"
    assert ordered["fully_read"] is True


def test_secretary_without_birth_is_not_called_a_mismatch(runtime):
    router = Router()
    router.routes["/search/disqualified-officers"] = (200, {
        "total_results": 1,
        "items": [_dq_item("Nora Claire HALE", "1972-04-18T00:00:00", "sec")],
    }, None)
    use(router, runtime)
    found = sources.fetch_disqualified(
        ["HALE, Nora Claire"],
        client.Deadline(),
        True,
        [{"name": "HALE, Nora Claire", "birth_month": "", "birth_year": "", "officer_role": "secretary", "officer_id": "sec-off"}],
        "NORTHGLASS LIMITED",
    )
    row = found["rows"][0]
    assert row["match"] == "possible"
    assert "cannot be compared" in row["identity_reason"]
    assert "do not match" not in row["identity_reason"]
    assert "/disqualified-officers/natural/sec" not in paths(router)


def test_foreign_postcode_is_not_called_missing(runtime):
    router = _sheet_named(NAME, "10001")
    use(router, runtime)
    body = invoke("uk_check", {"query": NUMBER})
    assert body["ok"] is True
    assert body["company"]["postcode"] == ""
    assert "not a UK postcode" in body["company"]["postcode_reason"]
    assert "did not include" not in body["company"]["postcode_reason"]
    assert body["sources"]["land"]["status"] == "not_read"
    assert "not a UK postcode" in body["sources"]["land"]["reason"]
    assert not any(call["path"] == "/landregistry/query" for call in router.calls)
