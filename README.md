# Tesla Invoices

A Python service that checks for Tesla Supercharger and Premium Connectivity invoices and emails new PDFs via SMTP. By default it runs on weekdays at 12:00 Europe/Berlin time. A persistent Docker volume stores the invoice database, delivery history, and Tesla browser session. Each PDF is archived in S3 before it can be emailed.

## Deploy on Coolify

1. Create an application using **Docker Compose** from this repository. Select branch `main` and Compose file `/compose.yaml`. This is a background worker. It needs a single replica, the persistent `invoices` volume mounted at `/data`, and no public domain, port, or Coolify scheduled task. Keep the same volume on redeploy and include it in backups.
2. Configure the variables from [`.env.example`](.env.example) in Coolify as runtime settings. Set `MAIL_TO` to Heinrich's email address. Set the Tesla country URL, invoice sources, SMTP settings, and S3 settings described below. Supply credentials through Coolify's environment settings and keep them out of the repository.
3. Deploy and check that the logs say `Schedule active`. The worker starts the day's check at noon Berlin time, including daylight saving changes. If started later on an eligible day, it runs that day's outstanding check. Failed checks retry after 30 minutes during the active window. The next eligible check discovers invoices published during missed days and resumes locally queued deliveries.
4. If you want the Supercharger fallback available, complete the one-time [Tesla Fleet authorization](#supercharger-fallback). In the worker terminal, run `python -m tesla_invoice run --download-only` to verify portal and S3 access. Use `python -m tesla_invoice status` to inspect queued invoices. Then run `python -m tesla_invoice run` to verify actual SMTP delivery with the configured recipient. Run these commands while a scheduled check is idle. On a fresh deployment after noon, the scheduler can already have started a delivery run, so deploy before noon or on a weekend if you need to verify without sending first.

[Coolify Docker Compose documentation](https://coolify.io/docs/knowledge-base/docker/compose)

### Shared S3 bucket

Set `S3_BUCKET` to the existing bucket name and `AWS_DEFAULT_REGION` to its region. Use a dedicated AWS principal for this application. An AWS administrator can replace `REPLACE_WITH_SHARED_BUCKET` in [the IAM policy](s3-iam-policy.json) and attach it to that principal. Use its credentials for `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` and set `AWS_SESSION_TOKEN` when the credentials are temporary.

The policy permits only object reads and writes under `tesla-invoices/`. It denies bucket listing and object access outside that prefix. Objects are private and use S3-managed encryption (SSE-S3). This policy and configuration assume a general-purpose S3 bucket that permits SSE-S3. If the bucket enforces a customer-managed KMS key, adapt the archive and key policy for that bucket before deploying.

Keys follow `tesla-invoices/<category>/<sha256>.pdf`. A conditional upload prevents overwriting an existing object. If the object already exists, the service verifies its contents before marking it archived. Exclude the invoice prefix from any shared lifecycle rule that expires objects sooner than your required retention period. The S3 archive contains PDFs. Preserve the SQLite database separately to retain delivery history.

## Local setup

1. Create the local configuration and import directories.

   ```sh
   umask 077
   cp .env.example .env
   ```

2. Enter your Tesla credentials, SMTP settings, and the shared-bucket settings in `.env`. Set `TESLA_COUNTRY_URL` to your local Tesla homepage, such as `https://www.tesla.com/de_de/` for Germany (the default). The worker appends `teslaaccount` for both automatic and interactive sign-in. Leave `TESLA_LOGIN_URL` blank to use that derived URL, or set it to a full Tesla login URL to override it. Clear any existing `TESLA_LOGIN_URL` when switching countries if you want the country setting to take effect. Set `MAIL_FROM` and `MAIL_TO` to one email address each. Use `SMTP_SECURITY=starttls` for port 587 or `SMTP_SECURITY=ssl` for port 465, as required by your provider. Put passwords containing `$` or `#` in single quotes in the Compose environment file. Both the file and the Docker volume contain sensitive data.

3. Configure both invoice sources in the signed-in Tesla web account. For Premium Connectivity, use **Manage Vehicle → Documents**. For Supercharging, use the charging invoice list if your web account exposes it. The [Tesla Fleet fallback](#supercharger-fallback) can cover Supercharger retrieval when that list is unavailable. [Premium Connectivity support](https://www.tesla.com/support/connectivity)

   | Setting | Value |
   | --- | --- |
   | `URL` | HTTPS URL on Tesla.com for the invoice list after signing in |
   | `LIST` | CSS selector for the visible list container |
   | `INVOICE` | CSS selector for PDF links or download buttons in this category |
   | `EMPTY` | CSS selector for the empty-state message, if available |
   | `NEXT` | CSS selector for the next-page or load-more control, if available |
   | `MODE` | `download` for buttons that trigger a browser download or `href` for direct PDF links on Tesla.com |
   | `ID_ATTRIBUTE` | Optional attribute that holds a stable, unique invoice ID, such as `data-invoice-id` |

   Use the `TESLA_CONNECTIVITY_` and `TESLA_SUPERCHARGER_` prefixes for the respective web sources. An existing `TESLA_SUPERCHARGER_SOURCE` setting can be removed when upgrading. Sign in to your Tesla account and use your browser's developer tools to find the URL and selectors. Match only invoices for the requested category, since the Documents page can contain other records. Without a stable ID, the service downloads the visible PDFs again and deduplicates them by content. Use `ID_ATTRIBUTE` if Tesla assigns a new ID to each invoice revision or if you only need one version per ID.

4. Verify the setup without sending email.

   ```sh
   docker compose build
   docker compose run --rm invoices run --download-only
   docker compose run --rm invoices status
   ```

   The first run collects every PDF found in the configured Tesla sources, including older invoices. The first delivery run sends one email per collected invoice. If you only want invoices discovered after setup, run `docker compose run --rm invoices run --baseline` before starting the service. This saves existing PDFs without queuing them for delivery.

5. Start the service.

   ```sh
   docker compose up -d
   docker compose logs -f invoices
   ```

   Set `RUN_DAYS` to a comma-separated list using `mon,tue,wed,thu,fri,sat,sun`. `RUN_TIME` is local to `RUN_TIMEZONE`. An outstanding check for the current scheduled day runs after startup if the scheduled time has passed. Run `docker compose run --rm invoices run` for an immediate check and delivery when no other check is active.

## Supercharger fallback

The Tesla web account is the primary source for both categories. The Fleet API fallback is optional and inactive by default. It runs only when both Fleet client credentials and a saved Fleet authorization are present and the Supercharger web source is unavailable or fails to complete. Leave the Fleet settings blank for web-only operation. A successful web check, including a confirmed empty list, skips Fleet entirely. Without the fallback, an unsuccessful Supercharger web check remains incomplete and is retried on the usual schedule. A Premium Connectivity failure is reported independently. The fallback downloads new Supercharger PDFs by invoice ID and uses the same S3 archive and email queue. Identical PDFs are deduplicated across sources by SHA-256. Fleet API requests may incur usage charges. [Charging endpoints](https://developer.tesla.com/docs/fleet-api/endpoints/charging-endpoints) · [Tesla billing](https://developer.tesla.com/docs/fleet-api/billing-and-limits)

1. Register a Tesla Fleet API application for Europe and complete Tesla's partner registration for that region. Register a trusted HTTPS callback URL you control. Set `TESLA_FLEET_CLIENT_ID`, `TESLA_FLEET_CLIENT_SECRET`, and `TESLA_FLEET_REDIRECT_URI` in Coolify or your local `.env`. Keep the callback free of third-party analytics or query-string logging. [Fleet API setup](https://developer.tesla.com/docs/fleet-api/)
2. Run `python -m tesla_invoice fleet-authorize` in the interactive Coolify worker terminal, or `docker compose run --rm invoices fleet-authorize` locally. Open the printed Tesla sign-in link, sign in, and grant Tesla's charging-management scope. After Tesla redirects you, paste the full callback URL into the hidden terminal prompt. The command checks the OAuth state and stores the rotating refresh token in the persistent worker volume. [Tesla OAuth flow](https://developer.tesla.com/docs/fleet-api/authentication/third-party-tokens)
3. Run a download-only check and compare the Supercharger result with your Tesla account. Renew authorization if Tesla revokes or expires the grant. Subsequent invoice checks refresh access automatically and require no invoice exports.

Premium Connectivity uses the Tesla web account session independently of Fleet authorization.

## Sign-in with MFA

The Premium Connectivity web source reuses the saved browser session and tries the configured credentials when sign-in is required. If Tesla prompts for MFA, a CAPTCHA, or another interactive check, sign in with the browser opened by the following commands on a Linux desktop with Python installed. Return to the signed-in account page before saving the session. When the session expires, repeat the import if interactive verification is still required.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
DATA_DIR=./data python -m tesla_invoice login
docker compose run --rm -T invoices import-session < data/session.json
```

## Retries and delivery status

- The service retries temporary download, S3, and SMTP errors. A failed scheduled check sets the worker health check to unhealthy until a subsequent successful check. Successfully saved PDFs remain queued. Only invoices confirmed in S3 can be emailed.
- SQLite deduplicates byte-identical PDFs by SHA-256 and preserves their first retrieval date and run ID across retries. Each English email to Heinrich contains one PDF, a short description, and the retrieval date and run ID in both the subject and body. The service marks an invoice `sent` when the SMTP server accepts it. Pending records created by older versions use their last recorded timestamp and first upgrade run ID because those versions did not record retrieval metadata.
- A disconnect during SMTP data transfer can leave acceptance uncertain. The service marks that invoice `uncertain` and pauses retries for it to avoid duplicates. Look up the displayed hash in the message ID `<HASH@tesla-invoice.local>` in the mailbox or SMTP logs. Use `resolve HASH sent` to confirm acceptance or `resolve HASH pending` to queue the invoice again.

```sh
docker compose run --rm invoices status
docker compose run --rm invoices resolve HASH pending
```

Monitor Coolify logs and resolve uncertain deliveries promptly. SMTP can acknowledge a message even when the client loses the acknowledgement. A recorded SMTP acceptance also does not guarantee inbox delivery, so check mail-provider bounce logs. The worker can only collect invoices exposed by the configured Tesla sources and cannot detect an invoice Tesla never lists. Check both configured categories against your account before relying on the deployment. Retain the Docker volume and its backups so already sent invoices stay recorded.
