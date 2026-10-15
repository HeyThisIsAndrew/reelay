# Mobile companion: smallest useful version and security requirements

Status: **planning only.** Nothing here is built, and nothing here is a product promise. The website says
nothing about a companion, and it should keep saying nothing until the first phase is decided.

The companion would extend the desktop workflow to a phone. It would not duplicate the desktop app. Every
"user need" below is a hypothesis: there's no user research yet.

## What exists today (verified in the code, v0.12)

- **The person decides; agents only ask.** Agents reach Reelay through `ScopedPipeline`. Anything that posts,
  archives or changes many tickets becomes an approval request (`reelay/scoped_pipeline.py`). A person
  decides in the app's Approvals window or the terminal (`reelay/approvals.py`). The running app's
  dispatcher carries an approved request out once, re-checking that the video hasn't changed.
- **Requests expire.** The default is 24 hours (`DEFAULT_TTL_HOURS`). A schedule request expires 10 minutes
  before its post time (`SCHEDULE_MARGIN`). An expired request does nothing.
- **The agent API is loopback-only by default** (`reelay/api.py`: 127.0.0.1, unless `REELAY_ALLOW_NETWORK=1`).
  It has one bearer token for every client, kept in the Keychain and replaced from Settings. Replacing it
  cuts off every agent at once.
- **The Threads media link shares the agent API's server.** Threads fetches videos from a URL, so Reelay
  serves `/media/<one-time token>` on the API port, and the Public URL (a Cloudflare Tunnel) points at that
  port (`reelay/media.py`). Unless the person's tunnel limits paths, `/v1` is reachable through it, still
  behind the bearer token.
- **Reelay already runs one server: the sign-in broker** (`server/auth_broker.py`). It holds Reelay's app
  secrets for TikTok and Meta sign-in, and keeps tokens only for the minutes a sign-in takes. It stores no
  user content. "No Reelay account" is a public promise (the website's pricing and download pages).
- **Interrupted sends wait for a person.** When posting might have gone through, Reelay asks instead of
  retrying (`Pipeline.resolve_interrupted`: retry, posted, or cancel). Retrying a send that went through
  posts twice.

## The problems a companion could solve (hypotheses)

1. **Decisions wait while the person is away from the Mac.** An agent's request expires unseen, and its
   work silently doesn't happen. *Measure first:* count requests that ended `expired` (the store keeps
   them) and how long interrupted sends waited, on a real profile for a few weeks.
2. **Problems surface late.** A failed post, "Still private on YouTube", or a ticket that isn't ready
   shortly before its time.
3. **Hand-posting happens on the phone.** TikTok and Instagram are posted by hand, in apps that live on
   the phone. The caption is on the Mac. (Plausible, but unverified.)
4. **Checking the Board** away from the Mac. This is the least urgent, because it's information, not action.

## Smallest useful version

One paired phone, one profile, and three things:

1. **Needs you:** a list of what's waiting on the person. That's pending approvals, interrupted sends,
   failed posts, and tickets not ready within a day of their time. It uses the same rules as the desktop's
   status line (`CardDelegate._status`), so the two never disagree.
2. **Approve or Reject** a pending request, with the same wording as the desktop window (`approvals.explain`).
   - A request that skips the Ready check is marked as clearly as on the desktop, and needs a second,
     explicit confirmation.
   - Deciding never carries anything out on the phone. The desktop's dispatcher does that, once, after
     the same hash check.
3. **Read and copy a ticket's captions**, per platform.

**Deferred:**
- Interrupted-send decisions (a wrong "retry" posts twice; that needs more context than a phone shows).
- Mark as Posted.
- Getting the video file onto the phone.
- Scheduling or publishing from the phone.
- Editing.
- Push notifications, until the connectivity phase below allows them.

## Security requirements (any phase)

1. **The phone is a person, not an agent.** It gets its own principal with its own scopes (read the
   attention list, read captions, decide approvals). It never reuses the agent token, and it never reaches
   the agent API (`/v1`).
2. **Pairing at the Mac.**
   - The Mac shows a QR code with a one-time code and its key's fingerprint. The phone pins that key.
   - Each phone gets its own credential: in the Mac's Keychain and the phone's secure storage, never in
     the database.
3. **Revocation.** Settings lists paired phones. Revoking one invalidates its credential at once, without
   touching any other phone or any agent.
4. **Decisions go through `approvals.approve` / `reject` only.**
   - The expiry, stale-hash, decided-elsewhere and run-once protections stay exactly where they are.
   - A decision carries an idempotency key, so a retried request can't decide twice (the store's atomic
     transition already refuses a second decision; the key makes the retry return the first result).
   - `decided_by` names the phone.
5. **Nothing from the phone weakens the Ready gate.** A forced request still says so and still needs the
   second confirmation. The phone can't create requests at all.
6. **A separate listener.** Companion routes are served by their own listener, with TLS and only those
   routes. It is not the agent API's port, and not the port the Threads tunnel points at.
7. **What leaves the Mac is the minimum.**
   - Titles, captions, statuses and request summaries. Never video files, credentials or the database.
   - Stale data shows how old it is.
8. **A shared database open view only can't decide** on the phone either, as on the desktop.

## Connectivity

| | A: same network only | B: cloud sync | C: hybrid (local, plus an end-to-end encrypted relay) |
|---|---|---|---|
| Works away from home | No | Yes | Yes |
| Push notifications | No (an iPhone push needs a server holding Apple's push credentials) | Yes | Yes (the relay sends a content-free nudge) |
| What leaves the Mac | Nothing beyond the local network | Metadata stored on a server | Ciphertext only; the relay can't read it |
| Needs a Reelay account | No | Yes: breaks a public promise | No, if the pairing key is the identity |
| New server to run | None | A database and a service | A stateless relay (Reelay already runs one server, the sign-in broker) |
| Local-first promise | Kept | Weakened | Kept, if the relay sees only ciphertext |

**Not an option: exposing the existing agent API**, directly or through the Threads tunnel. It was built for
loopback and has one token for everything.

### Recommendation

- **Phase 0 (desktop, now):** the Approvals window (done). Then a device principal with per-device
  credentials and revocation, a separate listener, and one shared "needs you" rule. All of these are
  needed for any phase.
- **Phase 1:** Option A, with the three things above. It's useful at home, and proves pairing, the
  principal and decisions with nothing leaving the network.
- **Phase 2, only if Phase 0's measurements show decisions waiting while away:** Option C.
  - The Mac keeps an outbound connection to a relay. The relay forwards end-to-end encrypted messages
    between paired devices and sends content-free push nudges.
  - It stores nothing it can read, and needs no account.
  - Before building it, decide whether running a second server is acceptable, and what the privacy policy
    must say about push tokens.

## Open questions

- Do approvals actually expire unseen often enough to matter? (Measure, Phase 0.)
- iOS only, or Android too, at first?
- Should a paired phone see every profile on the Mac, or one?
- How should a decision made on the phone show on the Mac (a notice, the request's history)?
- Is a relay operated by Reelay acceptable to the people who chose Reelay for keeping things local, even
  if it only sees ciphertext?
