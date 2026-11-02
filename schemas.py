"""Tool schemas. Each description is one string so the first sentence stays intact."""
import sys

sys.dont_write_bytecode = True

TOOLSET = "uk_companies"

UK_CHECK = {
    "name": "uk_check",
    "description": (
        "Look up a UK company by name or number. "
        "One sheet lists Companies House, disqualified officers, the Gazette insolvency feed, "
        "the Charity Commission, a VAT exclusion, and Price Paid. "
        "Several search hits stop in the candidates array. "
        "One hit whose legal name is not an exact match also stops there. "
        "State a ban or a notice only when the row says exact. "
        "A notice dated before incorporation stays possible. "
        "An exact ban row has disqualified_until after today and lists matched_officers."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": "Company name, company number, or an https link to /company/{number} on the official company-information host.",
            },
        },
    },
}

UK_SEARCH = {
    "name": "uk_search",
    "description": (
        "Search Companies House for a UK company name. "
        "One page returns company numbers, status words, and official URLs."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "description": "Company name to search."},
        },
    },
}

UK_OFFICERS = {
    "name": "uk_officers",
    "description": (
        "List officers of one UK company number. "
        "Appointed names stay in the Companies House order, including a resigned date when present."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["company_number"],
        "properties": {
            "company_number": {"type": "string", "description": "Companies House company number."},
        },
    },
}

UK_PSC = {
    "name": "uk_psc",
    "description": (
        "List persons with significant control for a company. "
        "Each row keeps the kind and the natures of control from Companies House."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["company_number"],
        "properties": {
            "company_number": {"type": "string", "description": "Companies House company number."},
        },
    },
}

UK_FILINGS = {
    "name": "uk_filings",
    "description": (
        "List filing history for one UK company number. "
        "One page is read, and a higher total means later filings were not read."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["company_number"],
        "properties": {
            "company_number": {"type": "string", "description": "Companies House company number."},
        },
    },
}

UK_CHARITY = {
    "name": "uk_charity",
    "description": (
        "Look up a Charity Commission record by name or number. "
        "Registration rows need CHARITY_COMMISSION_API_KEY or this register is skipped."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string", "description": "Charity name."},
            "number": {"type": "string", "description": "Registered charity number."},
            "suffix": {"type": "string", "description": "Subsidiary suffix. Defaults to 0."},
        },
    },
}

UK_VAT = {
    "name": "uk_vat",
    "description": (
        "Explain why a UK VAT number is not checked here. "
        "HMRC VAT needs an application and OAuth, so this tool sends no request."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {},
    },
}

UK_LAND = {
    "name": "uk_land",
    "description": (
        "List the newest Price Paid sales for one UK postcode. "
        "The list is capped at 10. Older sales are left out. "
        "Each row is a sale at that postcode. "
        "official_url is a SPARQL DESCRIBE of that sale, because the linked-data page returns 403."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "required": ["postcode"],
        "properties": {
            "postcode": {"type": "string", "description": "UK postcode, outward and inward parts."},
        },
    },
}

ALL = (UK_CHECK, UK_SEARCH, UK_OFFICERS, UK_PSC, UK_FILINGS, UK_CHARITY, UK_VAT, UK_LAND)
