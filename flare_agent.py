#!/usr/bin/env python3
"""
flare-agent: break-glass on-call responder.

Triggered by a file watcher when FLARE.md appears in the workspace.
- Notifies you over chat that a flare is active
- Invokes an AI agent CLI headlessly to investigate (read-only)
- Sends you the findings and waits for go/no-go
- Runs the fix only after you approve (or after the optional auto-go timer)
- Archives the flare and exits when resolved or stood down

Subcommands:
    flare_agent.py            handle the active flare (what the watcher runs)
    flare_agent.py diag       read-only pipeline health report, safe for a
                              small local model to run and relay
    flare_agent.py restart    re-fire the handler if it died mid-flare
    flare_agent.py encrypt F  encrypt a briefing file (writes F.enc)
    flare_agent.py rekey F    rotate the passphrase on an encrypted file

Design notes, each one paid for in production:
- Agent stdout/stderr go to a temp file, NOT subprocess.PIPE.
  PIPE + communicate() can deadlock if the agent or its grandchildren
  hold the pipes open. File redirection sidesteps the entire issue.
- The agent is launched with start_new_session=True so we can kill the
  whole process group on timeout or stand-down.
- Every network call is wrapped. A hiccup talking to the chat API must
  never wedge the main loop.
- Heartbeats fire every HEARTBEAT_INTERVAL so you always know the run
  is still alive.
- Findings are persisted to disk BEFORE any send is attempted. The
  network is often the thing that is broken.
- PID lock prevents two handlers racing the same incident.
- Log line at every state transition. The log is self-diagnosing.
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# ---------------------------------------------------------------------------
# Config - edit these to match your setup
# ---------------------------------------------------------------------------

WORKSPACE       = "/path/to/your/workspace"     # where FLARE.md appears
BRIEFING_FILES  = [                             # handed to the agent as context
    "/path/to/your/credentials.md",             # how to reach your machines
    "/path/to/your/topology.md",                # what your machines are
]
# A briefing file ending in ".enc" is encrypted at rest. At incident time the
# handler asks for the passphrase over chat, decrypts to a 0600 temp copy for
# the duration of the incident, and deletes the copy on exit. Create and
# rotate encrypted files with:
#   python3 flare_agent.py encrypt /path/to/credentials.md
#   python3 flare_agent.py rekey   /path/to/credentials.md.enc

NOTIFIER        = "telegram"                    # telegram | ntfy | webhook
ALLOWED_USER    = 0                             # your Telegram numeric user id
NTFY_URL        = ""                            # e.g. "https://ntfy.sh/your-private-topic"

AGENT           = "claude"                      # claude | codex | gemini | opencode | goose
AGENT_MODEL     = "opus"                        # strongest model you have, pinned. See README.
AGENT_BIN       = ""                            # explicit path override; "" = auto-resolve

# Budgets per phase, in USD. Only passed to agents that support a budget flag.
INVESTIGATE_BUDGET_USD = 1.50
FIX_BUDGET_USD         = 4.00

AGENT_TIMEOUT      = 600     # hard cap per agent invocation, seconds
AUTO_GO_SECONDS    = 0       # 0 = never auto-approve. I run mine at 1800. See README.
HEARTBEAT_INTERVAL = 90      # "still working" ping interval during agent runs
HTTP_TIMEOUT       = 12      # max seconds for any single notifier HTTP call
PASSPHRASE_TIMEOUT = 600     # how long to wait for a briefing passphrase over chat
LOG_REPLIES        = True    # set False if you might text secrets mid-incident,
                             # so they never land in the handler log

# Optional shared secret that must appear in any fix instruction, e.g. "4417".
# The chat account is otherwise the entire authentication boundary: whoever
# holds your unlocked phone can execute commands inside your network. With a
# PIN set, they can read findings but executing anything needs this too.
# Stand-down never needs the PIN; stopping things must always be easy.
GO_PIN = ""

# Secrets resolve in order: environment variable, then command, then file.
# The command form lets you keep tokens out of plaintext entirely:
#   "op read op://Homelab/flare-bot/credential"
#   "security find-generic-password -s flare-bot -w"
#   "pass show homelab/flare-bot"
BOT_TOKEN_ENV    = "FLARE_BOT_TOKEN"
BOT_TOKEN_CMD    = ""
BOT_TOKEN_FILE   = ""        # chmod 600, token on the first line, outside any repo

AGENT_TOKEN_ENV  = "FLARE_AGENT_TOKEN"
AGENT_TOKEN_CMD  = ""
AGENT_TOKEN_FILE = ""

# ---------------------------------------------------------------------------
# Derived paths and constants - usually no need to touch
# ---------------------------------------------------------------------------

FLARE_FILE     = os.path.join(WORKSPACE, "FLARE.md")
FLARE_RESPONSE = os.path.join(WORKSPACE, "FLARE_RESPONSE.md")
ARCHIVE_DIR    = os.path.join(WORKSPACE, "FLARE_ARCHIVE")
# Findings survive here even if chat delivery fails. Your dispatcher model can
# read and relay this file when you report that the flare bot went silent.
LAST_FINDINGS  = os.path.join(WORKSPACE, "state", "LAST_FINDINGS.md")

PIDFILE        = "/tmp/flare-agent.pid"
LOGFILE        = "/tmp/flare-agent.log"
WATCHER_LABEL  = "local.flare-watcher"          # launchd label / systemd unit stem

# Claude Code's macOS auto-updater rotates its install dir under here.
CLAUDE_BASE    = os.path.expanduser("~/Library/Application Support/Claude/claude-code")

# ---------------------------------------------------------------------------
# Agent adapters
#
# Each adapter maps the generic invocation onto one CLI's flags:
#   args        list of arguments; "{model}" and "{prompt}" are substituted
#   budget_flag per-invocation spend cap flag, or None if unsupported
#   token_env   env var the CLI reads its auth token from, or None if the
#               CLI manages its own auth (login flow, config file)
#
# Honesty note: claude is the reference implementation and the only adapter
# that has handled real incidents. The others are best-effort flag mappings.
# Test yours end to end before you depend on it. See README.
# ---------------------------------------------------------------------------

ADAPTERS = {
    "claude": {
        "bin": "claude",
        # Prompt arrives on stdin, not argv. argv is visible to every local
        # user via ps, and the prompt names your briefing file paths.
        "args": ["--dangerously-skip-permissions", "--model", "{model}", "{budget}", "-p"],
        "prompt_stdin": True,
        "budget_flag": "--max-budget-usd",
        "token_env": "CLAUDE_CODE_OAUTH_TOKEN",
    },
    "codex": {
        "bin": "codex",
        "args": ["exec", "--full-auto", "--model", "{model}", "{prompt}"],
        "budget_flag": None,
        "token_env": "OPENAI_API_KEY",
    },
    "gemini": {
        "bin": "gemini",
        "args": ["--yolo", "-m", "{model}", "-p", "{prompt}"],
        "budget_flag": None,
        "token_env": "GEMINI_API_KEY",
    },
    "opencode": {
        "bin": "opencode",
        "args": ["run", "--model", "{model}", "{prompt}"],
        "budget_flag": None,
        "token_env": None,   # opencode manages auth via its own config
    },
    "goose": {
        "bin": "goose",
        "args": ["run", "-t", "{prompt}"],   # model comes from goose's own config
        "budget_flag": None,
        "token_env": None,
    },
}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
    print(f"[flare {time.strftime('%H:%M:%S')}] {msg}")


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

def resolve_secret(env_name, cmd, path):
    """Env var first, then command output, then file. Empty string if none."""
    val = os.environ.get(env_name, "").strip()
    if val:
        return val
    if cmd:
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True,
                                 text=True, timeout=15).stdout.strip()
            if out:
                return out.splitlines()[0].strip()
        except Exception as e:
            log(f"secret command for {env_name} failed: {e}")
    if path:
        try:
            with open(os.path.expanduser(path)) as f:
                return f.readline().strip()
        except Exception as e:
            log(f"secret file for {env_name} unreadable: {e}")
    return ""


BOT_TOKEN   = resolve_secret(BOT_TOKEN_ENV, BOT_TOKEN_CMD, BOT_TOKEN_FILE)
AGENT_TOKEN = resolve_secret(AGENT_TOKEN_ENV, AGENT_TOKEN_CMD, AGENT_TOKEN_FILE)


# ---------------------------------------------------------------------------
# PID lock
# ---------------------------------------------------------------------------

def acquire_lock():
    if os.path.exists(PIDFILE):
        try:
            with open(PIDFILE) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            log(f"Another handler already running (PID {old_pid}), exiting.")
            sys.exit(0)
        except (ValueError, ProcessLookupError):
            pass
    with open(PIDFILE, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    try:
        os.unlink(PIDFILE)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Notifiers
#
# Telegram is the reference implementation and the only two-way backend:
# it can both send and receive, so the go/no-go loop works over it.
# ntfy is send-only: you get paged and you get findings, but the handler
# cannot hear replies, so pair it with AUTO_GO_SECONDS or run report-only.
# webhook is a marked seam, not an implementation.
# ---------------------------------------------------------------------------

def notify_send(text, retries=0):
    """Send to you. Returns True on success.

    retries=0 for chatty low-stakes messages (heartbeats); retries>0 with
    backoff for must-arrive messages (findings, fix results). A real outage
    once lost its findings to a single dropped send during a network blip.
    """
    if NOTIFIER == "telegram":
        return _tg_send(text, retries)
    if NOTIFIER == "ntfy":
        return _ntfy_send(text, retries)
    log(f"notify_send: no backend for NOTIFIER={NOTIFIER!r}")
    return False


def notify_poll(offset, timeout=10):
    """Return (messages, new_offset). Send-only backends have nothing to
    receive, but still sleep the poll interval so callers' loops keep the
    same cadence they'd have with a long-poll instead of spinning."""
    if NOTIFIER == "telegram":
        return _tg_poll(offset, timeout)
    time.sleep(timeout)
    return [], offset


