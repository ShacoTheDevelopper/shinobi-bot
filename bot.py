import json
import time
import base64
import os
import uuid
import random
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
kill_session = str(uuid.uuid4())
kill_totals = [0, 0, 0, 0, 0, 0, 0, 0]
current_map_stage = 1
server_deployed = []

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
    for tid, data in combat_tracker.items():
        if data["attacks"] > 0:
            hero_reports.append({
                "id": tid,
                "attacks": data["attacks"],
                "kills": data["kills"][:],
            })
    return {
        "killSession": kill_session,
        "killTotals": kill_totals[:],
        "killStage": current_map_stage,
        "combatVersion": 1,
        "combatSeq": combat_seq,
        "combatHeroes": hero_reports,
        "deployed": deployed_ids,
    }

def kill_distribution(stage):
    if stage <= 2:
        return [(0, 0.60), (1, 0.30), (2, 0.10)]
    elif stage <= 5:
        return [(0, 0.30), (1, 0.35), (2, 0.25), (3, 0.10)]
    else:
        return [(0, 0.15), (1, 0.30), (2, 0.30), (3, 0.20), (4, 0.05)]

def rebuild_kill_totals():
    global kill_totals
    kill_totals = [0, 0, 0, 0, 0, 0, 0, 0]
    for data in combat_tracker.values():
        for kind_idx, count in enumerate(data["kills"]):
            if count <= 0 or kind_idx > 3:
                continue
            tally_idx = kind_idx * 2
            kill_totals[tally_idx] += count
            kill_totals[tally_idx + 1] += max(0, round(count * 0.08))

