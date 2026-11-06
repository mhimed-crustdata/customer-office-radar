#!/usr/bin/env python3
"""Customer Office Radar.

Reads your customer accounts (a HubSpot companies export), pulls each account's
recent job posts, and compares every post's location with the company's
headquarters. A cluster of posts in a city or country that is not the HQ is a
new-office signal: a new team that still has to be set up on your product.

Data layer: Crustdata POST /company/search, POST /job/search and, optionally,
POST /watch/company (https://docs.crustdata.com).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

from crustdata_api import Crustdata, day, load_env, normalize_domain
from notify import ask_claude, claude_available, send_slack

COMPANY_FIELDS = ["crustdata_company_id", "basic_info.name", "basic_info.primary_domain", "locations"]
JOB_FIELDS = [
    "crustdata_job_id",
    "job_details.title",
    "job_details.url",
    "job_details.workplace_type",
    "company.basic_info.crustdata_company_id",
    "company.basic_info.name",
    "company.basic_info.primary_domain",
    "location",
    "metadata.date_added",
]

# Company records say "USA", job posts say "United States": compare on one name.
COUNTRY_ALIASES = {
    "usa": "united states", "us": "united states", "u.s.": "united states", "united states of america": "united states",
    "uk": "united kingdom", "great britain": "united kingdom", "england": "united kingdom",
    "uae": "united arab emirates", "korea, republic of": "south korea", "republic of korea": "south korea",
}


def norm_country(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    return COUNTRY_ALIASES.get(v, v)


# ---------------------------------------------------------------- input

def load_customers(path: str) -> List[Dict[str, str]]:
    """Read a HubSpot companies export (or any CSV with a domain column)."""
    out, seen = [], set()
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            lower = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            domain = normalize_domain(lower.get("company domain name") or lower.get("domain") or lower.get("website url")
                                      or lower.get("website") or "")
            if not domain or domain in seen:
                continue
            seen.add(domain)
            out.append({"domain": domain,
                        "name": lower.get("company name") or lower.get("name") or domain,
                        "owner": lower.get("company owner") or lower.get("owner") or ""})
    return out


# ---------------------------------------------------------------- search

def company_filters(domains: List[str]) -> Dict[str, Any]:
    return {"op": "or", "conditions": [
        {"field": "basic_info.primary_domain", "type": "=", "value": d} for d in domains]}


def parse_hq(company: Dict[str, Any]) -> Dict[str, Any]:
    """HQ facts from a /company/search record. Country comes from the end of the
    headquarters string ("Cambridge, Massachusetts, United States")."""
    loc = company.get("locations") or {}
    hq = (loc.get("headquarters") or "").strip()
    country = norm_country(hq.split(",")[-1]) if hq else norm_country(loc.get("country"))
    return {
        "company_id": company.get("crustdata_company_id"),
        "name": (company.get("basic_info") or {}).get("name", ""),
        "domain": normalize_domain((company.get("basic_info") or {}).get("primary_domain")),
        "hq": hq,
        "hq_city": (loc.get("city") or "").strip(),
        "hq_country": country,
        "offices": [a.lower() for a in loc.get("all_office_addresses") or []],
    }


def resolve_companies(client: Crustdata, domains: List[str], batch: int = 25) -> Dict[str, Dict[str, Any]]:
    """domain -> HQ facts. The search can return near matches, so keep exact domains only."""
    found: Dict[str, Dict[str, Any]] = {}
    wanted = set(domains)
    for i in range(0, len(domains), batch):
        chunk = domains[i:i + batch]
        for rec in client.company_search(company_filters(chunk), COMPANY_FIELDS, max_results=len(chunk) * 4):
            info = parse_hq(rec)
            if info["domain"] in wanted and info["domain"] not in found:
                found[info["domain"]] = info
    return found


def job_filters(companies: List[Dict[str, Any]], since: str) -> Dict[str, Any]:
    """Recent posts at these companies, leaving out posts in the HQ city on the server side."""
    per_company = []
    for c in companies:
        conds: List[Dict[str, Any]] = [{"field": "company.basic_info.primary_domain", "type": "=", "value": c["domain"]}]
        if c["hq_city"]:
            conds.append({"field": "location.city", "type": "!=", "value": c["hq_city"]})
        per_company.append({"op": "and", "conditions": conds})
    return {"op": "and", "conditions": [
        {"field": "metadata.date_added", "type": "=>", "value": since},
        {"op": "or", "conditions": per_company},
    ]}


def fetch_jobs(client: Crustdata, companies: List[Dict[str, Any]], since: str,
               max_jobs: int, batch: int = 10) -> List[Dict[str, Any]]:
    jobs: List[Dict[str, Any]] = []
    for i in range(0, len(companies), batch):
        chunk = companies[i:i + batch]
        jobs.extend(client.job_search(job_filters(chunk, since), JOB_FIELDS, max_results=max_jobs,
                                      sorts=[{"field": "metadata.date_added", "order": "desc"}]))
    return jobs


# ---------------------------------------------------------------- rules

def office_signals(jobs: Iterable[Dict[str, Any]], companies: Dict[str, Dict[str, Any]],
                   min_posts: int = 2, include_known: bool = False) -> List[Dict[str, Any]]:
    """Group posts by (company, place) and keep clusters that look like a new office.

    Rules, each unit tested:
      1. remote posts are not an office
      2. a post in the HQ city is not a new office
      3. a post in the HQ country with no city is too vague to place
      4. fewer than `min_posts` posts in one place is noise
      5. a city already listed in the company's office addresses is a known office
    """
    by_id = {c["company_id"]: c for c in companies.values() if c.get("company_id")}
    clusters: Dict[Any, Dict[str, Any]] = {}
    for job in jobs:
        cinfo = (job.get("company") or {}).get("basic_info") or {}
        company = by_id.get(cinfo.get("crustdata_company_id")) or companies.get(normalize_domain(cinfo.get("primary_domain")))
        if not company:
            continue
        details = job.get("job_details") or {}
        if (details.get("workplace_type") or "").lower() == "remote":
            continue
        loc = job.get("location") or {}
        country = norm_country(loc.get("country"))
        city = (loc.get("city") or "").strip()
        if not country and not city:
            continue
        if city and city.lower() == company["hq_city"].lower() and (not country or country == company["hq_country"]):
            continue
        if country == company["hq_country"] and not city:
            continue
        place = city or (loc.get("country") or "")
        key = (company["domain"], country, city.lower())
        c = clusters.setdefault(key, {
            "company_id": company["company_id"], "company": company["name"], "domain": company["domain"],
            "hq": company["hq"], "place": place, "country": loc.get("country") or "", "city": city,
            "kind": "new city" if country == company["hq_country"] else "new country",
            "known_office": bool(city) and any(city.lower() in addr for addr in company["offices"]),
            "posts": 0, "titles": [], "latest_post": "", "job_url": "",
        })
        c["posts"] += 1
        title = details.get("title") or ""
        if title and title not in c["titles"]:
            c["titles"].append(title)
        posted = day((job.get("metadata") or {}).get("date_added"))
        if posted >= c["latest_post"]:
            c["latest_post"], c["job_url"] = posted, details.get("url") or ""
    rows = [c for c in clusters.values() if c["posts"] >= min_posts and (include_known or not c["known_office"])]
    for r in rows:
        r["titles"] = r["titles"][:4]
    return sorted(rows, key=lambda r: (r["posts"], r["latest_post"]), reverse=True)


def attach_owners(rows: List[Dict[str, Any]], customers: List[Dict[str, str]]) -> None:
    owners = {c["domain"]: c["owner"] for c in customers}
    for r in rows:
        r["owner"] = owners.get(r["domain"], "")


# ---------------------------------------------------------------- watcher

def watch_body(domains: List[str], webhook_url: str) -> Dict[str, Any]:
    """POST /watch/company body: fire when a customer's HQ or country changes."""
    return {
        "entities": {"domains": domains},
        "track": {"op": "or", "conditions": [
            {"field": "locations.headquarters", "type": "changed"},
            {"field": "locations.country", "type": "changed"},
        ]},
        "fields": ["basic_info", "locations"],
        "config": {"trigger": {"type": "interval", "every_hours": 24}},
        "notifications": [{"type": "slack", "webhook_url": webhook_url}],
    }


