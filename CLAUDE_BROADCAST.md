# CLAUDE.md — Broadcast Module

This file provides detailed guidance for working with the broadcast/mailing system in this repository.

## Broadcast System Overview

The broadcast system allows users to:
1. Select groups to broadcast to
2. Add posts (text, photos, videos) to a pool
3. Schedule broadcasts by day/time
4. Run one-time broadcasts manually
5. Pay for posts via Stripe
6. Check balance before broadcasting

All broadcast logic is in `bot/main.py` with state persisted in `broadcast_state.json`.

---

## State Structure (`broadcast_state.json`)

```json
{
  "balance": {
    "posts": 30,
    "total_purchased": 0,
    "total_spent": 0,
    "created_at": "ISO_timestamp",
    "balance_history": [
      {"type": "initial_free|purchase|spend", "amount": 30, "date": "ISO_timestamp"}
    ]
  },
  "notifications": {
    "balance_low": {
      "enabled": true,
      "threshold": 30,
      "last_sent": null
    }
  },
  "campaign": {
    "send_mode": "user|channel",
    "send_as_channel": "@channel_name",
    "source_channel": "@channel_name",
    "source_message_id": int,
    "posts": [
      {
        "id": "8char_hex",
        "channel": "@channel",
        "message_id": int,
        "kind": "text|photo|video",
        "preview": "string"
      }
    ],
    "rotation_index": 0,
    "selected_groups": ["group1", "group2"],
    "test_passed": false,
    "last_test_at": null
  },
  "send_as_channels": ["@channel1", "@channel2"],
  "broadcast_groups_state": {
    "group_name": {
      "status": "active|blocked",
      "reason": "error message if blocked",
      "last_test_status": "ok|failed|deleted|unknown|null",
      "last_test_reason": "error code",
      "last_test_message_id": int,
      "last_test_sent_at": "ISO_timestamp",
      "last_test_verified_at": "ISO_timestamp|null"
    }
  },
  "broadcast_schedule": {
    "enabled": true,
    "tz": "Europe/Madrid"
  },
  "weekly_schedule": {
    "mon": {"enabled": true, "time": "08:12"},
    "tue": {"enabled": false, "time": null},
    ...
    "sun": {"enabled": true, "time": "11:33"}
  },
  "last_runs": {
    "2026-04-13_08:12": {
      "status": "ok|skipped|failed",
      "summary": "Групп: 10 | Отправлено: 8 | ...",
      "updated_at": "ISO_timestamp"
    }
  }
}
```

---

## Key Price Points

**3 Post Packages (Stripe):**

| Package | Posts | Price | €/post | Discount vs Small |
|---------|-------|-------|--------|------------------|
| Small | 100 | €3.99 | €0.0399 | — |
| Medium ⭐ | 300 | €7.99 | €0.0266 | 33% |
| Large | 1500 | €33.99 | €0.0226 | 73% |

**Psychological pricing:**
- All prices end in 9 (€3.99, €7.99, €33.99) → feels cheaper
- Medium shows "3x posts, 2x cheaper price" → strong anchor effect
- Large is anchor price (few buy it, but makes medium look good)
- Default free balance: 30 posts

---

## Main Broadcast Flow

### 1. **Post Selection (add to pool)**

**Handler:** `@dp.callback_query(F.data == "bcp_add")`  
**Flow:**
1. User navigates to broadcast menu → "📝 Мой текст"
2. Sees current posts (max 10)
3. If < 10 posts: show "Add post" button
4. User selects source channel + message ID
5. Post added to `campaign.posts[]`
6. Save via `broadcast_manager.save()`

**Key files:**
- `main.py` lines ~2000-2100 (broadcast_posts_keyboard, callbacks)
- `broadcast_manager.py` — `add_post()`, `delete_post()`, `list_posts()`

---

### 2. **Group Selection**

**Handler:** `@dp.callback_query(F.data.startswith("bcg_"))`  
**Flow:**
1. User clicks "🎯 Выбрать группы"
2. Shows paginated list of groups (6 per page)
3. Each group shows status:
   - ✅ (selected + active)
   - 🗑 (blocked from test)
   - 🗸 (in list but not selected)
