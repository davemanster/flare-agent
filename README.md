# flare-agent

Your VPN endpoint is dead, your DDNS record is stale, and you're on the other side of the planet on hotel wifi. Everything that could tell you what's wrong is on the far side of the thing that broke. This is how you still get eyes inside: something in the lab writes a flare file, a watcher wakes a strong AI agent with credentials and a briefing, and the agent investigates and talks to you over chat, which only needs the lab to have outbound internet. You get findings in a few short lines sized for a phone, and nothing gets fixed until you say go. One Python file, standard library only, plus a launchd or systemd unit.

## The problem

Monitoring tells you something broke. It doesn't tell you what broke, and it doesn't do anything about it.

That gap is fine when you're home. You get the alert, you SSH in, you look, you fix. It stops being fine when you're twelve time zones away, and it gets really bad when the thing that broke is your way in.

Being locked out and the lab being down are two different problems, and the first one is way more common. Your way in is the fragile half: a VPN endpoint, a stale DDNS record, a port forward, a WAN IP that changed while you were on a plane. Any of those breaks and every tool you'd normally use is on the wrong side of the problem, while the lab itself sits there healthy, still making outbound connections just fine.

So stop needing a way in. Put the responder inside the network, where it already has access to everything, and drive it over a channel that only needs outbound internet, meaning a chat app. Telegram works from anywhere on any connection, including the 2 Mbps throttled roaming data I've been stuck with more than once.

To be clear about the boundary: this handles "I can't get in", not "the lab can't get out". A full WAN outage takes the responder down along with everything else, and no software living inside the lab can fix that. That failure needs out-of-band hardware, LTE failover, IPMI on a separate path, that kind of thing. Everything short of it though, this covers, and in my experience that's most of what actually goes wrong.

## Where this came from

I run a fairly involved homelab: router, virtualization cluster, several NAS boxes, a media pipeline, a pile of always-on services. My living depends on some of it, and I spend long stretches abroad. Things would fall over at 3am my time and sit broken for a day because a monitoring alert saying "service down" is useless when you can't get a decent look inside.

I already had a small local LLM running on a machine inside the network, hooked to my chat. At some point it clicked that this thing doesn't need to be smart enough to fix anything, it just needs to be smart enough to take my call. I text it "SOS the media server is down", it writes a file, and the file wakes a much stronger cloud agent that has the credentials and actually does the work. By the time my phone buzzes with findings, something competent has already been inside looking around.

The name is literal. A flare is what you fire when you're in trouble and you need whoever's out there to come find you.

This repo is that system with my lab pulled out of it. It's not a rewrite, this is the code that actually runs, sanitized and made configurable. Every hardening decision in it was paid for by a real failure, and the notes section below documents each one. Honestly, those notes are the main reason to use this instead of writing your own in an afternoon.

## How it works

There are two AIs involved, a small one and a big one, and keeping their jobs separate is the point.

**The dispatcher** is small and always on. Mine is a local model in the 30B class running on a Mac, but anything that can chat with you and write a file qualifies, down to 7B models or a plain webhook script with no model at all. The dispatcher never touches infrastructure, never sees credentials, and is never asked to reason about anything hard. It takes your call and writes the flare file. That's it. This is deliberate: small local models are good enough for "write down what the operator said" and not good enough for "figure out why the VPN is flapping", so don't let them try.

**The responder** is the strongest agent CLI you have access to, invoked headlessly only when there's an incident. It gets the flare, your briefing files, and real credentials. It costs money every time it runs and that's fine, flares are rare and this is exactly the wrong place to save money.

The flow:

1. **Something raises a flare.** You text your dispatcher. Or a monitoring webhook, a cron job that noticed a disk filling, a UPS that switched to battery. Anything that can write a file.
2. **The watcher fires.** launchd `WatchPaths` on macOS, a systemd path unit on Linux. No polling, no resident process, zero cost while idle.
3. **The handler pages you and investigates.** It sends the flare text to your chat, then runs the agent with a diagnose-only prompt: read-only commands, findings written to a file in a strict five-line format, hard character cap so it fits on a phone screen.
4. **Go/no-go.** The findings arrive with a proposed fix. Reply "go" to run it, "stand down" to close, or type different instructions. Every instruction spawns a fresh agent run with the full context stitched in. Replies are allowlisted to your account ID. There's an optional auto-go timer for when you might be unreachable; it ships disabled.
5. **Cleanup.** On resolution or stand-down the flare is archived with its findings and the handler exits. Findings are also written to disk before any send is attempted, so a dead network can't eat them.

