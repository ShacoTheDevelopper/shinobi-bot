import json
import time
import base64
import os
import asyncio
import threading
import urllib.request
import urllib.error
from datetime import datetime

import nacl.signing
import discord
from discord.ext import commands

ENDPOINT = "https://shinobibombers.fun/.netlify/functions/ryo"
ORIGIN = "https://shinobibombers.fun"

RANK_NAMES = ["Genin", "Chunin", "Jonin", "Elite", "Legend", "Mythic"]
RANK_COLORS = ["#a8b3c2", "#5fd38d", "#5aa9ff", "#c58cff", "#ffb547", "#ff5a6a"]

WORK_ALL_MIN = 0.3
REST_RESUME = 0.1
BANDS = [[1, 3], [3, 6], [6, 9], [9, 12], [12, 15], [17, 20]]
COUNTS = [1, 2, 3, 4, 5, 7]
DOJO_RATES = [1, 2, 5, 8, 11, 14, 17]
MAX_DEPLOYED = 10

cached_token = None
account_address = None
start_time = None
start_balance = None
game_state = {}
combat_tracker = {}
combat_seq = 0
last_sync_time = 0

# --------------- helpers ---------------

def rank_name(r):
    return RANK_NAMES[r] if r < len(RANK_NAMES) else f"R{r}"

def hero_rank(hero):
    return max(0, min(5, hero.get("rank", hero.get("r", 0))))

def hero_stat(hero, key):
    direct = hero.get(key, 0)
    if direct and hero.get("rank") is None:
        return max(1, direct)
    r = hero_rank(hero)
    band = BANDS[min(r, 5)]
    base_stats = hero.get("baseStats", {})
    bonus = hero.get("bonus", {})
    base = base_stats.get(key, band[0]) if base_stats else hero.get(key, band[0])
    return max(1, max(band[0], min(band[1], base)) + (bonus.get(key, 0) if bonus else 0))

def hero_perks(hero):
    return hero.get("pk", hero.get("perks", []))

def has_perk(hero, perk_id):
    return perk_id in hero_perks(hero)

def max_chakra(hero):
    st = hero_stat(hero, "st")
    base = st * 50
    if has_perk(hero, 4):
        base *= 1.1
    return round(base, 3)

def attack_cooldown(hero):
    sp = hero_stat(hero, "sp")
    cd = max(0.6, 1.6 - sp * 0.05)
    if has_perk(hero, 6):
        cd *= 0.92
    return round(cd, 6)

def recovery_per_sec(hero, dojo=0):
    rate = DOJO_RATES[min(dojo, 6)]
    if has_perk(hero, 3):
        rate += 0.5
    return rate / 60

def attacks_in_interval(hero, seconds, available_chakra):
    cd = attack_cooldown(hero)
    max_attacks = int(seconds / cd)
    return min(max_attacks, int(available_chakra))

def chakra_pct(hero):
    mc = max_chakra(hero)
    return (hero.get("en", 0) / mc * 100) if mc > 0 else 0

def is_deployable(hero):
    return hero.get("en", 0) >= max_chakra(hero) * WORK_ALL_MIN

def time_to_ready(hero, dojo=0):
    target = max_chakra(hero) * WORK_ALL_MIN
    current = hero.get("en", 0)
    if current >= target:
        return 0
    needed = target - current
    rate = recovery_per_sec(hero, dojo)
    return needed / rate if rate > 0 else 9999

def fmt_amount(n):
    n = int(n)
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)

def format_time(seconds):
    if seconds <= 0:
        return "READY"
    m, s = divmod(int(seconds), 60)
    if m >= 60:
        h = m // 60
        m = m % 60
        return f"{h}h{m:02d}m"
    return f"{m}m{s:02d}s" if m else f"{s}s"

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# --------------- config ---------------

def load_config():
    if os.environ.get("PRIVATE_KEY"):
        return {
            "private_key": os.environ["PRIVATE_KEY"].strip(),
            "poll_interval_seconds": int(os.environ.get("POLL_INTERVAL", "30")),
        }
    try:
        with open("config.json") as f:
            return json.load(f)
    except FileNotFoundError:
        raise SystemExit("Set PRIVATE_KEY env var or create config.json")

