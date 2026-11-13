# Dogfood report: the full loop with the owner's own content

November 12 to 13, 2026, Reelay `main` (0.12.0 → 1.0.0). Profile **Hey This Is Andrew**: 405 of his own videos
(220 YouTube, 185 TikTok), sources @HeyThisIsAndrew (YouTube), @hey_thisisandrew (TikTok, Instagram).
Drafts were generated for real with Meta Muse Spark on his own videos. Nothing was posted publicly: no
platform account is connected in his profile, and posting on his accounts is his to do.

## The loop, step by step

| Step | Result | Friction found | Fixed |
|---|---|---|---|
| 1. Import from his sources | YouTube ✓, TikTok ✓ (both already in the Library) | His three sources weren't marked **Mine**, so 405 videos had no "Own content" provenance | Marked Mine in his database; re-listing a Mine source fills provenance in |
| 1. Import from Instagram | ✗ → fixed | Listing @hey_thisisandrew showed yt-dlp's raw "Unable to extract data; please report this issue on github" (owner hit it on Nov 12). yt-dlp marks its Instagram profile reader **broken**: it never worked for him | Instagram's official API now lists his whole profile once connected (Accounts → Instagram → Advanced Setup); without it, a plain message says what to do; no raw yt-dlp text anywhere |
| 1. Import a finished video | ✗ → fixed | **There was no way in for a file from his editor.** Only already-posted links could be imported, so his newest video, which is the one he needs to post, couldn't be packaged at all | Drop a file anywhere on the window, or Library → Add Video Files…; a same-name .srt/.vtt/.txt becomes the transcript |
| 2. Open the ticket | ✓ | A dropped file now opens its ticket on the Board | |
| 3. Repurpose (draft) | ✗ → fixed | (a) **0 of 405 tickets had a transcript**, so drafts were written from titles. (b) A full draft takes **50 to 86 s** (Muse Spark reasons first: 4,700 reasoning tokens) against a **60 s** timeout, so most runs failed. (c) The voice profile was prompt text only | (a) Repurpose and Batch fetch captions first. (b) Timeout 180 s. (c) Voice rules are enforced (banned phrases rewritten, emoji level, footer) |
| 3. Draft quality | ✓ for 2 of 3, one line for the third | See below | Default hashtags no longer forced onto unrelated topics |
| 4. Review and edit | ✓ | | |
| 5. READY | ✓ | Readiness correctly blocks a new file with "Add a description" until drafted, and turns green after | |
| 6. Post: YouTube | Blocked on the owner | "Choose a YouTube channel to post to": no channel connected | Needs him (below) |
| 6. Post: Bluesky | Blocked on the owner | "Connect a Bluesky account in Accounts first" | Needs him (below) |
| 6. Post: Threads | Blocked on the owner | Account, plus a **public URL** to the video (Threads fetches by link, it doesn't take uploads) | Needs him (below) |
| 6. Post: Facebook | Blocked on the owner | "Connect a Facebook account in Accounts first" | Needs him (below) |
| 6. Post: TikTok | ✓ hand-off | Prepare to Publish: file ✓, caption ✓ (271 characters, 5 hashtags) | |
| 6. Post: Instagram | ✓ hand-off | Prepare to Publish: file ✓, caption ✓ | |

Also fixed on the way: `scripts/build_local.sh` refused to back up a cleanly closed database (a read-only
open of a WAL database fails when its `-shm` is gone) and stopped before installing, as designed; the MCP
server reported version 0.5.0; an MCP test had a date that became the past on Nov 10.

## Draft quality, honestly

Three real drafts with his voice profile, on three of his recent videos:

- **"Why I Bought the Sony 16-35mm GM Twice" (13 s Short):** paste-ready. Every line comes from the
  transcript (first gen broke, bought it again, f/2.8 all the way through, compact), in first person with
  contractions, one real question at the end, his footer appended to the YouTube description.
- **"Why Is It So Hard to Take Your Own Creative Advice?" (28 s Short):** paste-ready. It picked up his own
  phrase "in everyone else's business" from the transcript.
- **"When I Worked at PlayStation…" (72 s):** the copy was paste-ready and better than generic (it kept the
  PlayStation backstory, "a little latency", "team TV or team handheld"). **The hashtag line failed**: it
  used his default `#sonyalpha #buildinpublic` on a PlayStation video. One line to rewrite. Cause fixed: the
  voice now says to use defaults only when they fit the topic. Not re-run since.

Quality bar ("rewrite more than a line or two and the draft failed"): **met on all three**, with one line on
the third before the hashtag fix.

## What each publisher needs to post for real

Every one of these is an account or approval only the owner can create. None is a dead button: each card
says why it can't connect yet and what to do.

- **YouTube.** Accounts → YouTube → Advanced Setup with his own Google Cloud project (the five-step wizard).
  While that project is in Testing, Google signs him out every 7 days and **uploads are private until the
  project passes YouTube's API compliance audit**. The audit needs the website's Privacy Policy and Terms
  (now written to that standard) and screencasts of the consent screen. Reelay's own one-click Google sign-in
  ships as a placeholder until Reelay's Google app is approved.
- **Bluesky.** Works today: Accounts → Bluesky, his handle and an App Password; then Settings → Automatic
  posting → Bluesky on, and restart.
- **Threads.** A Meta developer app he owns with threads_basic and threads_content_publish, a long-lived token
  (60 days), Settings → Automatic posting → Threads on, **and a public URL** (for example a Cloudflare Tunnel)
  so Threads can fetch the video.
- **Facebook.** A Meta app with pages_manage_posts, pages_read_engagement, pages_show_list; a long-lived Page
  token and the Page ID; Settings → Automatic posting → Facebook on.
- **TikTok.** Posted by hand with Prepare to Publish. Automatic posting needs TikTok's Content Posting API
  and an audit of Reelay's TikTok app; until that audit, TikTok only allows private posts from unaudited apps.
- **Instagram.** Posted by hand with Prepare to Publish. Automatic posting needs a Meta app with
  instagram_business_content_publish that passes App Review, and a public URL for the video. Listing his
  whole profile works now through the official API with a token from his own Meta app.

## Definition of done: not met yet

The bar is "the owner posts his own content through the app daily for a week with no workarounds". That
needs a week of him using it, so it can't be met inside this work. What still blocks day one:

1. Connect YouTube (his Google project) and accept private-only uploads until the audit, or post YouTube by
   hand for now.
2. Connect Bluesky (5 minutes), and Threads and Facebook if he wants them automatic (his Meta app, about
   30 minutes, plus a tunnel for Threads).
3. Nothing for Repurpose: his Meta API key is already in the Keychain and drafting works today.
4. Connect Instagram with a token if he wants his whole Instagram archive listed.

After that, the daily loop is: export from Resolve with an .srt next to the .mp4, drop it on Reelay,
Repurpose, review, Publish, and Prepare to Publish for TikTok and Instagram.
