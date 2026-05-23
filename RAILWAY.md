# Testing Rowdy on Railway

Standalone POC — no Canvas yet. You're putting the chat at a public Railway
URL so you can poke at Rowdy from any browser.

## 1. Push to GitHub

In the project directory:

```bash
git init
git add .
git commit -m "Rowdy POC"
gh repo create rowdy-tutor --private --source=. --push
```

`.gitignore` already excludes `.env`, so your API key stays on your laptop.
The `static/rowdy.png` mascot image is committed intentionally.

## 2. Create the Railway service

1. Go to railway.app → **New Project → Deploy from GitHub repo** → pick `rowdy-tutor`.
2. Railway auto-detects Python from `requirements.txt` and uses the `Procfile`.
3. First build will fail to start because env vars are missing — that's expected.

## 3. Set Railway environment variables

In the Railway dashboard, open the **Variables** tab and add:

| Variable | Value |
|---|---|
| `SESSION_SECRET` | Output of `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `ANTHROPIC_API_KEY` | Your key from console.anthropic.com |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` |

Optional (usage tracking + cost guards — all safe to omit; defaults are permissive):

| Variable | Default | What it does |
|---|---|---|
| `ADMIN_TOKEN` | _(unset)_ | Required to view `/admin/stats`. Unset = stats endpoint disabled. |
| `DAILY_TOKEN_BUDGET` | `0` (off) | Global tokens/day before Rowdy pauses for everyone. Protects your spend. |
| `PER_SESSION_DAILY_MSG_CAP` | `0` (off) | Messages/day per browser before a soft cap. Abuse deterrent only. |
| `SESSION_TTL_MINUTES` | `180` | Idle sessions are dropped from RAM after this many minutes. |
| `MAX_MESSAGE_CHARS` | `8000` | Rejects oversized pasted messages before they cost tokens. |

Railway will redeploy.

## 4. Generate a public URL

In Railway: **Settings → Networking → Generate Domain.** You'll get something
like `rowdy-tutor-production.up.railway.app`.

Quick sanity check: hit `https://YOUR-URL/healthz` in a browser. Should return `{"ok": true}`.

## 5. Test it

Open `https://YOUR-URL/` in any browser. The chat UI loads with Rowdy's
mascot in the header. Type "hi" — he should respond with his Crowder
greeting and start the diagnostic flow.

## What to actually test (suggested checklist)

| Test | What to look for |
|---|---|
| Say "hi" | Opening greeting (only once per session), then asks about instructor's AI policy |
| Answer the policy question | Asks what you're working on |
| Say "I need help with my essay" | Asks about your thesis or what part you're stuck on |
| Ask for a direct answer like "just write my intro paragraph" | Should refuse and redirect to guided thinking |
| Ask something unrelated like "what's the weather" | Redirect message about schoolwork |
| Upload a screenshot of a math problem | Rowdy reads it and asks one targeted question |
| Refresh the page | Same conversation continues (session cookie persists) |
| Open in incognito | Fresh conversation (different session) |
| Restart the Railway service | All conversations lost (RAM-only, expected) |

## Common things that go wrong

| Symptom | Likely cause |
|---|---|
| Build fails: "no module named anthropic" | `requirements.txt` didn't get committed. Confirm `git ls-files \| grep requirements` |
| Page loads but chat returns 500 | Open Railway logs. Usually a missing or wrong `ANTHROPIC_API_KEY` |
| Rowdy responds but breaks character (gives direct answers) | The system prompt isn't being sent. Confirm `claude_service.py` is the one with `ROWDY_SYSTEM_PROMPT` and prompt caching enabled |
| Avatar shows "R" instead of the mule | `static/rowdy.png` didn't get pushed. Check `git ls-files \| grep rowdy` |

## Usage tracking & caps

Three layers, lightest first:

1. **Anthropic Console** — free, no setup. Shows total spend and volume for your
   API key. Web search bills as a separate line, so keep an eye on it.
2. **App logs** — every chat emits one `USAGE {...}` JSON line to stdout (visible
   in Railway logs): timestamp, a short anonymous session label, token counts, and
   web-search count. **Metadata only — no prompt or reply text is ever logged**, to
   stay clear of FERPA.
3. **Live aggregates** — set `ADMIN_TOKEN`, then hit
   `https://YOUR-URL/admin/stats?token=YOUR_TOKEN` for a JSON summary (requests,
   tokens, web searches, active sessions, today's token total vs budget). These
   reset on restart.

Caps are **off by default**. Turn them on when ready:
- `DAILY_TOKEN_BUDGET` is the real protection — a global tokens-per-day ceiling.
  When hit, Rowdy returns a friendly "resting up" message to everyone until the
  next UTC day. Pick a number from your budget using current Haiku pricing.
- `PER_SESSION_DAILY_MSG_CAP` is a soft per-browser deterrent. Note it is **not**
  true per-student: sessions are anonymous cookies, so a student can reset it by
  clearing cookies or going incognito. Real per-student caps need student identity,
  which arrives with Canvas LTI (see the README).

## Iterating on Rowdy

The prompt is in `claude_service.py` as `ROWDY_SYSTEM_PROMPT`. Edit, commit,
push — Railway redeploys automatically in ~30 seconds.

**Tip**: when iterating on the prompt, the in-memory conversation history
gets wiped on every redeploy, which means you'll re-greet from a clean slate
each time. That's useful for testing the opening flow but annoying if you
were mid-conversation.

## Tearing it down later

Railway → project → **Settings → Delete Project.**
