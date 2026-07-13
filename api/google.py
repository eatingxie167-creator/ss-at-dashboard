"""Vercel serverless function: proxy Google Ads API for the dashboard.

GET /api/google?brand=samsonite|american_tourister&since=YYYY-MM-DD&until=YYYY-MM-DD
Returns: { "rows": [ {date, campaign_name, cost, clicks, conversions, conversions_value}, ... ] }

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

API_VERSION = "v21"

CUSTOMER_IDS = {
    "samsonite": "7170765915",
    "american_tourister": "3650448748",
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


def query_ads(brand, since, until):
    cid = CUSTOMER_IDS[brand]
    token = get_access_token()
    query = (
        "SELECT segments.date, campaign.name, metrics.cost_micros, "
        "metrics.clicks, metrics.conversions, metrics.conversions_value "
        "FROM campaign "
        f"WHERE segments.date BETWEEN '{since}' AND '{until}'"
    )
    r = requests.post(
        f"https://googleads.googleapis.com/{API_VERSION}/customers/{cid}/googleAds:searchStream",
        headers={
            "Authorization": f"Bearer {token}",
            "developer-token": os.environ["GOOGLE_DEVELOPER_TOKEN"],
            "login-customer-id": os.environ["GOOGLE_LOGIN_CUSTOMER_ID"],
        },
        json={"query": query},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    rows = []
    for batch in data:
        for res in batch.get("results", []):
            m = res.get("metrics", {})
            rows.append({
                "date": res.get("segments", {}).get("date"),
                "campaign_name": res.get("campaign", {}).get("name"),
                "cost": int(m.get("costMicros", 0)) / 1e6,
                "clicks": int(m.get("clicks", 0)),
                "conversions": float(m.get("conversions", 0)),
                "conversions_value": float(m.get("conversionsValue", 0)),
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
            since = params.get("since", [""])[0]
            until = params.get("until", [""])[0]

            if brand not in CUSTOMER_IDS:
                return self._send(400, {"error": f"invalid brand: {brand}"})
            if not DATE_RE.match(since) or not DATE_RE.match(until):
                return self._send(400, {"error": "since/until must be YYYY-MM-DD"})

            rows = query_ads(brand, since, until)
            self._send(200, {"rows": rows})
        except requests.HTTPError as e:
            detail = e.response.text if e.response is not None else str(e)
            self._send(502, {"error": "google_ads_api_error", "detail": detail[:1000]})
        except Exception as e:
            self._send(500, {"error": str(e)})
