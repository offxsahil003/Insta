#!/usr/bin/env python3
"""
Instagram Parallel DM & Group Chat Watcher (Fixed version)
- QR sending made robust
- Bot ignores self messages reliably
- GC welcome: suppressed for 1 hour after thread is first watched (only prints to terminal during that hour)
- Prevents duplicate welcomes within 24h
- DB persists watched_threads.start_time so restarts keep suppression behavior
- Minor safety / interval improvements
"""

import os
import json
import time
import threading
import sqlite3
import random
from datetime import datetime, timedelta
from collections import defaultdict
from instagrapi import Client
from instagrapi.exceptions import TwoFactorRequired, ChallengeRequired, ClientError
import requests

SESSION_DIR = "./sessions"
DB_PATH = "./bot_state.db"
CHECK_INTERVAL = 1.0               # small sleep inside per-thread watcher to avoid busy-loop
THREAD_FETCH_INTERVAL = 90        # 90 sec
WELCOME_MESSAGE = "welcome to Sahil's bot type help to see available commands ⚡"
MAX_MESSAGES_TRACK = 200
MAX_HISTORY_MESSAGES = 10  # max user+assistant pairs

GC_WELCOMES = [
    "👋 Sahil bot aapka swagat karta hai is GC mein! Type help for commands ⚡",
    "🎉 Welcome aboard! Sahil's bot is here to assist. Check help for magic ✨",
    "🚀 Namaste! Join the fun with Sahil bot. help to explore features 🌟"
]

COMMANDS_HELP = """
📋 **Available Commands:**
• start - Welcome message 🤖
• help - This help list 📖
• qr - Payment QR code 💳
• info @username - Fetch IG profile details 🔍
• enteraimode - Enable AI mode (Groq-powered) 🤖❓
• exitaimode - Disable AI mode ❌
• botinfo - Bot details & uptime ℹ️
"""

BOT_INFO = """
🤖 **Sahil-Bot Info:**
• Version: 1.1 Final ✨
• Owner: Sahil Shah 👑
• Username: @offxsahil62 📱
• Uptime: {uptime} ⏰
• Made by: Sahil ❤️
"""

# Global AI History and Lock
histories = defaultdict(list)
history_lock = threading.Lock()


def session_path(username: str):
    os.makedirs(SESSION_DIR, exist_ok=True)
    return os.path.join(SESSION_DIR, f"session_{username}.json")


def load_session(cl: Client, username: str):
    path = session_path(username)
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                cl.set_settings(json.load(f))
            # quick test to validate session
            cl.get_timeline_feed()
            print(f"✅ Loaded session for {username}")
            return True
        except Exception:
            print(f"⚠️ Failed loading saved session for {username}, will login fresh.")
    return False


def save_session(cl: Client, username: str):
    path = session_path(username)
    try:
        with open(path, "w") as f:
            json.dump(cl.get_settings(), f)
        print(f"💾 Session saved for {username}")
    except Exception as e:
        print("⚠️ Failed to save session:", e)


def relogin_if_needed(cl: Client, username: str, password: str):
    """Auto relogin if fetch fails (session expired)"""
    max_retries = 2
    for attempt in range(max_retries):
        try:
            # safe test: try a lightweight call
            cl.direct_threads(1)
            return True
        except Exception:
            print(f"⚠️ Session issue detected, attempting relogin (attempt {attempt+1})")
            try:
                login(cl, username, password)
                save_session(cl, username)
                return True
            except Exception as e:
                print("⚠️ Relogin attempt failed:", e)
                time.sleep(2)
    print("❌ Failed to relogin after retries")
    return False


