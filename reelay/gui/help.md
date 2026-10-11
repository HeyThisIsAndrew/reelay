# Reelay Help

Reelay moves a creator's short videos between platforms. It has two doors:

- **Videos come in** on the **Download** page, from TikTok, Instagram or YouTube.
- **Videos go out** through the accounts you connect on the **Accounts** page.

In between, every video becomes a **ticket** on the **Board**. A ticket holds everything needed to post the video anywhere: a caption for each platform, hashtags, a cover, a transcript and where the video came from. Reelay tells you whether a ticket is ready to go.

Hover over almost anything to see what it does. You can turn those tips off in **Settings → Show helpful tips**.

---

## Getting started

1. **Pick a profile.** The name at the top of the sidebar is the current profile. Each creator or channel gets their own profile, with its own videos, board and accounts. Click it to switch profiles or make a new one.
2. **Bring videos in.** On **Library**, paste a profile link and click **List Videos**. Nothing is selected at first: click **Select All**, or tick the ones you want, then click **Download**.
3. **Write the posts.** On **Board**, click a video to open its ticket. Fill in the captions, then check the readiness list at the top.
4. **Post.** Copy each caption into the platform's app, or connect YouTube under **Accounts** and let Reelay schedule it for you.

---

## Bringing videos in (Library)

Only import media you own or have permission to manage.

- **Grid or list.** Videos show as cards with big thumbnails (the grid), or as rows in a dense list for bulk work like renaming. Switch with the two buttons at the top right; Reelay remembers your choice.
  - **Badges on a card:** what kind of video it is (*Short · 1080p*, *Long-form · 4K*) and its length. Vertical videos sit on a soft, blurred copy of themselves, so Shorts and long-form are easy to tell apart.
  - **The circle at the top right** ticks a video for downloading (or press **Space**); a green check means it's downloaded. While downloading, a line along the bottom of the thumbnail shows progress.
  - **Hover-scrub:** move the mouse across a downloaded video's thumbnail to flip through it. The frames are made in the background the first time (about a second) and kept.
- **Filter** (top right) holds what to show (all, not downloaded yet, downloaded), the hashtag and brand or product filters, and the sort order. The button shows how many filters are on.
- **Download settings** (the gear) hold where videos are saved, the quality, browser sign-in, and how files are named.
- **Tile size:** the slider at the top (or ⌘+ and ⌘−) makes the thumbnails smaller or bigger: Small, Medium, Large or XL.
- **Pages:** big libraries show a page at a time. At the bottom, choose 24, 48, 96 or All per page and step through with the arrows. A new search or filter starts at page 1; **Select All** still selects every matching video, on every page.
- **Open Video List** (bottom, or **File → Downloaded Videos…**) shows every downloaded video in a table you can search and sort, with **Show in Finder**, **Copy Video Link** and **Play Preview**, and export it as a CSV spreadsheet or as the Markdown manifest.
- **Snapchat:** paste a Snapchat Spotlight link to download it like any other video.

