# PXL Seminar Reminder

Checks the [PXL-Digital seminaries page](https://pxl-digital.pxl.be/i-talent/seminaries-2tin-25-26) periodically and sends a **Discord webhook** when a seminar’s **Inschrijven** (register) link is no longer `#` (i.e. registration has opened). Each seminar is notified **only once**.

## Setup

1. **Python 3.10+** and a virtualenv (recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Linux/macOS
   pip install -r requirements.txt
   ```

2. **Discord webhook**
   - In your Discord server: Channel → Edit channel → Integrations → Webhooks → New webhook.
   - Copy the webhook URL.

3. **Configure the webhook**
   - Either set the environment variable:
     ```bash
     export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
     ```
   - Or pass it when running:
     ```bash
     python check_seminars.py --webhook "https://discord.com/api/webhooks/..."
     ```

4. **Ping** (optional): Notifications include a mention so people get alerted. Default is `@everyone`. Set `DISCORD_PING` or use `--ping`:
   - `@everyone` – ping all members (default)
   - `@here` – ping only online members
   - `<@&ROLE_ID>` – ping a specific role (use the role’s ID)
   - Empty string – no mention

## Usage

- **Single run** (e.g. from cron):
  ```bash
  python check_seminars.py
  ```

- **Periodic loop** (e.g. every 60 minutes):
  ```bash
  python check_seminars.py --loop --interval 60
  ```

- **Custom interval** (e.g. every 30 minutes):
  ```bash
  python check_seminars.py -l -i 30
  ```

State is stored in `notified_seminars.json` in the project directory so each seminar is only reported once across runs.

## Cron example

Run every hour:

```cron
0 * * * * cd /path/to/seminar-reminder && .venv/bin/python check_seminars.py
```

## Docker

Build and run with Docker for easy hosting (runs in a loop, default every 60 minutes):

```bash
docker build -t seminar-reminder .
docker run -d --restart unless-stopped \
  -e DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
  -e DISCORD_PING="@everyone" \
  --name seminar-reminder \
  seminar-reminder
```

- **Persist state** across container restarts (so you don’t get duplicate notifications):
  ```bash
  docker run -d --restart unless-stopped \
    -e DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." \
    -e DISCORD_PING="@everyone" \
    -v seminar-reminder-state:/app \
    --name seminar-reminder \
    seminar-reminder
  ```

- **Check more often** (e.g. every 30 minutes):
  ```bash
  docker run -d ... -e CHECK_INTERVAL=30 seminar-reminder
  ```

## How it works

1. Fetches the main seminaries list page and collects every “meer info” link (seminar detail URLs).
2. For each seminar page, fetches the HTML and looks for the **Inschrijven** link.
3. If its `href` is not `#` (and not empty), registration is considered open.
4. For each such seminar that is not yet in `notified_seminars.json`, the script sends one Discord message with an embed (title, company, date/time/location, and link to register), then adds that seminar to the state file.

## Optional ideas

- **Multiple webhooks**: Duplicate the script or add a second env var (e.g. `DISCORD_WEBHOOK_URL_2`) and call the webhook twice for each new seminar.
- **Filter by track**: If you only want e.g. AON/SWM seminars, you could filter in `get_seminar_links_from_list_page` or after parsing the detail page using the “Specialisatie” field.
- **Resetting state**: Delete `notified_seminars.json` to trigger new notifications for all currently open seminars (e.g. for testing or a new semester).
