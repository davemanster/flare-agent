# Install runbook for AI agents

This file is written for a capable AI coding agent (Claude Code, Codex CLI,
Gemini CLI, opencode, goose) to follow. If you are a human, you can read it
too, but the README's quick start is written for you. To use this file, open
your agent CLI in this repo's directory and say: "Follow LLM-INSTALL.md and
set up flare-agent for me."

If you are the agent: your job is to configure and install flare-agent on
this machine, interactively, with the operator answering questions as you
go. Work through the steps in order. Do not skip the verification steps at
the end; an emergency system that was never tested is worse than no system,
because it produces confidence instead of coverage.

This runbook installs the responder side only. The dispatcher (the small
always-on chat model that writes flare files) is a separate concern with its
own instruction file at `examples/dispatcher-instructions.md`; point the
operator there at the end, do not set it up yourself.

## Ground rules

- **Never ask the operator to paste a secret into this chat.** Tokens go
  into the OS keychain, an environment file, or a secrets manager, entered
  by the operator in their own terminal. You configure the lookup, never
  the value. If the operator pastes a token anyway, tell them to rotate it
  when setup is done, since it now exists in a chat transcript.
- **Do not read the operator's briefing or credentials files.** Confirm
  they exist with a file listing and move on. Their contents are none of
  your business and pulling credentials into your context helps nobody.
- **Touch nothing outside this repo, the workspace directory, and the one
  watcher unit you install.** Do not reorganize, update, or "improve"
  anything else you find on the machine.
- **Do not create a git repo, commit, or push anything.**

## STEP 1: Gather decisions

Ask the operator, one round of questions, plain language:

1. **Workspace directory.** Where flare files will live. Suggest something
   like `~/flare-workspace`. You will create it.
2. **Briefing files.** Which existing files describe their infrastructure
   and how to reach it. If they have nothing yet, offer to create skeleton
   files they can fill in later (a `topology.md` with section headings, a
   `credentials.md` with a warning header). Skeletons only; never fill in
   real values.
3. **Notifier.** Telegram (full two-way, recommended) or ntfy (send-only;
   explain that means no go/no-go conversation, findings only).
4. **Agent CLI and model.** Which of claude / codex / gemini / opencode /
   goose they run, and the strongest model their plan gives them. The
   README is blunt that mid-tier models produced wrong diagnoses; relay
   that if they hesitate.
5. **Auto-go.** Explain it in one sentence: after N minutes of silence the
   system executes its own proposed fix. Default is off. Let them choose.
6. **Credential posture.** Plaintext briefing with tight permissions, or
   encrypted briefing unlocked by passphrase at incident time. Summarize
   the tradeoff from the README's security section honestly: encrypted
   at rest means the system cannot act while they are unreachable.
7. **Go PIN.** A short secret that must accompany any fix instruction, so
   a stolen or borrowed phone can read findings but not execute commands.
   Recommend enabling it. The operator picks the value and types it into
   the config themselves; you should not know it.

## STEP 2: Telegram setup (skip if ntfy)

The operator does these on their phone; you relay the instructions and wait:

1. Message @BotFather, send `/newbot`, follow the prompts, and note the bot
   token it returns. Tell them NOT to paste the token to you. This must be
   a NEW bot: if the operator already runs a dispatcher or any other bot,
   do not reuse its token. Telegram allows one `getUpdates` consumer per
   token, and two pollers on one bot silently steal each other's messages.
2. Message @userinfobot (or any equivalent) to get their numeric user id.
   The id is not secret; they can paste that one.
3. Send their new bot any message once, so the bot can message them back.

Then store the token where the handler can find it, operator's terminal,
not yours. macOS:

```bash
security add-generic-password -s flare-bot -a flare -w
# prompts for the token, stores it in the login keychain
```

and set in the config block: `BOT_TOKEN_CMD = "security find-generic-password -s flare-bot -w"`.

Linux: a `chmod 600` environment file referenced by the systemd unit
(`examples/flare-watcher.service` shows the shape), or their secrets
manager's read command in `BOT_TOKEN_CMD`.

The agent CLI's own auth usually already exists on the machine from normal
use (login session or config file). Only set up `AGENT_TOKEN_*` if the
operator uses a token-based headless auth for it, same storage rules.

## STEP 3: Edit the config block

Open `flare_agent.py` and set, from the STEP 1 answers: `WORKSPACE`,
`BRIEFING_FILES`, `NOTIFIER`, `ALLOWED_USER` (or `NTFY_URL`), `AGENT`,
`AGENT_MODEL`, `AUTO_GO_SECONDS`, and the `BOT_TOKEN_*` lookup from STEP 2.
Leave everything else at its default on a first install.

Create the workspace directory. If the operator chose the encrypted
posture, have them run `python3 flare_agent.py encrypt <briefing-file>` in
their own terminal (it prompts for a passphrase; you must not know it),
then point `BRIEFING_FILES` at the `.enc` path. Do not delete the plaintext
original yourself; the operator does that after STEP 5 proves decryption
works.

## STEP 4: Install the watcher

macOS: copy `examples/local.flare-watcher.plist` to `~/Library/LaunchAgents/`,
fix the two paths inside (script path, `FLARE.md` path in the workspace),
remove the `EnvironmentVariables` block if STEP 2 used the keychain, then
`launchctl load` it. If a unit with the same label already exists, stop and
ask the operator instead of overwriting.

Linux: copy `examples/flare-watcher.path` and `flare-watcher.service` to
`~/.config/systemd/user/`, fix the paths, `systemctl --user daemon-reload`,
`systemctl --user enable --now flare-watcher.path`.

## STEP 5: Verify, all three checks

1. **Config check:** run `python3 flare_agent.py` with no flare present.
   It must exit cleanly with "FLARE.md not found". Config errors mean
   STEP 2 or 3 is wrong; fix and repeat.
2. **Pipeline check:** run `python3 flare_agent.py diag`. Every problem
   line it prints is yours to resolve before continuing.
3. **Live test:** write a test flare into the workspace:

   ```
   # FLARE: install test

   This is a test raised during setup. Confirm you can read your briefing
   files, report what infrastructure you can see, and change nothing.
   ```

   The operator's phone should buzz within seconds, findings should follow
   a few minutes later, and (on Telegram) replying "stand down" should
   archive the flare. Confirm with the operator that each of those
   happened. If the encrypted posture is in use, this test also proves the
   passphrase flow; only after it succeeds should the operator delete the
   plaintext original.

## STEP 6: Hand off

Tell the operator, briefly: where the workspace is, how to raise a flare by
hand, what `diag` and `restart` do, and that the dispatcher setup lives in
`examples/dispatcher-instructions.md` when they want chat-driven flares.
Remind them of anything left undone (briefing skeletons to fill in, a
pasted token to rotate). Then stop. Do not raise further flares, do not
schedule anything, do not keep watch.