# ---------------------------------------------------------------- output

def summary(rows: List[Dict[str, Any]], accounts_checked: int, accounts_resolved: int, jobs_seen: int) -> Dict[str, Any]:
    return {
        "accounts_checked": accounts_checked,
        "accounts_resolved": accounts_resolved,
        "job_posts_outside_hq_city": jobs_seen,
        "office_signals": len(rows),
        "accounts_with_signal": len({r["domain"] for r in rows}),
        "by_kind": dict(Counter(r["kind"] for r in rows).most_common()),
        "by_post_count": dict(sorted(Counter(("5+" if r["posts"] >= 5 else str(r["posts"])) for r in rows).items())),
    }


def slack_text(rows: List[Dict[str, Any]], since: str, limit: int = 10) -> str:
    lines = [f":office: {len(rows)} possible new customer office(s) since {since}"]
    for r in rows[:limit]:
        owner = f" · owner: {r['owner']}" if r.get("owner") else ""
        lines.append(f"• *{r['company']}* hiring in *{r['place']}* ({r['kind']}, HQ {r['hq'] or '?'}): "
                     f"{r['posts']} posts, latest {r['latest_post']}{owner} <{r['job_url']}|job>")
        if r.get("offer"):
            lines.append(f"   _Offer:_ {r['offer']}")
    if len(rows) > limit:
        lines.append(f"…and {len(rows) - limit} more in out/offices.csv")
    return "\n".join(lines)


