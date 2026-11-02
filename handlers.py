"""Turn a tool call into one JSON string and a per-call request count."""
from __future__ import annotations

import sys

sys.dont_write_bytecode = True

if __package__:
    from . import client, sheet
else:
    import client
    import sheet

TOOLS = {
    "uk_check": sheet.uk_check,
    "uk_search": sheet.uk_search,
    "uk_officers": sheet.uk_officers,
    "uk_psc": sheet.uk_psc,
    "uk_filings": sheet.uk_filings,
    "uk_charity": sheet.uk_charity,
    "uk_vat": sheet.uk_vat,
    "uk_land": sheet.uk_land,
}


def invoke(tool_name: str, args: object) -> str:
    # Clear before the snapshot. A clear inside the tool would make this delta 0.
    client.clear_host_counts()
    before = client.host_counts()
    try:
        fn = TOOLS.get(tool_name)
        if fn is None or not isinstance(args, dict):
            payload = client.failed("bad_input", "Pass an object of arguments for a known tool.", client.NEXT["bad_input"])
        else:
            payload = fn(args)
    except client.ApiError as exc:
        payload = client.from_error(exc)
    except Exception:
        payload = client.failed("upstream", "The lookup stopped before a result.", client.NEXT["upstream"])
    if not isinstance(payload, dict):
        payload = client.failed("upstream", "The lookup stopped before a result.", client.NEXT["upstream"])
    after = client.host_counts()
    delta = {}
    for host in set(before) | set(after):
        count = after.get(host, 0) - before.get(host, 0)
        if count:
            delta[host] = count
    body = dict(payload)
    body["requests"] = delta
    return client.dump(body)
