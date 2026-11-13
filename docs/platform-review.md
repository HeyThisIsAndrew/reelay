# Getting one-click sign-in approved

Goal: nobody pastes a token or an App Password. Each platform signs in with one click through Reelay's own
app. That needs Reelay's apps registered and approved by Google, TikTok and Meta, which in turn needs the
website live with honest policies, the sign-in server running, and a build reviewers can install.

Status on November 13, 2026: the app code and the website text are ready for review. What's left is accounts,
identifiers and recordings that only the owner can make, listed below in the order they unblock each other.

## Already done (no action needed)

- **Sign-in code** for all five platforms is in the app (`reelay/oauth.py`): Google on the Mac with PKCE,
  TikTok and Meta through the sign-in server (`server/auth_broker.py`) so no app secret ships in the app.
  Each platform switches on the moment its ID goes into `reelay/oauth_config.py` (or an environment variable).
- **Permissions trimmed to what's used.** The app now asks only for what a shipped feature uses (analytics,
  `video.list` and Instagram publishing were removed). `tests/test_review_scopes.py` fails if the app and the
  website's permissions table ever disagree, so a submission can't go in with an unexplained permission.

  | Platform | Requested | Used for |
  |---|---|---|
  | Google | `youtube.upload`, `youtube.readonly` | Uploading and scheduling; listing your own channel |
  | TikTok | `user.info.basic` | Showing which account is attached |
  | Instagram | `instagram_business_basic` | Showing the account; listing your own Reels |
  | Threads | `threads_basic`, `threads_content_publish` | Showing the account; posting when you turn it on |
  | Facebook | `pages_show_list`, `pages_read_engagement`, `pages_manage_posts` | Choosing a Page; posting to it |

- **Website pages reviewers check:** Privacy (with YouTube API Services, Google Limited Use, TikTok and Meta
  sections), Terms (with the YouTube Terms clause), Data Deletion, Security, and a Reviewer Notes page with
  test steps per permission. Updated today for the Instagram listing and Bluesky posting.
- **Handles typed with an @** now work in every field (Bluesky handle, Facebook Page, source usernames, the
  Library link box), as do profile links and tokens pasted with "Bearer ".

## The order to do it in

### 1. Things every platform asks for (do first, about a day)

1. **Own the domain** `reelay.app` (the site and the reviewers' links assume it). If it's a different domain,
   change `OPERATOR.domain` and `SITE_URL` once and rebuild.
2. **Make the mailboxes real:** support@, privacy@, compliance@ (reviewers write here), security@ and
   copyright@reelay.app. Reviewers do email these.
3. **Fill the placeholders.** The release audit lists them; run it until it passes:
   `cd sites/reelay && npm run build && REELAY_RELEASE=1 node scripts/audit-dist.mjs`
   - Legal: `LEGAL_ENTITY`, `ADDRESS`, `COUNTRY`, `GOVERNING_LAW`, `LIABILITY_CAP`, `DMCA_AGENT`
     (`src/data/operator.ts`). Google, TikTok and Meta all show your legal name and address to users.
   - Hosting: `WEB_HOST`, `WEB_LOG_RETENTION`, `AUTH_SERVER_REGION` (same file).
   - Sales: `PURCHASE_URL`, `REFUND_WINDOW` (`src/data/site.ts`, `PRICING`).
   - App IDs (`APP_IDS`): filled in as each platform gives them to you in steps 3 to 5.
4. **Deploy the website** (your approval; branch `reelay-1.0-site` is local only). Every platform's form
   checks the URLs live.
5. **Notarize a build** (`docs/release-signing.md`, Developer ID checklist) and publish it on the download
   page. Reviewers install the app; an unnotarized build stops them at a Gatekeeper warning.
6. **Deploy the sign-in server** at `auth.reelay.app` (`server/auth_broker.py`, run command in its header).
   TikTok and Meta sign-in can't work without it, and reviewers must be able to sign in.

### 2. Google (YouTube): usually 1 to 4 weeks

1. In Google Cloud, create the production project "Reelay". Enable YouTube Data API v3.
2. OAuth consent screen: External, app name **Reelay**, the Reelay logo, homepage `https://reelay.app`,
   privacy `https://reelay.app/privacy`, terms `https://reelay.app/terms`, authorized domain `reelay.app`.
3. Verify `reelay.app` in Google Search Console with the same Google account.
4. Create a **Desktop app** OAuth client. Put its ID and secret in `reelay/oauth_config.py` (replace the
   `PLACEHOLDER`; Google treats a desktop secret as public) and in `APP_IDS` on the site.
5. Record the demo (see "Recordings" below) and upload it to YouTube as unlisted.
6. Submit for verification with the justifications from the website's permissions table.
7. **Before the YouTube API audit** (the step that lets uploads be public and raises quota): the app must
   refresh or delete stored YouTube API data within 30 days. **Done.** `reelay/youtube_retention.py` checks at
   launch, daily and on every account change: data 27 days or older is refreshed through the connected
   channel, or cleared when the channel isn't connected, the video is gone, or it would pass 30 days.
   `docs/youtube-data-retention.md` lists which stored fields count (cite it in the audit form). The site
   flag `YOUTUBE_30_DAY_REFRESH` is `true` on the website's local `reelay-1.0-site` branch (commit 61d7d2d,
   not pushed). Ship an app build with this change before deploying that, so the site never runs ahead
   of the app.

