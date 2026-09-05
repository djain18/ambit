# Deploying a public demo

This is optional. Local (`http://127.0.0.1:8000`) is completely normal for a
Buildathon video and needs none of this. Do this only if you want a link a
judge can click after watching.

**Nothing here has been run.** I have no hosting account connected to this
session, and putting your Razorpay key on a public host is a credentials
action — this file is instructions for *you* to run, not something already
done on your behalf.

---

## What you get, honestly

A public URL running the exact app you have been testing locally: the
landing page, the console, the API, all of it. Test mode only.

**What does not survive a redeploy, on the free tier of either platform
below:** the audit chain, any grants beyond the one seeded at boot, and the
signing key. Free-tier disks are not guaranteed to persist across restarts.
Every time the box restarts, `start.sh` notices there is no key or no grant
yet and creates a fresh one — so the app always comes back up demoable, but a
verification history built before a restart will not still be there after
one. **For the video, screen-record your local server instead**, where you
control exactly when it restarts. Use the public link only for judges to
click around afterwards.

---

## What was added to the repo for this

| File | Job |
|---|---|
| `Dockerfile` | builds the container both platforms below can run |
| `start.sh` | on boot: generate the signing key if missing, issue the demo grant if none exist, then start the server bound to the host's `$PORT` |

Both are no-ops on a box that already has a key and a grant, so redeploying an
already-running instance changes nothing.

---

## Option A — Render (recommended: simplest free tier)

1. Go to <https://render.com>, sign up with the GitHub account that owns
   `djain18/ambit`.
2. **New +** → **Web Service** → connect the `ambit` repository.
3. Render will detect the `Dockerfile` automatically. If it asks:
   - **Environment:** Docker
   - **Region:** anything
   - **Instance type:** Free
4. **Environment variables** (Render dashboard → your service → Environment).
   Add these — values referenced by name only, never pasted anywhere but
   here:
   | Key | Value |
   |---|---|
   | `RAZORPAY_KEY_ID` | your `rzp_test_...` key id |
   | `RAZORPAY_KEY_SECRET` | your Razorpay test key secret |
   | `AMBIT_REQUIRE_TEST_MODE` | `true` |
   | `AMBIT_DATA_DIR` | `/app/data` |
   | `AMBIT_GRANT_SIGNING_KEY_PATH` | `/app/keys/grant_signing_key.pem` |

   Leave `RAZORPAY_WEBHOOK_SECRET` and `AMBIT_PUBLIC_URL` blank unless you are
   also doing the ngrok webhook setup — that is a separate, independent step.
5. **Create Web Service.** First build takes 2 to 5 minutes. Render gives you
   a URL like `https://ambit-xxxx.onrender.com` the moment it goes live.
6. Visit `https://<your-url>/healthz` first. You should see
   `"ok": true, "test_mode_enforced": true`. Then visit `/` and `/console`.

**Known free-tier behaviour:** the service sleeps after 15 minutes with no
traffic and takes 30 to 60 seconds to wake on the next request. That is
normal, not a bug — mention it if you send the link to a judge cold.

---

## Option B — Railway

1. Go to <https://railway.app>, sign up with GitHub.
2. **New Project** → **Deploy from GitHub repo** → pick `djain18/ambit`.
   Railway also detects the `Dockerfile` automatically.
3. **Variables** tab — add the same five variables as the Render table above.
4. Railway assigns a `$PORT` itself; `start.sh` already reads it, nothing to
   change.
5. **Settings → Networking → Generate Domain** gives you the public URL.

---

## After it is live

Run this from your own machine, replacing the URL:

```
curl https://<your-url>/healthz
curl https://<your-url>/agent/catalog
```

Both should return real JSON, not an error page. If `healthz` shows
`"test_mode_enforced": false`, the `AMBIT_REQUIRE_TEST_MODE` variable was not
set — the app is designed to refuse to start in that state against a live
key, so seeing it start at all means the key was a test key, but set the
variable anyway so the intent is explicit rather than accidental.

**Do not point the Razorpay Dashboard webhook at this URL casually.** That is
the one action in this whole file that reaches real Razorpay infrastructure
in a way that outlives the demo. If you want the webhook working end to end
on the public URL, treat that as its own decision, made deliberately, not a
side effect of following this guide.