def login(cl: Client, username: str, password: str):
    try:
        cl.login(username, password)
    except TwoFactorRequired:
        code = input("🔐 Enter 2FA code: ")
        cl.login(username, password, verification_code=code)
    except ChallengeRequired:
        print("⚠️ Challenge required — complete it in the app.")
        raise


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Watched threads: added start_time for 1 hour suppression
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS watched_threads (
            thread_id TEXT PRIMARY KEY,
            is_group INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            start_time TIMESTAMP
        )
    """)
    # DM welcomes (per thread)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS dm_welcomes (
            thread_id TEXT PRIMARY KEY,
            welcomed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # GC user states
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS gc_users (
            thread_id TEXT,
            user_id TEXT,
            welcomed BOOLEAN DEFAULT 0,
            last_msg_time TIMESTAMP,
            ai_enabled BOOLEAN DEFAULT 0,
            PRIMARY KEY (thread_id, user_id)
        )
    """)
    conn.commit()
    conn.close()


def get_watched_threads():
    """
    Returns dictionary: thread_id -> (is_group_bool, start_time_or_None)
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT thread_id, is_group, start_time FROM watched_threads")
    rows = cursor.fetchall()
    conn.close()
    result = {}
    for tid, isg, st in rows:
        st_dt = None
        if st:
            try:
                st_dt = datetime.fromisoformat(st)
            except Exception:
                try:
                    st_dt = datetime.strptime(st, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    st_dt = None
        result[str(tid)] = (bool(isg), st_dt)
    return result


def add_watched_thread(thread_id: str, is_group: bool, start_time: datetime = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if start_time:
        st_iso = start_time.isoformat()
        cursor.execute("INSERT OR IGNORE INTO watched_threads (thread_id, is_group, start_time) VALUES (?, ?, ?)",
                       (thread_id, int(is_group), st_iso))
        # If it existed without start_time, update it
        cursor.execute("UPDATE watched_threads SET start_time = ? WHERE thread_id = ? AND (start_time IS NULL OR start_time = '')",
                       (st_iso, thread_id))
    else:
        cursor.execute("INSERT OR IGNORE INTO watched_threads (thread_id, is_group) VALUES (?, ?)", (thread_id, int(is_group)))
    conn.commit()
    conn.close()


def mark_dm_welcome(thread_id: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO dm_welcomes (thread_id) VALUES (?)", (thread_id,))
    conn.commit()
    conn.close()


def is_dm_welcomed(thread_id: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM dm_welcomes WHERE thread_id = ?", (thread_id,))
    result = cursor.fetchone()
    conn.close()
    return bool(result)


def update_gc_user(thread_id: str, user_id: str, welcomed: bool = None, last_msg_time: str = None, ai_enabled: bool = None):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Ensure row exists
    cursor.execute("INSERT OR IGNORE INTO gc_users (thread_id, user_id) VALUES (?, ?)", (thread_id, user_id))
    # Build updates
    updates = []
    params = []
    if welcomed is not None:
        updates.append("welcomed = ?")
        params.append(int(welcomed))
    if last_msg_time is not None:
        updates.append("last_msg_time = ?")
        params.append(last_msg_time)
    if ai_enabled is not None:
        updates.append("ai_enabled = ?")
        params.append(int(ai_enabled))
    if updates:
        params.append(thread_id)
        params.append(user_id)
        cursor.execute(f"UPDATE gc_users SET {', '.join(updates)} WHERE thread_id = ? AND user_id = ?", params)
    conn.commit()
    conn.close()


def get_gc_user_state(thread_id: str, user_id: str) -> dict:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT welcomed, last_msg_time, ai_enabled FROM gc_users WHERE thread_id = ? AND user_id = ?", (thread_id, user_id))
    result = cursor.fetchone()
    conn.close()
    if result:
        welcomed = bool(result[0])
        last_time_raw = result[1]
        ai_en = bool(result[2])
        last_time = None
        if last_time_raw:
            try:
                last_time = datetime.fromisoformat(last_time_raw)
            except Exception:
                try:
                    last_time = datetime.strptime(last_time_raw, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    last_time = None
        return {'welcomed': welcomed, 'last_msg_time': last_time, 'ai_enabled': ai_en}
    return {'welcomed': False, 'last_msg_time': None, 'ai_enabled': False}


def calculate_uptime(start_time: float) -> str:
    elapsed = time.time() - start_time
    hours = int(elapsed // 3600)
    minutes = int((elapsed % 3600) // 60)
    return f"{hours} hr {minutes} m"


def send_qr(cl: Client, thread_id: str):
    # Robust QR send: try multiple approaches depending on instagrapi capabilities
    if not os.path.exists("qr.jpg"):
        try:
            cl.direct_send("❌ QR not found!", thread_ids=[int(thread_id)])
        except Exception:
            # fallback string thread id
            cl.direct_send("❌ QR not found!", thread_ids=[thread_id])
        print("⚠️ qr.jpg missing")
        return

    try:
        # Preferred: use direct_send with fileobj if available
        with open("qr.jpg", "rb") as f:
            # instagrapi accepts file-like with direct_send (some versions), but direct_send_photo is also common
            try:
                cl.direct_send("", thread_ids=[int(thread_id)], file=f)
            except TypeError:
                # The signature might not accept file kwarg: fallback to direct_send_photo
                f.seek(0)
                cl.direct_send_photo(f, thread_ids=[int(thread_id)])
        print(f"💳 QR sent to {thread_id}")
    except Exception as e:
        print("⚠️ Failed to send QR using preferred methods, trying fallback. Error:", e)
        try:
            cl.direct_send_photo(open("qr.jpg", "rb"), thread_ids=[int(thread_id)])
            print(f"💳 QR sent (fallback) to {thread_id}")
        except Exception as e2:
            print("❌ All QR send attempts failed:", e2)
            try:
                cl.direct_send("❌ QR could not be sent due to an error.", thread_ids=[int(thread_id)])
            except Exception:
                pass


def fetch_profile_info(cl: Client, username: str, thread_id: str):
    try:
        user = cl.user_info_by_username(username)
        bio = user.biography or "No bio 📝"
        info_msg = f"""