# --------------- auth ---------------

TOKEN_FILE = "/tmp/shinobi_token.txt"

def save_token(token):
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(token)
    except Exception:
        pass

def load_saved_token():
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except Exception:
        return None

def get_keypair(private_key_base58):
    import base58
    secret = base58.b58decode(private_key_base58)
    return nacl.signing.SigningKey(secret[:32])

def get_address(signing_key):
    import base58
    return base58.b58encode(bytes(signing_key.verify_key)).decode()

# --------------- API ---------------

def build_headers(token=None):
    headers = {
        "Accept": "*/*",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Origin": ORIGIN,
        "Referer": f"{ORIGIN}/play/",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def api_call(action, body=None, token=None, retries=5):
    payload = {"action": action}
    if body:
        payload.update(body)
    data = json.dumps(payload).encode()
    headers = build_headers(token)

    for attempt in range(retries):
        req = urllib.request.Request(ENDPOINT, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            if e.code == 403 and "<!DOCTYPE html>" in body_text:
                wait = 5 * (attempt + 1)
                log(f"    Cloudflare block, retrying in {wait}s ({attempt + 1}/{retries})")
                time.sleep(wait)
                continue
            if e.code == 401:
                return {"error": "Unauthorized", "status": 401}
            try:
                return json.loads(body_text)
            except Exception:
                return {"error": f"HTTP {e.code}: {body_text[:200]}"}
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(3)
                continue
            return {"error": str(e)}
    return {"error": "Failed after retries"}

def authenticate(cfg):
    global cached_token, account_address
    signing_key = get_keypair(cfg["private_key"])
    address = get_address(signing_key)
    account_address = address
    log(f"  Authenticating wallet {address[:6]}...{address[-4:]}")

    server_cfg = api_call("config")
    if "error" in server_cfg:
        raise Exception(f"Config failed: {server_cfg['error']}")
    log(f"  Server network: {server_cfg.get('network', '?')}, mint: {str(server_cfg.get('mint', '?'))[:8]}...")

    challenge = api_call("challenge", {"wallet": address})
    if "error" in challenge:
        raise Exception(f"Challenge failed: {challenge['error']}")

    message = challenge["message"]
    nonce = challenge["nonce"]

    msg_bytes = message.encode("utf-8")
    signed = signing_key.sign(msg_bytes)
    signature_b64 = base64.b64encode(signed.signature).decode()

    resp = api_call("login", {
        "wallet": address,
        "nonce": nonce,
        "signature": signature_b64,
    })
    if "error" in resp:
        raise Exception(f"Login failed: {resp['error']}")

    token = resp.get("token")
    if not token:
        raise Exception("Login response missing token")

    cached_token = token
    save_token(token)
    log("  [OK] Authenticated!")
    return token

def get_token(cfg):
    global cached_token
    if cached_token:
        return cached_token
    env_token = os.environ.get("TOKEN", "").strip()
    if env_token:
        log("  Using TOKEN from env")
        cached_token = env_token
        save_token(env_token)
        return env_token
    saved = load_saved_token()
    if saved:
        log("  Reusing saved token")
        cached_token = saved
        return saved
    return authenticate(cfg)

def fetch_state(cfg):
    global cached_token
    token = get_token(cfg)
    state = api_call("state", token=token)
    if state.get("status") == 401 or "nauthorized" in str(state.get("error", "")):
        log("  Token expired, re-authenticating...")
        cached_token = None
        save_token("")
        token = authenticate(cfg)
        state = api_call("state", token=token)
    return token, state

def build_combat_payload(deployed_ids):
    global combat_seq
    combat_seq += 1
    hero_reports = []
    for tid in deployed_ids:
        if tid in combat_tracker and combat_tracker[tid]["attacks"] > 0:
            hero_reports.append({
                "id": tid,
                "attacks": combat_tracker[tid]["attacks"],
                "kills": combat_tracker[tid]["kills"][:],
            })
    return {
        "combatVersion": 1,
        "combatSeq": combat_seq,
        "combatHeroes": hero_reports,
        "deployed": deployed_ids,
    }

def simulate_combat(deploy_team, elapsed_seconds, chakra_map):
    global combat_tracker
    for hero in deploy_team:
        tid = hero.get("tokenId", "")
        if not tid:
            continue
        chakra_info = chakra_map.get(tid, {})
        available = chakra_info.get("value", 0)
        num_attacks = attacks_in_interval(hero, elapsed_seconds, available)
        if num_attacks <= 0:
            continue
        if tid not in combat_tracker:
            combat_tracker[tid] = {"attacks": 0, "kills": [0, 0, 0, 0, 0]}
        combat_tracker[tid]["attacks"] += num_attacks
        kills_per = max(1, num_attacks // 3)
        combat_tracker[tid]["kills"][0] += kills_per

def adventure_sync(token, deployed_ids, active=True):
    payload = build_combat_payload(deployed_ids)
    payload["active"] = active
    return api_call("adventure", payload, token=token)

def claim_adventure(token, deployed_ids):
    payload = build_combat_payload(deployed_ids)
    payload["active"] = True
    return api_call("claim-adventure", payload, token=token)

def send_presence(token):
    return api_call("presence", {"active": True}, token=token)

# --------------- game logic ---------------

def get_heroes_from_profile(state_data):
    profile = state_data.get("profile")
    if not profile:
        return []
    return profile.get("heroes", [])

def get_chakra_map(chest):
    if not chest:
        return {}
    return chest.get("chakra", {})

def select_deploy_team(heroes, chakra_map, dojo=0):
    eligible = []
    for h in heroes:
        tid = h.get("tokenId", "")
        if not tid:
            continue
        chakra_info = chakra_map.get(tid, {})
        en = chakra_info.get("value", 0)
        if chakra_info.get("reserve") or chakra_info.get("hunt"):
            continue
        lock = chakra_info.get("lockUntil", 0)
        if lock and lock > time.time() * 1000:
            continue
        hero_data = {**h, "en": en}
        if is_deployable(hero_data):
            hero_data["_score"] = h.get("rank", h.get("r", 0)) * 1000 + en
            eligible.append(hero_data)

    eligible.sort(key=lambda h: h["_score"], reverse=True)
    return eligible[:MAX_DEPLOYED]

# --------------- farming loop ---------------

def farm_loop(cfg):
    global start_time, start_balance, cached_token, game_state, combat_tracker, last_sync_time
    poll_interval = cfg.get("poll_interval_seconds", 30)
    consecutive_errors = 0
    sync_counter = 0
    prev_deployed_ids = []

    while True:
        try:
            token, state = fetch_state(cfg)

            if "error" in state:
                consecutive_errors += 1
                wait = min(30 * consecutive_errors, 300)
                log(f"  ERROR: {state['error']} (retry in {format_time(wait)})")
                time.sleep(wait)
                continue

            consecutive_errors = 0
            game_state = state

            balance = state.get("balance", 0)
            bonus = state.get("bonus", 0)
            profile = state.get("profile", {})
            heroes = profile.get("heroes", [])
            dojo = profile.get("dojo", 0)
            hero_count = len(heroes)

            initial_sync = adventure_sync(token, prev_deployed_ids, active=True)
            if "error" in initial_sync:
                log(f"  Adventure sync error: {initial_sync['error']}")
                time.sleep(poll_interval)
                continue

            chest = initial_sync if "chakra" in initial_sync else initial_sync.get("chest", initial_sync)
            chakra_map = chest.get("chakra", {})
            chest_pending = chest.get("pending", 0)
            chest_claimed = chest.get("claimed", 0)
            chest_map = chest.get("map", 1)

            if start_time is None:
                start_time = time.time()
                start_balance = balance + bonus

            elapsed_hours = (time.time() - start_time) / 3600
            earned_session = (balance + bonus) - start_balance
            daily_rate = (earned_session / elapsed_hours * 24) if elapsed_hours > 0.01 else 0

            deploy_team = select_deploy_team(heroes, chakra_map, dojo)
            deployed_ids = [h["tokenId"] for h in deploy_team]

            now = time.time()
            elapsed_since_sync = now - last_sync_time if last_sync_time > 0 else poll_interval
            elapsed_since_sync = min(elapsed_since_sync, poll_interval * 2)

            if deployed_ids and elapsed_since_sync > 0:
                simulate_combat(deploy_team, elapsed_since_sync, chakra_map)

            resting_heroes = []
            deployed_heroes = []
            for h in heroes:
                tid = h.get("tokenId", "")
                chakra_info = chakra_map.get(tid, {})
                en = chakra_info.get("value", 0)
                mc = max_chakra({**h, "en": en})
                pct = (en / mc * 100) if mc > 0 else 0
                if tid in deployed_ids:
                    deployed_heroes.append((h, en, pct))
                else:
                    ttr = time_to_ready({**h, "en": en}, dojo)
                    resting_heroes.append((h, en, pct, ttr))

            total_attacks = sum(combat_tracker.get(tid, {}).get("attacks", 0) for tid in deployed_ids)

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] === Shinobi Bombers ===", flush=True)
            print(f"  Balance: {fmt_amount(balance)} | Bonus: {fmt_amount(bonus)} | Chest: {fmt_amount(chest_pending)} pending / {fmt_amount(chest_claimed)} claimed", flush=True)
            print(f"  Map stage: {chest_map} | Heroes: {hero_count} | Dojo: {dojo}", flush=True)
            if elapsed_hours > 0.01:
                print(f"  Session: {fmt_amount(earned_session)} earned in {format_time(elapsed_hours * 3600)} (~{fmt_amount(daily_rate)}/day)", flush=True)
            print(f"  Deployed: {len(deployed_ids)}/{MAX_DEPLOYED} | Attacks queued: {total_attacks}", flush=True)

            for h, en, pct in deployed_heroes:
                name = h.get("name", f"Hero#{h.get('tokenId', '?')[:6]}")
                r = h.get("rank", h.get("r", 0))
                atk = combat_tracker.get(h.get("tokenId", ""), {}).get("attacks", 0)
                print(f"    [{rank_name(r)}] {name} - Chakra: {en:.0f}/{max_chakra({**h, 'en': en}):.0f} ({pct:.0f}%) atk:{atk}", flush=True)

            if resting_heroes:
                recovering = [(h, en, pct, ttr) for h, en, pct, ttr in resting_heroes if ttr > 0 and ttr < 9999]
                if recovering:
                    recovering.sort(key=lambda x: x[3])
                    next_h, _, _, next_ttr = recovering[0]
                    next_name = next_h.get("name", "?")
                    print(f"  Next hero ready: {next_name} in {format_time(next_ttr)}", flush=True)

            if deployed_ids:
                sync_result = adventure_sync(token, deployed_ids, active=True)
                if "error" in sync_result:
                    log(f"  Deploy sync error: {sync_result['error']}")
                else:
                    log(f"  Synced {len(deployed_ids)} heroes ({total_attacks} attacks reported)")
                    combat_tracker = {}

            last_sync_time = time.time()
            prev_deployed_ids = deployed_ids

            sync_counter += 1
            if sync_counter % 5 == 0:
                send_presence(token)

            sleep_time = poll_interval
            if resting_heroes:
                recovering = [ttr for _, _, _, ttr in resting_heroes if 0 < ttr < 9999]
                if recovering and len(deployed_ids) < MAX_DEPLOYED:
                    next_ready = min(recovering)
                    if next_ready < poll_interval:
                        sleep_time = max(10, next_ready + 5)

            time.sleep(sleep_time)

        except KeyboardInterrupt:
            log("Stopped by user")
            break
        except Exception as e:
            consecutive_errors += 1
            wait = min(30 * consecutive_errors, 300)
            log(f"  EXCEPTION: {e} (retry in {format_time(wait)})")
            time.sleep(wait)

# --------------- Discord bot ---------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    log(f"Discord bot logged in as {bot.user}")

@bot.command(name="status")
async def cmd_status(ctx):
    if not game_state:
        await ctx.send("Bot hasn't fetched state yet.")
        return
    balance = game_state.get("balance", 0)
    bonus = game_state.get("bonus", 0)
    profile = game_state.get("profile", {})
    heroes = profile.get("heroes", [])
    dojo = profile.get("dojo", 0)

    elapsed = (time.time() - start_time) / 3600 if start_time else 0
    earned = (balance + bonus) - start_balance if start_balance is not None else 0
    daily = (earned / elapsed * 24) if elapsed > 0.01 else 0

    lines = [
        f"**Shinobi Bombers Status**",
        f"Balance: {fmt_amount(balance)} | Bonus: {fmt_amount(bonus)}",
        f"Heroes: {len(heroes)} | Dojo: {dojo}",
        f"Session: {fmt_amount(earned)} earned (~{fmt_amount(daily)}/day)",
    ]
    await ctx.send("\n".join(lines))

@bot.command(name="heroes")
async def cmd_heroes(ctx):
    if not game_state:
        await ctx.send("Bot hasn't fetched state yet.")
        return
    profile = game_state.get("profile", {})
    heroes = profile.get("heroes", [])
    if not heroes:
        await ctx.send("No heroes found.")
        return
    lines = ["**Heroes:**"]
    for h in heroes[:20]:
        name = h.get("name", "?")
        r = h.get("rank", h.get("r", 0))
        tid = h.get("tokenId", "?")[:8]
        lines.append(f"  [{rank_name(r)}] {name} ({tid})")
    if len(heroes) > 20:
        lines.append(f"  ... and {len(heroes) - 20} more")
    await ctx.send("\n".join(lines))

@bot.command(name="balance")
async def cmd_balance(ctx):
    if not game_state:
        await ctx.send("Bot hasn't fetched state yet.")
        return
    balance = game_state.get("balance", 0)
    bonus = game_state.get("bonus", 0)
    await ctx.send(f"**Balance:** {fmt_amount(balance)} Ryo | **Bonus:** {fmt_amount(bonus)} Ryo")

@bot.command(name="shinobi")
async def cmd_shinobi(ctx):
    if not game_state:
        await ctx.send("Bot hasn't fetched state yet.")
        return
    balance = game_state.get("balance", 0)
    bonus = game_state.get("bonus", 0)
    profile = game_state.get("profile", {})
    heroes = profile.get("heroes", [])
    dojo = profile.get("dojo", 0)

    elapsed = (time.time() - start_time) / 3600 if start_time else 0
    earned = (balance + bonus) - start_balance if start_balance is not None else 0
    daily = (earned / elapsed * 24) if elapsed > 0.01 else 0

    rank_counts = {}
    for h in heroes:
        r = rank_name(h.get("rank", h.get("r", 0)))
        rank_counts[r] = rank_counts.get(r, 0) + 1

    rank_line = " | ".join(f"{k}: {v}" for k, v in sorted(rank_counts.items()))

    lines = [
        "```",
        "=== SHINOBI BOMBERS ===",
        f"Balance : {fmt_amount(balance)} Ryo",
        f"Bonus   : {fmt_amount(bonus)} Ryo",
        f"Heroes  : {len(heroes)} ({rank_line})",
        f"Dojo    : Lv {dojo}",
        f"Session : {fmt_amount(earned)} earned in {format_time(elapsed * 3600)}",
        f"Rate    : ~{fmt_amount(daily)} Ryo/day",
        "```",
    ]
    await ctx.send("\n".join(lines))

# --------------- main ---------------

def main():
    cfg = load_config()
    log("Starting Shinobi Bombers bot...")

    discord_token = os.environ.get("DISCORD_TOKEN", "").strip()

    farm_thread = threading.Thread(target=farm_loop, args=(cfg,), daemon=True)
    farm_thread.start()

    if discord_token:
        log("Starting Discord bot...")
        bot.run(discord_token)
    else:
        log("No DISCORD_TOKEN set, running without Discord")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            log("Stopped")

if __name__ == "__main__":
    main()
