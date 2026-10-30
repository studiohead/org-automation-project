# Connecting to Salesforce

**Plain English:** create a restricted "robot user" in Salesforce that can read and edit exactly three fields, give
this app a key for that user, and test in a **sandbox** before anything near production. I could not test against a
live org, and Salesforce renames settings screens often, so confirm each step against Salesforce's current documentation
and with your Salesforce admin.

## 1. Decide how a client is identified
The app finds a client by the firm's own client number (like `C10482`), stored in a Salesforce **custom field marked
External ID and Unique** (default API name `Client_ID__c`). Two more fields are used (defaults shown):

| App concept | Default Salesforce field | Notes |
|---|---|---|
| client number | `Client_ID__c` (Text, External ID, Unique) | the lookup key |
| email | `Email` | for Person Accounts the field is usually `PersonEmail` on `Account` |
| effective date | `Email_Effective_Date__c` (Date) | custom field; there is no standard equivalent |

Wealth firms often use **Financial Services Cloud / Person Accounts**. If so, set `SALESFORCE_OBJECT=Account` and
`SALESFORCE_EMAIL_FIELD=PersonEmail`. Ask your admin which model the org uses; that is the main thing that can differ.

## 2. Create the integration identity (principle of least privilege)
1. Create a dedicated **integration user** (no human login, API-only profile if available).
2. Create a **permission set** granting only: read on the client object; read + edit on the email and effective-date
   fields; read on the client-number field. Nothing else. (Field-level security is the real backstop: even a bug or a
   hijacked AI cannot touch fields this user can't edit.)
3. Create an **External Client App** (older orgs: *Connected App*) with OAuth enabled, the **client credentials flow**
   turned on, "run as" set to the integration user, and scopes limited to API access (`api`). Restrict by IP if you can.
4. Copy the consumer key and secret into your secrets manager. (For a stricter production posture, Salesforce's
   JWT-bearer flow with a certificate avoids shared secrets; this adapter's login method is the one place to change.)

## 3. Configure the app (`.env`)
```
CRM_PROVIDER=salesforce
SALESFORCE_DOMAIN=https://yourfirm--sandbox.sandbox.my.salesforce.com   # your My Domain URL; sandbox first
SALESFORCE_CLIENT_ID=<consumer key>
SALESFORCE_CLIENT_SECRET=<consumer secret>
SALESFORCE_API_VERSION=v62.0                                           # set to your org's current version
```
Only `https` URLs on `salesforce.com` / `force.com` are accepted; anything else is refused at startup.

## 4. Verify, read-only, in the sandbox
```bash
python scripts/salesforce_smoke_test.py C10482   # use a real sandbox client number
uvicorn org_automation.main:app            # then: GET /health/crm with a reviewer key
```
Then run the normal demo (`/agent/runs/...`, approve) against **sandbox** records only, and confirm the Salesforce
field history shows exactly two fields changed.

## What the adapter guarantees (and doesn't)
- Asks Salesforce for only the two mapped fields; never reads names, addresses, balances.
- Refuses to write any field except email and effective date.
- Refuses to overwrite a value that changed after the human reviewed it (a person re-reviews instead).
- Re-applying the same update is a harmless no-op. A very small window between re-read and write remains (the REST API has
  no compare-and-set); rely on field-level security and Salesforce field history/audit trail as backstops.
- Retries and an "outbox" for outages are not built: a failed apply is reported, stays approved, and can be retried
  through the agent's resume run.

## Start in shadow mode

With `CRM_PROVIDER=salesforce` the app defaults to `WRITE_MODE=shadow`: it reads records and records "would have written" entries but never
writes. For the first run, also give the integration user a **read-only** permission set, so Salesforce itself guarantees nothing can be changed.
When you are ready for real writes, grant edit access to only the two fields the app updates, set `WRITE_MODE=enabled` in `.env`, and restart.