4. Toggle selection: `broadcast_manager.toggle_group_selected(group)`
5. If blocked → show test results from last attempt

**Key files:**
- `main.py` lines ~2300-2450 (broadcast_groups_keyboard, bcg_* callbacks)
- `broadcast_manager.py` — `toggle_group_selected()`, `set_group_blocked()`, `set_group_last_test()`

---

### 3. **Balance Check & Purchase**

**Menu:** ⚙️ Settings → 💰 Balance

**Flow:**
1. Show current balance + total spent
2. If balance < selected groups:
   - Show "❌ НЕДОСТАТОЧНО БАЛАНСА"
   - Redirect to purchase menu
3. User picks package → redirect to Stripe Checkout
4. Stripe confirms payment → webhook updates balance

**Key files:**
- `main.py` lines ~2200-2250 (bc_balance, bc_tariffs, buy_* handlers)
- `stripe_handler.py` — `create_checkout_session()`, `process_webhook()`
- `broadcast_manager.py` — `get_balance()`, `set_balance()`, `subtract_balance()`

**Stripe flow:**
```
User clicks [Купить] 
  → handle_purchase(query, tier="medium")
    → create_checkout_session(user_id, "medium")
      → stripe.checkout.Session.create(
          price_id="price_...",
          client_reference_id=str(user_id)
        )
    → Redirect to session.url
      
User pays on Stripe
  
Stripe sends webhook: payment_intent.succeeded
  → POST /stripe-webhook
    → process_webhook(payload, sig_header)
      → Verify signature
      → Extract user_id, tier
      → bm.set_balance(old_balance + STRIPE_PRICES[tier]["posts"])
      → bm.save()
      → Send notification to user
```

---

### 4. **Test Broadcast (verify groups work)**

**Handler:** `@dp.callback_query(F.data == "bc_test")`  
**Flow:**
1. Enforce prerequisite steps:
   - Account connected ✓
   - Posts added ✓
   - Groups selected ✓
   - Schedule configured ✓
   - Test not already passed (can retry anytime)

2. Execute `execute_broadcast(user_id, test_groups, is_test=True)`
   - Message marked with 🧪 emoji
   - Returns: `sent_message_ids = {"group": message_id}`

3. Wait 60 seconds for user to see if messages appear

4. Verify & delete:
   - Check each message still in group (if deleted → note it)
   - Auto-delete test messages to keep groups clean

5. Show results:
   - ✅ Groups where test worked (success)
   - ❌ Groups where test failed (blocked/error)
   - Mark failed groups as `status: "blocked"` so they're skipped in mass broadcast

6. **IMPORTANT:** Test does NOT deduct posts from balance

**Key files:**
- `main.py` lines ~2595-2683 (broadcast_test_v2 handler)
- `parser/broadcast_sender.py` — `execute_broadcast()`, `verify_and_delete_test_messages()`
- `broadcast_manager.py` — `can_run_test()` (cooldown/limit logic)

**Rate limiting (for regular users):**
- 30-second cooldown between test clicks
- Max 5 tests per day
- `OWNER_ID` exempt (unlimited tests)

---

### 5. **Mass Broadcast (one-time send)**

**Handler:** `@dp.callback_query(F.data == "bc_mass")`  
**Flow:**
1. Enforce same prerequisites as test
2. Check balance: `bm.get_balance() >= len(selected_groups)`
   - If not enough → show purchase menu
3. Show confirmation menu with:
   - Groups count
   - Posts to spend
   - Current balance → remaining balance
4. User clicks [✅ Подтвердить]

**Handler:** `@dp.callback_query(F.data == "bc_mass_confirm")`
1. Get active selected groups (not blocked)
2. Execute `execute_broadcast(user_id, groups, is_test=False)`
3. Get `sent_count` from result
4. **Deduct balance:** `bm.subtract_balance(sent_count)`
5. Save: `bm.save()`
6. Show analytics:
   ```
   ✅ РАССЫЛКА ЗАВЕРШЕНА
   Групп: 10
   Отправлено: 8
   Ошибки: 2
   Потрачено: 8 постов
   Баланс: 22 поста
   ```
