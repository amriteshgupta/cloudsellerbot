# Telegram marketplace bot

Runnable Python 3.11-3.14 / aiogram 3 project for a small, manually operated marketplace. Sellers submit products for approval; buyers browse approved stock and ask the admin to arrange the deal. The bot does not collect payments or create reservations. Includes an initialized SQLite database at `data/marketplace.sqlite3`.

## Setup

Prerequisite: a working Python 3.11-3.14 interpreter (`python3.14 --version`). Python 3.15 is not supported by the pinned aiogram release.

1. Create a Telegram bot with [@BotFather](https://t.me/BotFather) and get its token.
2. Open a terminal in this project directory, then run:

   ```sh
   python3.14 -m venv .venv
   source .venv/bin/activate
   python -m pip install -r requirements.txt
   cp .env.example .env
   chmod 600 .env
   ```

3. Edit `.env`. Set `BOT_TOKEN` to a fresh BotFather token and comma-separated numeric `ADMIN_IDS`. IDs are Telegram user IDs, never usernames or chat titles. Find yours with `@userinfobot`. Every admin must open the bot and press Start before it can message them.
4. Set `CURRENCY` before accepting listings. Buyer prices use the seller asking price plus 10%, rounded to cents. Do not put secrets in `.env.example` or Telegram messages.
5. Run:

   ```sh
   python -m marketplace
   ```

   Keep this process running on a machine with Internet access. Stop with Ctrl+C. Long polling needs no public web server. Use one instance per token and database, on local disk. If the bot previously used webhooks, remove its webhook through the Bot API before polling; this project does not discard pending Telegram updates.
6. Open the bot's private chat and send `/start`. Group/channel trading is disabled. Use `/help` for commands and `/admin` for admin controls.

Defaults: USD, 10% markup, 10 messages/second globally. `BROADCAST_PER_SECOND` accepts >0–20. Admin ID changes require a restart.

## Included workflows

- `/sell` asks for product name, quantity, seller asking price, negotiability, and delivery time in hours.
- Every new or edited listing stays pending until an admin reviews and approves it. `/changes ID reason` and `/reject ID reason` notify the seller.
- `/buy` shows a numbered list of approved products with available stock, buyer price, negotiability, and delivery time. A buyer's number selection notifies the configured admin for manual follow-up.
- `/edit` shows the seller's products and opens the same five-field form. Editing an approved product sends it back for fresh approval; quantity cannot go below held plus sold stock.
- Buyer prices are seller asking price × 1.10, rounded half up to cents. No payment, reservation, delivery, refund, or payout is processed by the bot.
- `/ban USER_ID` blocks trading and pauses approved listings owned by that user. `/unban` restores access; admins resume listings individually after review.

## Commands

| Everyone | Purpose |
| --- | --- |
| `/start`, `/help` | Show the short usage guide |
| `/sell` | Submit a product for admin approval |
| `/buy` | Show approved products and contact the admin |
| `/edit` | Choose one of your products to update |
| `/cancel` | Stop the current seller form |

| Admin | Purpose |
| --- | --- |
| `/admin` | Admin command help |
| `/pending [PAGE]`, `/review ID` | Review full private listing and approve with a button |
| `/reject ID reason`, `/changes ID reason` | Reject/request changes with seller feedback |
| `/pause ID reason`, `/resume ID` | Hide/restore an approved product |
| `/ban ID`, `/unban ID` | Manage trading permissions |
| `/history PRODUCT_ID` | Last 20 inventory events |
| `/outbox` | Last 20 permanently failed notifications |

## Product moderation

Live API keys, credentials, third-party accounts/account access, and nontransferable/promotional/trial credits are rejected. A conservative text filter rejects obvious terms and recognizable secret formats **before storing the submitted field or listing**. It intentionally also rejects innocent descriptions containing prohibited terms. Only text submissions are accepted; no attachments or secret-file storage.

This filter is a first pass, not a guarantee against disguised or mislabeled products. Mandatory human approval is the final gate. Verify the product description, stock, delivery time, and seller identity before approving. Ask for changes or reject when details are insufficient. Do not paste live secrets into Telegram at all: Telegram itself receives messages before the bot can reject them.

Admin permissions come exclusively from configured numeric IDs. Every admin command and admin callback checks that identity; ownership is also checked in database operations. Sellers cannot approve listings/restocks or finalize payments. Callback IDs convey no permission. SQL inputs are parameterized and message text is sent without HTML/Markdown parsing.

## Storage and operations

`schema.sql` documents the schema. SQLite stores users, listings, inventory events, audit entries, and a durable notification outbox. Database checks prevent negative inventory. `BEGIN IMMEDIATE`, WAL, and foreign keys guard writes. Critical listing actions and their queued notifications commit together.

Money is stored as integer cents; buyer unit price is seller cents × 1.10, rounded half up to the nearest cent. Example: seller asks $400.00 -> buyer sees $440.00. A 10% markup is not a 10% margin on revenue.

Status is displayed as pending/changes/rejected/paused, or for approved listings: **available** when free stock >0. A paused product is hidden from buyers. Draft forms use memory and are discarded on restart; submitted listings persist.

SendMessage calls are paced globally and per private chat; outbox 429 responses respect Telegram's retry delay. Transient failures retry with bounded backoff. `/outbox` shows failures; inspect SQLite if you need full history. Notifications are **at least once**: a crash after Telegram accepts a send but before SQLite records it can duplicate a message.

This is a single-process MVP for a small marketplace. SQLite calls are brief synchronous operations; large subscriber lists, extensive history and heavy traffic need a dedicated database/queue, retention policies and more operational tooling. Broadcast queue entries are created transactionally at approval. Back up and protect the database and `.env`; they contain personal and business data. There is no automated customer-data deletion or payout reconciliation feature.

For a consistent backup, stop the bot first or use SQLite's backup API. Do not copy only the main file while WAL writes are active. Restore with the bot stopped. The included database is schema version 1; there is no migration framework. Do not replace a live database with the bundled empty file during upgrades. A local `.lock` file enforces one instance per database. Keep the working directory and DATABASE_PATH consistent; do not use network filesystems.

## Verification

```sh
python -m unittest discover -s tests -v
python -m compileall -q marketplace tests
```

Validated with Python 3.12 and aiogram 3.31.0: 17 tests passed, compilation passed and dependency checks passed. No live Telegram token was used.

Tests cover money rounding, product-policy rejection, private/public pricing, permissions, approval revisions, concurrent oversell prevention, reservation idempotency/expiry, paid hold persistence, completion/cancellation accounting, restock review, bans and broadcast selection. Bot integration tests use a fake Telegram session; no real bot token or network calls are needed.

Before opening your marketplace, use separate seller/buyer/admin Telegram users to rehearse submission → approval → broadcast → multi-unit purchase → payment → delivery → completion, plus expiry, cancellation and restock. Actual Telegram connectivity requires your token and is not covered by offline tests.

References: [aiogram dispatcher/polling documentation](https://docs.aiogram.dev/en/latest/dispatcher/dispatcher.html), [Telegram broadcast limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this-).
