"""Vercel serverless function: proxy the Meta Graph API for the dashboard.

The dashboard is served from a PUBLIC GitHub Pages site, so the access token
cannot live in index.html — anyone viewing source would get read access to all
four ad accounts. It is read from the environment here instead:

  META_ACCESS_TOKEN   (set it in the Vercel dashboard)

Two modes, both GET:

  /api/meta?mode=insights&brand=..&objective=..&level=..&since=..&until=..
            [&time_increment=1]
      -> {"data": [ ...all pages already merged... ]}

  /api/meta?mode=objects&fieldset=ad_status|adset_status&ids=1,2,3
      -> {"<id>": {...}, ...}   (raw Graph shape, callers read Object.values)

Errors come back as {"error": {"message": ...}} so the frontend can keep using
the same `json.error` check it used when it called Graph directly.

Deliberately narrow surface: the caller never supplies an account id, a raw
field list, or a URL. Everything is chosen from a server-side allowlist, so a
stranger who finds this endpoint cannot turn it into a general-purpose reader
for the token's whole permission scope.
"""
from http.server import BaseHTTPRequestHandler
import json
import os
import re
import urllib.parse
import requests

API_VERSION = "v21.0"
GRAPH_URL = f"https://graph.facebook.com/{API_VERSION}"

# objective -> brand -> ad account. Mirrors ACCOUNT_IDS in index.html.
ACCOUNT_IDS = {
    "branding": {
        "samsonite": "act_1318174565548910",
        "american_tourister": "act_1530988521034172",
    },
    "ec": {
        "samsonite": "act_599620824954716",
        "american_tourister": "act_484946893063783",
    },
}

# Branding has no purchase funnel, so it never asks for actions/action_values.
OBJECTIVE_FIELDS = {
    "branding": "spend,impressions,cpm,inline_link_clicks,inline_link_click_ctr,cost_per_inline_link_click",
    "ec": "spend,impressions,inline_link_clicks,cost_per_inline_link_click,actions,action_values",
}

LEVEL_EXTRA_FIELDS = {
    "account": "",
    "campaign": ",campaign_name,campaign_id",
    "adset": ",adset_name,adset_id,campaign_name",
    "ad": ",ad_name,ad_id,adset_name,campaign_name",
}

# Named field sets for the /?ids=... batch lookups. Callers pick a name; they
# cannot pass raw fields.
OBJECT_FIELDSETS = {
    "ad_status": "name,effective_status,creative{thumbnail_url,image_url,object_story_spec},adset{end_time},campaign{stop_time}",
    "adset_status": "name,effective_status,end_time,campaign{stop_time}",
}

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IDS_RE = re.compile(r"^\d+(,\d+)*$")
MAX_IDS = 50
MAX_PAGES = 50  # hard stop so a runaway cursor can't hang the function

# Breakdown used to work around the Graph daily-spend fault (see _patch_zero_spend).
# publisher_platform is the cheapest one: a handful of rows per day, and it
# partitions delivery exactly, so the parts sum back to the true daily total.
REPAIR_BREAKDOWN = "publisher_platform"


def _token():
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("META_ACCESS_TOKEN is not set")
    return token


def fetch_insights(brand, objective, level, since, until, time_increment=None):
    """Walk every page of the insights cursor and return the merged rows.

    Pagination has to happen here rather than in the browser: Graph's
    `paging.next` is a fully-formed URL with the access token embedded, so
    handing it to the client would leak exactly what this proxy exists to hide.
    """
    fields = OBJECTIVE_FIELDS[objective] + LEVEL_EXTRA_FIELDS[level]
    params = {
        "access_token": _token(),
        "fields": fields,
        "time_range": json.dumps({"since": since, "until": until}),
        "level": level,
        "limit": 500,
    }
    if time_increment:
        params["time_increment"] = time_increment

    account_id = ACCOUNT_IDS[objective][brand]
    url = f"{GRAPH_URL}/{account_id}/insights"
    rows = []
    for _ in range(MAX_PAGES):
        r = requests.get(url, params=params, timeout=60)
        body = r.json()
        if "error" in body:
            return {"error": body["error"]}
        rows.extend(body.get("data", []))
        url = body.get("paging", {}).get("next")
        if not url:
            break
        params = None  # `next` already carries every query parameter

    if time_increment:
        rows = _patch_zero_spend(
            account_id, fields, since, until, level, time_increment, rows)
    return {"data": rows}