One thing worth knowing: the agent runs are stateless. The handler carries all the state between invocations and stitches it into each prompt. There's no long-running agent session to babysit, which is most of why this survives bad networks.

## The flare file

A flare is just a markdown file. There's no schema and no parser, it gets handed to a language model, so plain prose works.

What makes a flare useful is context the agent can't get by looking around: what you were doing when it broke, what changed recently, what you already tried. The stuff that's still true at investigation time, like which services are down, is better discovered live, because a flare written twenty minutes ago may already be stale.

```markdown
# FLARE: Can't VPN in, DDNS looks stale

VPN from my phone and my laptop both time out on handshake, started
around 02:15 local. The DDNS hostname resolves to an IP I don't
recognize, so the WAN address probably changed and the update didn't
take. Nothing inside has alerted, so the lab itself is presumably fine.

Do not change firewall or VPN config. Confirm the current WAN address,
find out why the DDNS update failed, and report. Diagnose only.
```

That last line matters more than it looks. The flare is where you set the rules of engagement, and the investigation prompt tells the agent to respect them. "Diagnose and report only" gets you an investigation. "Restart it if it's wedged" gets you a fix. You can always escalate over chat once you've seen the findings.

## Install

**The easy way, and honestly the way I'd recommend:** this repo ships with `LLM-INSTALL.md`, which is the install guide written for an AI agent to follow instead of a human. If you already run Claude Code or one of the other agent CLIs, open it in this directory and say:

```
Follow LLM-INSTALL.md and set up flare-agent for me.
```

It'll ask you the setup questions in plain language, do the configuration, install the watcher, and prove the whole pipeline works with a live test flare before it calls itself done. It's under standing orders to never ask for a token in chat and to keep its hands off your credential files, so it can't leak what it never sees.

**The manual way:**

```bash
git clone https://github.com/davemanster/flare-agent
cd flare-agent
```

Nothing to install. Python 3.9+, standard library only.

Open `flare_agent.py` and edit the config block at the top:

```python
WORKSPACE      = "/path/to/your/workspace"   # where FLARE.md appears
BRIEFING_FILES = ["/path/to/credentials.md", "/path/to/topology.md"]
NOTIFIER       = "telegram"                  # telegram | ntfy | webhook
ALLOWED_USER   = 0                           # your Telegram numeric user id
AGENT          = "claude"                    # claude | codex | gemini | opencode | goose
AGENT_MODEL    = "opus"                      # strongest you have, pinned. See notes.
```

Tokens come from the environment, or a command, or a chmod-600 file, never from the script:

```bash
export FLARE_BOT_TOKEN="..."    # your chat bot's token
export FLARE_AGENT_TOKEN="..."  # the agent CLI's auth token, if it uses one
```

Install the watcher (macOS):

```bash
cp examples/local.flare-watcher.plist ~/Library/LaunchAgents/   # edit paths first
launchctl load ~/Library/LaunchAgents/local.flare-watcher.plist
```

Linux equivalents are in `examples/` as a systemd path + service pair.

Test end to end by raising a flare by hand:

```bash
echo "# FLARE: test

This is a test. Confirm you can reach the lab, report what you see,
and change nothing." > /path/to/your/workspace/FLARE.md
```

Your phone should buzz within a few seconds.

If you want the full chat-driven setup, `examples/dispatcher-instructions.md` is the instruction file for the small always-on model that fronts your chat. It's the exact instruction set mine runs, genericized, including the troubleshooting runbook the dispatcher follows when the responder goes quiet.

## Configuration