def offer_prompt(r: Dict[str, Any]) -> str:
    return (f"Our customer {r['company']} (HQ {r['hq'] or 'unknown'}) is hiring {r['posts']} people in {r['place']}, "
            f"for roles like {', '.join(r['titles'][:3])}. Write a 2-sentence note from their account owner offering "
            "to onboard the new team on our product (a kickoff call and seat setup). Specific, no hype, no emojis. "
            "Return only the note.")


def write_outputs(rows: List[Dict[str, Any]], info: Dict[str, Any], out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "offices.json"), "w") as fh:
        json.dump({"summary": info, "rows": rows}, fh, indent=1)
    with open(os.path.join(out_dir, "offices.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["company", "domain", "owner", "hq", "place", "kind", "posts", "latest_post", "titles", "job_url", "offer"])
        for r in rows:
            w.writerow([r["company"], r["domain"], r.get("owner", ""), r["hq"], r["place"], r["kind"], r["posts"],
                        r["latest_post"], "; ".join(r["titles"]), r["job_url"], r.get("offer", "")])


def row_key(r: Dict[str, Any]) -> str:
    return f"{r['domain']}:{(r['country'] or '').lower()}:{(r['city'] or '').lower()}"


def _load_state(path: str) -> set:
    if os.path.exists(path):
        with open(path) as fh:
            return set(json.load(fh))
    return set()


def only_new(rows: List[Dict[str, Any]], state_path: str) -> List[Dict[str, Any]]:
    seen = _load_state(state_path)
    return [r for r in rows if row_key(r) not in seen]


def save_state(rows: List[Dict[str, Any]], state_path: str) -> None:
    seen = _load_state(state_path)
    seen.update(row_key(r) for r in rows)
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    with open(state_path, "w") as fh:
        json.dump(sorted(seen), fh, indent=1)


def run(client: Crustdata, customers: List[Dict[str, str]], since: str, min_posts: int = 2,
        max_jobs: int = 500, include_known: bool = False):
    companies = resolve_companies(client, [c["domain"] for c in customers])
    jobs = fetch_jobs(client, list(companies.values()), since, max_jobs)
    rows = office_signals(jobs, companies, min_posts, include_known)
    attach_owners(rows, customers)
    return rows, companies, jobs


def main(argv: Optional[List[str]] = None) -> int:
    load_env()
    p = argparse.ArgumentParser(description="Customer Office Radar (Crustdata data layer)")
    p.add_argument("--customers", default=os.environ.get("CUSTOMERS_CSV", "examples/customers.example.csv"))
    p.add_argument("--since-days", type=int, default=int(os.environ.get("SINCE_DAYS", "30")))
    p.add_argument("--min-posts", type=int, default=int(os.environ.get("MIN_POSTS", "2")))
    p.add_argument("--max-jobs", type=int, default=500, help="job posts read per batch of 10 accounts")
    p.add_argument("--include-known", action="store_true", help="keep cities already listed as an office")
    p.add_argument("--offers", action="store_true", help="draft an onboarding offer per signal with Claude")
    p.add_argument("--create-watch", action="store_true",
                   help="also create a daily POST /watch/company watch on HQ changes (needs SLACK_WEBHOOK_URL)")
    p.add_argument("--dry-run", action="store_true", help="never send to Slack or create watches")
    p.add_argument("--all", action="store_true", help="include signals already alerted on")
    p.add_argument("--out", default="out")
    p.add_argument("--state", default=".state/seen.json")
    args = p.parse_args(argv)

    customers = load_customers(args.customers)
    since = (dt.date.today() - dt.timedelta(days=args.since_days)).isoformat()
    client = Crustdata()
    rows, companies, jobs = run(client, customers, since, args.min_posts, args.max_jobs, args.include_known)
    if not args.all:
        rows = only_new(rows, args.state)
    if args.offers:
        if not claude_available():
            print("Claude not configured (set ANTHROPIC_API_KEY and pip install anthropic): skipping offers", file=sys.stderr)
        else:
            for r in rows:
                r["offer"] = ask_claude(offer_prompt(r)) or ""
    info = summary(rows, len(customers), len(companies), len(jobs))
    write_outputs(rows, info, args.out)
    print(json.dumps(info, indent=1))
    if args.create_watch and not args.dry_run:
        hook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if hook:
            client.watch_company(watch_body(list(companies), hook))
            print("Watch created: daily check on HQ changes")
        else:
            print("SLACK_WEBHOOK_URL not set: watch not created")
    if rows:
        print(send_slack(slack_text(rows, since), dry_run=args.dry_run))
        if not args.dry_run:
            save_state(rows, args.state)
    else:
        print("No new office signals.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