def simulate_combat(heroes_data, elapsed_seconds, chakra_map, ack_map=None):
    global combat_tracker
    if ack_map is None:
        ack_map = {}
    dist = kill_distribution(current_map_stage)
    for hero in heroes_data:
        tid = hero.get("tokenId", "")
        if not tid:
            continue
        chakra_info = chakra_map.get(tid, {})
        available = chakra_info.get("value", 0)
        napping = chakra_info.get("napping")
        if napping or available < 1:
            continue
        local_attacks = combat_tracker.get(tid, {}).get("attacks", 0)
        acked = ack_map.get(tid, 0) if isinstance(ack_map, dict) else 0
        pending = max(0, local_attacks - acked)
        effective = max(0, available - pending)
        if effective < 1:
            continue
        num_attacks = attacks_in_interval(hero, elapsed_seconds, effective)
        if num_attacks <= 0:
            continue
        if tid not in combat_tracker:
            combat_tracker[tid] = {"attacks": 0, "kills": [0, 0, 0, 0, 0]}
        combat_tracker[tid]["attacks"] += num_attacks
        kills = max(1, num_attacks // 3)
        for kind_idx, weight in dist:
            kind_kills = max(0, round(kills * weight))
            combat_tracker[tid]["kills"][kind_idx] += kind_kills
    rebuild_kill_totals()

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
    global start_time, start_balance, cached_token, game_state
    global last_sync_time, current_map_stage, server_deployed
    poll_interval = cfg.get("poll_interval_seconds", 30)
    consecutive_errors = 0
    sync_counter = 0
    start_chest = None
    prev_chakra_map = {}
    prev_hero_map = {}
    prev_attack_ack = {}

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
            hero_map = {h["tokenId"]: h for h in heroes if h.get("tokenId")}

            now = time.time()
            if server_deployed and last_sync_time > 0:
                elapsed_since_sync = min(now - last_sync_time, poll_interval * 2)
                active_heroes = []
                for tid in server_deployed:
                    h = hero_map.get(tid) or prev_hero_map.get(tid)
                    if h:
                        active_heroes.append(h)
                if active_heroes and elapsed_since_sync > 0:
                    simulate_combat(active_heroes, elapsed_since_sync, prev_chakra_map, prev_attack_ack)

            sync_resp = adventure_sync(token, server_deployed, active=True)
            if "error" in sync_resp:
                log(f"  Adventure sync error: {sync_resp['error']}")
                time.sleep(poll_interval)
                continue

            chest = sync_resp if "chakra" in sync_resp else sync_resp.get("chest", sync_resp)
            chakra_map = chest.get("chakra", {})
            chest_pending = chest.get("pending", 0)
            chest_claimed = chest.get("claimed", 0)
            chest_map = chest.get("map", 1)
            current_map_stage = max(1, chest_map)
            server_deployed = chest.get("deployed", [])
            attack_ack = chest.get("attackAck", {})

            napping_ids = set()
            for tid in server_deployed:
                ci = chakra_map.get(tid, {})
                if ci.get("napping") or ci.get("value", 0) < 1:
                    napping_ids.add(tid)

            need_rotation = not server_deployed or len(napping_ids) >= len(server_deployed) // 2

            if need_rotation:
                best_team = select_deploy_team(heroes, chakra_map, dojo)
                wanted_ids = [h["tokenId"] for h in best_team]
                new_heroes = set(wanted_ids) - set(server_deployed)
                if wanted_ids and new_heroes:
                    action = "Rotating" if server_deployed else "Deploying"
                    log(f"  {action}: {len(napping_ids)} napping, {len(new_heroes)} fresh of {len(wanted_ids)} team...")
                    deploy_resp = adventure_sync(token, wanted_ids, active=True)
                    if "error" not in deploy_resp:
                        d_chest = deploy_resp if "chakra" in deploy_resp else deploy_resp.get("chest", deploy_resp)
                        server_deployed = d_chest.get("deployed", [])
                        chakra_map = d_chest.get("chakra", chakra_map)
                        chest_pending = d_chest.get("pending", chest_pending)
                        attack_ack = d_chest.get("attackAck", {})
                        log(f"  Server accepted {len(server_deployed)} heroes")
                elif not server_deployed:
                    log(f"  All heroes exhausted, waiting for recovery...")

            if start_time is None:
                start_time = time.time()
                start_balance = balance + bonus
                start_chest = chest_pending

            elapsed_hours = (time.time() - start_time) / 3600
            chest_earned = chest_pending - (start_chest or 0)
            daily_rate = (chest_earned / elapsed_hours * 24) if elapsed_hours > 0.01 else 0

            deployed_heroes = []
            resting_heroes = []
            deployed_set = set(server_deployed)
            for h in heroes:
                tid = h.get("tokenId", "")
                chakra_info = chakra_map.get(tid, {})
                en = chakra_info.get("value", 0)
                mc = chakra_info.get("max", max_chakra({**h, "en": en}))
                pct = (en / mc * 100) if mc > 0 else 0
                napping = chakra_info.get("napping")
                if tid in deployed_set:
                    deployed_heroes.append((h, en, mc, pct, napping))
                else:
                    ttr = time_to_ready({**h, "en": en}, dojo)
                    resting_heroes.append((h, en, pct, ttr))

            total_attacks = sum(d["attacks"] for d in combat_tracker.values())
            acked = sum(attack_ack.values()) if isinstance(attack_ack, dict) else 0

            print(f"\n[{datetime.now().strftime('%H:%M:%S')}] === Shinobi Bombers ===", flush=True)
            print(f"  Balance: {fmt_amount(balance)} | Bonus: {fmt_amount(bonus)} | Chest: {fmt_amount(chest_pending)} pending / {fmt_amount(chest_claimed)} claimed", flush=True)
            print(f"  Map stage: {chest_map} | Heroes: {hero_count} | Dojo: {dojo} | Kills: {chest.get('kills', 0)}", flush=True)
            if elapsed_hours > 0.01:
                print(f"  Session: {fmt_amount(chest_earned)} chest earned in {format_time(elapsed_hours * 3600)} (~{fmt_amount(daily_rate)}/day)", flush=True)
            print(f"  Deployed: {len(server_deployed)}/{MAX_DEPLOYED} | Attacks: {total_attacks} sent / {acked} acked", flush=True)

            for h, en, mc, pct, napping in deployed_heroes:
                name = h.get("name", f"Hero#{h.get('tokenId', '?')[:6]}")
                r = h.get("rank", h.get("r", 0))
                status = "NAP" if napping else "ATK"
                print(f"    [{rank_name(r)}] {name} - Chakra: {en:.0f}/{mc:.0f} ({pct:.0f}%) [{status}]", flush=True)

            if resting_heroes:
                recovering = [(h, en, pct, ttr) for h, en, pct, ttr in resting_heroes if ttr > 0 and ttr < 9999]
                if recovering:
                    recovering.sort(key=lambda x: x[3])
                    next_h, _, _, next_ttr = recovering[0]
                    next_name = next_h.get("name", "?")
                    print(f"  Next hero ready: {next_name} in {format_time(next_ttr)}", flush=True)

            log(f"  Synced {len(server_deployed)} heroes | {total_attacks} atk sent, {acked} acked | kills: {sum(kill_totals)}")

            last_sync_time = time.time()
            prev_chakra_map = chakra_map
            prev_hero_map = hero_map
            prev_attack_ack = attack_ack

            sync_counter += 1
            if sync_counter % 5 == 0:
                send_presence(token)

            sleep_time = poll_interval
            if resting_heroes:
                recovering = [ttr for _, _, _, ttr in resting_heroes if 0 < ttr < 9999]
                if recovering and len(server_deployed) < MAX_DEPLOYED:
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