| Setting | What it does |
|---|---|
| `WORKSPACE` | Directory watched for `FLARE.md`. Also holds the archive and saved findings. |
| `BRIEFING_FILES` | Files handed to the agent as context. This is what makes it useful about your infrastructure instead of generically smart. |
| `NOTIFIER` | `telegram` (two-way, the reference), `ntfy` (send-only), `webhook` (unimplemented seam). |
| `ALLOWED_USER` | Only this chat account can drive the handler. Not optional. |
| `AGENT` | Which agent CLI to invoke. See the adapter table below. |
| `AGENT_MODEL` | Pinned model name. See the notes on why this is never left to default. |
| `INVESTIGATE_BUDGET_USD` / `FIX_BUDGET_USD` | Per-invocation spend caps, passed to agents that support a budget flag. |
| `AGENT_TIMEOUT` | Hard ceiling per agent run before the process group is killed. |
| `AUTO_GO_SECONDS` | Auto-approve the proposed fix after this much silence. `0` disables. Ships disabled; I run 1800. |
| `HEARTBEAT_INTERVAL` | Seconds between "still working" pings during long runs. |
| `PASSPHRASE_TIMEOUT` | How long to wait for a briefing passphrase over chat before proceeding without the encrypted files. |
| `LOG_REPLIES` | Set `False` if you might text secrets mid-incident, so they never land in the handler log. |
| `GO_PIN` | Optional shared secret that must appear in any fix instruction ("go 4417"). Stand-down never needs it. See the security section. |

