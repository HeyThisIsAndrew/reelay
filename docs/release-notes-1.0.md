# Reelay 1.0.0

Get everything ready to publish everywhere. Reelay 1.0 turns one finished video into a complete,
platform-ready publishing package.

## New

- **Add a finished video.** Drop a video file anywhere on the window, or use **Add Video Files…** in the
  Library. It becomes a ticket on the Board, opened and ready to draft. A caption file next to it with the same
  name (.srt, .vtt or .txt) becomes the transcript; with the optional faster-whisper add-on, speech is
  transcribed on your Mac.
- **Repurpose drafts from what you actually say.** The transcript is the source of truth for every draft.
  When a ticket has none, Repurpose fetches the original post's captions first. Drafts are paste-ready: first
  person, no placeholders, no generic AI phrasing, within every platform's limits.
- **Voice profiles.** Tone, words to avoid, emoji level, default hashtags (used only when they fit the
  video), hashtag style, call to action, and a footer added exactly as written to every YouTube description.
  Banned phrases are rewritten once and otherwise the draft isn't used. Import and export voices as files.
- **Batch Repurpose** across a set of tickets, with each ticket's own status, the same transcript fetch and the
  same voice.
- **Your whole Instagram profile** lists through Meta's official Instagram API once you connect it under
  Accounts → Instagram → Advanced Setup (a Professional account and a token from your own Meta app).
- **MCP.** Pair Claude Code, Claude Desktop or Cursor under Settings → AI agents (advanced) → Pair AI Client…
  Paired clients get their own key that works only for MCP; consequential actions wait for your approval.
- **One-command build:** `./scripts/build_local.sh` builds, backs up your database, installs to
  /Applications and self-tests.

## Fixed

- Repurpose timed out on most drafts: a full draft takes about a minute and the limit was 60 seconds. It's
  180 seconds now.
- Listing an Instagram profile showed yt-dlp's raw "please report this issue" error. Reelay now explains what
  works (the official API for your own profile, reel links for any other), and never shows yt-dlp's report
  text.
- The pre-install database backup failed on a cleanly closed database.
- The MCP server reported version 0.5.0.

## Known limits

- YouTube uploads from your own Google project stay private until that project passes YouTube's API
  compliance audit, and Testing-mode sign-ins expire every 7 days.
- Bluesky, Threads and Facebook post automatically only after you connect them and turn automatic posting on.
  Threads also needs a public URL for the video.
- TikTok and Instagram are posted by hand with Prepare to Publish until their posting APIs approve Reelay.
- One-click sign-in for Google, TikTok and Meta waits on each platform's app review.
- Builds are signed but not notarized yet, so other Macs show a warning the first time.

Database schema 11 (voice profiles). Your database is backed up and upgraded automatically on first launch.
