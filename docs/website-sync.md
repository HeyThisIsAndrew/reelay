# Website ↔ app: corrections for the website session

Paste this into the session that builds the Reelay site (`HeyThisIsAndrew/hey-this-is-andrew`, `sites/reelay/`).
The app repo is the source of truth for what Reelay does; these are the places where the site's app renderings
(compared at 1440 px, dark and light, on 2026-10-12) show something the app doesn't do, plus the app's new look
so the renderings can follow it.

## The app's current look (match these)

- **Accounts** (`AccountsScene.astro`) now matches the app: six equal cards (3 × 2, 2 × 3, 1 × 6), Snapchat as a
  "Posted by hand" strip, "N of 6 connected", one action per card plus YouTube's quiet "Advanced Setup…". Two
  small additions in the app the scene can show: a small "?" (Learn more) beside each card's name, and a
  "Manage…" link on the status line when one account is connected (several accounts show "3 connected" with a
  Manage… button).
- **Section headings are sentence case**, semibold, secondary colour: Board columns read "Backlog",
  "Staging"…; side-panel sections read "Platforms", "Caption", "YouTube title". Table column headers stay
  uppercase (DATE, CAPTION…), as on the site.
- **Status pills are tinted**: a soft fill in the status colour with darker text (green connected, amber needs
  attention, neutral otherwise).
- **Cards**: 14 px corners, hairline borders; buttons inside cards are one size smaller and semibold.
- **Readiness in the side panel** is one compact row per platform: logo · name · short note · mark
  (✓ ready, – posted by hand or automatic posting off, ! blocking with the first problem as the note), under
  a ring ("4/4") and a one-line summary.
- **Library**: with more than one page the header reads "1–24 of 213 · N downloaded", and Previous / Page n of m
  / Next sit centred under the videos. The per-page menu (24 / 48 / 96 / All) stays bottom left.
- **Sidebar**: the profile's tagline is a single line under its name (from the profile's branding).
- **Platform logos** in the app are the official colour marks. Please use those instead of one-colour glyphs:
  the platforms' brand guidelines expect their colour logos.

## Renderings that show things the app doesn't do (fix the site)

1. **Download scene** (`ImportScene.astro`): the "Saved sources" chip row no longer exists. The Library has a
   **Download Sources** button (opens a window with one section per platform; add by @username or link) next to
   the link field and **List Videos**.
2. **Download scene**: the Library opens on the **picture grid**; the table is the List view (a toggle). The
   toolbar also has the tile-size slider, the grid/list toggle and the settings gear.
3. **Sidebar item names**: the first item is **Library**, not "Download". (Then Board, Automations, Accounts,
   Settings; Help at the bottom.)
4. **Branding scene** (`BrandingScene.astro`): there's no "Schedule" button in the Board header. Scheduling is
   done from the ticket (side panel), with Publish Now / Schedule.
5. **Board header**: a search field ("Search titles, captions and what was said"), one **Filter** button, a
   refresh button and a ⋯ menu. Not "Show: All" / "All hashtags" dropdowns (those live inside Filter).
6. **Board cards**: each shows a "Short · 1080p" (or Long-form) badge on the cover, a date · views line, the
   platform marks with status dots, and a status line such as "Ready · pick a channel" or "Ready · not
   started". Replace "Needs copy for 3 platforms" / "Live on 4 platforms" style text with the app's wording.
7. **Side panel**: the app also has a **Posting to** row of platform chips (ticked = going), one caption box per
   ticked platform whose header shows its status and a ⋯ menu, and **Save Changes** / **Publish Now** at the bottom; readiness is the compact rows described
   above.
8. **Sidebar profile**: name with the tagline under it on one line (the scene's "Travel, slowly" is right in
   shape; it comes from the profile's branding).
9. **Platform logos**: see above (official colour marks).