`flare_agent.py diag` prints a read-only health report of the whole pipeline (watcher loaded, handler running, agent binary found, notifier reachable, last run's log). It's sized to fit in a chat message and safe for the dispatcher to run and relay. `flare_agent.py restart` re-fires a dead handler, and refuses unless a flare is active and no handler is running. `flare_agent.py encrypt` and `flare_agent.py rekey` manage encrypted briefing files; see the security section.

## Agent CLIs

| `AGENT` | Invocation | Budget cap | Auth |
|---|---|---|---|
| `claude` | `claude --dangerously-skip-permissions --model M -p PROMPT` | yes | `CLAUDE_CODE_OAUTH_TOKEN` |
| `codex` | `codex exec --full-auto --model M PROMPT` | no | `OPENAI_API_KEY` |
| `gemini` | `gemini --yolo -m M -p PROMPT` | no | `GEMINI_API_KEY` |
| `opencode` | `opencode run --model M PROMPT` | no | own config |
| `goose` | `goose run -t PROMPT` | no | own config |

`claude` is what runs in my lab and has handled real incidents. The other four are my best effort at the right flags, correct as of this writing, but agent CLIs change their flags constantly. Before you depend on one, run the hand-raised test flare above and watch `/tmp/flare-agent.log` to make sure the invocation actually works. If a mapping is stale, it's a one-line fix in the `ADAPTERS` dict and I'll take the PR.

All of them run with permission prompts disabled, because there's no human at the keyboard to approve anything. That's what the go/no-go loop is for instead.

## Notes from actually running this

This is the part I'd want to read if I found this repo. Every item is a failure that actually happened to me, and each one is also a comment in the code.

**Don't use `subprocess.PIPE` for the agent's output.** Capturing stdout through a pipe and calling `communicate()` deadlocks intermittently, because the agent spawns children that inherit the pipe descriptors. A grandchild holds the write end open, your read never sees EOF, and the handler wedges silently in exactly the emergency where you needed it. Redirect to a temp file and read it afterward. The problem disappears and you get a debuggable artifact for free.

**Launch the agent with `start_new_session=True`.** Without its own process group there's no reliable way to kill the whole tree on timeout or stand-down. You kill the parent and orphaned children keep running, sometimes still holding SSH sessions. Signal the group and everything goes down together.

**Resolve the agent binary at runtime, newest version wins.** Agent CLIs auto-update and move their install path when they do. I had the path hardcoded, and every Claude Code update silently broke the rescue system until the next manual test caught it. Enumerate the install dir, sort by version, take the newest executable, fall back to `which`.

**Pin the model, and use the strongest one you have.** Mine originally inherited the CLI's rolling default, a mid-tier model. Three real incidents, zero correct diagnoses, and one confident false positive. Pinned to the top model, it started actually finding root causes. Flares are rare, so the cost is noise, and a vendor changing their default tier must never silently downgrade your rescue path. This is the one place where paying for the good model is unambiguously correct.

**Persist findings before you try to send them.** During one real incident the machine lost outbound network mid-flare. The investigation completed fine, the single send of the findings failed, and the findings were gone: in memory only, never retried, deleted on archive. Now findings hit disk first, must-arrive messages retry with backoff, and the go/no-go loop keeps re-sending until delivery confirms.

**My "Hello?" once got executed as a fix instruction.** Same incident, worse wrinkle. The findings never reached me, so from my end the system had gone silent, and I texted "Hello?". The handler took that as my instruction and cheerfully spun up a paid agent run to execute "Hello?". Now, until findings are confirmed delivered, nothing executes, not even a bare "go", because a message sent before you've seen the findings is almost certainly "where are you?", not an order. The explicit override is "go anyway", for the case where delivery failed but the findings reached you another way, like your dispatcher relaying the copy saved to disk.

**Heartbeat during long runs.** An investigation can legitimately take several minutes, and several minutes of silence from an emergency system is indistinguishable from a dead one. That ambiguity is corrosive when you're far away and already worried. A ping every 90 seconds costs nothing and removes it.

**Keep the output phone-sized.** Early findings were multi-screen dumps that Telegram truncated. The prompts now demand a fixed five-field format with a hard character cap. The agent keeps its full detail in its own session, and you get the short version you can actually make a decision from.

**Wrap every network call.** A timeout talking to the chat API must never propagate into the main loop. The handler's job is to keep running while things are broken, so every external call degrades to a log line.

**Take a PID lock.** File watchers can fire twice for what is morally one event. Two handlers on the same incident double-send everything and race each other on the archive. One lock file solves it.

**Log every state transition.** When this misbehaves you're debugging it remotely, under stress, over a bad link, while something else is already broken. A log you read top to bottom and see exactly where it stopped beats clever code. `flare_agent.py diag` exists so the dispatcher can read that log for you and relay the punchline.

## Security

Read this before pointing it at anything real.

The agent gets whatever your briefing files give it, and in a useful deployment that means real credentials, running as your user, inside your network. That's the whole point, and it's also the whole risk. Treat it like any other privileged automation.

**The allowlist is not optional.** A chat bot token is a bearer credential; anyone who obtains it can message your bot. The single-account allowlist is what stops that from becoming a shell on your network. The handler refuses to start without one.

**Understand that your chat account is the whole authentication boundary.** The allowlist trusts one account, so whoever controls that account controls an agent with real credentials inside your network. SIM swap, stolen session, your unlocked phone in the wrong hands, any of those is now remote code execution in your lab. `GO_PIN` narrows this: set it to a short secret that lives in your head, and any fix instruction has to include it ("go 4417") or it gets ignored. An attacker with your phone can read findings but can't execute anything, and stand-down deliberately never needs the PIN, because stopping things should always be easy. Costs you five keystrokes per incident. I'd turn it on.

**The responder machine is assumed single-user.** For the `claude` adapter the prompt goes to the agent over stdin. For the other adapters it currently rides in argv, where any local user can read it with `ps`, and the prompt contains your flare text and briefing file paths (not their contents). On a box only you log into this is nothing; on a shared box, know it's there.

**One bot per consumer.** If your dispatcher is also a Telegram bot, the flare bot must be its own separate bot with its own token. Telegram allows one `getUpdates` consumer per token, and two processes polling the same bot steal each other's messages, which you would discover mid-incident as silently dropped replies.

**Keep tokens out of files.** The handler resolves each secret from an environment variable, then a command, then a chmod-600 file, in that order. The command form means you never store plaintext at all: `security find-generic-password -s flare-bot -w` on macOS, `op read` for 1Password, `pass show`, or your secrets manager's CLI. Don't paste tokens into the script. It feels harmless on a private machine and it's one sloppy backup, screenshot, or published repo away from a very bad day.

**Your briefing files are the crown jewels.** They hold the credentials that make the responder useful. Same handling rules: outside any repo, tight permissions, and never in the dispatcher's reach. The dispatcher gets chat and one writable directory, nothing else. A small model with credentials is a bad combination even before anyone attacks it.

### Credentials without plaintext at rest

You have three postures for the briefing credentials, in increasing order of paranoia. Pick based on who else can touch the machine and how you sleep.

**1. Plaintext file, tight permissions.** A chmod-600 file outside any repo. Simplest, and what everything above assumes. The machine itself is your security boundary.

**2. Encrypted at rest, passphrase over chat at incident time.** Name a briefing file with a `.enc` extension and the handler changes behavior: when a flare fires, it asks you over chat for the passphrase, decrypts to a 0600 temp copy for the duration of the incident, deletes your passphrase message from the chat immediately, and shreds the copy when the handler exits. Nothing usable sits on disk between incidents. Create and rotate with:

```bash
python3 flare_agent.py encrypt /path/to/credentials.md    # writes credentials.md.enc
python3 flare_agent.py rekey   /path/to/credentials.md.enc # rotate the passphrase
```

It's standard `openssl enc -aes-256-cbc -pbkdf2` with the iteration count pinned at 600k (openssl's default is uselessly low), so you can decrypt by hand anywhere even without this script. Deliberate tradeoff to be aware of: this is not authenticated encryption. Someone who can tamper with the file can't read it, but can make it decrypt to garbage, or worse, to chosen garbage that ends up in the agent's context. `age` or `gpg` would close that and cost the decrypt-anywhere property; I chose openssl-everywhere. If your threat model includes an attacker who can modify files on the responder machine, you have bigger problems than the briefing anyway, but now it's said out loud. Be aware of what crosses the wire: Telegram bot chats are encrypted in transit but not end to end, so treat the passphrase as something Telegram's infrastructure could in principle see. That's still a much better thing to expose than the credentials themselves, because it's useless without the file, and `rekey` makes it cheap to retire after every incident if you want. The real tradeoff is operational: an encrypted briefing means the system can't help you while you're asleep or unreachable, because nothing useful happens until you supply the passphrase. Auto-go and encrypted-at-rest pull in opposite directions; you can't have both at full strength.