7. Send separate notification with detailed results

**Key files:**
- `main.py` lines ~2703-2800 (bc_mass, bc_mass_confirm handlers)
- `broadcast_manager.py` — `subtract_balance()`

---

### 6. **Scheduled Broadcast (automatic)**

**Location:** `main.py` — `scheduler_loop()` (runs every 20 seconds in background)

**Flow:**
1. For each user with schedule enabled:
   - Get current time in user's TZ
   - Check if current time matches any scheduled slot (e.g., "08:12")
   - Check if slot already ran today (via `last_runs` dict)
   - If ready:
     - Get active selected groups
     - Execute broadcast with `advance_rotation=True`
     - Update `last_runs` with status + summary
     - Send analytics notification

2. **Analytics notification after scheduled broadcast:**
   ```
   📊 АВТОРАССЫЛКА ЗАВЕРШЕНА
   🕐 Пн 14 апр, 08:12
   
   Группы: 10
   ├─ ✅ Отправлено: 8
   └─ ❌ Не отправлено: 2
   
   💰 Потрачено: 8 постов
   📉 Баланс: 92 поста
   ```

3. **Balance alert (if enabled):**
   - If balance ≤ threshold: send one message per day
   - User can enable/disable in settings

**Key files:**
- `main.py` lines ~857-912 (scheduler_loop)
- `broadcast_manager.py` — `was_slot_run()`, `mark_slot_run()`

---

## Menu Navigation Tree

```
📋 MAIN MENU (/start)
├─ 📢 Broadcast
│  ├─ 💰 Balance
│  │  ├─ [Купить тариф] → Stripe packages
│  │  ├─ [История] → transaction history
│  │  └─ [Назад]
│  │
│  ├─ 📝 Posts (Мой текст)
│  │  ├─ [+ Add post] → select source & message
│  │  ├─ [Delete] × N (if > 0 posts)
│  │  └─ [Done]
│  │
│  ├─ 🎯 Groups (Выбрать группы)
│  │  ├─ Group list (paginated, toggleable)
│  │  │  ├─ ✅ Selected + active
│  │  │  ├─ 🗑 Blocked (show reason)
│  │  │  └─ ☐ Unselected
│  │  ├─ [◀️ / ▶️] (pagination)
│  │  └─ [Back]
│  │
│  ├─ 🧪 Test
│  │  ├─ Validate prerequisites
│  │  ├─ Send test messages with 🧪 marker
│  │  ├─ Wait 60 sec
│  │  ├─ Verify & delete
│  │  └─ Show results → groups blocked on failure
│  │
│  ├─ 📢 Mass Broadcast
│  │  ├─ Show confirmation (groups, posts to spend, new balance)
│  │  ├─ [✅ Confirm] → execute + deduct + show analytics
│  │  └─ [❌ Cancel]
│  │
│  └─ ⏰ Schedule
│     ├─ Select day
│     ├─ Set time
│     ├─ [Toggle enabled/disabled]
│     └─ [Copy to other days]
│
├─ ⚙️ Settings
│  ├─ 🔔 Notifications
│  │  ├─ Balance alerts (enable/disable, set threshold)
│  │  └─ Broadcast analytics (always on)
│  │
│  └─ [Other settings...]
│
└─ [Other menus...]
```

---

## Callback Data Patterns

**Broadcast callbacks:**
- `bc_balance` → show balance menu
- `bc_tariffs` → show purchase options
- `buy_small|buy_medium|buy_large` → create Stripe session
- `bc_posts` → post pool menu
- `bcp_add` → add post prompt
- `bcp_del_{post_id}` → delete specific post
- `bc_groups` → group selection menu
- `bcg_{group_name}` → toggle group selection
- `bcgp_{page_num}` → paginate groups
- `bc_test` → run test broadcast
- `bc_mass` → confirm mass broadcast
- `bc_mass_confirm` → execute mass broadcast
- `bcs_day_{weekday}` → select day
- `bcs_set_{weekday}` → set time for day
- `bcs_toggle_{weekday}` → enable/disable day
- `bcs_copy_{weekday}` → copy settings to another day