- **Saved sources** are buttons for links you use often. Click **List Videos** on one to see its videos. To add one, paste a link and click **Save This Link…**. Use the **⋯** menu on a source to rename it, mark it as one of your own accounts, or remove it.
- **Connected YouTube channels** (from Accounts) appear as sources automatically and don't need a link. Private and scheduled videos are shown but left unticked.
- **Pasting a link:** paste a profile link, or several video links separated by spaces, then click **List Videos**. Nothing is downloaded until you click **Download**.
- **Choosing videos:** newly listed videos start unselected, so nothing downloads by surprise. Your choices are kept when the list refreshes.
  - **Select All** selects every video shown (if you've searched, only the search results).
  - **Unselect All** clears the selection.
  - **More ▾** selects only the newest few, only what your search shows, or the highlighted rows.
  - You can also tick boxes one by one, or press **Space** on highlighted rows.
  - The count at the bottom shows how many are selected.
- **Search** matches captions (hashtags included), the names you've given videos, and file names.
- **Hashtags as categories.** The menu next to the search (**All hashtags ▾**) lists every hashtag in the list, with how many videos have it. Pick one to show only those videos. You can also click a hashtag in the side panel. **Select All** then selects just what's shown.
- **Tagged products (TikTok Shop).** Reelay looks up which products are tagged on each TikTok video, in the background. **All products ▾** (next to the hashtags) lists them with how many videos have each; pick one to see every video for that product, even when the captions and hashtags are worded differently. Search finds product names too: typing a brand finds every video tagged with its products. Products also show in the side panel (click one to filter) and on the ticket, under *Where it came from*.
  - YouTube and Instagram product tags aren't read yet. YouTube's API doesn't include them, and Instagram's need the account owner's Meta app.
- **The selection strip** right above the videos always says what will download ("Nothing selected yet" or "12 selected"), and holds **Select All**, **Unselect All** and **More ▾** (which also has renaming and Download Settings).
- **Use browser sign-in** borrows your sign-in from a web browser. Instagram usually needs it, as do your own private YouTube videos. Reelay never sees your password.
- **TikTok** downloads are the clean original without the watermark, with the creator's own audio.

Every imported video records where it came from: the platform, the original link, when it was imported, and the permission you have to use it.

### Naming files

**Name files** (in Download Settings, the gear) chooses how downloaded files are named, and shows an example right next to it. Common choices:

- *Platform, date and video ID* (the default), such as `TikTok_20261002_7692175258450677023.mp4`
- *Date and your name*, such as `20261002_letting-go.mp4`

"Your name" is a short name you give a video: double-click the **YOUR NAME** cell, type it in the side panel, or use **Suggest Names ▾** to fill names from the captions.

**Rename Files…** renames videos you've already downloaded to match, and shows every change before anything happens.

For full control, choose **Custom pattern…** and combine pieces such as `{date}`, `{custom}`, `{platform}`, `{id}`, `{title}` and `{uploader}`. Hover the pattern box to see them all.

---

## Tickets (Board)

The Board starts with four columns: **Backlog → Staging → Scheduled → Published**. Drag cards between them.

**Make the columns your own.** Click **Edit Columns…** (or right-click a column's name) to rename, add, remove or reorder columns. For example, you could have just *Ready* and *Posted*, or six columns of your own. Reelay's four steps always live in some column: removing a column hands its steps (and cards) to the neighbouring column, so cards still move by themselves when they're scheduled or posted. Columns you add are your own steps that you drag cards in and out of. Each profile has its own columns, and many columns scroll sideways.

Click a card to open its ticket:

- **Ready check.** The top of the ticket lists each platform the video is going to, with a ✓ or a ⚠. The badge says **READY** when at least one of those platforms is ready to go, and **NOT READY** when none are. Platforms Reelay doesn't post to automatically are marked *posted by hand* and never block.
- **Platforms.** One row per platform (YouTube, TikTok, Instagram, Bluesky, Threads, Facebook), each with its logo and one choice:
  - **Not posting:** it isn't going there.
  - **Preparing:** you'll post it there; it isn't scheduled yet. Its logo shows faded on the card.
  - **Scheduled:** you scheduled it in that platform's own app. Reelay asks when it goes live.
  - **Posted:** it's live.

  **Publish…** (next to TikTok, Instagram and any platform Reelay doesn't post to for you) starts **Prepare to Publish**; see below.

  The card moves by itself. When every platform it's going to is scheduled or live, it moves to **Scheduled**; when every one is live (a scheduled time passing counts), it moves to **Published**. Unticking a box moves it back. Platforms Reelay posts itself (like YouTube) update on their own.
- **On the Board,** each card shows a **coverage map**: every platform's logo, always in the same order, with a badge:
  - green ✓ **published**, blue clock **scheduled**, amber dot **ready to publish**;
  - red ! **needs work** (a missing caption, a missing file, a failed or interrupted post);
  - a faint logo with no badge means it isn't going there.

  The line under the logos says what needs doing. Hover a card for each platform's details.
- **Captions.** Each platform's copy is in its own section, with its logo. Click a section's name to open or close it; the right side shows its length against the limit, "empty", or "⚠ too long". Reelay remembers which sections you keep open. Each box has a **Copy** button and a live count. Red means the text is too long, and Reelay never cuts your text short.
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

### Prepare to Publish (TikTok, Instagram)

TikTok and Instagram don't let apps post a creator's videos with their sounds, tags and shop links, so you post in their own uploader, and Reelay does everything around it. Click **Publish…** on the platform's row:

1. Reelay checks the video file and the caption (length, hashtag limits).
2. **Copy Caption, Open TikTok & Show Video** copies the caption (with your hashtag block), opens the upload page in your browser, and shows the video file selected in Finder, ready to drag in.
3. Post (or schedule) it there, then paste the post's link back into Reelay (Share → Copy link) and click **Record as Published**. Reelay saves the link and the time, and the card moves on.

### Writing help (Assist ▾)

Every caption box has an **Assist ▾** menu. It runs on this computer: no paid AI service, nothing sent anywhere.

- **Check Spelling & Grammar** lists possible mistakes; click a suggestion to fix it. Hashtags, @handles and links are left alone. **Add to Dictionary** teaches it your slang and names. Full grammar checking needs the free LanguageTool add-on (see Settings → Local AI helper).
- **Rewrite** suggests a shorter, friendlier or punchier version, or one with a call to action, using a free local model through Ollama (Settings → Local AI helper). You always see the suggestion first and can edit it.
- **Brand voice** (Settings → Brand voice) tells the helper how you sound: tone, words to avoid, default hashtags and a few example captions. Rewrites follow it, add a "In our brand voice" option, and flag any avoided word. **Fill Empty from Original Post** uses the default hashtags when the original has none.
- **Get SEO Suggestions** (in the YouTube section) shows what people type into YouTube's search box for your topic, scores the title and tags with tips, and adds the phrases you tick as tags.

### Video tools

The ticket's **VIDEO** section edits the file on this computer:

- **Remove Sound** makes a silent copy, e.g. to drop a TikTok trending sound before posting elsewhere.
- **Replace Sound…** adds music you have the rights to (royalty-free or your own), looped to the video's length, with the original sound kept quietly underneath if you like.
- **Add Captions…** draws captions onto the video from a caption file (.srt, .vtt or .ass from CapCut, Premiere or YouTube Studio), or makes them automatically if the free faster-whisper add-on is installed.

Edits never change the original download: they make one edited copy, and Reelay posts that copy until you click **Use Original**.

### Working on many tickets

- **⌘-click or Shift-click** cards in a column to select several. The side panel then sets the same permission, type, topic, keywords, creator or hashtags on all of them at once, fills them from their original posts, gets their transcripts, or archives them.
- **Right-click a card** for the same actions on one card or the selection.
- **Search** (under the Board's title) finds videos by any words in their title, original caption, any platform's copy, topic, keywords, notes, **and anything said in the video** (its transcript). Words match as you type ("sungl" finds "sunglasses"); put "quotes" around an exact phrase. A card found by what was said shows the quote. Get transcripts with **Get from Original Post**, or type or paste them into Details.
- **All hashtags ▾**, **All brands ▾** and **All products ▾** narrow the Board to a hashtag, or to a TikTok Shop brand or product tagged in the video.
- **Show: …** narrows the Board to tickets that are ready, not ready, or blocked on a particular platform. Each column shows how many of its cards are visible ("3 of 12").
- **Archive** hides videos from the Board and the Library without deleting anything. **Archived…** (in the Board's ⋯ menu) brings them back.
- **Change History…** lists every saved change. Highlight one and click **Restore the Earlier Value** to undo it. The undo is saved as a new change, so nothing is ever lost.

### Scheduling

Tick the platforms under **Post automatically to**, choose who can see it and when, then click **Schedule**. If a ticked platform isn't ready, Reelay shows the problems and asks before going ahead. Copying and pasting is never blocked.

- **YouTube** and **Facebook** receive the video right away and publish it at the time you set, even if Reelay is closed.
- **Bluesky** and **Threads** are posted by Reelay at the set time, so Reelay must be running then.

**Check YouTube for Updates** moves cards to Published once YouTube shows them as live.

**If Reelay is interrupted.** Every send is written down before it starts, so nothing vanishes when Reelay is closed, crashes, loses power or the Mac sleeps:

- Closing Reelay during a YouTube upload pauses it; it carries on next time Reelay opens.
- If Reelay stopped in the middle of sending and can't know whether the post went out, it doesn't guess, because guessing could post twice. The card shows **⚠ Interrupted**, and Reelay asks when it opens: **Try Again**, **It Was Posted** (paste the link) or **Cancel It**. Bluesky and Threads posts carry on by themselves: Reelay can check whether they went out.
- A post whose time passed while Reelay was closed (or the Mac was asleep) would now go out late, so Reelay asks first: **Post Now**, **It Was Posted** or **Cancel It**. YouTube and Facebook posts that those platforms are already holding still go out on time.

---

## Automations

**Automations** (in the sidebar) are rules that work your tickets for you, for example: *when a video is added to the board, if its caption mentions "sunglasses", fill the TikTok caption from a template, add #summer, and schedule it for next Tuesday at 9:00.*

- **When:** a video is added to the board, or moved to a column.
- **If:** words in the caption or copy, the title, a hashtag, a tagged product or brand, the length, or where it came from (all of them, or any).
- **Then:** fill an empty field from a template (placeholders: {hook} {caption} {hashtags} {title} {topic} {creator}), add hashtags, choose platforms, set a ticket field, move to a column, or schedule for the next weekday at a time.

Rules are careful:

- Each rule acts on a video once, so it never undoes your edits.
- A new rule never touches videos already on the board. **Run on Existing Videos…** does that on purpose, after showing how many match.
- Templates only fill empty fields.
- Scheduling only happens if the ticket is ready.
- **Activity** lists everything rules did.

**Quick-start recipes** (on the empty Automations page, or **Recipes ▾**) add ready-made rules in one click: *Cross-post short-form* (TikTok and Instagram videos get YouTube Shorts and Threads captions with a link-in-bio line), *Auto-hashtag by topic* (fitness, tech and recipe hashtag bundles; change the words to suit you), and *Ready queue* (tickets moved to Staging are scheduled for the next Tuesday at 9:00 if they're ready).

**Test this rule:** at the bottom of a rule, pick any video on the board to see whether the rule would run for it, which conditions it meets, and exactly what each action would write. Nothing changes until you save the rule and it runs.

---

## Connecting accounts (Accounts)

Each platform has different rules for apps like Reelay:

- **Sign in with…** buttons appear for platforms where Reelay's own sign-in is set up: you just sign in with your account, and Reelay is allowed to post and read your analytics. Where they don't appear, use the steps below.
- **YouTube:** without Reelay's sign-in, Google requires a free Google Cloud project that you own. Click **Set Up YouTube…** for a five-step guide (about five minutes, once). After that, connecting a channel is just **Sign in with Google**.
  - While your project is in *Testing*, Google signs you out every 7 days.
  - Uploads stay private until the project passes YouTube's free API compliance audit.
- **Bluesky:** connect with an **App Password**, a separate password just for Reelay. Bluesky doesn't offer a sign-in page for apps.
- **Threads** and **Facebook:** these need a Meta developer app that you own, and a token from it. **Connect…** walks you through it.
- **TikTok:** Reelay can't post for you. TikTok only allows automatic posting from developer apps it has approved; without approval, apps can only send private posts or drafts. Copy the caption and post in the TikTok app.
- **Instagram:** Reelay can't post for you yet. Copy the caption and post in the Instagram app.

Automatic posting to Bluesky, Threads and Facebook is off until you turn it on in **Settings**.

Passwords and tokens are kept in your Mac's Keychain (or Windows Credential Manager), never in files.

---

## Sharing the database with your team

**Settings → Data → Move Database…** moves Reelay's database into a folder you choose: for example a shared Google Drive, Dropbox or iCloud Drive folder, so teammates can open the same board from their computers (**Open Shared Database…**).

- **One person edits at a time.** Whoever opens it first can edit. Anyone else opens it **view only**: they see every change as it syncs, but can't edit, and nothing is posted from their computer, so a video is never sent twice. When the editor quits, **Edit Now** appears.
- **Take Over** is there for when someone forgot to quit. Their Reelay stops and closes, so you don't overwrite each other's work.
- Sign-ins and passwords stay in each person's own keychain: sharing the database never shares them.
- The old copy stays where it was as a backup. **Use This Computer's Database** switches back.

---

## Profiles

- Every profile has its own download folder, board, sources and accounts.
- Switching profiles only changes what you see. Nothing is moved or deleted.
- **My Channel** is the starting profile. It stays empty until you add sources or connect accounts.
- **Branding:** **Profile → Edit Profile & Branding…** (or **Settings → Profile**) gives a profile its name, a tagline, a logo and brand colours. The logo becomes its icon in the sidebar and the Profile menu, and a thin bar in the brand colour runs across the top of the sidebar in every theme, so you always know whose account you're working in. **Match Logo** picks colours from the logo.
- **Custom theme:** **Settings → Appearance → Custom** shows Reelay in the current profile's brand colours, and switching profiles switches the colours. Reelay keeps text readable whatever colours you pick, and warns if buttons would be hard to see. Light, Dark and Black look the same for every profile.
- **The panel on the right** (a video's details in the Library, its ticket on the Board) only opens when something is selected, so the list and board get the whole window otherwise. Close it with **✕** or **Esc**; if you typed copy you haven't saved, Reelay asks first. Drag its edge to resize it; Reelay remembers the width.

---

## Watching videos and the Command Center

- **The player.** Selecting a downloaded video plays it at the top of the right-hand panel: press play (or **Space**), click anywhere on the bar to jump, hover the bar to see the time. **J** and **L** skip 10 seconds, the arrow keys 5, **M** mutes. The buttons on the right change the speed, loop, open it in its own window, or open it in QuickTime.
- **If a video won't play** (often 4K YouTube videos, which use formats the built-in player can't decode), click **Make a Preview Copy**: Reelay makes a small copy that plays, in the background. The original file is never changed, and copies are cleaned up automatically.
- **On the Board** the panel is your Command Center: the video on top, then the readiness ring (how many of the platforms it's going to are ready), the platforms, the captions, and **Schedule** pinned at the bottom with one line saying exactly what will happen.
- **Card marks on the Board:** ⚡ an Automation acted on the video, ⏳ an AI assistant asked to change it and it's waiting for your approval, ✦ the caption was last edited by an AI assistant. Hover a card for details. **Filter → Cards** switches between large cards with thumbnails and compact ones.
- **Long videos.** #Shorts is only added for Shorts (vertical, 3 minutes or less); longer videos go to YouTube as regular videos. Bluesky (3 minutes) and Threads (5 minutes) say so in the readiness list when a video is too long for them.
- **The sidebar** can shrink to icons (the button at its bottom right), and *Up next* shows the next post going out for this profile.

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

- **Videos:** the folder shown under **Where videos go** in Download Settings (the gear on the Library).
- **Database:** shown at the top of **Settings → Data** (it can live in a shared folder; see above).
- **Caches:** **Settings → Open Data Folder**.
- **Passwords and tokens:** your system keychain.