### 3. Meta (Instagram, Threads, Facebook): usually 2 to 6 weeks, business verification first

1. **Business verification** in Meta Business Manager (legal name, address, a document). It takes days and
   is needed before Advanced Access, so start it as soon as step 1.3 is done.
2. Create the Meta app (type Business) with the Instagram API (Instagram Login), Threads API and Facebook
   Login for Business. App icon 1024 × 1024, category, privacy, terms, and data deletion URL
   `https://reelay.app/data-deletion`. App domain `reelay.app`.
3. Redirect URIs: `https://auth.reelay.app/v1/oauth/instagram/callback`, `.../threads/callback`,
   `.../facebook/callback`.
4. Put the app IDs in `oauth_config.py` (or `REELAY_INSTAGRAM_CLIENT_ID` and friends) and `APP_IDS`, and the
   secrets only on the sign-in server.
5. App Review: request Advanced Access for each permission in the table, one screencast each.

### 4. TikTok: usually 1 to 3 weeks

1. TikTok for Developers: create the app, platform **Desktop**, product **Login Kit**, scope
   `user.info.basic` only.
2. Verify the Terms, Privacy and Desktop URLs. TikTok gives a verification file or DNS record; a file goes in
   the site's `public/` folder (tell me the file and I'll add it).
3. Redirect URI: `http://127.0.0.1/callback/` (the app picks a free port; check TikTok's desktop rules for
   loopback ports when you register it).
4. Client key in `oauth_config.py` and `APP_IDS`; client secret only on the sign-in server.
5. Demo video: launch Reelay, sign in with TikTok, the card shows the account.

### 5. Bluesky: no review needed

Bluesky's own sign-in (AT Protocol OAuth) needs no approval: Reelay publishes a small file on its website
describing itself, and the app does the sign-in. That's app work (the sign-in flow) plus one file on the site,
and it removes App Passwords entirely.

## Recordings

The release audit counts 24 empty screenshot and screencast slots (`src/data/screens.json`, listed in
`sites/reelay/SCREENS.md`). Reviewers need, for each permission: the consent screen with "Reelay" and the
client ID visible in the address bar, then that permission in use in the app. They can only be recorded once
each platform's app exists (the consent screen shows the real app), so record each platform's set right after
its step above. Use the **Be Unconventional** or **Hey, This Is Andrew** profile and a test account, never a
client's.

## What a reviewer will try, and where it's written down

`https://reelay.app/reviewers` has per-permission test steps. Before each submission, walk those steps
yourself on the notarized build with the platform's sign-in switched on, since a step that doesn't match the
app is the most common reason for a rejection.
