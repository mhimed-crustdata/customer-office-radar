import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import office_radar as radar  # noqa: E402
from crustdata_api import Crustdata  # noqa: E402

ACME = {"company_id": 1, "name": "Acme", "domain": "acme.com", "hq": "Cambridge, Massachusetts, United States",
        "hq_city": "Cambridge", "hq_country": "united states",
        "offices": ["57 southwark street, london, england, se1 irl, gb"]}
COMPANIES = {"acme.com": ACME}


def job(city="Austin", country="United States", wp="Hybrid", title="Account Executive", date="2026-11-01T10:00:00"):
    return {"job_details": {"title": title, "url": f"https://example.com/{title}/{city}", "workplace_type": wp},
            "company": {"basic_info": {"crustdata_company_id": 1, "primary_domain": "acme.com"}},
            "location": {"city": city, "country": country}, "metadata": {"date_added": date}}


class RuleTests(unittest.TestCase):
    def test_cluster_in_new_city_passes(self):
        rows = radar.office_signals([job(), job(title="SDR")], COMPANIES)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "new city")
        self.assertEqual(rows[0]["posts"], 2)
        self.assertEqual(rows[0]["titles"], ["Account Executive", "SDR"])

    def test_new_country_kind(self):
        rows = radar.office_signals([job("Tokyo", "Japan"), job("Tokyo", "Japan", title="SDR")], COMPANIES)
        self.assertEqual(rows[0]["kind"], "new country")

    def test_single_post_is_noise(self):
        self.assertEqual(radar.office_signals([job()], COMPANIES), [])
        self.assertEqual(len(radar.office_signals([job()], COMPANIES, min_posts=1)), 1)

    def test_remote_posts_are_not_an_office(self):
        self.assertEqual(radar.office_signals([job(wp="Remote"), job(wp="Remote")], COMPANIES), [])

    def test_hq_city_is_not_new(self):
        self.assertEqual(radar.office_signals([job("Cambridge"), job("cambridge")], COMPANIES), [])

    def test_country_alias_matches_hq(self):
        # "USA" and "United States" are the same country; a country-only post at HQ country is too vague
        self.assertEqual(radar.office_signals([job(city="", country="USA"), job(city="", country="USA")], COMPANIES), [])

    def test_known_office_is_dropped_unless_asked(self):
        jobs = [job("London", "United Kingdom"), job("London", "United Kingdom", title="SDR")]
        self.assertEqual(radar.office_signals(jobs, COMPANIES), [])
        rows = radar.office_signals(jobs, COMPANIES, include_known=True)
        self.assertTrue(rows[0]["known_office"])

    def test_unknown_company_is_ignored(self):
        stray = job()
        stray["company"]["basic_info"] = {"crustdata_company_id": 99, "primary_domain": "other.com"}
        self.assertEqual(radar.office_signals([stray, stray], COMPANIES), [])

    def test_latest_post_and_ordering(self):
        rows = radar.office_signals(
            [job(date="2026-10-01T00:00:00"), job(date="2026-11-03T00:00:00"),
             job("Dublin", "Ireland"), job("Dublin", "Ireland"), job("Dublin", "Ireland")], COMPANIES)
        self.assertEqual(rows[0]["place"], "Dublin")
        self.assertEqual(rows[1]["latest_post"], "2026-11-03")


class InputTests(unittest.TestCase):
    def test_load_customers_reads_hubspot_headers_and_dedupes(self):
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as fh:
            fh.write("Company name,Company domain name,Company owner\nAcme,https://www.Acme.com/,a@x.io\nAcme again,acme.com,b@x.io\nNo domain,,c@x.io\n")
        try:
            rows = radar.load_customers(fh.name)
        finally:
            os.unlink(fh.name)
        self.assertEqual(rows, [{"domain": "acme.com", "name": "Acme", "owner": "a@x.io"}])

    def test_parse_hq_takes_country_from_headquarters_string(self):
        info = radar.parse_hq({"crustdata_company_id": 5, "basic_info": {"name": "A", "primary_domain": "a.com"},
                               "locations": {"country": "USA", "headquarters": "Dublin, Ireland", "city": "Dublin"}})
        self.assertEqual(info["hq_country"], "ireland")
        self.assertEqual(info["hq_city"], "Dublin")


class QueryTests(unittest.TestCase):
    def test_job_filters_skip_hq_city_per_company(self):
        f = radar.job_filters([ACME], "2026-10-01")
        self.assertEqual(f["conditions"][0], {"field": "metadata.date_added", "type": "=>", "value": "2026-10-01"})
        inner = f["conditions"][1]["conditions"][0]["conditions"]
        self.assertIn({"field": "location.city", "type": "!=", "value": "Cambridge"}, inner)
        self.assertIn({"field": "company.basic_info.primary_domain", "type": "=", "value": "acme.com"}, inner)

    def test_watch_body_tracks_hq_changes(self):
        body = radar.watch_body(["acme.com"], "https://hooks.invalid/x")
        fields = [c["field"] for c in body["track"]["conditions"]]
        self.assertEqual(fields, ["locations.headquarters", "locations.country"])
        self.assertEqual(body["entities"], {"domains": ["acme.com"]})

    def test_run_uses_documented_endpoints(self):
        calls = []

        def transport(url, body, headers, timeout):
            calls.append((url, body, headers))
            if url.endswith("/company/search"):
                return {"companies": [{"crustdata_company_id": 1, "basic_info": {"name": "Acme", "primary_domain": "acme.com"},
                                       "locations": {"headquarters": "Cambridge, Massachusetts, United States",
                                                     "city": "Cambridge", "all_office_addresses": []}},
                                      {"crustdata_company_id": 2, "basic_info": {"name": "Near", "primary_domain": "near.com"},
                                       "locations": {}}], "next_cursor": None}
            return {"job_listings": [job(), job(title="SDR")], "next_cursor": None}

        rows, companies, jobs = radar.run(Crustdata(api_key="test", transport=transport),
                                          [{"domain": "acme.com", "name": "Acme", "owner": "a@x.io"}], "2026-10-01")
        self.assertEqual([u.split("/", 3)[3] for u, _, _ in calls], ["company/search", "job/search"])
        self.assertTrue(all(h["x-api-version"] == "2025-11-01" for _, _, h in calls))
        self.assertEqual(list(companies), ["acme.com"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner"], "a@x.io")

    def test_watch_company_endpoint(self):
        calls = []
        Crustdata(api_key="t", transport=lambda u, b, h, t: calls.append(u) or {}).watch_company({})
        self.assertTrue(calls[0].endswith("/watch/company"))


class OutputTests(unittest.TestCase):
    def rows(self):
        return radar.office_signals([job(), job(title="SDR")], COMPANIES)

    def test_slack_text(self):
        text = radar.slack_text(self.rows(), "2026-10-01")
        self.assertTrue(text.startswith(":office: 1 possible new customer office(s) since 2026-10-01"))
        self.assertIn("*Acme* hiring in *Austin*", text)

    def test_summary_counts(self):
        s = radar.summary(self.rows(), 3, 2, 10)
        self.assertEqual((s["office_signals"], s["accounts_with_signal"], s["by_kind"]), (1, 1, {"new city": 1}))
        self.assertEqual(s["by_post_count"], {"2": 1})

    def test_state_dedupes(self):
        rows = self.rows()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "seen.json")
            self.assertEqual(len(radar.only_new(rows, path)), 1)
            radar.save_state(rows, path)
            self.assertEqual(radar.only_new(rows, path), [])


if __name__ == "__main__":
    unittest.main()
