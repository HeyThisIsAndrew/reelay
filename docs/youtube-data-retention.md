# YouTube API data: what Reelay stores and how it stays under 30 days

For the YouTube API compliance audit. Code: `reelay/youtube_retention.py`. Tests:
`tests/test_youtube_retention.py`.

## The rules (YouTube API Services Developer Policies, read 2026-11-13)

- **III.E.4.c**: Authorized Data other than statistics may be stored "for no longer than 30 calendar
  days. After 30 calendar days, the API Client must either delete or refresh the stored data."
- **III.E.4.b**: statistics (such as view counts) may be kept longer, but every 30 days the client must
  check that it's still authorized, and that the video hasn't been deleted.
- **III.E.4.d**: Non-Authorized Data also has a 30-day limit. Reelay requests nothing without the
  creator's sign-in, so it has none.
- **III.D.2.c**: when access is revoked in the app, delete that consent's Authorized Data within 7 days.
  When it's revoked in Google's settings, delete within 30 days, and delete the data of users whose
  tokens can't be refreshed.
- **III.E.4.a**: tokens may be kept as long as needed. Reelay's are in the macOS Keychain.

Reelay applies one rule to all of it: anything fetched through the YouTube Data API is refreshed or
cleared within 30 days, statistics included.

## What counts as stored YouTube API data

| Stored | Where | Counts? |
| --- | --- | --- |
| Connected channel's name, handle, picture link | `accounts` row (`display_name`, `handle`, `avatar_url`) | Yes. Stamped `api_fetched_at`. |
| Channel ID | `accounts.external_id`, keychain key | It identifies the connection. It's kept while the channel is connected and deleted on Disconnect. |
| Videos listed through the Data API: title, description, publish date, length, view/like/comment counts, tags, channel name, thumbnail link, privacy | `videos.data` JSON (`sources.API_VIDEO_FIELDS`) | Yes. Each record is stamped `api_fetched_at`, `api_channel_id`, and `api_fields` (the fields that still hold API values). |
| The ticket's creator and original publish date, copied from the API at import | `videos.creator`, `videos.original_publish_date` | Yes, while they still equal the API value. Once the creator edits them, they're the creator's. |
| Cover image cached from the API's thumbnail link | `<data>/cache/covers/<key>.img` | Yes. Deleted on every refresh or clear. |
| A downloaded video's metadata sidecar and `manifest.md` | the creator's library folder | Rewritten whenever the record is refreshed or cleared. |
| The same fields when yt-dlp read them, or the creator typed them | `videos.data` | No. They aren't from the API. A field yt-dlp fills is removed from `api_fields`. |
| Channel Schedule list (Accounts) | shown only, never saved | No. |
| "Check YouTube for updates" (scheduled uploads going live) | `dispatches.status`/`error` | No. That's Reelay's own record of its own uploads. Video IDs Reelay created are kept as the upload's receipt. |
| Post drafts (title, description, tags) | `posts` | No. That's copy the creator writes to publish, even if it started from the old title. |

## What the app does

`YouTubeRetention.run_once` runs at launch, again whenever the connected accounts change, and once a day
while Reelay runs (desktop app and headless server; `Services.start_background`):

1. **Due (27 days or older)**: refresh through the API with the sign-in of the channel the data came from,
   when that channel is still connected in the same profile. Videos are fetched 50 per call
   (`videos.list`, 1 quota unit); channels with `channels.list?mine=true`.
2. **Channel not connected any more** (Disconnect, revoked or expired sign-in, sign-in missing): clear at
   once, whatever the age. A disconnect therefore clears that channel's videos within seconds.
3. **Video gone from YouTube**, or it now belongs to another channel: clear.
4. **Refresh failed for a passing reason** (offline, quota): keep, try again the next day. At 29 days old
   it's cleared instead, because the next daily check would be past 30.

Clearing removes only the API fields and the cached cover. It keeps the creator's files, custom names,
notes, ticket fields they changed, and post drafts. A cleared channel shows as "YouTube channel" until
it's reconnected. Records written before this version are migrated (schema 12) with the earliest time
the data could be from: when the channel was connected, or when the video was first imported. That
means they're refreshed early, never late.

One limit: Reelay is a desktop app, so it can only check while it's running. If it hasn't been opened for
30 days, the first check runs as soon as it's next opened.
