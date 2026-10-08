# Reelay Help

Reelay moves a creator's short videos between platforms. It has two doors:

- **Videos come in** on the **Download** page, from TikTok, Instagram or YouTube.
- **Videos go out** through the accounts you connect on the **Accounts** page.

In between, every video becomes a **ticket** on the **Board**. A ticket holds everything needed to post the video anywhere: a caption for each platform, hashtags, a cover, a transcript and where the video came from. Reelay tells you whether a ticket is ready to go.

Hover over almost anything to see what it does. You can turn those tips off in **Settings → Show helpful tips**.

---

## Getting started

1. **Pick a profile.** The name at the top of the sidebar is the current profile. Each creator or channel gets their own profile, with its own videos, board and accounts. Click it to switch profiles or make a new one.
2. **Bring videos in.** On **Download**, paste a profile link and click **List Videos**. Tick the ones you want, then click **Download**.
3. **Write the posts.** On **Board**, click a video to open its ticket. Fill in the captions, then check the readiness list at the top.
4. **Post.** Copy each caption into the platform's app, or connect YouTube under **Accounts** and let Reelay schedule it for you.

---

## Bringing videos in (Download)

Only import media you own or have permission to manage.

- **Saved sources** are buttons for links you use often. Click **List Videos** on one to see its videos. To add one, paste a link and click **Save This Link…**. Use the **⋯** menu on a source to rename it, mark it as one of your own accounts, or remove it.
- **Connected YouTube channels** (from Accounts) appear as sources automatically and don't need a link. Private and scheduled videos are shown but left unticked.
- **Pasting a link:** paste a profile link, or several video links separated by spaces, then click **List Videos**. Nothing is downloaded until you click **Download**.
- **Choosing videos:** tick the boxes, use **Choose Videos ▾** (all, the newest few, or what your search shows), or press **Space** on highlighted rows.
- **Search** matches captions, the names you've given videos, and file names.
- **Use browser sign-in** borrows your sign-in from a web browser. Instagram usually needs it, as do your own private YouTube videos. Reelay never sees your password.
- **TikTok** downloads are the clean original without the watermark, with the creator's own audio.

Every imported video records where it came from: the platform, the original link, when it was imported, and the permission you have to use it.

### Naming files

**Name files** chooses how downloaded files are named, and shows an example right next to it. Common choices:

- *Platform, date and video ID* (the default), such as `TikTok_20261002_7692175258450677023.mp4`
- *Date and your name*, such as `20261002_letting-go.mp4`

"Your name" is a short name you give a video: double-click the **YOUR NAME** cell, type it in the side panel, or use **Suggest Names ▾** to fill names from the captions.

**Rename Files…** renames videos you've already downloaded to match, and shows every change before anything happens.

For full control, choose **Custom pattern…** and combine pieces such as `{date}`, `{custom}`, `{platform}`, `{id}`, `{title}` and `{uploader}`. Hover the pattern box to see them all.

---

## Tickets (Board)

The Board has four columns: **Backlog → Staging → Scheduled → Published**. Drag cards between them.

Click a card to open its ticket:

- **Ready check.** The top of the ticket lists each platform the video is going to, with a ✓ or a ⚠. The badge says **READY** when at least one of those platforms is ready to go, and **NOT READY** when none are. Platforms Reelay doesn't post to automatically are marked *posted by hand* and never block.
- **Captions.** There's one box per platform, each with a **Copy** button and a live count against that platform's limit. Red means the text is too long. Reelay never cuts your text short.
  - An empty TikTok, Instagram or Facebook box copies the YouTube description instead.
  - An empty Bluesky or Threads box copies the title, plus the description if it fits.
- **Details.** The transcript, cover image, content type, topic, keywords, creator, original publish date, and where the video came from.
- **History.** Every saved change (what changed, when, and whether you, the app or an AI agent made it), newest first.
- **Where it's been posted.** One list per platform of every post, automatic or by hand. After you post by hand, click **Mark as Posted** so the ticket knows.

### Filling tickets quickly