def _fault_rows(rows):
    """Rows showing the Graph daily fault: zero spend/impressions yet real clicks.

    A day that genuinely had no delivery reports zero across the board, so
    requiring clicks > 0 keeps real zero-spend days from being "repaired".
    """
    return [
        r for r in rows
        if float(r.get("spend") or 0) == 0
        and int(r.get("impressions") or 0) == 0
        and int(r.get("inline_link_clicks") or 0) > 0
    ]


def _patch_zero_spend(account_id, fields, since, until, level, time_increment, rows):
    """Repair daily rows that Graph returned with spend/impressions zeroed out.

    As of 2026-10, `time_increment=1` on some accounts comes back with
    spend == impressions == 0 while inline_link_clicks and actions are correct.
    The same request with any `breakdowns` returns the real figures, and the
    breakdown parts sum back to the un-incremented total exactly, so we refetch
    broken down and copy only spend/impressions onto the affected days.

    Conversions are deliberately NOT touched: `actions`/`action_values` already
    arrive correct, and merging them from breakdown rows would risk
    double-counting. Derived rates are recomputed by the dashboard from these
    raw counts, so patching the two raw fields repairs everything downstream.
    """
    broken = _fault_rows(rows)
    if not broken:
        return rows

    params = {
        "access_token": _token(),
        "fields": "spend,impressions",
        "time_range": json.dumps({"since": since, "until": until}),
        "level": level,
        "time_increment": time_increment,
        "breakdowns": REPAIR_BREAKDOWN,
        "limit": 500,
    }
    url = f"{GRAPH_URL}/{account_id}/insights"
    totals = {}
    for _ in range(MAX_PAGES):
        r = requests.get(url, params=params, timeout=60)
        body = r.json()
        if "error" in body:
            return rows  # repair is best-effort; never fail the whole request
        for part in body.get("data", []):
            day = totals.setdefault(part.get("date_start"), [0.0, 0])
            day[0] += float(part.get("spend") or 0)
            day[1] += int(part.get("impressions") or 0)
        url = body.get("paging", {}).get("next")
        if not url:
            break
        params = None

    for row in broken:
        fixed = totals.get(row.get("date_start"))
        if not fixed:
            continue
        row["spend"] = f"{fixed[0]:.2f}"
        row["impressions"] = str(fixed[1])
        # Stale zeros Graph derived from the bad spend. The dashboard rebuilds
        # these from spend/impressions/clicks, so drop them rather than guess.
        for derived in ("cpm", "cost_per_inline_link_click", "inline_link_click_ctr"):
            row.pop(derived, None)
    return rows


def fetch_objects(fieldset, ids):
    r = requests.get(
        f"{GRAPH_URL}/",
        params={
            "access_token": _token(),
            "ids": ids,
            "fields": OBJECT_FIELDSETS[fieldset],
        },
        timeout=60,
    )
    body = r.json()
    if "error" in body:
        return {"error": body["error"]}
    return body


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

    def _fail(self, code, message):
        self._send(code, {"error": {"message": message}})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            mode = params.get("mode", ["insights"])[0]

            if mode == "insights":
                brand = params.get("brand", [""])[0]
                objective = params.get("objective", [""])[0]
                level = params.get("level", [""])[0]
                since = params.get("since", [""])[0]
                until = params.get("until", [""])[0]
                time_increment = params.get("time_increment", [""])[0]

                if objective not in ACCOUNT_IDS:
                    return self._fail(400, f"invalid objective: {objective}")
                if brand not in ACCOUNT_IDS[objective]:
                    return self._fail(400, f"invalid brand: {brand}")
                if level not in LEVEL_EXTRA_FIELDS:
                    return self._fail(400, f"invalid level: {level}")
                if not DATE_RE.match(since) or not DATE_RE.match(until):
                    return self._fail(400, "since/until must be YYYY-MM-DD")
                if time_increment and time_increment != "1":
                    return self._fail(400, "time_increment must be 1")

                return self._send(200, fetch_insights(
                    brand, objective, level, since, until, time_increment))

            if mode == "objects":
                fieldset = params.get("fieldset", [""])[0]
                ids = params.get("ids", [""])[0]
                if fieldset not in OBJECT_FIELDSETS:
                    return self._fail(400, f"invalid fieldset: {fieldset}")
                if not IDS_RE.match(ids):
                    return self._fail(400, "ids must be a comma-separated list of numbers")
                if len(ids.split(",")) > MAX_IDS:
                    return self._fail(400, f"at most {MAX_IDS} ids per request")
                return self._send(200, fetch_objects(fieldset, ids))

            return self._fail(400, f"invalid mode: {mode}")

        except requests.HTTPError as e:
            detail = e.response.text if e.response is not None else str(e)
            self._fail(502, f"meta_graph_api_error: {detail[:500]}")
        except Exception as e:
            self._fail(500, str(e))
