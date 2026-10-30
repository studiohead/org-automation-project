"""PLAIN ENGLISH: A pretend Salesforce org that lives in memory, for testing without internet or an account.
It behaves like the real service in the ways our code relies on: it demands a login, hands out a token, rejects
calls with a bad/expired token (401), answers lookups with ONLY the fields that were asked for, and applies updates.
It also records every request so tests can check exactly what we sent (and that we never sent anything extra)."""
import re
from urllib.parse import parse_qs

import httpx

DOMAIN = "https://acme.my.salesforce.com"
INSTANCE = "https://acme.my.salesforce.com"


class FakeSalesforceOrg:
    def __init__(self, records=None, secret="s3cret"):
        self.records = records if records is not None else [
            {"Id": "003A", "Client_ID__c": "C10482", "Email": "old.client@example.com", "Email_Effective_Date__c": "2025-01-01"},
            {"Id": "003B", "Client_ID__c": "C10001", "Email": "two@example.com", "Email_Effective_Date__c": "2024-06-30"},
        ]
        self.secret, self.token_n, self.valid_token = secret, 0, None
        self.requests: list[httpx.Request] = []
        self.patches: list[tuple[str, dict]] = []
        self.queries: list[str] = []
        self.fail_with: int | None = None          # force every data call to fail with this HTTP status
        self.token_status = 200

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def expire_token(self):
        self.valid_token = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/services/oauth2/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if self.token_status != 200 or form.get("grant_type") != "client_credentials" or form.get("client_secret") != self.secret:
                return httpx.Response(self.token_status if self.token_status != 200 else 400, json={"error": "invalid_client"})
            self.token_n += 1
            self.valid_token = f"tok-{self.token_n}"
            return httpx.Response(200, json={"access_token": self.valid_token, "instance_url": INSTANCE})
        if request.headers.get("Authorization") != f"Bearer {self.valid_token}":
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "Session expired"}])
        if self.fail_with:
            return httpx.Response(self.fail_with, json=[{"errorCode": "SERVER_ERROR", "message": "client data here"}])
        if request.method == "GET" and re.fullmatch(r"/services/data/v\d+\.0/", path):
            return httpx.Response(200, json={"sobjects": "/services/data/v62.0/sobjects"})
        if request.method == "GET" and path.endswith("/query"):
            soql = request.url.params["q"]
            self.queries.append(soql)
            cols = [c.strip() for c in re.match(r"SELECT (.+?) FROM", soql).group(1).split(",")]
            who = re.search(r"WHERE (\w+) = '([^']*)'", soql)
            hits = [r for r in self.records if r.get(who.group(1)) == who.group(2)]
            rows = [{"attributes": {"type": "Contact"}, **{c: r.get(c) for c in cols}} for r in hits]
            return httpx.Response(200, json={"totalSize": len(rows), "done": True, "records": rows})
        if request.method == "PATCH" and "/sobjects/Contact/" in path:
            import json
            sf_id, body = path.rsplit("/", 1)[1], json.loads(request.content)
            rec = next((r for r in self.records if r["Id"] == sf_id), None)
            if rec is None:
                return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": "x"}])
            rec.update(body)
            self.patches.append((sf_id, body))
            return httpx.Response(204)
        return httpx.Response(404, json=[{"errorCode": "NOT_FOUND", "message": "x"}])
