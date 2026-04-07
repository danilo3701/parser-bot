# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**Tutor Finder Bot** is a Telegram bot that searches for Spanish tutors in Spanish Telegram groups. It has two main components:
1. **Bot** (`bot/main.py`) — aiogram 3.x Telegram bot with inline keyboard UI for managing searches and broadcasts
2. **Parser** (`parser/`) — Telethon-based scrapers for scanning group history and monitoring new messages in real-time

## Architecture

### Bot Component (`bot/`)

**Key architecture:**
- **Dispatcher-based handlers** — All bot logic is in `main.py` using aiogram's `@dp.message()` and `@dp.callback_query()` decorators
- **FSM states** — `MainMenu` state group manages user flow through inline keyboard menus
- **Keyboard builders** — Functions like `main_keyboard()`, `broadcast_main_keyboard()`, `broadcast_channels_keyboard()` construct inline keyboards
- **Callback data patterns** — Buttons pass data like `"broadcast"`, `"bc_test"`, `"bct_08:12"` to identify which handler to invoke

**Key managers (separate modules):**
- `BroadcastManager` (`broadcast_manager.py`) — Loads/saves `broadcast_state.json`; orchestrates broadcast scheduling, group selection, send-as channels
- `categories.py` — CRUD for `categories.json` (keyword search categories); tracks active category
- `groups_manager.py` — CRUD for `groups.json` (list of Telegram groups to scan/broadcast to)

**Key state files (persisted JSON):**
- `broadcast_state.json` — Campaign settings, send mode (user/channel), send-as channels, selected groups, schedule enabled/disabled, active times (e.g., `["08:12", "11:33"]`), timezone
- `categories.json` — Keyword search categories (name, keywords, active flag)
- `groups.json` — List of group usernames (@groupname) to include in scans/broadcasts

### Parser Component (`parser/`)

**Key modules:**
- `scanner.py` — `scan_groups_history()` async function; uses Telethon to scan group history for keyword matches
- `broadcast_sender.py` — `send_broadcast_campaign()` async function; connects via Telethon, sends message to each selected group sequentially with 5s delay + jitter
- `dedupe_store.py` — `DedupeStore` class; TTL-based JSON deduplication (24h TTL by default, keyed by `author_day:{sender_id}:{YYYY-MM-DD}`)
- Standalone scripts `telegram_history_scan.py`, `telegram_keyword_monitor.py` — Manual runner scripts

### Scheduler

**Location:** `main.py`, `scheduler_loop()` function (runs as `asyncio.Task`).

**How it works:**
- Runs every 20 seconds in the background while the bot is polling
- Checks if current time matches any configured broadcast slot (e.g., 08:12, 11:33)
- Prevents duplicate runs per day (tracks in `broadcast_state.json["last_runs"]`)
- Calls `send_broadcast_campaign()` via Telethon if conditions are met
- Respects `broadcast_schedule.enabled` flag — when disabled, scheduler sleeps and does nothing

## Running the Bot

```bash
# Install dependencies
pip install -r requirements.txt

# Start bot (from bot/ directory or adjust path)
cd bot
python main.py

# The bot starts polling and listening for /start, callbacks, and runs the scheduler
```

**Environment setup:**
- Copy `bot/.env.example` to `bot/.env` and fill in:
  - `BOT_TOKEN` — Telegram bot token from BotFather
  - `OWNER_ID` — User ID(s) to restrict access (CSV for multiple)
  - `BROADCAST_TZ` — Timezone for scheduled broadcasts (default: `Europe/Madrid`)
  - `TG_API_ID`, `TG_API_HASH`, `TG_PHONE` — Telethon credentials (for parser; optional if not running parser)

## Important Patterns

### Handler Ownership and Access Control
- All callback handlers check `await ensure_owner_callback(query)` to restrict to owner(s) defined in `OWNER_IDS`
- Message handlers check `is_owner(message.from_user.id)` before processing
- Owner IDs are parsed from `OWNER_IDS_ENV` (comma-separated list)

### Inline Keyboard Callback Data
- **Format convention:** `callback_data="noun_action"` or `callback_data="noun_value"` (e.g., `"bc_schedule_toggle"`, `"bct_08:12"`, `"bcg_@groupname"`)
- **Dynamic buttons:** Buttons with variable data use `callback_data=f"prefix_{variable}"` and matched with `F.data.startswith("prefix_")` in handlers
- **Edit pattern:** Handlers typically call `query.message.edit_text()` to update the message in-place rather than sending new messages

### State and Persistence
- **Broadcast state:** `BroadcastManager.load()` reads JSON, modifies dict in memory, calls `.save(state)` to write back
- **No database:** All state is persisted to JSON files in the `bot/` directory (no SQL, no Redis)
- **Timezone awareness:** Scheduled times are stored as strings (HH:MM), interpreted in `BROADCAST_TZ` via `zoneinfo.ZoneInfo`

### Async Patterns
- **Global lock:** `broadcast_lock` (asyncio.Lock) prevents concurrent broadcasts
- **Scheduler task:** `scheduler_task` is created on bot startup, cancelled on shutdown via finally block
- **Telethon integration:** Both scanner and broadcast sender use Telethon's async API; they connect, do work, disconnect each time (no persistent connection pooling)

## Common Development Tasks

### Adding a new menu/keyboard
1. Create a function `def my_keyboard()` that returns `InlineKeyboardMarkup`
2. In the function, construct `rows` as a list of button rows, e.g., `rows.append([InlineKeyboardButton(text="...", callback_data="...")])`
3. Return `InlineKeyboardMarkup(inline_keyboard=rows)`

### Adding a new callback handler
1. Decorate with `@dp.callback_query(F.data == "my_callback")` for exact match, or `F.data.startswith("my_prefix_")` for dynamic data
2. Inside handler: check `await ensure_owner_callback(query)` if restricted
3. Load state if needed: `state = broadcast_manager.load()` or category/group managers
4. Call `query.message.edit_text()` to update the message with new text and keyboard
5. Call `query.answer()` to acknowledge the callback (shows/hides spinner)

### Modifying scheduled broadcast times
- Times are stored in `broadcast_state.json["broadcast_schedule"]["times"]` as a list of strings
- Use `broadcast_manager.set_schedule_times(times_list)` to update
- The scheduler reads from JSON on every loop iteration, so changes take effect immediately without restart

### Adding a new persisted state (JSON file)
1. Create a manager class (see `BroadcastManager`, `groups_manager.py` patterns)
2. Implement `load()`, `save()`, and mutation methods
3. Call `load()` before reads, `save()` after writes; return the state dict for chaining

## Notes for Future Work

- **No database layer** — If the project scales, consider migrating state to a database (SQLite for local, PostgreSQL for production)
- **Telethon session files** — `*.session` files are created in the bot directory when Telethon connects; they cache authentication; do not delete unless you want to re-login
- **Error handling in scanner/broadcast** — Parser scripts have minimal error handling; consider adding exponential backoff and retries for network issues
- **Broadcast concurrency** — Only one broadcast can run at a time (protected by `broadcast_lock`); multiple scheduled slots at the same time will queue