**Settings callbacks:**
- `settings_notifications` → notification preferences menu
- `notif_threshold_menu` → balance threshold picker
- `notif_set_threshold_{n}` → set threshold to N
- `notif_balance_toggle` → enable/disable balance alerts

---

## Adding Features to Broadcast

### Add a new button to broadcast menu

1. **Edit `broadcast_main_keyboard()`** (main.py ~505):
   ```python
   rows.append([InlineKeyboardButton(text="📊 Stats", callback_data="bc_stats")])
   ```

2. **Create handler:**
   ```python
   @dp.callback_query(F.data == "bc_stats")
   async def broadcast_stats(query: CallbackQuery):
       user_id = query.from_user.id
       bm = scoped_broadcast_manager(user_id)
       
       # Load state, build response
       text = f"📊 STATISTICS\n\nBalance: {bm.get_balance()} posts"
       
       rows = [[InlineKeyboardButton(text="◀️ Назад", callback_data="bc_main")]]
       await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
       await query.answer()
   ```

### Modify balance deduction

**Current logic:** Only deduct `sent_count` (successful messages)

**If you want to change:**
1. Edit `broadcast_mass_confirm()` → `sent_count = result.get("sent_count", 0)`
2. Decide: deduct all groups attempted? Only successes? Add penalty for failures?
3. Update `subtract_balance(count)` call with new logic
4. Test that balance doesn't go negative

### Add new notification type

1. Add to `broadcast_state.json["notifications"]`:
   ```python
   "my_new_alert": {
       "enabled": True,
       "threshold": 100,  # or other param
       "last_sent": null
   }
   ```

2. Add methods to `broadcast_manager.py`:
   ```python
   def is_my_alert_enabled(self) -> bool:
       return self.state.get("notifications", {}).get("my_new_alert", {}).get("enabled", True)
   ```

3. Add check in `scheduler_loop()`:
   ```python
   if bm.is_my_alert_enabled() and condition_met:
       await bot.send_message(user_id, text)
   ```

4. Add UI in settings menu (bc_settings → my_alert handler)

---

## Common Pitfalls

1. **Forgetting to call `bm.save()`** after modifying state → changes lost
2. **Not checking balance before broadcast** → user thinks post counted but it didn't
3. **Test broadcast deducting posts** → should always be free, only mass broadcasts deduct
4. **Group status not updated** → if broadcast fails, mark group as blocked via `set_group_blocked()`
5. **Stripe webhook not verified** → always check `stripe-signature` header before trusting payment

---

## Testing Broadcast Locally

```bash
# Start bot
cd bot
python main.py

# In Telegram:
1. /start → initializes 30 free posts
2. 📢 Broadcast → 💰 Balance → see "30 постов"
3. 📝 Posts → [+ Add] → select a message from your channel
4. 🎯 Groups → toggle some groups (need at least 1 selected)
5. 🧪 Test → send test messages (should see ✅ success)
6. 📢 Mass → confirm & send (should deduct posts, show analytics)
7. Check balance → should be 30 - sent_count

# Test Stripe webhook (in separate terminal):
stripe listen --forward-to localhost:8000/stripe-webhook  # if you have local endpoint

# Create test payment with:
Card: 4242 4242 4242 4242
Expiry: any future date
CVC: any 3 digits
```

---

## Performance Notes

- **Scheduler runs every 20s** → light load, good for ~100 users per instance
- **Broadcast_lock** prevents concurrent sends → one at a time (safe)
- **JSON file I/O** — fast for < 1MB files, consider DB migration if > 10k users
- **Telethon connections** — establish fresh each broadcast, no pooling (slower but safer for Telegram rate limits)

