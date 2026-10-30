"""PLAIN ENGLISH: A safe, READ-ONLY "does our Salesforce connection work?" check — run it against your SANDBOX first.

It will: (1) log in, (2) ping the API, (3) optionally look up one client number and report which fields came back.
It NEVER writes anything. To keep client data off your screen it prints only whether fields are present, not their values.
Usage (after filling in the SALESFORCE_* settings in .env):
    python scripts/salesforce_smoke_test.py            # login + ping
    python scripts/salesforce_smoke_test.py C10482     # ...plus look up this client number
"""
import sys

from org_automation.integrations.crm import CrmError
from org_automation.integrations.salesforce import SalesforceCrmAdapter


def main() -> int:
    try:
        sf = SalesforceCrmAdapter.from_settings()
        print("1. configuration looks valid")
        sf.ping()
        print("2. logged in and API reachable")
        if len(sys.argv) > 1:
            record = sf.get_record(sys.argv[1])
            if record is None:
                print(f"3. no record found with client number {sys.argv[1]!r} (check the External ID field name and the data)")
            else:
                print("3. record found; fields present:", {k: ("yes" if v else "EMPTY") for k, v in record.items()})
        print("done (nothing was written)")
        return 0
    except CrmError as exc:
        print(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
