"""Vercel serverless function: proxy Google Ads API for the dashboard.

GET /api/google?brand=samsonite|american_tourister&objective=ec|branding
            &since=YYYY-MM-DD&until=YYYY-MM-DD
Returns: { "rows": [ {date, campaign_name, cost, clicks, impressions, conversions, conversions_value}, ... ] }

`objective` picks which Google Ads account to read: each brand runs EC and
Branding out of separate accounts. It defaults to "ec" so older callers keep
working.

Secrets are read from environment variables (set them in the Vercel dashboard):
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN
  GOOGLE_DEVELOPER_TOKEN
  GOOGLE_LOGIN_CUSTOMER_ID
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import urllib.parse
import requests

API_VERSION = "v22"

# objective -> brand -> customer id. All four sit under the same MCC
# (GOOGLE_LOGIN_CUSTOMER_ID), so one refresh token covers them.
CUSTOMER_IDS = {
    "ec": {
        "samsonite": "7170765915",           # 717-076-5915
        "american_tourister": "3650448748",  # 365-044-8748
    },
    "branding": {
        "samsonite": "8266755074",           # 826-675-5074
        "american_tourister": "9732762027",  # 973-276-2027
    },
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def get_access_token():
    r = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": os.environ["GOOGLE_CLIENT_ID"],
            "client_secret": os.environ["GOOGLE_CLIENT_SECRET"],
            "refresh_token": os.environ["GOOGLE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def run_query(cid, token, query):
    r = requests.post(
        f"https://googleads.googleapis.com/{API_VERSION}/customers/{cid}/googleAds:searchStream",
        headers={
            "Authorization": f"Bearer {token}",
            "developer-token": os.environ["GOOGLE_DEVELOPER_TOKEN"],
            "login-customer-id": os.environ["GOOGLE_LOGIN_CUSTOMER_ID"],
        },
        json={"query": query},
        timeout=60,
    )
    r.raise_for_status()
    results = []
    for batch in r.json():
        results.extend(batch.get("results", []))
    return results


def query_add_to_cart(cid, token, since, until):
    """Add-to-cart is NOT in metrics.conversions (those actions have
    include_in_conversions_metric = false), so it has to be read from
    metrics.all_conversions segmented by conversion action.

    A campaign can have several ADD_TO_CART actions firing on the same event
    (e.g. Samsonite has both the Adgeek button tag and GA4's add_to_cart).
    Summing the category would double-count, so when an "Adgeek" action exists
    we use only that one — it is the tag both brands share, and the one that
    matches how Meta counts. Otherwise we fall back to the category total.
    """
    query = (
        "SELECT segments.date, campaign.name, segments.conversion_action_name, "
        "segments.conversion_action_category, metrics.all_conversions "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{since}' AND '{until}' "
        "AND segments.conversion_action_category = 'ADD_TO_CART'"
    )
    results = run_query(cid, token, query)

    has_adgeek = any(
        "adgeek" in res.get("segments", {}).get("conversionActionName", "").lower()
        for res in results
    )
    atc = {}
    for res in results:
        seg = res.get("segments", {})
        name = seg.get("conversionActionName", "")
        if has_adgeek and "adgeek" not in name.lower():
            continue
        key = (seg.get("date"), res.get("campaign", {}).get("name"))
        val = float(res.get("metrics", {}).get("allConversions", 0))
        atc[key] = atc.get(key, 0.0) + val
    return atc


def query_ads(brand, since, until, objective="ec"):
    cid = CUSTOMER_IDS[objective][brand]
    token = get_access_token()
    query = (
        "SELECT segments.date, campaign.name, metrics.cost_micros, "
        "metrics.clicks, metrics.impressions, metrics.conversions, metrics.conversions_value "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{since}' AND '{until}'"
    )
    results = run_query(cid, token, query)

    # Branding accounts have no purchase funnel, so skip the extra round trip.
    atc = {}
    if objective == "ec":
        try:
            atc = query_add_to_cart(cid, token, since, until)
        except Exception:
            atc = {}  # never let add-to-cart break the main numbers

    rows = []
    for res in results:
        m = res.get("metrics", {})
        date = res.get("segments", {}).get("date")
        cname = res.get("campaign", {}).get("name")
        rows.append({
            "date": date,
            "campaign_name": cname,
            "cost": int(m.get("costMicros", 0)) / 1e6,
            "clicks": int(m.get("clicks", 0)),
            "impressions": int(m.get("impressions", 0)),
            "conversions": float(m.get("conversions", 0)),
            "conversions_value": float(m.get("conversionsValue", 0)),
            "add_to_cart": atc.get((date, cname), 0.0),
        })
    return rows


class handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            brand = params.get("brand", [""])[0]
            objective = params.get("objective", ["ec"])[0] or "ec"
            since = params.get("since", [""])[0]
            until = params.get("until", [""])[0]

            if objective not in CUSTOMER_IDS:
                return self._send(400, {"error": f"invalid objective: {objective}"})
            if brand not in CUSTOMER_IDS[objective]:
                return self._send(400, {"error": f"invalid brand: {brand}"})
            if not DATE_RE.match(since) or not DATE_RE.match(until):
                return self._send(400, {"error": "since/until must be YYYY-MM-DD"})

            rows = query_ads(brand, since, until, objective)
            self._send(200, {"rows": rows})
        except requests.HTTPError as e:
            detail = e.response.text if e.response is not None else str(e)
            self._send(502, {"error": "google_ads_api_error", "detail": detail[:1000]})
        except Exception as e:
            self._send(500, {"error": str(e)})