🔍 **Profile: @{username}**
👤 Followers: {user.follower_count:,} ❤️
👥 Following: {user.following_count:,} 👀
📸 Posts: {user.media_count} 
🖼️ DP: https://instagram.com/{username}/
📄 Bio: {bio[:200]}...
        """
        cl.direct_send(info_msg.strip(), thread_ids=[int(thread_id)])
        print(f"📊 Profile sent for @{username}")
    except Exception as e:
        try:
            cl.direct_send(f"❌ Error fetching @{username}: {str(e)}", thread_ids=[int(thread_id)])
        except Exception:
            print("⚠️ Could not send profile error message.", e)


def get_ai_response(thread_id: str, sender_id: str, query: str, cl: Client) -> str:
    groq_api_key = os.getenv("GROQ_API_KEY")
    if not groq_api_key:
        return "❌ API key missing. Ask the bot owner to set GROQ_API_KEY."

    groq_api_base = "https://api.groq.com/openai/v1"
    key = (thread_id, sender_id)
    with history_lock:
        messages = histories[key][:]  # clone

        # Ensure system role is always first
        if not messages or messages[0]['role'] != 'system':
            messages.insert(0, {
                'role': 'system',
                'content': "You are Sahil's helpful bot named Sahil-Bot. Respond helpfully and conversationally."
            })

        # Add current user query
        messages.append({'role': 'user', 'content': query})

        # Limit history size (keep system role intact)
        if len(messages) > MAX_HISTORY_MESSAGES + 1:  # +1 for system
            system_msg = messages.pop(0)  # remove system
            messages = messages[-MAX_HISTORY_MESSAGES:]  # last user+assistant pairs
            messages.insert(0, system_msg)  # re-add system

    try:
        response = requests.post(
            f"{groq_api_base}/chat/completions",
            json={
                "model": "meta-llama/llama-4-maverick-17b-128e-instruct",
                "messages": messages,
            },
            headers={
                'Authorization': f'Bearer {groq_api_key}',
                'Content-Type': 'application/json',
            },
            timeout=30
        )
        response.raise_for_status()
        answer = response.json()['choices'][0]['message']['content']

        with history_lock:
            histories[key].append({'role': 'assistant', 'content': answer})

        return answer
    except Exception as e:
        print(f"❌ Groq API error: {e}")
        return "❌ Sorry, AI is taking a break. Try again later! ⚡"


def is_command(text: str) -> bool:
    commands = ['start', 'help', 'qr', 'enteraimode', 'exitaimode', 'botinfo']
    if text in commands:
        return True
    if text.startswith('info @'):
        return True
    return False


def process_command(cl: Client, text: str, thread_id: str, sender_str: str, sender_id: int, is_group: bool, bot_start_time: float):
    try:
        if text == 'start':
            reply_msg = WELCOME_MESSAGE
            cl.direct_send(reply_msg, thread_ids=[int(thread_id)])
        elif text == 'help':
            cl.direct_send(COMMANDS_HELP, thread_ids=[int(thread_id)])
        elif text == 'qr':
            send_qr(cl, thread_id)
        elif text.startswith('info @'):
            username_to_fetch = text.split('@', 1)[1].strip() if '@' in text else None
            if username_to_fetch:
                threading.Thread(target=fetch_profile_info, args=(cl, username_to_fetch, thread_id), daemon=True).start()
            else:
                cl.direct_send("❌ Usage: info @username", thread_ids=[int(thread_id)])
        elif text == 'enteraimode':
            update_gc_user(thread_id, sender_str, ai_enabled=True)
            if is_group:
                try:
                    sender_username = cl.user_info(sender_id).username
                    cl.direct_send(f"@{sender_username} AI mode enabled ✅ Ask me a question ❓", thread_ids=[int(thread_id)])
                except:
                    cl.direct_send("AI mode enabled ✅ Ask me a question ❓", thread_ids=[int(thread_id)])
            else:
                cl.direct_send("AI mode enabled ✅ Ask me a question ❓", thread_ids=[int(thread_id)])
        elif text == 'exitaimode':
            update_gc_user(thread_id, sender_str, ai_enabled=False)
            if is_group:
                try:
                    sender_username = cl.user_info(sender_id).username
                    cl.direct_send(f"@{sender_username} Exited AI mode ✅", thread_ids=[int(thread_id)])
                except:
                    cl.direct_send("Exited AI mode ✅", thread_ids=[int(thread_id)])
            else:
                cl.direct_send("Exited AI mode ✅", thread_ids=[int(thread_id)])
        elif text == 'botinfo':
            uptime = calculate_uptime(bot_start_time)
            info_msg = BOT_INFO.format(uptime=uptime)
            cl.direct_send(info_msg, thread_ids=[int(thread_id)])
        else:
            cl.direct_send("❓ Unknown command. Type help for list 📋", thread_ids=[int(thread_id)])
    except Exception as e:
        print("⚠️ process_command error:", e)


def watch_thread(cl, thread_id: str, is_group: bool, bot_start_time: float, threads_lock: threading.Lock, username: str, password: str):
    thread_id_str = str(thread_id)
    if is_group:
        print(f"\n🤖 Bot active on GROUP thread {thread_id_str}")
    else:
        print(f"\n🤖 Bot active on DM thread {thread_id_str}")

    # Ensure record exists and get start_time
    add_watched_thread(thread_id_str, is_group)
    watched = get_watched_threads().get(thread_id_str)
    thread_start_time = None
    if watched:
        thread_start_time = watched[1]  # may be None

    # Build seen message set
    seen_ids = set()
    try:
        thread_obj = cl.direct_thread(thread_id)
        for msg in getattr(thread_obj, "messages", []):
            try:
                seen_ids.add(str(msg.id))
            except Exception:
                pass
    except Exception:
        pass

    while True:
        try:
            if not relogin_if_needed(cl, username, password):
                time.sleep(5)
                continue

            thread_obj = cl.direct_thread(thread_id)
            bot_user_id = str(cl.user_id)

            for msg in list(reversed(getattr(thread_obj, "messages", []))):
                mid = getattr(msg, "id", None)
                if mid is None:
                    continue
                mid = str(mid)
                if mid in seen_ids:
                    continue
                seen_ids.add(mid)

                sender_id = getattr(msg, "user_id", None)
                if sender_id is None:
                    continue
                sender_id_str = str(sender_id)

                # Skip self messages robustly
                if sender_id_str == bot_user_id:
                    # ignore messages originating from bot user
                    continue

                text = (getattr(msg, "text", "") or "").strip()
                text_lower = text.lower()

                if not text_lower:
                    continue

                now = datetime.now()

                if is_group:
                    # Get per-user state
                    state = get_gc_user_state(thread_id_str, sender_id_str)

                    # Update last_msg_time always
                    update_gc_user(thread_id_str, sender_id_str, last_msg_time=now.isoformat())

                    # Decide whether to welcome: only if not welcomed within 24h
                    already_welcomed = state['welcomed']
                    last_msg_time = state['last_msg_time']
                    needs_welcome = not already_welcomed or (last_msg_time and (now - last_msg_time) > timedelta(hours=24))

                    # If this thread was started less than 1 hour ago, do NOT send welcome to IG.
                    # Instead, log to terminal that we *would* welcome (so admins see it); after 1 hour real welcomes resume.
                    within_suppression = False
                    if thread_start_time:
                        if (now - thread_start_time) < timedelta(hours=1):
                            within_suppression = True

                    if needs_welcome:
                        try:
                            sender_username = cl.user_info(sender_id).username
                        except Exception:
                            sender_username = "user"

                        welcome_text = random.choice(GC_WELCOMES)
                        if within_suppression:
                            # Log only to terminal and update DB to NOT mark welcomed (so it still can be welcomed after 1h)
                            print(f"[SKIP-WELCOME] (within 1h) Would welcome @{sender_username} in thread {thread_id_str} — skipping IG send to avoid spam.")
                            # Update last_msg_time already done; do not set welcomed=True so welcome can happen after suppression expires.
                        else:
                            # Only send if not welcomed in last 24h
                            if not already_welcomed or (last_msg_time and (now - last_msg_time) > timedelta(hours=24)):
                                reply_msg = f"@{sender_username} {welcome_text}"
                                try:
                                    cl.direct_send(reply_msg, thread_ids=[int(thread_id)])
                                    print(f"🎉 GC Welcome sent to @{sender_username} in {thread_id_str}")
                                    # mark welcomed now and update last_msg_time
                                    update_gc_user(thread_id_str, sender_id_str, welcomed=True, last_msg_time=now.isoformat())
                                except Exception as e:
                                    print("⚠️ Failed to send GC welcome:", e)

                    # AI Mode handling in GC
                    state_after = get_gc_user_state(thread_id_str, sender_id_str)  # refetch
                    if state_after['ai_enabled'] and not is_command(text_lower):
                        ai_reply = get_ai_response(thread_id_str, sender_id_str, text, cl)
                        reply_msg = f"〄 The Reply:\n\n{ai_reply}"
                        try:
                            cl.direct_send(reply_msg, thread_ids=[int(thread_id)])
                        except Exception as e:
                            print("⚠️ Failed sending AI reply in GC:", e)
                        continue

                    # Commands in GC only processed if it's a command
                    if not is_command(text_lower):
                        continue

                else:
                    # DM logic
                    welcomed = is_dm_welcomed(thread_id_str)
                    if not welcomed and not is_command(text_lower):
                        # Send welcome ONCE per DM thread
                        try:
                            cl.direct_send(WELCOME_MESSAGE, thread_ids=[int(thread_id)])
                            mark_dm_welcome(thread_id_str)
                            print(f"✅ DM Welcome sent to {thread_id_str}")
                        except Exception as e:
                            print("⚠️ Failed to send DM welcome:", e)
                        continue

                    if not is_command(text_lower):
                        # If in DM user has AI enabled, respond
                        dm_state = get_gc_user_state(thread_id_str, sender_id_str)  # reuse table for DM AI state
                        if dm_state['ai_enabled']:
                            ai_reply = get_ai_response(thread_id_str, sender_id_str, text, cl)
                            try:
                                cl.direct_send(f"〄 The Reply:\n\n{ai_reply}", thread_ids=[int(thread_id)])
                            except Exception as e:
                                print("⚠️ Failed sending AI reply in DM:", e)
                        continue

                # If reached here, it's a command — process it
                process_command(cl, text_lower, thread_id_str, sender_id_str, int(sender_id), is_group, bot_start_time)

            # Trim seen_ids occasionally
            if len(seen_ids) > MAX_MESSAGES_TRACK:
                seen_ids = set(list(seen_ids)[-MAX_MESSAGES_TRACK:])

            time.sleep(CHECK_INTERVAL)

        except Exception as e:
            print(f"⚠️ Watcher error in {thread_id_str}: {e}")
            time.sleep(0.5)


def main():
    USERNAME = os.getenv("IG_USERNAME", "YOUR_USERNAME")
    PASSWORD = os.getenv("IG_PASSWORD", "YOUR_PASSWORD")

    if USERNAME == "YOUR_USERNAME" or PASSWORD == "YOUR_PASSWORD":
        print("⚠️ Please set IG_USERNAME and IG_PASSWORD environment variables.")
        exit(1)

    init_db()

    cl = Client()
    bot_start_time = time.time()

    if not load_session(cl, USERNAME):
        print("🔑 Logging in fresh...")
        login(cl, USERNAME, PASSWORD)
        save_session(cl, USERNAME)

    # Resume watchers from DB
    watched_threads = get_watched_threads()  # returns thread_id -> (is_group, start_time)
    threads_lock = threading.Lock()

    # Launch resumed watchers
    for tid, (is_group, start_time) in watched_threads.items():
        with threads_lock:
            t = threading.Thread(target=watch_thread, args=(cl, tid, is_group, bot_start_time, threads_lock, USERNAME, PASSWORD), daemon=True)
            t.start()

    # Main loop for new threads
    while True:
        try:
            if not relogin_if_needed(cl, USERNAME, PASSWORD):
                time.sleep(5)
                continue

            threads = cl.direct_threads(20)  # fetch many threads

            dm_threads = [t for t in threads if len(getattr(t, "users", [])) == 1][:8]
            gc_threads = [t for t in threads if len(getattr(t, "users", [])) > 1][:8]

            # Launch watchers for new DMs
            for t in dm_threads:
                tid = str(t.id)
                with threads_lock:
                    if tid in watched_threads:
                        continue
                    # For DMs we set start_time to now (suppression logic only applies to GC but storing consistent)
                    watched_threads[tid] = (False, datetime.now())
                    add_watched_thread(tid, False, start_time=datetime.now())

                thread = threading.Thread(target=watch_thread, args=(cl, tid, False, bot_start_time, threads_lock, USERNAME, PASSWORD), daemon=True)
                thread.start()

            # Launch watchers for new GCs
            for t in gc_threads:
                tid = str(t.id)
                with threads_lock:
                    if tid in watched_threads:
                        continue
                    # For new group threads, set start_time so suppression works
                    start_time = datetime.now()
                    watched_threads[tid] = (True, start_time)
                    add_watched_thread(tid, True, start_time=start_time)

                thread = threading.Thread(target=watch_thread, args=(cl, tid, True, bot_start_time, threads_lock, USERNAME, PASSWORD), daemon=True)
                thread.start()

            time.sleep(THREAD_FETCH_INTERVAL)

        except KeyboardInterrupt:
            print("\n👋 Bot stopped. Saving session.")
            save_session(cl, USERNAME)
            break
        except Exception as e:
            print("⚠️ Main error:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()