**3. Nothing on disk at all.** Keep no credentials on the responder machine. The investigation runs shallow (network-level checks, whatever works unauthenticated), and when the findings come back you supply what's needed as part of your instruction: "root password for the NAS is X, go restart the export". Every reply already becomes part of the fix prompt, so this works today with no extra machinery. Set `LOG_REPLIES = False` so the secret never lands in the handler log, and rotate that credential afterward since it transited chat. This is the most spartan posture and the most limited: the agent can only be as useful as what you're willing to text it mid-incident.

**Treat the flare file as untrusted input if anything but you can write to it.** It goes into the context of a model holding credentials. If someone else can write that file, they're effectively talking to an agent that has your keys, and that's prompt injection with real consequences. If your flare producer is an internet-exposed webhook, put a real boundary in front of it. Mine is written only by processes I control on the LAN.

**Think before enabling auto-go.** It exists because self-healing is the point when you're truly unreachable, and I run it enabled at 30 minutes. But it means the system will eventually execute its own proposed fix with nobody watching, and the incidents where I've been gladdest to have a human veto are the ones where the agent's first read was confidently wrong. It ships disabled. Enable it knowing what it is.

## Limitations

It responds to flares, it doesn't detect problems. Something else has to notice and write the file. That's deliberate; detection is what monitoring is for.

It depends on the responder machine being alive with outbound internet. During a real WAN outage a LAN-side watchdog can still raise a flare and the watcher will even fire the handler, but the responder needs a cloud API to think and a chat API to talk, so it's a dead end until connectivity returns. The handler is built to survive that gracefully rather than fix it: every network call is wrapped, so it doesn't crash, it keeps state and re-sends what it has once the WAN comes back, and you pick up the conversation from there. Mine is on a UPS, which covers the common case and not the interesting one; LTE failover would cover the interesting one. One related deployment note: give the responder machine a public DNS resolver directly, so your internal DNS dying doesn't blind it.

Telegram is the only two-way notifier. ntfy works send-only, which pairs with auto-go or report-only use. A generic webhook is a marked seam in the code, not an implementation.

One incident at a time, by design. The PID lock makes a second flare wait. Correct for a homelab, wrong for anything bigger.

## License

MIT. It's a weekend project, do whatever you want. -Dave
