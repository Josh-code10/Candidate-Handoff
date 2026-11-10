#!/usr/bin/env python3
"""
Candidate Handoff Demo - Airtable setup
=======================================
Creates four tables in Airtable and seeds them with FAKE data only:

  Agency_Candidates      stand-in for the agency's CRM
  Client_Jobs            stand-in for the client's open jobs
  Client_ATS_Candidates  stand-in for the client's ATS (where approved candidates land)
  Audit_Log              metadata-only record of every decision (no CV, no notes, no raw payloads)

Uses only the Python standard library (no pip install).

Usage
-----
  export AIRTABLE_PAT="pat..."          # scopes: schema.bases:read, schema.bases:write,
                                        #         data.records:read, data.records:write
  # Option A: use an empty base you created by hand
  export AIRTABLE_BASE_ID="app..."
  # Option B: let the script create the base
  export AIRTABLE_WORKSPACE_ID="wsp..."

  python airtable_setup.py --dry-run    # prints what it WOULD create, touches nothing
  python airtable_setup.py              # does it

Safe to re-run: tables that already exist are skipped and are NOT re-seeded.
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

API = "https://api.airtable.com/v0"
PAT = os.environ.get("AIRTABLE_PAT", "")
BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")
WORKSPACE_ID = os.environ.get("AIRTABLE_WORKSPACE_ID", "")
BASE_NAME = "Candidate Handoff Demo"
DRY_RUN = "--dry-run" in sys.argv


# ----------------------------------------------------------------------------
# Field helpers
# ----------------------------------------------------------------------------
def text(name):
    return {"name": name, "type": "singleLineText"}


def long_text(name):
    return {"name": name, "type": "multilineText"}


def email(name):
    return {"name": name, "type": "email"}


def number(name):
    return {"name": name, "type": "number", "options": {"precision": 0}}


def checkbox(name):
    return {"name": name, "type": "checkbox", "options": {"icon": "check", "color": "greenBright"}}


def date_field(name):
    return {"name": name, "type": "date", "options": {"dateFormat": {"name": "iso"}}}


def datetime_field(name):
    return {
        "name": name,
        "type": "dateTime",
        "options": {
            "dateFormat": {"name": "iso"},
            "timeFormat": {"name": "24hour"},
            "timeZone": "utc",
        },
    }


def select(name, choices):
    return {"name": name, "type": "singleSelect", "options": {"choices": [{"name": c} for c in choices]}}


# ----------------------------------------------------------------------------
# Schema (first field of each table is its primary field)
# ----------------------------------------------------------------------------
TABLES = [
    {
        "name": "Agency_Candidates",
        "description": "DEMO stand-in for an agency CRM. Fake data only.",
        "fields": [
            text("candidate_code"),
            text("full_name"),
            email("email"),
            text("country"),
            select("consent_status", ["Granted", "Not given", "Withdrawn"]),
            text("consent_scope"),  # "ANY" or a specific client name
            date_field("consent_date"),
            checkbox("cross_border_consent"),
            select("rtw_status", ["Verified", "Pending", "Failed", "Not run"]),
            text("rtw_country"),
            date_field("rtw_check_date"),
            date_field("rtw_expiry"),  # empty = no expiry
            select("id_status", ["Verified", "Pending", "Failed"]),
            select("dbs_status", ["Clear", "Pending", "Not run", "Flagged"]),
            number("references_received"),
        ],
    },
    {
        "name": "Client_Jobs",
        "description": "DEMO stand-in for the client's open jobs.",
        "fields": [
            text("job_code"),
            text("title"),
            text("client_name"),
            text("country"),
            checkbox("requires_dbs"),
            number("min_references"),
            select("status", ["Open", "Closed"]),
        ],
    },
    {
        "name": "Client_ATS_Candidates",
        "description": "DEMO stand-in for the client's ATS. Approved candidates land here.",
        "fields": [
            text("agency_ref"),
            text("full_name"),
            email("email"),
            text("job_code"),
            select("status", ["Submitted", "Interviewing", "Rejected", "Hired"]),
            datetime_field("submitted_at"),
            long_text("compliance_summary"),
        ],
    },
    {
        "name": "Audit_Log",
        "description": "Metadata-only audit trail (zero data-retention): codes, outcome, reasons, reviewer. No candidate details.",
        "fields": [
            text("run_id"),
            datetime_field("event_time"),
            text("candidate_code"),
            text("job_code"),
            select(
                "outcome",
                ["SUBMITTED", "BLOCKED", "REJECTED", "CHANGES_REQUESTED", "EXPIRED", "PUSH_FAILED"],
            ),
            long_text("reasons"),
            text("reviewed_by"),
        ],
    },
]


# ----------------------------------------------------------------------------
# Seed data (all fake). Dates are relative to the day you run the script.
# ----------------------------------------------------------------------------
def build_seeds():
    today = date.today()
    d = lambda days: (today + timedelta(days=days)).isoformat()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def cand(code, name, **over):
        base = {
            "candidate_code": code,
            "full_name": name,
            "email": f"{code.lower()}@example.com",
            "country": "United Kingdom",
            "consent_status": "Granted",
            "consent_scope": "ANY",
            "consent_date": d(-10),
            "cross_border_consent": False,
            "rtw_status": "Verified",
            "rtw_country": "United Kingdom",
            "rtw_check_date": d(-8),
            "id_status": "Verified",
            "dbs_status": "Clear",
            "references_received": 2,
        }
        base.update(over)
        return base

    candidates = [
        # Clean pass. Consent is scoped to ONE client, so it passes for JOB-1001 but is blocked for JOB-1002.
        cand("CAND-001", "Tolu Adeyemi", consent_scope="Northbridge Finance"),
        # Blocked: no consent
        cand("CAND-002", "Grace Mensah", consent_status="Not given", consent_date=None),
        # Blocked: right-to-work still pending
        cand("CAND-003", "Daniel Brooks", rtw_status="Pending", rtw_check_date=None),
        # Passes with a WARNING: right-to-work expires in 20 days
        cand("CAND-004", "Sarah Whitfield", rtw_expiry=d(20)),
        # Blocked on JOB-1002 (care role needs a clear background check): background check not run
        cand("CAND-005", "Kemi Balogun", dbs_status="Not run"),
        # Blocked on JOB-1001: already sitting in the client's ATS (seeded below)
        cand("CAND-006", "Liam Parker"),
        # Blocked: lives outside the job's country with no cross-border data-transfer consent
        cand("CAND-007", "Thando Nkosi", country="South Africa", cross_border_consent=False),
    ]
    # Airtable rejects null for empty fields on create: drop them instead
    candidates = [{k: v for k, v in c.items() if v is not None} for c in candidates]

    jobs = [
        {
            "job_code": "JOB-1001",
            "title": "Senior Accountant",
            "client_name": "Northbridge Finance",
            "country": "United Kingdom",
            "requires_dbs": False,
            "min_references": 2,
            "status": "Open",
        },
        {
            "job_code": "JOB-1002",
            "title": "Care Coordinator",
            "client_name": "Greenfield Care",
            "country": "United Kingdom",
            "requires_dbs": True,
            "min_references": 2,
            "status": "Open",
        },
    ]

    ats = [
        {
            "agency_ref": "CAND-006",
            "full_name": "Liam Parker",
            "email": "cand-006@example.com",
            "job_code": "JOB-1001",
            "status": "Submitted",
            "submitted_at": now_iso,
            "compliance_summary": "Seeded demo record: already submitted by another consultant.",
        }
    ]
    return {"Agency_Candidates": candidates, "Client_Jobs": jobs, "Client_ATS_Candidates": ats, "Audit_Log": []}


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def call(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        API + path,
        method=method,
        data=data,
        headers={"Authorization": f"Bearer {PAT}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as err:
        sys.exit(f"\nAirtable error {err.code} on {method} {path}\n{err.read().decode('utf-8')}")
    finally:
        time.sleep(0.25)  # stay well under Airtable's 5 requests/second limit


def chunks(items, size=10):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    seeds = build_seeds()

    if DRY_RUN:
        print("DRY RUN: nothing will be sent to Airtable.\n")
        for t in TABLES:
            print(f"TABLE {t['name']}: {len(t['fields'])} fields -> " + ", ".join(f["name"] for f in t["fields"]))
            print(f"  seed rows: {len(seeds[t['name']])}")
        print("\nSeed preview (Agency_Candidates):")
        print(json.dumps(seeds["Agency_Candidates"][:2], indent=2))
        return

    if not PAT:
        sys.exit("Set AIRTABLE_PAT first.")
    if not BASE_ID and not WORKSPACE_ID:
        sys.exit("Set AIRTABLE_BASE_ID (existing empty base) or AIRTABLE_WORKSPACE_ID (script creates the base).")

    base_id = BASE_ID
    created = set()

    if not base_id:
        first = TABLES[0]
        res = call("POST", "/meta/bases", {"name": BASE_NAME, "workspaceId": WORKSPACE_ID, "tables": [first]})
        base_id = res["id"]
        created.add(first["name"])
        print(f"Created base {base_id} with table {first['name']}")

    existing = {t["name"] for t in call("GET", f"/meta/bases/{base_id}/tables").get("tables", [])}

    for t in TABLES:
        if t["name"] in created:
            continue
        if t["name"] in existing:
            print(f"Skipping {t['name']} (already exists, not re-seeded)")
            continue
        call("POST", f"/meta/bases/{base_id}/tables", t)
        created.add(t["name"])
        print(f"Created table {t['name']}")

    for name in created:
        rows = seeds.get(name, [])
        for batch in chunks(rows):
            call(
                "POST",
                f"/{base_id}/{urllib.parse.quote(name)}",
                {"typecast": True, "records": [{"fields": r} for r in batch]},
            )
        if rows:
            print(f"Seeded {len(rows)} fake rows into {name}")

    print("\nDone.")
    print(f"  Base ID (paste into the n8n 'Config' node): {base_id}")
    print("  The default blank first row Airtable adds to new tables, if any, can be deleted by hand.")


if __name__ == "__main__":
    main()