def notifier_is_two_way():
    return NOTIFIER == "telegram"


# ---- Telegram ----

def _tg_send(text, retries=0):
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": ALLOWED_USER, "text": text}).encode()
    attempt = 0
    while True:
        try:
            urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT)
            log(f"notify_send OK ({len(text)} chars)")
            return True
        except Exception as e:
            log(f"notify_send FAILED (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt >= retries:
                return False
            attempt += 1
            time.sleep(min(3 * attempt, 10))


def _tg_poll_raw(offset, timeout=10):
    """Long-poll for replies. Only messages from ALLOWED_USER are returned.
    The allowlist is what stops a leaked bot token from becoming a shell.
    Returns ([(text, message_id, chat_id)], new_offset)."""
    url = (
        f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
        f"?offset={offset}&timeout={timeout}"
    )
    try:
        with urllib.request.urlopen(url, timeout=timeout + HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
        updates = data.get("result", [])
        new_off = offset
        msgs    = []
        for u in updates:
            new_off = max(new_off, u["update_id"] + 1)
            msg = u.get("message", {})
            if msg.get("from", {}).get("id") != ALLOWED_USER:
                continue
            text = msg.get("text", "").strip()
            if text:
                msgs.append((text, msg.get("message_id"), msg.get("chat", {}).get("id")))
        return msgs, new_off
    except Exception as e:
        log(f"notify_poll FAILED: {e}")
        return [], offset


def _tg_poll(offset, timeout=10):
    msgs, new_off = _tg_poll_raw(offset, timeout)
    return [m[0] for m in msgs], new_off


def _tg_delete(chat_id, message_id):
    """Delete a message from the chat (used to scrub a texted passphrase).
    Best effort; Telegram only allows deletion within a time window."""
    if not (chat_id and message_id):
        return False
    url  = f"https://api.telegram.org/bot{BOT_TOKEN}/deleteMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "message_id": message_id}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read()).get("ok", False)
    except Exception as e:
        log(f"tg_delete FAILED: {e}")
        return False


# ---- ntfy (send-only) ----

def _ntfy_send(text, retries=0):
    if not NTFY_URL:
        log("ntfy: NTFY_URL not configured")
        return False
    attempt = 0
    while True:
        try:
            req = urllib.request.Request(NTFY_URL, data=text.encode("utf-8"))
            urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)
            log(f"notify_send OK via ntfy ({len(text)} chars)")
            return True
        except Exception as e:
            log(f"ntfy send FAILED (attempt {attempt + 1}/{retries + 1}): {e}")
            if attempt >= retries:
                return False
            attempt += 1
            time.sleep(min(3 * attempt, 10))


# ---- webhook (seam) ----
# To add a backend: implement send (and poll, if your channel can receive),
# then wire it into notify_send / notify_poll / notifier_is_two_way above.
# That is the entire interface.


# ---------------------------------------------------------------------------
# Reply parsing
#
# Order matters and it burned me once already in review: the PIN must be
# stripped BEFORE the stand-down check, or "stand down 4417" misses the
# keyword list and becomes a fix instruction that says "stand down".
# ---------------------------------------------------------------------------

STAND_DOWN_WORDS = ("stand down", "no go", "close", "done", "resolved")


def strip_pin(text):
    """Remove GO_PIN from a reply. Returns (clean_text, had_pin)."""
    if GO_PIN and GO_PIN in text:
        return text.replace(GO_PIN, "").strip(), True
    return text.strip(), False


def is_stand_down(text):
    """Keyword check, tolerant of a trailing period and stray whitespace."""
    return text.lower().strip().rstrip(".!").strip() in STAND_DOWN_WORDS


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------

def resolve_agent_bin():
    """Find the agent binary. Newest installed version wins.

    Agent CLIs auto-update and move their install path when they do.
    Hardcoding a path means the rescue system quietly breaks on some
    ordinary Tuesday and you find out during the emergency.
    """
    if AGENT_BIN:
        p = os.path.expanduser(AGENT_BIN)
        return p if os.access(p, os.X_OK) else None

    adapter = ADAPTERS[AGENT]

    # Claude Code on macOS keeps versioned install dirs; pick the newest.
    if AGENT == "claude" and os.path.isdir(CLAUDE_BASE):
        def vkey(v):
            try:
                return [int(x) for x in v.split(".")]
            except ValueError:
                return [-1]
        versions = sorted(
            (d for d in os.listdir(CLAUDE_BASE)
             if os.path.isdir(os.path.join(CLAUDE_BASE, d))),
            key=vkey,
            reverse=True,
        )
        for v in versions:
            cand = os.path.join(CLAUDE_BASE, v, "claude.app/Contents/MacOS/claude")
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand

    try:
        which = subprocess.run(["/usr/bin/which", adapter["bin"]],
                               capture_output=True, text=True, timeout=5).stdout.strip()
        if which and os.access(which, os.X_OK):
            return which
    except Exception:
        pass
    return None


def get_live_claude_token():
    """Last-resort fallback: scrape a live token from a running process env.

    Stored token is always preferred. Scraping live first was a historical
    bug here: captured tokens are often desktop session tokens that 401 on
    headless invocations. Live is only a fallback when nothing is stored.
    """
    try:
        result = subprocess.run(["ps", "eww", "-A"], capture_output=True, text=True, timeout=10)
        for line in result.stdout.split("\n"):
            if "CLAUDE_CODE_OAUTH_TOKEN=" in line:
                for part in line.split():
                    if part.startswith("CLAUDE_CODE_OAUTH_TOKEN="):
                        return part.split("=", 1)[1]
    except Exception as e:
        log(f"get_live_claude_token failed: {e}")
    return None


def build_agent_cmd(binary, prompt, budget_usd):
    adapter = ADAPTERS[AGENT]
    cmd = [binary]
    for a in adapter["args"]:
        if a == "{budget}":
            if adapter["budget_flag"] and budget_usd:
                cmd += [adapter["budget_flag"], str(budget_usd)]
        elif a == "{prompt}":
            if adapter["budget_flag"] and budget_usd:
                cmd += [adapter["budget_flag"], str(budget_usd)]
            cmd.append(prompt)
        else:
            cmd.append(a.replace("{model}", AGENT_MODEL))
    return cmd


def make_prompt_stdin(prompt):
    """Prompt as an already-unlinked open temp file, usable as stdin.
    Unlinking immediately means there is nothing to clean up on any exit
    path; the file vanishes when the descriptor closes."""
    fd, path = tempfile.mkstemp(prefix="flare-prompt-")
    os.write(fd, prompt.encode("utf-8"))
    os.lseek(fd, 0, os.SEEK_SET)
    os.unlink(path)
    return fd


# The agent currently running, if any. Tracked so the SIGTERM handler can
# take it down with us: the agent lives in its own session, so a service
# stop aimed at the handler would otherwise orphan a credentialed agent
# while the decrypted briefing vanishes out from under it.
CURRENT_AGENT = None


def kill_process_group(proc):
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    log(f"killing process group {pgid}")
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(15):
        if proc.poll() is not None:
            return
        time.sleep(0.2)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log("WARN: process group still alive after SIGKILL")


def run_agent(prompt, offset, budget_usd, phase_label, busy_msg):
    """
    Run the agent with stdout/stderr redirected to a temp file (no PIPE
    deadlock). Polls the notifier concurrently for stand-down.
    Returns (output_text, new_offset, stand_down_bool).
    """
    global CURRENT_AGENT
    env     = os.environ.copy()
    adapter = ADAPTERS[AGENT]

    if adapter["token_env"]:
        token = AGENT_TOKEN
        source = "stored"
        if not token and AGENT == "claude":
            token  = get_live_claude_token()
            source = "live"
        if token:
            env[adapter["token_env"]] = token
        else:
            source = "ambient"   # whatever the CLI's own auth state provides
        log(f"{phase_label}: agent auth source: {source}")

    binary = resolve_agent_bin()
    if not binary:
        log(f"{phase_label}: FATAL, no {AGENT} binary found")
        notify_send(f"⚠️ flare handler cannot find the {AGENT} binary. "
                    f"Run it once by hand to reinstall, then retry the flare.")
        return "NO_AGENT_BINARY", offset, False
    log(f"{phase_label}: using {binary}")

    out_fd, out_path = tempfile.mkstemp(prefix="flare-agent-", suffix=".log")
    os.close(out_fd)
    out_file = open(out_path, "wb")

    if adapter.get("prompt_stdin"):
        stdin_fd = make_prompt_stdin(prompt)
    else:
        stdin_fd = subprocess.DEVNULL

    proc = subprocess.Popen(
        build_agent_cmd(binary, prompt, budget_usd),
        cwd=WORKSPACE,
        stdin=stdin_fd,
        stdout=out_file,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    if stdin_fd != subprocess.DEVNULL:
        os.close(stdin_fd)
    CURRENT_AGENT = proc
    log(f"{phase_label}: agent pid={proc.pid} pgid={os.getpgid(proc.pid)}")

    start          = time.time()
    last_heartbeat = start
    try:
        while True:
            rc = proc.poll()
            if rc is not None:
                out_file.close()
                with open(out_path, "rb") as f:
                    output = f.read().decode("utf-8", errors="replace").strip()
                os.unlink(out_path)
                log(f"{phase_label}: agent exited rc={rc} output_len={len(output)}")
                if rc != 0:
                    log(f"{phase_label}: agent STDERR/STDOUT (first 600):\n{output[:600]}")
                low = output.lower()
                if rc != 0 and len(output) < 800 and any(
                    s in low for s in ("login", "authenticate", "sign in", "unauthorized", "401")
                ):
                    notify_send(f"⚠️ {AGENT} auth expired. Re-authenticate it on the "
                                f"responder machine, then retry the flare.")
                return output, offset, False

            elapsed = time.time() - start
            if elapsed > AGENT_TIMEOUT:
                log(f"{phase_label}: TIMEOUT after {int(elapsed)}s")
                kill_process_group(proc)
                out_file.close()
                try:
                    os.unlink(out_path)
                except FileNotFoundError:
                    pass
                notify_send(f"⏱️ Agent timed out ({AGENT_TIMEOUT // 60} min). Flare still active.")
                return "TIMEOUT", offset, False

            if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL:
                last_heartbeat = time.time()
                notify_send(f"💓 Still working ({int(elapsed)}s). 'stand down' to abort.")

            replies, offset = notify_poll(offset, timeout=10)
            for text in replies:
                clean, _ = strip_pin(text)
                if is_stand_down(clean):
                    log(f"{phase_label}: stand-down received")
                    kill_process_group(proc)
                    out_file.close()
                    try:
                        os.unlink(out_path)
                    except FileNotFoundError:
                        pass
                    return None, offset, True
                else:
                    notify_send(busy_msg)
    except Exception as e:
        log(f"{phase_label}: EXCEPTION {e!r}")
        kill_process_group(proc)
        out_file.close()
        try:
            os.unlink(out_path)
        except FileNotFoundError:
            pass
        notify_send(f"⚠️ Handler exception in {phase_label}: {e}")
        return f"HANDLER_EXCEPTION: {e}", offset, False
    finally:
        CURRENT_AGENT = None


# ---------------------------------------------------------------------------
# Flare lifecycle
# ---------------------------------------------------------------------------

def archive_flare(note=None):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    ts   = time.strftime("%Y-%m-%d-%H%M%S")
    dest = os.path.join(ARCHIVE_DIR, f"FLARE-{ts}.md")
    if os.path.exists(FLARE_FILE):
        if note:
            with open(FLARE_FILE, "a") as f:
                f.write(f"\n\n---\nClosed: {note}\nTimestamp: {ts}\n")
        os.rename(FLARE_FILE, dest)
    if os.path.exists(FLARE_RESPONSE):
        os.remove(FLARE_RESPONSE)
    log(f"archived to {dest}")
    return dest


def persist_findings(text, header, fresh=False):
    """Findings survive on disk regardless of chat delivery.
    fresh=True starts a new file (new flare); fresh=False appends (fix results)."""
    try:
        os.makedirs(os.path.dirname(LAST_FINDINGS), exist_ok=True)
        with open(LAST_FINDINGS, "w" if fresh else "a") as f:
            f.write(f"# {header}\nSaved: {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n\n{text}\n\n---\n\n")
        log(f"persisted '{header}' to {LAST_FINDINGS}")
    except Exception as e:
        log(f"persist_findings FAILED: {e}")


def read_response_file():
    if os.path.exists(FLARE_RESPONSE):
        with open(FLARE_RESPONSE) as f:
            content = f.read().strip()
        os.remove(FLARE_RESPONSE)
        return content
    return None


# ---------------------------------------------------------------------------
# Encrypted briefing
#
# Nothing usable at rest: briefing files ending in ".enc" are AES-encrypted
# (openssl enc -aes-256-cbc -pbkdf2). At incident time you text the
# passphrase to the bot, the handler decrypts to 0600 temp copies for the
# duration of the incident, deletes your passphrase message from the chat,
# and shreds the copies on exit. Rotation is one `rekey` command, so a
# passphrase that may have transited a chat server is cheap to retire.
# ---------------------------------------------------------------------------

DECRYPTED_TEMPS = []


def openssl_run(args, passphrase, timeout=60):
    """Run openssl with the passphrase passed via a per-call env var, so it
    never appears in argv or on disk. Iteration count is pinned well above
    openssl's weak default; encrypt and decrypt must agree on it, so do not
    change it once you have encrypted files in service."""
    env = os.environ.copy()
    env["FLARE_PASSPHRASE"] = passphrase
    return subprocess.run(["openssl"] + args + ["-iter", "600000",
                                                "-pass", "env:FLARE_PASSPHRASE"],
                          capture_output=True, text=True, timeout=timeout, env=env)


def request_passphrase(offset):
    """Ask for the briefing passphrase over chat and wait for it.
    Returns (passphrase, offset, stand_down). Never logs the passphrase."""
    notify_send("🔐 The briefing is encrypted. Reply with the passphrase to "
                "unlock it for this incident. ('stand down' to abort.)")
    deadline = time.time() + PASSPHRASE_TIMEOUT
    while time.time() < deadline:
        msgs, offset = _tg_poll_raw(offset, timeout=10)
        for text, mid, cid in msgs:
            if text.lower() in ("stand down", "no go", "close", "done"):
                return None, offset, True
            deleted = _tg_delete(cid, mid)
            log("passphrase received" + (" (chat message deleted)" if deleted else ""))
            if not deleted:
                notify_send("🔐 Got it. I could not delete your message; "
                            "delete it from the chat yourself.")
            return text, offset, False
    notify_send(f"⏱️ No passphrase in {PASSPHRASE_TIMEOUT // 60} min. "
                f"Continuing without the encrypted briefing files.")
    return None, offset, False


def decrypt_briefing(passphrase):
    """Decrypt every .enc briefing file to a 0600 temp copy.
    Returns the effective briefing list, or None if decryption failed
    (wrong passphrase or corrupted file)."""
    effective = []
    for path in BRIEFING_FILES:
        if not path.endswith(".enc"):
            effective.append(path)
            continue
        fd, tmp = tempfile.mkstemp(prefix="flare-briefing-", suffix=".md")
        os.close(fd)
        try:
            res = openssl_run(["enc", "-d", "-aes-256-cbc", "-pbkdf2",
                               "-in", path, "-out", tmp], passphrase)
        except Exception as e:
            log(f"decrypt errored for {path}: {e}")
            res = None
        if res is not None and res.returncode == 0:
            DECRYPTED_TEMPS.append(tmp)
            effective.append(tmp)
            log(f"decrypted {os.path.basename(path)}")
        else:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            if res is not None:
                log(f"decrypt FAILED for {path} (rc={res.returncode})")
            for t in list(DECRYPTED_TEMPS):
                try:
                    os.unlink(t)
                except FileNotFoundError:
                    pass
                DECRYPTED_TEMPS.remove(t)
            return None
    return effective


def unlock_briefing(offset):
    """Full passphrase flow. Returns (effective_briefing, offset, stand_down)."""
    if not any(p.endswith(".enc") for p in BRIEFING_FILES):
        return BRIEFING_FILES, offset, False
    without_enc = [p for p in BRIEFING_FILES if not p.endswith(".enc")]
    if not notifier_is_two_way():
        log("encrypted briefing but no reply channel; continuing without it")
        return without_enc, offset, False
    for attempt in range(3):
        passphrase, offset, stand_down = request_passphrase(offset)
        if stand_down:
            return None, offset, True
        if passphrase is None:                      # timed out
            return without_enc, offset, False
        effective = decrypt_briefing(passphrase)
        if effective is not None:
            notify_send("🔓 Briefing unlocked.")
            return effective, offset, False
        notify_send("❌ Wrong passphrase (or corrupted file). Try again.")
    notify_send("⚠️ Three failed attempts. Continuing without the encrypted briefing files.")
    return without_enc, offset, False


def cleanup_decrypted():
    for p in DECRYPTED_TEMPS:
        try:
            os.unlink(p)
            log(f"removed decrypted briefing copy {p}")
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------------
# Prompts
#
# Terse output rules are deliberate. Findings are read on a phone, possibly
# over bad hotel wifi. The full detail stays in the agent's own session; you
# only need a decision-grade summary.
# ---------------------------------------------------------------------------

def briefing_list(files):
    return "\n".join(f"- {p}" for p in files) or "- (none configured)"


INVESTIGATE_PROMPT = """You are the flare investigation agent for someone's personal infrastructure.

FLARE REPORT:
{flare_context}

Briefing files (read these first, they tell you what exists and how to reach it):
{briefing}

YOUR JOB - diagnose only, 4 steps:

1. Read the briefing files to find the affected host and how to access it.
2. Connect to the relevant host(s) and run read-only status checks (service up? logs? state?).
3. Write your findings to {response_file} in EXACTLY this format:

STATUS: <one line - what's broken, e.g. "Photo service down - Docker engine not running on host">
ROOT CAUSE: <one line>
EVIDENCE: <the single most decisive line of output - one line, not a dump>
PROPOSED FIX: <the exact command(s), terse>
VERIFY: <one command that confirms it worked>

AWAITING GO/NO-GO

4. Stop. Do not run any fix commands. Write the file and exit.

OUTPUT RULES - THIS IS READ ON A PHONE:
- Be precise and terse. Overview, not details. NO raw log dumps, NO multi-line command output.
- One line per field above. Hard cap: keep the ENTIRE file under 900 characters.
- No preamble, no "I found that...", no closing remarks. Just the fields.
- The full detail is in your own session; the operator only needs the decision-grade summary.

CONSTRAINTS:
- Read-only access only: status checks, log reads, pgrep, ps, df, ping
- No restart, start, stop, reload, kill, or any state-changing command
- Respect any rules of engagement stated in the flare report itself
- Be efficient. Diagnose, don't over-investigate. After writing the file: stop immediately.
"""

FIX_PROMPT = """You are authorized to fix an infrastructure issue.

FLARE CONTEXT:
{flare_context}

INVESTIGATION FINDINGS:
{diagnostic}

OPERATOR'S INSTRUCTION: {instruction}

Briefing files:
{briefing}

YOUR JOB - 3 steps only:

1. Run the fix commands from the investigation findings.
2. Run one verification command to confirm the service is back up.
3. Write {response_file} in EXACTLY this format and stop:

RESOLVED: <one line - what you ran and the outcome>
VERIFY: <the verification command and its one-line result>

- OR -

UNRESOLVED: <one line - what failed>
NEXT: <what the operator must do manually, terse>

OUTPUT RULES - THIS IS READ ON A PHONE:
- Be precise and terse. Overview, not details. NO raw command/log dumps.
- Hard cap: keep the ENTIRE file under 700 characters. No preamble, no closing remarks.

After writing the file: stop. Do not investigate further. Do not check other things.
"""


# ---------------------------------------------------------------------------
# Handle: the main flow
# ---------------------------------------------------------------------------

def handle():
    if not os.path.exists(FLARE_FILE):
        log("FLARE.md not found, exiting")
        sys.exit(0)

    with open(FLARE_FILE) as f:
        flare_context = f.read().strip()

    log(f"flare triggered:\n{flare_context}")
    notify_send(f"🚨 FLARE ACTIVE\n\n{flare_context}\n\nInvestigating now...")

    _, offset = notify_poll(0, timeout=1)

    briefing, offset, stand_down = unlock_briefing(offset)
    if stand_down:
        notify_send("✅ Stand down received. Flare archived.")
        archive_flare(note="Stand down before investigation")
        return

    # ---------- Investigation ----------
    investigate_prompt = INVESTIGATE_PROMPT.format(
        flare_context = flare_context,
        briefing      = briefing_list(briefing),
        response_file = FLARE_RESPONSE,
    )

    response, offset, stand_down = run_agent(
        prompt       = investigate_prompt,
        offset       = offset,
        budget_usd   = INVESTIGATE_BUDGET_USD,
        phase_label  = "investigating",
        busy_msg     = "⏳ Still investigating. I can't take instructions mid-run, "
                       "send that again when findings arrive. 'stand down' to abort.",
    )

    if stand_down:
        notify_send("✅ Stand down received. Flare archived.")
        archive_flare(note="Stand down during investigation")
        return

    diagnostic = read_response_file() or response or "(no findings)"
    log(f"investigation complete, findings length={len(diagnostic)}")

    persist_findings(f"FLARE:\n{flare_context}\n\nFINDINGS:\n{diagnostic}",
                     "FLARE INVESTIGATION FINDINGS", fresh=True)

    findings_msg = f"📋 Findings:\n\n{diagnostic[:3800]}"
    if AUTO_GO_SECONDS:
        gonogo_msg = (
            "💬 Reply 'go' to fix, 'stand down' to close, or give alternate instructions.\n"
            f"⏱️ Auto-go in {AUTO_GO_SECONDS // 60} min if no reply at all. Any message resets the timer."
        )
    else:
        gonogo_msg = "💬 Reply 'go' to fix, 'stand down' to close, or give alternate instructions."
    if GO_PIN:
        gonogo_msg += "\n🔑 Include your PIN with any instruction."

    findings_delivered = notify_send(findings_msg, retries=3)

    if diagnostic.upper().startswith("RESOLVED:"):
        archive_flare(note="Self-resolved during investigation")
        return

    # Send-only notifier: there is no reply channel, so there is no go/no-go
    # loop to run. With auto-go enabled we wait out the timer and fix; without
    # it this is a report-only deployment and we archive here.
    if not notifier_is_two_way():
        if not AUTO_GO_SECONDS:
            notify_send("📁 Report-only mode (send-only notifier, auto-go off). Flare archived.")
            archive_flare(note="Report-only: findings delivered, no reply channel")
            return
        notify_send(f"⏱️ Auto-go in {AUTO_GO_SECONDS // 60} min (no reply channel on this notifier).")
    else:
        if findings_delivered:
            notify_send(gonogo_msg, retries=1)
        else:
            log("findings NOT delivered, will keep re-sending from go/no-go loop")

    # ---------- Go/no-go loop ----------
    gonogo_deadline = time.time() + AUTO_GO_SECONDS if AUTO_GO_SECONDS else None
    auto_fired      = False
    last_resend     = time.time()

    while True:
        instruction = None
        auto        = False

        if notifier_is_two_way() and not findings_delivered and time.time() - last_resend >= 60:
            last_resend = time.time()
            log("re-sending undelivered findings")
            findings_delivered = notify_send(findings_msg)
            if findings_delivered:
                notify_send(gonogo_msg)

        if gonogo_deadline and not auto_fired and time.time() > gonogo_deadline:
            auto_fired  = True
            instruction = f"AUTO-GO: no response in {AUTO_GO_SECONDS // 60} min - execute the proposed fix"
            auto        = True
            notify_send(f"⏱️ No response in {AUTO_GO_SECONDS // 60} min. Auto-authorizing fix.", retries=1)
        else:
            replies, offset = notify_poll(offset, timeout=10)
            for text in replies:
                if gonogo_deadline:
                    gonogo_deadline = time.time() + AUTO_GO_SECONDS
                # PIN comes off before anything else, so "stand down 4417"
                # is a stand-down, not a fix instruction reading "stand down".
                text, had_pin = strip_pin(text)
                if is_stand_down(text):
                    # Stand-down never needs the PIN. Stopping must be easy.
                    notify_send("✅ Stand down received. Flare archived.")
                    archive_flare(note=f"Stand down: '{text}'")
                    return
                if GO_PIN and not had_pin:
                    log("instruction without PIN ignored")
                    notify_send("🔑 Ignored: include your PIN with any instruction.")
                    continue
                if had_pin and not text:
                    text = "go"          # a bare PIN means "go"
                if LOG_REPLIES:
                    log(f"operator replied '{text[:60]}'")
                else:
                    log(f"operator replied ({len(text)} chars, reply logging off)")
                if not findings_delivered:
                    # Delivery is unconfirmed, so the operator probably has
                    # not seen the findings, and their message is "where are
                    # you?", not an order. A real "Hello?" once launched the
                    # fix phase this way. Even a bare "go" is not trusted
                    # here; "go anyway" is the explicit override for when the
                    # findings reached you some other way (relayed from disk
                    # by the dispatcher).
                    if text.lower().strip().rstrip(".!") in ("go anyway", "force go"):
                        # Latch: the operator has asserted they have the
                        # findings, so stop re-sending and stop gatekeeping
                        # every later instruction in this incident.
                        findings_delivered = True
                        instruction = "go"
                        break
                    log("operator messaged before findings delivery, re-sending findings instead")
                    findings_delivered = notify_send(findings_msg, retries=2)
                    if findings_delivered:
                        notify_send(gonogo_msg)
                    else:
                        notify_send("⚠️ Findings delivery unconfirmed. If you already have "
                                    "them (e.g. relayed from disk), reply 'go anyway'.")
                    continue
                instruction = text
                break

            if not instruction:
                continue

        notify_send(f"⚙️ On it: {instruction}")

        fix_prompt = FIX_PROMPT.format(
            flare_context = flare_context,
            diagnostic    = diagnostic,
            instruction   = instruction,
            briefing      = briefing_list(briefing),
            response_file = FLARE_RESPONSE,
        )

        result, offset, stand_down = run_agent(
            prompt      = fix_prompt,
            offset      = offset,
            budget_usd  = FIX_BUDGET_USD,
            phase_label = "fixing",
            busy_msg    = "⏳ Agent is fixing. I can't take new instructions mid-run, "
                          "send that again when it reports back. 'stand down' to abort.",
        )

        if stand_down:
            notify_send("✅ Stand down received. Flare archived.")
            archive_flare(note="Stand down during fix")
            return

        file_result = read_response_file()
        if file_result:
            result = file_result

        if result and result not in ("TIMEOUT",) and not (result or "").startswith("HANDLER_EXCEPTION"):
            persist_findings(f"INSTRUCTION: {instruction}\n\nRESULT:\n{result}", "FLARE FIX RESULT")
            notify_send(f"📋 {result[:3800]}", retries=3)

        if result and "RESOLVED:" in (result or "").upper():
            notify_send("✅ Resolved. Flare archived.")
            archive_flare(note="Resolved")
            return

        if not notifier_is_two_way():
            # No reply channel to continue the conversation on. One auto-go
            # attempt is all a send-only deployment gets.
            notify_send("📁 Auto-go attempt finished (see result above). Flare archived.")
            archive_flare(note="Auto-go attempt on send-only notifier")
            return

        if not auto:
            notify_send("💬 Reply to continue, or 'stand down' to close.")
        else:
            notify_send("⚠️ Auto-go did not resolve. Reply to direct or 'stand down' to close.")
            gonogo_deadline = time.time() + AUTO_GO_SECONDS
            auto_fired      = False


# ---------------------------------------------------------------------------
# Diag: read-only pipeline health report
#
# Safe for a small local model (your dispatcher) to run and relay verbatim.
# Looks ONLY at the flare pipeline on this machine. Never touches your
# infrastructure, never reads briefing files, never prints tokens, changes
# nothing. Output is sized to fit in a chat message.
# ---------------------------------------------------------------------------

def diag():
    import re

    problems = []
    lines    = []

    def add(label, value, problem=False):
        lines.append(f"{label}: {value}")
        if problem:
            problems.append(f"{label}: {value}")

    def age_str(path):
        secs = int(time.time() - os.path.getmtime(path))
        if secs < 120:
            return f"{secs}s ago"
        if secs < 7200:
            return f"{secs // 60}m ago"
        if secs < 172800:
            return f"{secs // 3600}h ago"
        return f"{secs // 86400}d ago"

    # --- 1. Active flare? ---
    if os.path.exists(FLARE_FILE):
        add("Active flare", f"YES, FLARE.md written {age_str(FLARE_FILE)}")
    else:
        add("Active flare", "no (FLARE.md absent, idle or archived)")

    # --- 2. Handler process ---
    handler_running = False
    try:
        out = subprocess.run(["pgrep", "-fl", os.path.basename(__file__)],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        own = str(os.getpid())
        procs = [l for l in out.splitlines()
                 if not l.startswith(own + " ") and " diag" not in l and " restart" not in l]
        handler_running = bool(procs)
    except Exception:
        pass
    if os.path.exists(FLARE_FILE) and not handler_running:
        add("Handler process", "NOT RUNNING despite active flare", problem=True)
    else:
        add("Handler process", "running" if handler_running else "not running (normal when idle)")

    # --- 3. Watcher ---
    if sys.platform == "darwin":
        try:
            res = subprocess.run(["launchctl", "list", WATCHER_LABEL],
                                 capture_output=True, text=True, timeout=10)
            if res.returncode == 0:
                m = re.search(r'"LastExitStatus"\s*=\s*(\d+)', res.stdout)
                last_exit = m.group(1) if m else "?"
                add("Watcher (launchd)", f"loaded, last exit {last_exit}",
                    problem=(last_exit not in ("0", "?")))
            else:
                add("Watcher (launchd)", "NOT LOADED, FLARE.md will not trigger anything", problem=True)
        except Exception as e:
            add("Watcher (launchd)", f"check failed: {e}", problem=True)
    else:
        try:
            res = subprocess.run(["systemctl", "--user", "is-active", "flare-watcher.path"],
                                 capture_output=True, text=True, timeout=10)
            state = res.stdout.strip() or "unknown"
            add("Watcher (systemd)", state, problem=(state != "active"))
        except Exception as e:
            add("Watcher (systemd)", f"check failed: {e}", problem=True)

    # --- 4. Agent binary ---
    binary = resolve_agent_bin()
    add(f"Agent binary ({AGENT})",
        binary if binary else "NOT FOUND, handler cannot run the agent",
        problem=not binary)

    # --- 4b. openssl, if the briefing is encrypted ---
    if any(p.endswith(".enc") for p in BRIEFING_FILES):
        try:
            subprocess.run(["openssl", "version"], capture_output=True, timeout=5)
            add("openssl", "found (encrypted briefing configured)")
        except Exception:
            add("openssl", "NOT FOUND, encrypted briefing cannot be unlocked", problem=True)

    # --- 5. Notifier reachability (no token used) ---
    probe = "https://api.telegram.org" if NOTIFIER == "telegram" else NTFY_URL
    if probe:
        try:
            urllib.request.urlopen(probe, timeout=8)
            add("Notifier API", "reachable")
        except Exception as e:
            add("Notifier API", f"UNREACHABLE ({type(e).__name__}), updates will not arrive",
                problem=True)

    # --- 6. Handler log ---
    log_tail = []
    if os.path.exists(LOGFILE):
        with open(LOGFILE, errors="replace") as f:
            log_lines = f.read().splitlines()
        log_tail = log_lines[-12:]
        fails = sum("notify_send FAILED" in l for l in log_lines)
        exits = [l for l in log_lines if "agent exited" in l]
        add("Handler log", f"updated {age_str(LOGFILE)}, {len(log_lines)} lines, "
                           f"{fails} failed sends this run")
        if exits:
            add("Last agent run", exits[-1].split("] ")[-1])
        if fails:
            problems.append(f"{fails} notifier sends FAILED in last run, "
                            f"messages were lost or delayed")
    else:
        add("Handler log", "absent, handler has not run since last boot")

    # --- 7. Saved findings ---
    if os.path.exists(LAST_FINDINGS):
        add("Saved findings", f"{LAST_FINDINGS} (written {age_str(LAST_FINDINGS)}), "
                              f"read this file and relay it if findings never arrived")
    else:
        add("Saved findings", "none saved yet")

    # --- 8. Last archived flare ---
    try:
        arcs = sorted(f for f in os.listdir(ARCHIVE_DIR) if f.startswith("FLARE-"))
        if arcs:
            add("Last archived flare", arcs[-1])
    except Exception:
        pass

    # --- report ---
    print("FLARE PIPELINE DIAGNOSTIC,", time.strftime("%Y-%m-%d %H:%M:%S"))
    if problems:
        print(f"SUMMARY: {len(problems)} PROBLEM(S) FOUND")
        for p in problems:
            print(f"  ⚠️ {p}")
    else:
        print("SUMMARY: all flare pipeline checks pass")
    print()
    for l in lines:
        print(f"- {l}")
    if log_tail:
        print()
        print("Last handler log lines:")
        for l in log_tail:
            print(f"  {l}")


# ---------------------------------------------------------------------------
# Restart: re-fire the handler when it died mid-flare
#
# Safe for the dispatcher to run. Contains no credentials. Guard rails:
#   - refuses if there is no active flare (FLARE.md absent)
#   - refuses if a handler is already running (PID lock honored)
# ---------------------------------------------------------------------------

def restart():
    def handler_pid():
        if not os.path.exists(PIDFILE):
            return None
        try:
            pid = int(open(PIDFILE).read().strip())
            os.kill(pid, 0)
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            return None

    if not os.path.exists(FLARE_FILE):
        print("REFUSED: no active flare (FLARE.md does not exist).")
        print("To raise a flare, write FLARE.md; the watcher fires the handler automatically.")
        print("Only use restart when the watcher fired but the handler died or never started.")
        sys.exit(1)

    pid = handler_pid()
    if pid:
        print(f"REFUSED: handler already running (PID {pid}). Nothing to do.")
        print("If it is stuck, the operator must kill it themselves.")
        sys.exit(1)

    logf = open(LOGFILE, "a")
    proc = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__)],
        stdout=logf, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(3)
    if proc.poll() is None:
        print(f"OK: flare handler started (PID {proc.pid}). The agent is being spun up.")
        print("Watch your notifier for updates. Check progress with: flare_agent.py diag")
    else:
        print(f"FAILED: handler exited immediately (rc={proc.returncode}).")
        print(f"Run 'flare_agent.py diag' and relay the report. Log: {LOGFILE}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Encrypt / rekey: manage encrypted briefing files
# ---------------------------------------------------------------------------

def encrypt_cmd(path):
    import getpass
    if not os.path.isfile(path):
        print(f"No such file: {path}")
        sys.exit(1)
    if path.endswith(".enc"):
        print("That file is already encrypted.")
        sys.exit(1)
    p1 = getpass.getpass("Passphrase: ")
    p2 = getpass.getpass("Again: ")
    if not p1 or p1 != p2:
        print("Passphrases empty or mismatched. Nothing written.")
        sys.exit(1)
    out = path + ".enc"
    res = openssl_run(["enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                       "-in", path, "-out", out], p1)
    if res.returncode != 0:
        print(f"openssl failed: {res.stderr.strip()}")
        sys.exit(1)
    print(f"Wrote {out}")
    print("Next: point BRIEFING_FILES at the .enc path, raise a test flare to")
    print("confirm it decrypts, THEN delete the plaintext original yourself.")


def rekey_cmd(path):
    """Rotate the passphrase. Plaintext exists only briefly as a 0600 temp
    file on the same filesystem, then is removed."""
    import getpass
    if not os.path.isfile(path) or not path.endswith(".enc"):
        print(f"Expected an existing .enc file, got: {path}")
        sys.exit(1)
    old = getpass.getpass("Current passphrase: ")
    n1  = getpass.getpass("New passphrase: ")
    n2  = getpass.getpass("Again: ")
    if not n1 or n1 != n2:
        print("New passphrases empty or mismatched. Nothing changed.")
        sys.exit(1)
    fd, tmp = tempfile.mkstemp(prefix="flare-rekey-", dir=os.path.dirname(path) or ".")
    os.close(fd)
    keep_tmp = False
    try:
        res = openssl_run(["enc", "-d", "-aes-256-cbc", "-pbkdf2",
                           "-in", path, "-out", tmp], old)
        if res.returncode != 0:
            print("Decryption failed. Wrong current passphrase? Nothing changed.")
            sys.exit(1)
        res = openssl_run(["enc", "-aes-256-cbc", "-pbkdf2", "-salt",
                           "-in", tmp, "-out", path], n1)
        if res.returncode != 0:
            keep_tmp = True
            print(f"Re-encryption failed: {res.stderr.strip()}. The .enc file may be damaged; "
                  f"plaintext preserved at {tmp} so you can recover. Handle it carefully.")
            sys.exit(1)
    finally:
        if not keep_tmp:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
    print(f"Rekeyed {path}. Old passphrase is dead.")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def check_config():
    fatal = []
    if NOTIFIER not in ("telegram", "ntfy", "webhook"):
        fatal.append(f"unknown NOTIFIER {NOTIFIER!r}")
    if NOTIFIER == "webhook":
        fatal.append("webhook notifier is not implemented yet; the seam is marked in the "
                     "Notifiers section, or use telegram / ntfy")
    if NOTIFIER == "telegram" and not BOT_TOKEN:
        fatal.append(f"no bot token: set {BOT_TOKEN_ENV}, or configure BOT_TOKEN_CMD / BOT_TOKEN_FILE")
    if NOTIFIER == "telegram" and not ALLOWED_USER:
        fatal.append("ALLOWED_USER is unset; the reply allowlist is not optional")
    if NOTIFIER == "ntfy" and not NTFY_URL:
        fatal.append("NTFY_URL is unset")
    if AGENT not in ADAPTERS:
        fatal.append(f"unknown AGENT {AGENT!r}; choices: {', '.join(ADAPTERS)}")
    if any(p.endswith(".enc") for p in BRIEFING_FILES):
        try:
            subprocess.run(["openssl", "version"], capture_output=True, timeout=5)
        except Exception:
            fatal.append("encrypted briefing configured but openssl is not on PATH")
        if AUTO_GO_SECONDS:
            log("NOTE: encrypted briefing + auto-go: nothing useful can happen until "
                "a passphrase arrives, so a flare while you are unreachable stays "
                "diagnostic-only. That is the tradeoff of nothing-usable-at-rest.")
    for msg in fatal:
        log(f"CONFIG ERROR: {msg}")
    if fatal:
        sys.exit(1)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "handle"
    if mode == "diag":
        diag()
    elif mode == "restart":
        restart()
    elif mode in ("encrypt", "rekey"):
        if len(sys.argv) < 3:
            print(f"usage: flare_agent.py {mode} /path/to/file")
            sys.exit(2)
        (encrypt_cmd if mode == "encrypt" else rekey_cmd)(sys.argv[2])
    elif mode == "handle":
        check_config()
        # launchctl unload / systemctl stop deliver SIGTERM, which would skip
        # the finally block below and strand decrypted briefing copies. Worse,
        # the agent runs in its own session, so it would survive as an orphan
        # holding credentials while its briefing vanishes. Kill the agent's
        # process group first, then exit normally so cleanup runs.
        def _sigterm(*_):
            log("SIGTERM received, shutting down")
            if CURRENT_AGENT is not None:
                kill_process_group(CURRENT_AGENT)
            sys.exit(143)
        signal.signal(signal.SIGTERM, _sigterm)
        acquire_lock()
        try:
            try:
                handle()
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                log(f"FATAL handler exception: {e!r}\n{tb}")
                try:
                    notify_send(f"⚠️ flare handler crashed: {type(e).__name__}: {e}\nSee {LOGFILE}")
                except Exception:
                    pass
                raise
        finally:
            cleanup_decrypted()
            release_lock()
    else:
        print(f"unknown mode {mode!r}: use no argument to handle a flare, "
              f"or 'diag' / 'restart' / 'encrypt' / 'rekey'")
        sys.exit(2)
