# Tesla Invoices

A Python service that checks for Tesla Supercharger and Premium Connectivity invoices and emails new PDFs via SMTP. By default it runs on Mondays, Wednesdays, and Fridays at 08:00 Europe/Berlin time. A Docker volume stores PDFs, the Tesla browser session, and delivery status.

## Setup

1. Create the local configuration and import directories.

   ```sh
   umask 077
   cp .env.example .env
   umask 022
   mkdir -p inbox/supercharger inbox/connectivity
   ```

2. Enter your Tesla credentials and SMTP settings in `.env`. Set `MAIL_FROM` and `MAIL_TO` to one email address each. Use `SMTP_SECURITY=starttls` for port 587 or `SMTP_SECURITY=ssl` for port 465, as required by your provider. Put passwords containing `$` or `#` in single quotes in the Compose environment file. Both the file and the Docker volume contain sensitive data.

3. Configure the source for each category. Tesla lists Premium Connectivity invoices under **Manage Vehicle → Documents** in the web account. The German Supercharging support page directs users to the charging history in the Tesla app. Automatic web downloads require a PDF download for that category in your Tesla account. [Premium Connectivity support](https://www.tesla.com/support/connectivity) · [Supercharging support for Germany](https://www.tesla.com/de_de/support/charging/supercharging)

   | Setting | Value |
   | --- | --- |
   | `SOURCE` | `portal` to download from the web account or `inbox` to import local PDFs |
   | `URL` | HTTPS URL on Tesla.com for the invoice list after signing in |
   | `LIST` | CSS selector for the visible list container |
   | `INVOICE` | CSS selector for PDF links or download buttons in this category |
   | `EMPTY` | CSS selector for the empty-state message, if available |
   | `NEXT` | CSS selector for the next-page or load-more control, if available |
   | `MODE` | `download` for buttons that trigger a browser download or `href` for direct PDF links on Tesla.com |
   | `ID_ATTRIBUTE` | Optional attribute that holds a stable, unique invoice ID, such as `data-invoice-id` |

   Settings use the `TESLA_CONNECTIVITY_` or `TESLA_SUPERCHARGER_` prefix. Sign in to your Tesla account and use your browser's developer tools to find the URL and selectors. Match only invoices for the requested category, since the Documents page can contain other records. Without a stable ID, the service downloads the visible PDFs again and deduplicates them by content. Use `ID_ATTRIBUTE` if Tesla assigns a new ID to each invoice revision or if you only need one version per ID.

   If your Supercharger invoices are only available in the app, set `TESLA_SUPERCHARGER_SOURCE=inbox` and export PDFs to `inbox/supercharger/`. The equivalent directory for Premium Connectivity is `inbox/connectivity/`. Make the files readable by the container. While the service is running, copy each file with a temporary extension and rename it to `.pdf` once copying finishes. The app export is manual. The scheduled import and email delivery are automatic.

4. Verify the setup without sending email.

   ```sh
   docker compose build
   docker compose run --rm invoices run --download-only
   docker compose run --rm invoices status
   ```

   The first run collects every PDF found in the configured lists and import directories, including older invoices. The first delivery run sends one email per collected invoice. If you only want invoices discovered after setup, run `docker compose run --rm invoices run --baseline` before starting the service. This saves existing PDFs without queuing them for delivery.

5. Start the service.

   ```sh
   docker compose up -d
   docker compose logs -f invoices
   ```

   Set `RUN_DAYS` to a comma-separated list using `mon,tue,wed,thu,fri,sat,sun`. `RUN_TIME` is local to `RUN_TIMEZONE`. After downtime, the service runs the most recent due check if it was missed. Run `docker compose run --rm invoices run` for an immediate check and delivery when no other check is active.

## Sign-in with MFA

The service reuses the saved session and tries the configured credentials when sign-in is required. If Tesla prompts for MFA, a CAPTCHA, or another interactive check, sign in with the browser opened by the following commands on a Linux desktop with Python installed. Return to the signed-in account page before saving the session. When the session expires, repeat the import if interactive verification is still required.

```sh
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
DATA_DIR=./data python -m tesla_invoice login
docker compose run --rm -T invoices import-session < data/session.json
```

## Retries and delivery status

- The service retries temporary download and SMTP errors with a delay. If a scheduled check remains incomplete, it retries after 30 minutes. Successfully saved PDFs stay available for delivery.
- SQLite stores each PDF with its delivery status and deduplicates identical PDFs by SHA-256. The service marks an invoice `sent` when the SMTP server accepts its email.
- A disconnect during SMTP data transfer can leave acceptance uncertain. The service marks that invoice `uncertain` and pauses retries for it to avoid duplicates. Look up the displayed hash in the mailbox or SMTP logs. Use `resolve HASH sent` to confirm acceptance or `resolve HASH pending` to queue the invoice again.

```sh
docker compose run --rm invoices status
docker compose run --rm invoices resolve HASH pending
```

Back up the Docker volume to retain the invoice archive and delivery history.
