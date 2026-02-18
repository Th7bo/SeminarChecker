# PXL Seminar Reminder

Checks the [PXL-Digital seminaries page](https://pxl-digital.pxl.be/i-talent/seminaries-2tin-25-26) periodically and sends a **Discord message (as a bot)** when a seminar’s **Inschrijven** (register) link is no longer `#` (i.e. registration has opened). Each seminar is notified **only once**.

## Setup

1. **Python 3.10+** and a virtualenv (recommended):

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate   # Linux/macOS
   pip install -r requirements.txt
   ```

2. **Discord bot**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications) → New Application → name it (e.g. “PXL Seminar Reminder”).
   - In the app: **Bot** → Add Bot → copy the **Token** (this is `DISCORD_BOT_TOKEN`). Reset the token if you ever expose it.
   - **OAuth2 → URL Generator**: Scopes = `bot`; Bot Permissions = **Send Messages**, **Embed Links**, and (if you use @everyone) **Mention Everyone**. Copy the generated URL, open it in a browser, and invite the bot to your server.
   - In Discord: enable **Developer Mode** (User Settings → App Settings → Advanced). Right‑click the channel where the bot should post → **Copy Channel ID**. This is `DISCORD_CHANNEL_ID`.

3. **Configure the bot**
   - Set environment variables:
     ```bash
     export DISCORD_BOT_TOKEN="your_bot_token"
     export DISCORD_CHANNEL_ID="1234567890123456789"
     ```
   - Or pass when running: `--bot-token` and `--channel` (see Usage).

4. **PostgreSQL database** (required): Notified seminars are stored in PostgreSQL so each is only notified once. Set `DATABASE_URL`:
   ```bash
   export DATABASE_URL="postgresql://user:password@host:5432/dbname"
   ```
   The app creates the `notified_seminars` table automatically on first run. See [Docker Compose](#docker-compose) for a one-command Postgres + app setup.

5. **Ping** (optional): Notifications include a mention so people get alerted. Default is `@everyone`. Set `DISCORD_PING` or use `--ping`:
   - `@everyone` – ping all members (default)
   - `@here` – ping only online members
   - `<@&ROLE_ID>` – ping a specific role (use the role’s ID)
   - Empty string – no mention

## Usage

The app is a **long-lived Discord bot**: it stays online and runs the seminar check on a schedule (every `CHECK_INTERVAL` minutes, default 60). No cron or external loop needed.

```bash
python check_seminars.py
```

All configuration is via environment variables (`DISCORD_BOT_TOKEN`, `DISCORD_CHANNEL_ID`, `DATABASE_URL`, and optionally `DISCORD_PING`, `CHECK_INTERVAL`, `LOG_LEVEL`).

- **More verbose logs**: set `LOG_LEVEL=DEBUG` or run `python check_seminars.py --log-level DEBUG`

The bot posts a **single status embed** in the channel (seminaries on list, open for registration, total notified, new this run, next update). That message is **edited in place** after each check; the embed shows the next run time in Discord’s local time format.

## Docker

Build and run with Docker (requires a PostgreSQL instance and `DATABASE_URL`). The container runs the bot; it stays online and performs checks every `CHECK_INTERVAL` minutes.

```bash
docker build -t seminar-reminder .
docker run -d --restart unless-stopped \
  -e DATABASE_URL="postgresql://user:password@host:5432/dbname" \
  -e DISCORD_BOT_TOKEN="your_bot_token" \
  -e DISCORD_CHANNEL_ID="1234567890123456789" \
  -e DISCORD_PING="@everyone" \
  --name seminar-reminder \
  seminar-reminder
```

- **Check more often** (e.g. every 30 minutes): add `-e CHECK_INTERVAL=30`

### Docker Compose

Run the app and PostgreSQL together (state persisted in a Postgres volume):

```bash
# Copy env example and set your bot token and channel ID
cp .env.example .env
# Edit .env: set DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID (and optionally DISCORD_PING, CHECK_INTERVAL, LOG_LEVEL)

docker compose up -d
```

The app will create the `notified_seminars` table on first run. Data is stored in the `pgdata` volume.

## How it works

1. Fetches the main seminaries list page and collects every “meer info” link (seminar detail URLs).
2. For each seminar page, fetches the HTML and looks for the **Inschrijven** link.
3. If its `href` is not `#` (and not empty), registration is considered open. Only seminars in the **current year** are considered (older events are ignored).
4. For each such seminar that is not yet in the `notified_seminars` table, the script sends one Discord message with an embed (title, company, date/time/location, and link to register), then inserts a row into the database.
5. A **status embed** in the same channel is created once, then **edited** after each check with global stats and the next update time in Discord time format.

## Database

- **`notified_seminars`**: `seminar_id` (PK), `seminar_url`, `title`, `notified_at`. Tracks which seminars have already been announced.
- **`bot_state`**: key/value store used e.g. for the status embed message ID (so it can be edited in place).

The schema is created automatically on first run. You can add more tables or columns later for extra features.

## Optional ideas

- **Multiple channels**: Call the Discord API for each channel (add `DISCORD_CHANNEL_ID_2`, etc.) to post the same notification in several channels.
- **Filter by track**: If you only want e.g. AON/SWM seminars, you could filter in `get_seminar_links_from_list_page` or after parsing the detail page using the “Specialisatie” field.
- **Resetting state**: `DELETE FROM notified_seminars;` (or drop and recreate the table) to trigger new notifications for all currently open seminars (e.g. for testing or a new semester).
