"""What the Help Center says: the Get Started tour and the topic guides, as data.

Every guide follows one pattern: what you're doing (one sentence), what to do (a few numbered steps), what to
expect, then optional details and related guides. A step can point at a control in the guide's picture: the
picture is a real screenshot (help_shots/, made by scripts/capture_screens.py --help-shots) and the control's
position comes from marks.json, measured from the real widget, so the numbered marker lands on it.

Only describe what Reelay does. Text may use **bold** for the names of buttons and menus.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Visual:
    """A real screenshot (`shot`, from help_shots/) or a small drawn picture ("platforms", "storage").
    `crop` shows just part of the window, enlarged: (x, y, width, height) as fractions of the screenshot."""
    shot: str
    alt: str = ""
    crop: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Step:
    text: str
    mark: str = ""          # a mark in the guide's screenshot; it gets this step's number


@dataclass(frozen=True)
class Detail:
    title: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class TourStep:
    key: str
    title: str
    goal: str
    visual: Visual
    steps: tuple[Step, ...]
    next: str
    go: str = ""            # a page Reelay can take you to ("download", "board"…)
    go_label: str = ""
    note: str = ""
    short: str = ""         # its name on the tour's step buttons


@dataclass(frozen=True)
class Guide:
    key: str
    title: str
    doing: str
    visual: Visual | None
    steps: tuple[Step, ...]
    expect: str
    details: tuple[Detail, ...] = ()
    related: tuple[str, ...] = ()
    keywords: str = ""
    go: str = ""
    go_label: str = ""
    note: str = ""


@dataclass(frozen=True)
class Category:
    key: str
    title: str
    blurb: str
    icon: str
    guides: tuple[Guide, ...] = field(default=())


OWN_MEDIA = "Only import media you own or have permission to manage."

# ====================================================================== the Get Started tour
TOUR: tuple[TourStep, ...] = (
    TourStep(
        "profiles", "Pick a profile",
        "A profile is one creator or channel, with its own videos, Board, sources and accounts.",
        Visual("profile", "The profile menu open at the top of the sidebar, listing two profiles, New Profile… "
                          "and Edit Profile & Branding…"),
        (Step("Click the name at the top of the sidebar to see your profiles and switch between them.", "profile"),
         Step("Choose **New Profile…** to add another creator or channel.", "new"),
         Step("**Edit Profile & Branding…** sets its name, tagline, logo and colours.", "brand")),
        "Switching only changes what you see; nothing is moved or deleted. Next, bring some videos in.",
        short="Profiles"),
    TourStep(
        "library", "Bring videos in",
        "List the videos from a profile or link, then choose which ones to download.",
        Visual("library", "The Library: Download Sources and the link field at the top, platform tabs, the "
                          "selection strip with Select All, a grid of videos and the Download button."),
        (Step("Paste a profile or video link, or pick a saved account in **Download Sources**.", "link"),
         Step("Click **List Videos**. You see every video first; nothing downloads yet.", "list"),
         Step("Tick the videos you want, or click **Select All**.", "select"),
         Step("Click **Download**. Each video becomes a ticket on the Board.", "download")),
        "Downloaded videos wait in the Board's first column, ready to prepare.",
        go="download", go_label="Open the Library", note=OWN_MEDIA, short="Videos in"),
    TourStep(
        "prepare", "Prepare a video",
        "Each ticket holds a caption for every platform, hashtags, a cover and a transcript.",
        Visual("ticket-copy", "A ticket open beside the Board, showing Fill Empty from Original Post and the "
                              "YouTube caption section with its Copy buttons."),
        (Step("On the **Board**, click a card to open its ticket on the right.", "card"),
         Step("**Fill Empty from Original Post** writes the empty captions from the original post.", "fill"),
         Step("Edit each platform's caption in its own section. The count turns red if it's too long.", "caption"),
         Step("**Copy** puts that caption on the clipboard, ready to paste.", "copy")),
        "The transcript, cover and topic are further down, under Details and provenance.",
        go="board", go_label="Open the Board", short="Prepare"),
    TourStep(
        "readiness", "Check readiness",
        "See at a glance which platforms are ready and what still needs work.",
        Visual("ticket", "A ticket's readiness: a ring reading 4/4 above one row per platform, beside the "
                         "selected card on the Board."),
        (Step("The ring counts the platforms that are ready. Each row: **✓** ready, **–** you post it by hand, "
              "**!** something to fix, with the note saying what.", "ready"),
         Step("On the card, each logo has a badge: green published, blue scheduled, a plain dot ready, red "
              "needs work. Amber means it's waiting for you to decide.", "card"),
         Step("Hover a row or a card to see every check.")),
        "Fix anything marked !, then choose where the video goes.",
        go="board", go_label="Open the Board", short="Readiness"),
    TourStep(
        "publish", "Publish or schedule",
        "Reelay posts to some platforms for you; for the rest, it sets everything up and you post.",
        Visual("ticket-platforms", "The ticket's Platforms section: YouTube and TikTok set to Preparing, TikTok's "
                                   "Publish… button, and Publish Now at the bottom."),
        (Step("Under **Platforms**, set each place the video is going to **Preparing**.", "youtube"),
         Step("TikTok and Instagram are posted by hand: **Publish…** copies the caption, opens the uploader and "
              "shows the video file.", "publish"),
         Step("For YouTube (and Bluesky, Threads or Facebook once turned on), choose when, then click "
              "**Schedule** or **Publish Now**.", "post")),
        "Cards move to Scheduled and Published by themselves as each platform goes live.",
        go="board", go_label="Open the Board", short="Publish"),
    TourStep(
        "find", "Find your work",
        "Every video keeps its status, its posts and its history, and nothing is ever deleted by archiving.",
        Visual("board-more", "The Board's Published column, the search field, and the ⋯ menu open showing "
                             "Archived Videos…"),
        (Step("**Scheduled** holds videos waiting to go live; **Published**, the ones live everywhere they "
              "were going.", "published"),
         Step("Search finds a video by its title, any caption, or what's said in it.", "search"),
         Step("**⋯ → Archived Videos…** brings back anything you archived.", "archived"),
         Step("In a ticket, **Where it's been posted** lists every post with its link and time.")),
        "That's the whole loop. The Help Center has a guide for each part whenever you need more.",
        go="board", go_label="Open the Board", short="Find"),
)

# ====================================================================== topic guides
LIBRARY = Category("library", "Library & downloads", "Bring videos in, choose what to download, find them again.",
                   "library", (
    Guide("library-add", "Add videos", "Bring in videos from TikTok, Instagram, YouTube or Snapchat.",
          Visual("library", "The Library's link field, Download Sources button and List Videos button."),
          (Step("Paste a profile link, or one or more video links separated by spaces.", "link"),
           Step("Or open **Download Sources** and click **List Videos** on a saved account.", "sources"),
           Step("Click **List Videos**. You see every video before anything downloads.", "list")),
          "The videos appear in the Library unticked, so nothing downloads by surprise.",
          (Detail("Which links work", (
               "TikTok, Instagram and YouTube profile or video links.",
               "Snapchat Spotlight video links. Snapchat doesn't let apps list a profile's videos, so paste "
               "the videos' links.",
               "YouTube channels you connect under Accounts appear in Download Sources by themselves. Their "
               "private and scheduled videos are listed but left unticked.")),
           Detail("Private videos and Instagram", (
               "**Use browser sign-in** (in Download Settings, the gear) borrows your sign-in from a web "
               "browser. Instagram usually needs it, as do your own private YouTube videos.",
               "Reelay never sees your password.")),
           Detail("What Reelay records", (
               "Every imported video keeps where it came from: the platform, the original link, when it was "
               "imported, and the permission you have to use it.",))),
          ("library-sources", "library-select", "trouble"),
          "import list fetch paste link url profile tiktok instagram youtube snapchat spotlight private",
          "download", "Open the Library", OWN_MEDIA),
    Guide("library-sources", "Download Sources", "Keep the accounts you import from one click away.",
          Visual("sources", "The Download Sources window: one section per platform with saved accounts, "
                            "List Videos buttons and a field to add an account."),
          (Step("Click **Download Sources** (left of the link field)."),
           Step("To add an account, type its @username or paste its profile link, then click **Add**."),
           Step("Turn **Mine** on for this profile's own accounts."),
           Step("Click **List Videos** on a source to list its videos in the Library.")),
          "The button shows which source the link field holds. The same sources are on the Accounts page.",
          (Detail("Managing sources", (
               "Use a source's **⋯** menu to rename or remove it.",
               "Snapchat's section takes Spotlight links to list right away.")),),
          ("library-add", "accounts-connect"),
          "source saved account username handle mine add remove rename",
          "download", "Open the Library"),
    Guide("library-select", "Select and download", "Choose exactly which videos to download.",
          Visual("library", "The Library's platform tabs, the selection strip with Select All, and Download."),
          (Step("Pick a platform tab to work on one platform's videos, or stay on **All**.", "tabs"),
           Step("Tick videos one by one (or press **Space**), or click **Select All**.", "select"),
           Step("Click **Download**.", "download")),
          "A line along each thumbnail shows progress; a green check means it's downloaded and on the Board.",
          (Detail("More ways to select", (
               "**Select All** ticks every video shown: only your search, filter or tab, if you've used one. "
               "Videos already downloaded are skipped.",
               "**More ▾** selects only the newest few, only what your search shows, or the highlighted videos.",
               "**Unselect All** clears the selection. The selection strip always says what will download.")),
           Detail("While downloading", (
               "**Stop** finishes the current video and keeps the ones already done.",
               "TikTok downloads are the original file with the creator's own audio.")),
           Detail("Platform tabs", (
               "Tabs appear once the Library holds videos from more than one platform, with each platform's "
               "count. Listing a source opens its platform's tab.",))),
          ("library-views", "board-overview"),
          "select tick choose download stop progress tabs platform all space",
          "download", "Open the Library"),
    Guide("library-views", "Filters and pages", "Find the videos you want in a big library.",
          Visual("library", "The Library's search field, Filter button, tile-size slider, grid/list toggle and "
                            "per-page menu."),
          (Step("Type in search to match captions, hashtags, product names, your names and file names.", "search"),
           Step("**Filter** shows all, not downloaded or downloaded videos, one hashtag, a brand or product, and "
                "the sort order.", "filter"),
           Step("Drag the slider (or press ⌘+ and ⌘−) for smaller or bigger tiles.", "tiles"),
           Step("Switch between the picture grid and a dense list.", "view")),
          "A new search, filter or sort starts at page 1. Choose 24, 48, 96 or All per page at the bottom left.",
          (Detail("Reading a tile", (
               "Badges say what kind of video it is (**Short · 1080p**, **Long-form · 4K**) and its length.",
               "Move the mouse across a downloaded video's thumbnail to flip through it.")),
           Detail("Tagged products (TikTok Shop)", (
               "Reelay looks up which products are tagged on each TikTok video, in the background. Filter "
               "by a brand or product, or type a brand in search.",
               "YouTube and Instagram product tags aren't read yet.")),
           Detail("Pages and selecting", (
               "**Select All** still selects every matching video, on every page.",))),
          ("library-select", "library-files"),
          "search filter sort hashtag brand product grid list tiles size zoom page pages per page",
          "download", "Open the Library"),
    Guide("library-files", "Files and names", "Choose where videos go and how files are named.",
          Visual("library", "The Library's settings gear and Open Video List button."),
          (Step("The gear holds where videos are saved, the quality, browser sign-in and how files are named.",
                "gear"),
           Step("**Open Video List** shows every downloaded video in a table you can search, sort and export.",
                "videolist")),
          "File names follow your naming choice; **Rename Files…** shows every change before anything happens.",
          (Detail("Naming files", (
               "The default is platform, date and video ID, such as TikTok_20261002_7692175258450677023.mp4.",
               "**Custom pattern…** combines {date}, {custom}, {platform}, {id}, {title} and {uploader}.",
               "Your own short name for a video: double-click its **Your name** cell, type it in the side "
               "panel, or use **Suggest Names ▾** to fill names from captions.")),
           Detail("The video list", (
               "**Show in Finder**, **Copy Video Link** and **Play Preview** for any video.",
               "Export the list as a CSV spreadsheet or as a Markdown manifest.",
               "Also on **File → Downloaded Videos…**."))),
          ("library-views", "privacy-local"),
          "folder save where quality naming rename file names manifest csv export finder",
          "download", "Open the Library"),
    Guide("library-player", "Watch a video", "Play a downloaded video right in Reelay.",
          Visual("ticket", "A video's player at the top of the side panel."),
          (Step("Select a downloaded video; it plays at the top of the side panel."),
           Step("Press **Space** to play or pause. **J** and **L** skip 10 seconds, the arrow keys 5, **M** mutes."),
           Step("The buttons beside the bar change the speed, loop, or open it in its own window or QuickTime.")),
          "The side panel only opens when something is selected. Close it with **✕** or **Esc**.",
          (Detail("If a video won't play", (
               "Often a 4K YouTube video. Click **Make a Preview Copy**: Reelay makes a small copy that plays.",
               "The original file is never changed, and preview copies are cleaned up automatically.")),),
          ("library-views",),
          "play watch player video preview speed loop quicktime keyboard shortcuts 4k won't play"),
))

BOARD = Category("board", "Board & preparing posts", "Turn videos into ready-to-post tickets.", "board", (
    Guide("board-overview", "The Board", "Follow every video from downloaded to posted.",
          Visual("board", "The Board's four columns, with search, Filter, refresh and ⋯ along the top."),
          (Step("Downloaded videos start in **Backlog**.", "backlog"),
           Step("Drag a card to **Staging** when you start preparing it.", "staging"),
           Step("Cards move to Scheduled and **Published** by themselves as platforms go live.", "published"),
           Step("**⋯ → Edit Columns…** renames, adds, removes or reorders columns.", "more")),
          "Each profile has its own Board and its own columns.",
          (Detail("Your own columns", (
               "Removing a column hands its cards to the neighbouring column, so cards still move by "
               "themselves when they're scheduled or posted.",
               "Columns you add are steps you drag cards in and out of.")),
           Detail("Marks on cards", (
               "⚡ an Automation acted on the video, ⏳ an AI assistant's change is waiting for your approval "
               "(review it under **Approvals** at the top of the Board), ✦ an AI assistant last edited the caption.",
               "Colours mean one thing each: green live, blue scheduled, red blocked or failed, amber waiting "
               "for your decision. Ready is the normal state, so it has no colour.",
               "**Filter → Cards** switches between large cards and compact ones.",
               "The sidebar's **Up next** shows the next post going out for this profile."))),
          ("board-captions", "board-readiness", "board-find"),
          "board columns backlog staging scheduled published drag cards edit columns",
          "board", "Open the Board"),
    Guide("board-captions", "Write captions", "Write each platform's caption once, within its limits.",
          Visual("ticket-copy", "A ticket's Fill Empty from Original Post button, the YouTube caption section, "
                                "and its Copy button."),
          (Step("Click a card to open its ticket.", "card"),
           Step("**Fill Empty from Original Post** fills empty captions, title, tags and hashtags.", "fill"),
           Step("Each platform has its own section with a live count against its limit.", "caption"),
           Step("**Copy** puts the caption on the clipboard.", "copy")),
          "Red means too long. Reelay never cuts your text short.",
          (Detail("Empty boxes", (
               "An empty TikTok, Instagram or Facebook box copies the YouTube description instead.",
               "An empty Bluesky or Threads box copies the title, plus the description if it fits.",
               "Fill Empty only fills what's empty; if the original is too long for a platform, that box "
               "stays empty for you.")),
           Detail("Writing help (Assist ▾)", (
               "Runs on this computer: no paid AI service, nothing sent anywhere.",
               "**Check Spelling & Grammar** lists possible mistakes. Full grammar checking needs the free "
               "LanguageTool add-on.",
               "**Rewrite** suggests a shorter, friendlier or punchier version using a free local model "
               "through Ollama. You always see it first.",
               "**Brand voice** (Settings) tells it how you sound.",
               "**Get SEO Suggestions** (YouTube section) shows what people search for and scores the title "
               "and tags.")),
           Detail("Many tickets at once", (
               "**⋯ → Import Sheet…** fills tickets from a CSV file or a shared Google Sheets link. You see "
               "every row first, empty cells never erase anything, and it all lands at once or not at all.",))),
          ("board-readiness", "board-details", "publish-schedule"),
          "caption captions copy text write hashtags title description limit fill original assist spelling "
          "rewrite seo brand voice import sheet csv",
          "board", "Open the Board"),
    Guide("board-readiness", "Readiness", "Know what still needs work before you post.",
          Visual("ticket", "A ticket's readiness ring and platform rows beside the selected card."),
          (Step("The ring counts the ready platforms. **✓** ready, **–** posted by hand, **!** needs work.", "ready"),
           Step("Card badges: green published, blue scheduled, a plain dot ready, red needs work.", "card"),
           Step("Hover a row for every check behind it.")),
          "A ticket can go when at least one of its platforms is ready. Platforms you post by hand never block.",
          (Detail("What's checked", (
               "Whether each platform's copy is written and within its limit, and whether the video file is "
               "downloaded.",
               "#Shorts is only added for vertical videos of 3 minutes or less. Bluesky (3 minutes) and "
               "Threads (5 minutes) say so when a video is too long for them.")),
           Detail("Finding what's blocked", (
               "**Filter → Show** narrows the Board to tickets that are ready, not ready, or blocked on one "
               "platform.",))),
          ("board-captions", "publish-schedule"),
          "ready readiness check checklist ring blocked blocking needs work badge coverage",
          "board", "Open the Board"),
    Guide("board-details", "Details and video tools", "Add the transcript and cover, or edit the video.",
          Visual("ticket-details", "A ticket's Details and provenance section open, showing the transcript, "
                                   "cover and topic fields."),
          (Step("Open **Details and provenance** for the transcript, cover, topic, keywords and where the video "
                "came from.", "details"),
           Step("**Video** (above it) removes or replaces the sound, or adds captions to the picture."),
           Step("**Where it's been posted** (below it) lists every post of this video.")),
          "Video edits make one edited copy that Reelay posts; the original download is never changed.",
          (Detail("Transcripts", (
               "**Get from Original Post** uses the original post's captions as the transcript, when the "
               "platform has them. TikTok and YouTube often do.",)),
           Detail("Video tools", (
               "**Remove Sound** makes a silent copy.",
               "**Replace Sound…** adds music you have the rights to, looped to the video's length.",
               "**Add Captions…** draws captions from a .srt, .vtt or .ass file, or makes them automatically "
               "if the free faster-whisper add-on is installed.",
               "**Use Original** goes back to the downloaded file."))),
          ("board-captions", "board-find"),
          "details transcript cover topic keywords provenance video sound music captions subtitles srt edit"),
    Guide("board-find", "Find, filter and archive", "Find any video and keep the Board tidy.",
          Visual("board-more", "The Board's search, Published column, and the ⋯ menu with Archived Videos…"),
          (Step("Search by title, any caption, topic, notes or what's said in the video.", "search"),
           Step("**Published** keeps every video that's gone live.", "published"),
           Step("**⋯ → Archived Videos…** brings archived videos back.", "archived"),
           Step("Right-click a card (or ⌘-click several) to archive, fill, mark as posted or see history.")),
          "Archiving hides a video from the Board and the Library. Its file, ticket and history are kept.",
          (Detail("Searching", (
               "Words match as you type (\"sungl\" finds \"sunglasses\"); put \"quotes\" around an exact phrase.",
               "A card found by what was said shows the quote.")),
           Detail("Change history", (
               "**Change History…** lists every saved change: what, when, and whether you, the app or an AI "
               "agent made it.",
               "Highlight one and click **Restore the Earlier Value**. The undo is saved as a new change.")),
           Detail("Working on many cards", (
               "⌘-click or Shift-click cards in a column; the side panel then sets the same permission, type, "
               "topic, keywords, creator or hashtags on all of them.",))),
          ("board-overview", "publish-by-hand"),
          "find search archive archived restore bring back history undo change published right-click select "
          "multiple",
          "board", "Open the Board"),
))

PUBLISHING = Category("publishing", "Publishing & accounts",
                      "Connect accounts, schedule posts, and post by hand where you must.", "send", (
    Guide("publish-which", "Where Reelay posts", "Know what Reelay can post for you, and what you post.",
          Visual("platforms", "A chart: YouTube posts from Reelay once connected; Facebook, Bluesky and Threads "
                              "post from Reelay once connected and turned on in Settings; TikTok, Instagram and "
                              "Snapchat are posted by you, with Reelay preparing everything."),
          (Step("Connect the accounts you post to on **Accounts**."),
           Step("Turn on Bluesky, Threads or Facebook under **Settings → Automatic posting** if you want Reelay "
                "to post there."),
           Step("Everything else, Reelay prepares and you post in that platform's own app.")),
          "Every ticket always holds ready-to-paste text for every platform, so you can always post by hand.",
          (Detail("Why TikTok and Instagram are by hand", (
               "TikTok only allows automatic posting from developer apps it has approved, and Instagram needs "
               "an approved Meta app. Signing in to them (where available) attaches your account.",)),
           Detail("You don't need an account for local work", (
               "Downloading, preparing captions, readiness, automations and copying all work without "
               "connecting anything.",))),
          ("accounts-connect", "publish-schedule", "publish-by-hand"),
          "platforms which automatic manual by hand youtube tiktok instagram bluesky threads facebook snapchat "
          "post publish"),
    Guide("accounts-connect", "Connect an account", "Let Reelay post to a platform for this profile.",
          Visual("accounts", "The Accounts page: six platform cards with status pills, and the Snapchat strip."),
          (Step("On YouTube's card, click **Sign in with Google** and sign in in your browser.", "youtube"),
           Step("Or use **Advanced Setup…** to connect through your own developer keys.", "advanced"),
           Step("Bluesky connects with an **App Password**, a separate password just for Reelay.", "bluesky"),
           Step("Threads and Facebook use their sign-in when available, or your own Meta app's token.",
                "facebook")),
          "The card says Connected, and the count at the top goes up.",
          (Detail("YouTube with your own Google project", (
               "**Set Up YouTube…** is a five-step guide, about five minutes, once.",
               "While your project is in Testing, Google signs you out every 7 days, and uploads stay private "
               "until it passes YouTube's free API compliance audit.")),
           Detail("Disconnecting", (
               "**Disconnect** removes the account from the profile and, where the platform allows it "
               "(Google, TikTok, Facebook), tells the platform to drop Reelay's access.",)),
           Detail("Where sign-ins are kept", (
               "Passwords and tokens are kept in your Mac's Keychain, never in files. Reelay never sees your "
               "password when you sign in through your browser.",))),
          ("publish-which", "settings-posting", "trouble"),
          "connect account sign in login google youtube bluesky app password threads facebook meta token "
          "disconnect keychain advanced setup",
          "accounts", "Open Accounts"),
    Guide("publish-schedule", "Schedule or publish", "Post a ready video from Reelay, now or later.",
          Visual("ticket-post", "A ticket's Post automatically section with the YouTube channel and when to "
                                "publish, the summary line and Publish Now."),
          (Step("Under **Platforms**, set the platforms it's going to **Preparing**."),
           Step("In **Post automatically**, choose the channel and who can see it.", "auto"),
           Step("Choose **Publish as soon as it's uploaded** or **Schedule for a date and time**.", "when"),
           Step("Check the line above the buttons, then click **Publish Now** or **Schedule**.", "post")),
          "YouTube and Facebook get the video now and publish it at your time, even if Reelay is closed. "
          "Bluesky and Threads are posted by Reelay at the time, so it must be running then.",
          (Detail("If something isn't ready", (
               "Reelay lists the problems and asks before going ahead. Copying and pasting is never blocked.",)),
           Detail("Checking YouTube", (
               "The refresh button on the Board checks YouTube and moves cards to Published once they're "
               "live.",)),
           Detail("If Reelay is interrupted", (
               "Every send is written down before it starts. Closing Reelay during a YouTube upload pauses it; "
               "it carries on next time.",
               "If Reelay can't tell whether a post went out, it doesn't guess: it asks **Try Again**, **It Was "
               "Posted** or **Cancel It**.",
               "A post whose time passed while Reelay was closed asks first: **Post Now**, **It Was Posted** or "
               "**Cancel It**."))),
          ("publish-which", "board-readiness", "publish-by-hand"),
          "schedule publish post now later time date upload youtube private public unlisted interrupted "
          "check youtube",
          "board", "Open the Board"),
    Guide("publish-by-hand", "Post by hand", "Post to TikTok or Instagram in their own uploader, with Reelay's help.",
          Visual("handoff", "The Publish on TikTok window: checks, Copy Caption, Open TikTok & Show Video, and "
                            "Record as Published."),
          (Step("In the ticket's **Platforms**, click **Publish…** next to TikTok or Instagram."),
           Step("Reelay checks the video file and the caption."),
           Step("**Copy Caption, Open TikTok & Show Video** copies the caption, opens the upload page and "
                "shows the file in Finder."),
           Step("Post it there, then paste the post's link back and click **Record as Published**.")),
          "Reelay saves the link and the time, and the card moves on.",
          (Detail("Snapchat", (
               "Copy the caption from the ticket and post Spotlight from the Snapchat app.",)),
           Detail("Already posted?", (
               "Set the platform's row to **Posted**, or right-click the card and choose **Mark as Posted…**.",))),
          ("publish-which", "board-find"),
          "manual by hand tiktok instagram snapchat publish prepare copy caption uploader record link posted"),
))

AUTOMATIONS = Category("automations", "Automations", "Rules that fill, tag and schedule tickets for you.", "bolt", (
    Guide("auto-rules", "Make a rule", "Let Reelay do the repetitive parts of preparing tickets.",
          Visual("rule", "A rule being edited: When a video is added to the board, If conditions, Then actions, "
                         "and Test this rule on a video."),
          (Step("On **Automations**, click **New Rule**."),
           Step("**When**: a video is added to the board, or moved to a column."),
           Step("**If**: words in the caption, title, a hashtag, a product or brand, the length, or the source."),
           Step("**Then**: fill an empty field from a template, add hashtags, choose platforms, set a field, "
                "move it, or schedule it.")),
          "Saved rules act on videos added from now on, and everything they do is listed under Activity.",
          (Detail("Template placeholders", (
               "{hook} {caption} {hashtags} {title} {topic} {creator}",)),
           Detail("Rules are careful", (
               "Each rule acts on a video once, so it never undoes your edits.",
               "Templates only fill empty fields.",
               "Scheduling only happens if the ticket is ready."))),
          ("auto-recipes", "auto-test"),
          "automation automations rule rules when if then template trigger condition action schedule hashtags",
          "automations", "Open Automations"),
    Guide("auto-recipes", "Quick-start recipes", "Add a ready-made rule in one click.",
          Visual("automations", "The Automations page with a rule, the Recipes menu and New Rule."),
          (Step("Click **Recipes ▾** (or a recipe on the empty Automations page).", "recipes"),
           Step("Pick one; it's added as a rule you can edit.", "rule")),
          "The recipe starts with videos added from now on.",
          (Detail("The recipes", (
               "**Cross-post short-form**: TikTok and Instagram videos get YouTube Shorts and Threads captions "
               "with a link-in-bio line.",
               "**Auto-hashtag by topic**: fitness, tech and recipe hashtag bundles. Change the words to suit "
               "you.",
               "**Ready queue**: tickets moved to Staging are scheduled for the next Tuesday at 9:00, if "
               "they're ready.")),),
          ("auto-rules", "auto-test"),
          "recipe recipes cross-post hashtag ready queue quick start template",
          "automations", "Open Automations"),
    Guide("auto-test", "Test, run and review", "See what a rule would do before it does anything.",
          Visual("automations", "A rule card with Run on Existing Videos…, and the Activity list below."),
          (Step("In a rule, **Test this rule on a video** shows whether it would run and what it would write."),
           Step("**Run on Existing Videos…** applies it to videos already on the Board, after showing how many "
                "match.", "rule"),
           Step("**Activity** lists everything rules did, newest first.", "activity")),
          "Nothing changes until you save a rule and it runs.",
          (),
          ("auto-rules",),
          "test run existing activity log what happened",
          "automations", "Open Automations"),
))

SETTINGS = Category("settings", "Profiles & settings", "Profiles, branding, tips and the settings you'll use most.",
                    "person", (
    Guide("profiles", "Profiles", "Keep each creator or channel separate.",
          Visual("profile", "The profile menu: profiles, New Profile… and Edit Profile & Branding…"),
          (Step("Click the name at the top of the sidebar to switch profiles.", "profile"),
           Step("**New Profile…** adds one.", "new"),
           Step("**Edit Profile & Branding…** changes its name, tagline, logo and colours.", "brand")),
          "Every profile has its own download folder, Board, sources and accounts.",
          (Detail("Good to know", (
               "Switching only changes what you see. Nothing is moved or deleted.",
               "You can't switch while a download is running: let it finish, or stop it.",
               "**My Channel** is the starting profile.")),),
          ("branding", "privacy-local"),
          "profile profiles switch creator channel new delete"),
    Guide("branding", "Branding and themes", "Make it obvious whose account you're working in.",
          Visual("settings", "Settings → Appearance with the Light, Dark, Black and Custom buttons."),
          (Step("In **Edit Profile & Branding…**, add a logo and a tagline; **Match Logo** picks colours from it."),
           Step("Choose Light, Dark, Black or **Custom** under **Settings → Appearance**.", "theme")),
          "The logo becomes the profile's icon, and a thin bar in its colour runs across the top of the sidebar.",
          (Detail("Custom theme", (
               "Custom shows Reelay in the current profile's brand colours, and switching profiles switches "
               "them. Reelay keeps text readable whatever colours you pick.",)),),
          ("profiles", "settings-tips"),
          "branding brand logo tagline colours colors theme dark light black custom appearance",
          "settings", "Open Settings"),
    Guide("settings-tips", "Helpful tips", "Hover almost anything to see what it does.",
          Visual("settings", "Settings → Appearance with Show helpful tips."),
          (Step("Hover a button, field or label for a short explanation."),
           Step("Turn tips off or on with **Show helpful tips** in **Settings → Appearance**.", "tips")),
          "Turn them back on any time you want a reminder of what something does.",
          (),
          ("branding",),
          "tips tooltips hover hints turn off",
          "settings", "Open Settings"),
    Guide("settings-posting", "Automatic posting", "Choose which platforms besides YouTube Reelay posts to.",
          Visual("settings-posting", "Settings → Automatic posting with switches for Bluesky, Threads and "
                                     "Facebook."),
          (Step("Connect the account under **Accounts** first."),
           Step("Tick **Post to Bluesky / Threads / Facebook automatically**.", "posting"),
           Step("Restart Reelay: changes apply after a restart.")),
          "YouTube can always post automatically. Every other platform stays copy-and-paste until turned on here.",
          (Detail("Timing", (
               "Facebook holds the video until the set time. Bluesky and Threads are posted by Reelay at the "
               "set time, so Reelay must be running and the Mac awake then.",)),),
          ("publish-which", "accounts-connect"),
          "automatic posting bluesky threads facebook turn on enable restart",
          "settings", "Open Settings"),
    Guide("settings-more", "More settings", "Writing help, AI agents and sharing, in short.",
          None,
          (Step("**Local AI helper**: free writing help on this computer, used from **Assist ▾** in captions."),
           Step("**Brand voice**: your tone, words to avoid, default hashtags and example captions."),
           Step("**AI agents (advanced)**: let AI assistants on this Mac read the Board and edit tickets, with "
                "an access key."),
           Step("**Data → Move Database…**: share one Board with your team through a shared folder.")),
          "Each of these is optional; Reelay works without them.",
          (Detail("AI agents", (
               "Reelay only listens on this Mac, and every request needs the access key from Settings → AI "
               "agents.",
               "If a ticket isn't ready, scheduling through an agent is refused with the list of problems, "
               "unless the agent explicitly asks to go ahead.")),
           Detail("Sharing the database", (
               "Move it into a Google Drive, Dropbox or iCloud Drive folder; teammates use **Open Shared "
               "Database…**.",
               "One person edits at a time; others see changes view-only and nothing posts from their computer.",
               "**Take Over** is for when someone forgot to quit. Sign-ins stay in each person's own keychain.",
               "The old copy stays as a backup. **Use This Computer's Database** switches back."))),
          ("privacy-local", "board-captions"),
          "ai agents api access key ollama languagetool brand voice share database team dropbox drive icloud",
          "settings", "Open Settings"),
))

PRIVACY = Category("privacy", "Privacy & troubleshooting", "Where your data lives, and fixes for common problems.",
                   "shield", (
    Guide("privacy-local", "Where your data lives", "Reelay keeps your work on this Mac.",
          Visual("storage", "A diagram: videos in your chosen folder, the database and caches in Reelay's data "
                            "folder, and sign-ins in the Keychain, all on this Mac."),
          (Step("**Videos**: the folder under **Where videos go**, in Download Settings (the gear on the "
                "Library)."),
           Step("**Database**: shown at the top of **Settings → Data**."),
           Step("**Caches and logs**: **Settings → Open Data Folder** and **Open Logs**."),
           Step("**Passwords and tokens**: your Mac's Keychain.")),
          "Reelay has no cloud of its own: it doesn't sync or back up your work anywhere.",
          (Detail("When Reelay goes online", (
               "Only to do what you ask: list and download videos (with their covers and captions), sign in "
               "to and post on the platforms you connect, look up tagged products, read a Google Sheets link "
               "you import, and fetch YouTube search suggestions when you ask for SEO help.",
               "**Sign in with…** opens the platform's own sign-in page in your browser; Reelay's sign-in "
               "server only passes the result back to the app.",
               "Writing help runs on this computer and sends nothing anywhere.")),
           Detail("Keeping a copy", (
               "Copy the database and your videos folder to keep a backup. If you move the database to a "
               "shared folder, that folder's service keeps it in sync.",))),
          ("trouble", "settings-more"),
          "privacy data local where stored storage files folder database keychain backup sync cloud offline"),
    Guide("trouble", "Fix common problems", "Quick fixes for the problems people hit most.",
          None,
          (Step("**\"This account is private\" or Instagram lists nothing**: set **Use browser sign-in** (the "
                "gear on the Library) to a browser where you're signed in, then try again."),
           Step("**YouTube says you're signed out**: click **Sign in with Google** again. With your own "
                "project in Testing, this happens weekly."),
           Step("**Scheduled YouTube videos stay private**: your Google project hasn't passed YouTube's audit "
                "yet."),
           Step("**A video won't play**: click **Make a Preview Copy**.")),
          "Still stuck? **Settings → Open Logs** has the details to share.",
          (Detail("A post shows ⚠ Interrupted", (
               "Reelay stopped mid-send and won't guess whether it went out. Open the ticket and choose **Try "
               "Again**, **It Was Posted** (paste the link) or **Cancel It**.",)),
           Detail("Can't switch profiles", (
               "Wait for the current download to finish, or stop it.",))),
          ("privacy-local", "accounts-connect", "library-add"),
          "troubleshooting problem error fix broken private instagram signed out youtube private won't play "
          "interrupted logs help stuck"),
))

CATEGORIES: tuple[Category, ...] = (LIBRARY, BOARD, PUBLISHING, AUTOMATIONS, SETTINGS, PRIVACY)
GUIDES: dict[str, Guide] = {g.key: g for c in CATEGORIES for g in c.guides}
CATEGORY_OF: dict[str, Category] = {g.key: c for c in CATEGORIES for g in c.guides}

# The drawn pictures (no screenshot): what the "platforms" chart shows, one source for chart and text.
POSTING = (
    ("youtube", "YouTube", "Reelay posts", "once connected"),
    ("facebook", "Facebook", "Reelay posts", "connected and turned on in Settings"),
    ("bluesky", "Bluesky", "Reelay posts", "connected and turned on in Settings"),
    ("threads", "Threads", "Reelay posts", "connected and turned on in Settings"),
    ("tiktok", "TikTok", "You post", "Reelay prepares it: Publish…"),
    ("instagram", "Instagram", "You post", "Reelay prepares it: Publish…"),
    ("snapchat", "Snapchat", "You post", "copy the caption from the ticket"),
)
DRAWN = ("platforms", "storage")

# The part of each screenshot worth showing, enlarged (x, y, width, height as fractions); full window otherwise.
CROPS: dict[str, tuple[float, float, float, float]] = {
    "profile": (0.0, 0.0, 0.42, 0.42),                 # the sidebar and its profile menu
    "ticket": (0.35, 0.08, 0.65, 0.75),                # a card and the open ticket
    "ticket-copy": (0.35, 0.08, 0.65, 0.84),
    "ticket-platforms": (0.58, 0.42, 0.42, 0.58),      # the ticket's lower half
    "ticket-post": (0.58, 0.40, 0.42, 0.60),
    "ticket-details": (0.58, 0.40, 0.42, 0.60),
    "board-more": (0.45, 0.0, 0.55, 0.6),              # the top right of the Board and its ⋯ menu
    "handoff": (0.29, 0.13, 0.42, 0.74),               # the dialogs themselves
    "rule": (0.22, 0.06, 0.56, 0.88),
    "sources": (0.26, 0.09, 0.48, 0.82),
    "archived": (0.26, 0.23, 0.48, 0.54),
    "settings": (0.14, 0.0, 0.6, 0.45),
    "settings-posting": (0.14, 0.0, 0.86, 0.45),
}


def crop_of(visual: Visual) -> tuple[float, float, float, float]:
    return visual.crop or CROPS.get(visual.shot, (0.0, 0.0, 1.0, 1.0))


def search(query: str) -> list[tuple[str, str, str]]:
    """Guides and tour steps matching every word of `query` (a word matches the start of any word in the
    title, text or keywords). Returns (kind, key, title) best first: title matches before text matches."""
    import re
    words = [w for w in re.findall(r"[\w#@]+", query.lower()) if w]
    if not words:
        return []

    def text_of(obj) -> str:
        parts = [obj.title, getattr(obj, "doing", "") or getattr(obj, "goal", ""),
                 getattr(obj, "expect", "") or getattr(obj, "next", ""), getattr(obj, "keywords", "")]
        parts += [s.text for s in obj.steps]
        for d in getattr(obj, "details", ()):
            parts.append(d.title)
            parts.extend(d.lines)
        return " ".join(parts).lower().replace("**", "")

    def matches(text: str) -> bool:
        tokens = re.findall(r"[\w#@]+", text)
        return all(any(t.startswith(w) for t in tokens) for w in words)

    results = []
    for kind, items in (("guide", GUIDES.values()), ("tour", TOUR)):
        for obj in items:
            if matches(text_of(obj)):
                rank = 0 if matches(obj.title.lower()) else 1
                results.append((rank, kind, obj.key, obj.title))
    results.sort(key=lambda r: (r[0], r[1] != "guide", r[3]))
    return [(kind, key, title) for _r, kind, key, title in results]