- **Fill Empty from Original Post** (above the captions) fills a ticket's empty boxes from the original caption: the title, description, tags, every platform's caption and the hashtags. Each one stays within that platform's limits. Anything you've already written stays as it is, and nothing is shortened: if the caption is too long for a platform, that box stays empty for you.
- **Get from Original Post** (next to Transcript) uses the original post's captions as the transcript, when the platform has them. TikTok and YouTube often do.
- **Import Sheet…** (top of the Board) fills many tickets from a spreadsheet: a CSV file, or a Google Sheets link shared as "Anyone with the link".
  - **One row per video.** Rows are matched to videos by the video's link, its ID, the file name or your name for it.
  - **Columns map automatically** by their headers (for example *YouTube Title*, *TikTok Caption*, *Hashtags*, *Topic*), and you can change any of them.
  - **Preview first.** You see every row before anything changes.
  - **Safe by default.** Empty cells never erase anything, only empty fields are filled unless you tick *Replace what's already filled in*, and rows with a problem (like a caption over a limit) are skipped and explained.
  - **All at once.** The import lands in one go, or not at all.

### Working on many tickets

- **⌘-click or Shift-click** cards in a column to select several. The side panel then sets the same permission, type, topic, keywords, creator or hashtags on all of them at once, fills them from their original posts, gets their transcripts, or archives them.
- **Right-click a card** for the same actions on one card or the selection.
- **Show: …** (top of the Board) narrows the Board to tickets that are ready, not ready, or blocked on a particular platform. Each column shows how many of its cards are visible ("3 of 12").
- **Archive** hides videos from the Board and the Download page without deleting anything. **Archived…** brings them back.
- **Change History…** lists every saved change. Highlight one and click **Restore the Earlier Value** to undo it. The undo is saved as a new change, so nothing is ever lost.

### Scheduling

Tick the platforms under **Post automatically to**, choose who can see it and when, then click **Schedule**. If a ticked platform isn't ready, Reelay shows the problems and asks before going ahead. Copying and pasting is never blocked.

- **YouTube** and **Facebook** receive the video right away and publish it at the time you set, even if Reelay is closed.
- **Bluesky** and **Threads** are posted by Reelay at the set time, so Reelay must be running then.

**Check YouTube for Updates** moves cards to Published once YouTube shows them as live.

---

## Connecting accounts (Accounts)

Each platform has different rules for apps like Reelay:

- **YouTube:** Google requires a free Google Cloud project that you own. Click **Set Up YouTube…** for a five-step guide (about five minutes, once). After that, connecting a channel is just **Sign in with Google**.
  - While your project is in *Testing*, Google signs you out every 7 days.
  - Uploads stay private until the project passes YouTube's free API compliance audit.
- **Bluesky:** connect with an **App Password**, a separate password just for Reelay. Bluesky doesn't offer a sign-in page for apps.
- **Threads** and **Facebook:** these need a Meta developer app that you own, and a token from it. **Connect…** walks you through it.
- **TikTok:** Reelay can't post for you. TikTok only allows automatic posting from developer apps it has approved; without approval, apps can only send private posts or drafts. Copy the caption and post in the TikTok app.
- **Instagram:** Reelay can't post for you yet. Copy the caption and post in the Instagram app.

Automatic posting to Bluesky, Threads and Facebook is off until you turn it on in **Settings**.

Passwords and tokens are kept in your Mac's Keychain (or Windows Credential Manager), never in files.

---

## Profiles

- Every profile has its own download folder, board, sources and accounts.
- Switching profiles only changes what you see. Nothing is moved or deleted.
- **My Channel** is the starting profile. It stays empty until you add sources or connect accounts.

---

## AI agents (advanced)

Reelay can let AI assistants on this Mac read your board, edit tickets and schedule posts. It only listens on this Mac, and every request needs the access key from **Settings → AI agents**. If a ticket isn't ready, scheduling through an agent is refused with a list of problems, unless the agent explicitly asks to go ahead anyway.

---

## Troubleshooting

- **"This account is private" or Instagram won't list anything:** set **Use browser sign-in** to a browser where you're signed in, then try again.
- **YouTube says you're signed out:** click **Sign in with Google** again. In Testing mode, this happens weekly.
- **Scheduled YouTube videos stay private:** your Google project hasn't passed YouTube's audit yet.
- **Something went wrong:** **Settings → Open Logs** has the details to share.

## Where things are kept

- **Videos:** the folder shown next to **Save videos to** on the Download page.
- **Database and caches:** **Settings → Open Data Folder**.
- **Passwords and tokens:** your system keychain.
