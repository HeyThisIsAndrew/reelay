"""Automations: "When … if … then …" rules that work tickets for you.

Example: when a video is added to the board, if its caption mentions
"sunglasses", fill the TikTok caption from a template, add #summer, and
schedule it for next Tuesday at 9:00.

Safety first:
* A rule acts on each video **once** (remembered per rule), so edits made
  afterwards are never overwritten by the same rule again.
* A new or re-enabled rule **never** fires on videos already on the board.
  "Run on existing videos" does that, on purpose, after showing how many match.
* Copy templates only fill **empty** fields; nothing typed by a person is replaced.
* Scheduling goes through the Ready gate like any other scheduling: a ticket
  that isn't ready isn't scheduled, and the activity log says why.
* A rule never schedules copy that came from an AI agent, whether the agent
  wrote it or a rule filled it from text the agent supplied (a topic, a
  creator): that waits for a person's approval instead.
* Everything a rule does is logged (Automations → Activity).

Rules and their memory live in the profile's settings (no schema change), so
they travel with a shared database.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone

from .models import MANUAL_PLATFORMS, Platform, Stage

RULES_KEY = "rules.{pid}"
SEEN_KEY = "rules_seen.{pid}"
LOG_KEY = "rules_log.{pid}"
LOG_LIMIT = 300

TRIGGERS = {"added": "A video is added to the board", "moved": "A video is moved to a column"}
CONDITIONS = {
    "caption": "Caption or copy contains",
    "title": "Title contains",
    "hashtag": "Has hashtag",
    "product": "Tagged product or brand contains",
    "shorter": "Shorter than (seconds)",
    "longer": "Longer than (seconds)",
    "source": "Came from (TikTok, Instagram, YouTube…)",
}
ACTIONS = {
    "fill_copy": "Fill an empty field from a template",
    "add_hashtags": "Add hashtags",
    "set_platforms": "Post to these platforms",
    "set_ticket": "Set a ticket field",
    "move": "Move to column",
    "schedule": "Schedule for the next…",
}
COPY_FIELDS = {"title": "Title", "description": "YouTube description", "tiktok_text": "TikTok caption",
               "instagram_text": "Instagram caption", "facebook_text": "Facebook text", "threads_text": "Threads text",
               "bluesky_text": "Bluesky text", "hashtags": "Hashtags"}
TICKET_FIELDS_SETTABLE = {"topic": "Topic", "keywords": "Keywords", "content_type": "Content type",
                          "creator": "Creator"}
PLACEHOLDERS = ("{caption}", "{title}", "{hashtags}", "{topic}", "{creator}", "{hook}")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass
class Condition:
    kind: str
    value: str = ""


@dataclass
class Action:
    kind: str
    field: str = ""       # fill_copy: copy field; set_ticket: ticket field; move: column stage
    value: str = ""       # template / hashtags / platforms / "Tuesday 09:00"


@dataclass
class Rule:
    name: str
    trigger: str = "added"
    column: str = ""                  # for "moved": the stage it's moved to
    match: str = "all"                # all | any conditions
    conditions: list[Condition] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    enabled: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Rule":
        return cls(name=d.get("name", "Rule"), trigger=d.get("trigger", "added"), column=d.get("column", ""),
                   match=d.get("match", "all"), enabled=bool(d.get("enabled", True)), id=d.get("id") or uuid.uuid4().hex[:10],
                   conditions=[Condition(c.get("kind", ""), c.get("value", "")) for c in d.get("conditions", [])],
                   actions=[Action(a.get("kind", ""), a.get("field", ""), a.get("value", "")) for a in d.get("actions", [])])

    def summary(self, column_name=lambda stage: stage) -> str:
        when = TRIGGERS.get(self.trigger, self.trigger)
        if self.trigger == "moved":
            when = f"A video is moved to {column_name(self.column)}"
        ifs = f" {' and ' if self.match == 'all' else ' or '} ".join(
            f"{CONDITIONS.get(c.kind, c.kind).lower()} “{c.value}”" for c in self.conditions) or "any video"
        thens = "; ".join(describe_action(a, column_name) for a in self.actions) or "nothing yet"
        return f"When: {when.lower()}. If: {ifs}. Then: {thens}."


class RuleError(Exception):
    """Plain-language reason a rule can't be saved."""


def describe_action(a: Action, column_name=lambda stage: stage) -> str:
    if a.kind == "fill_copy":
        return f"fill {COPY_FIELDS.get(a.field, a.field)} (if empty) with “{_short(a.value)}”"
    if a.kind == "add_hashtags":
        return f"add {a.value}"
    if a.kind == "set_platforms":
        return f"post to {a.value}"
    if a.kind == "set_ticket":
        return f"set {TICKET_FIELDS_SETTABLE.get(a.field, a.field)} to “{_short(a.value)}”"
    if a.kind == "move":
        return f"move to {column_name(a.field)}"
    if a.kind == "schedule":
        return f"schedule for the next {a.value} (if the ticket is ready)"
    return a.kind


def _short(text: str, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# ---------------------------------------------------------------------- storage
def load(store, profile_id: int) -> list[Rule]:
    try:
        data = json.loads(store.get_setting(RULES_KEY.format(pid=profile_id), "[]") or "[]")
    except ValueError:
        return []
    return [Rule.from_dict(d) for d in data if isinstance(d, dict)]


def save(store, profile_id: int, rules: list[Rule], pipeline=None) -> None:
    """Save the rules. Rules that are new or newly enabled start from now: every video already
    on the board counts as seen, so they never fire on the archive by themselves."""
    for r in rules:
        validate(r)
    before = {r.id: r for r in load(store, profile_id)}
    store.set_setting(RULES_KEY.format(pid=profile_id), json.dumps([r.to_dict() for r in rules]))
    fresh = [r for r in rules if r.enabled and (r.id not in before or not before[r.id].enabled)]
    if fresh:
        videos = store.load_videos(profile_id)
        seen = _seen(store, profile_id)
        for r in fresh:
            # "added": everything already here; "moved": only what's already in that column
            # (a video moved there later still triggers it)
            ids = [v.id for v in videos if r.trigger != "moved" or v.stage == r.column]
            seen[r.id] = sorted(set(seen.get(r.id, [])) | set(ids))
        _save_seen(store, profile_id, seen)


def validate(rule: Rule) -> None:
    if not rule.name.strip():
        raise RuleError("Give the rule a name.")
    if rule.trigger not in TRIGGERS:
        raise RuleError("Choose when the rule runs.")
    if rule.trigger == "moved" and not rule.column:
        raise RuleError("Choose which column triggers the rule.")
    if not rule.actions:
        raise RuleError("Add at least one thing for the rule to do.")
    for c in rule.conditions:
        if c.kind not in CONDITIONS or not c.value.strip():
            raise RuleError("Every condition needs a value.")
        if c.kind in ("shorter", "longer") and not c.value.strip().isdigit():
            raise RuleError("Lengths are in whole seconds, e.g. 60.")
    for a in rule.actions:
        if a.kind not in ACTIONS:
            raise RuleError("Unknown action.")
        if a.kind == "fill_copy" and (a.field not in COPY_FIELDS or not a.value.strip()):
            raise RuleError("Choose a field and write the template.")
        if a.kind == "set_ticket" and (a.field not in TICKET_FIELDS_SETTABLE or not a.value.strip()):
            raise RuleError("Choose a ticket field and its value.")
        if a.kind == "add_hashtags" and not re.findall(r"#\w+", a.value):
            raise RuleError("Write the hashtags to add, e.g. #summer #sunnies.")
        if a.kind == "set_platforms" and not parse_platforms(a.value):
            raise RuleError("List platforms, e.g. TikTok, Instagram, YouTube.")
        if a.kind == "move" and not a.field:
            raise RuleError("Choose the column to move to.")
        if a.kind == "schedule" and parse_when(a.value) is None:
            raise RuleError("Schedule needs a weekday and time, e.g. Tuesday 09:00.")


def _seen(store, profile_id: int) -> dict[str, list]:
    try:
        data = json.loads(store.get_setting(SEEN_KEY.format(pid=profile_id), "{}") or "{}")
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def _save_seen(store, profile_id: int, seen: dict) -> None:
    store.set_setting(SEEN_KEY.format(pid=profile_id), json.dumps(seen))


def activity(store, profile_id: int) -> list[dict]:
    try:
        data = json.loads(store.get_setting(LOG_KEY.format(pid=profile_id), "[]") or "[]")
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _log(store, profile_id: int, entries: list[dict]) -> None:
    if entries:
        store.set_setting(LOG_KEY.format(pid=profile_id), json.dumps((entries[::-1] + activity(store, profile_id))
                                                                      [:LOG_LIMIT]))


# ---------------------------------------------------------------------- parsing
def parse_platforms(text: str) -> list[str]:
    names = {v.lower(): k for k, v in Platform.LABELS.items()} | {k: k for k in Platform.LABELS}
    out = []
    for part in re.split(r"[,\s]+", text or ""):
        key = names.get(part.strip().lower())
        if key in MANUAL_PLATFORMS and key not in out:
            out.append(key)
    return out


def parse_when(text: str) -> tuple[int, time] | None:
    """"Tuesday 09:00" → (1, 09:00)."""
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2}):(\d{2})\s*$", text or "")
    if not m:
        return None
    day = next((i for i, d in enumerate(WEEKDAYS) if d.lower().startswith(m.group(1).lower()[:3])), None)
    h, mi = int(m.group(2)), int(m.group(3))
    if day is None or h > 23 or mi > 59:
        return None
    return day, time(h, mi)


def next_occurrence(weekday: int, at: time, now: datetime) -> datetime:
    """The next `weekday` at `at` local time, strictly in the future (at least an hour away)."""
    local = now.astimezone()
    candidate = local.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0)
    candidate += timedelta(days=(weekday - local.weekday()) % 7)
    if candidate <= local + timedelta(hours=1):
        candidate += timedelta(days=7)
    return candidate.astimezone(timezone.utc)


# ---------------------------------------------------------------------- matching
def matches(rule: Rule, row, post) -> bool:
    results = [ok for _label, ok in condition_results(rule, row, post)]
    if not results:
        return True
    return all(results) if rule.match == "all" else any(results)


def condition_results(rule: Rule, row, post) -> list[tuple[str, bool]]:
    """Each condition in plain words, and whether this video meets it."""
    from .tags import video_tags
    item = row.item
    results = []
    for c in rule.conditions:
        v = c.value.strip().lower()
        if c.kind == "caption":
            text = " ".join([item.description or "", item.title or ""] + ([
                post.description or "", post.tiktok_text or "", post.instagram_text or "", post.facebook_text or "",
                post.threads_text or "", post.bluesky_text or "", post.hashtags or ""] if post else []))
            results.append(v in text.lower())
        elif c.kind == "title":
            results.append(v in " ".join([post.title if post else "", item.title or "", item.custom_name or ""]).lower())
        elif c.kind == "hashtag":
            results.append(v.lstrip("#") in video_tags(item, post))
        elif c.kind == "product":
            text = " ".join(f"{p.get('name', '')} {p.get('title', '')} {p.get('brand', '')}" for p in item.products or [])
            results.append(bool(text) and v in text.lower())
        elif c.kind == "shorter":
            results.append(bool(item.duration) and item.duration < int(v))
        elif c.kind == "longer":
            results.append(bool(item.duration) and item.duration > int(v))
        elif c.kind == "source":
            results.append(v in (item.platform or "").lower())
        else:
            results.append(False)
    return [(f"{CONDITIONS.get(c.kind, c.kind)} “{c.value}”", ok) for c, ok in zip(rule.conditions, results)]


def _fill(template: str, row, post) -> str:
    item, t = row.item, row.ticket
    values = {"{caption}": item.description or "", "{title}": (post.title if post else "") or item.title or "",
              "{hashtags}": (post.hashtags if post else "") or " ".join(f"#{h}" for h in item.hashtags),
              "{topic}": t.topic or "", "{creator}": t.creator or item.uploader or "", "{hook}": item.hook or ""}
    out = template
    for k, v in values.items():
        out = out.replace(k, v)
    return out.strip()


# ---------------------------------------------------------------------- running
def apply(pipeline, rule: Rule, row_id: int, now: datetime | None = None) -> list[str]:
    """Do the rule's actions on one video (logged as "automation"). Returns what happened, in plain words."""
    with pipeline.store.origin("automation"):
        return _apply(pipeline, rule, row_id, now)


def _apply(pipeline, rule: Rule, row_id: int, now: datetime | None = None) -> list[str]:
    from .pipeline import PipelineError
    now = now or datetime.now(timezone.utc)
    done: list[str] = []
    for a in rule.actions:
        row = pipeline.store.get_video(row_id)
        post, _saved = pipeline.post_or_default(row)
        try:
            if a.kind == "fill_copy":
                if (getattr(post, a.field) or "").strip():
                    done.append(f"{COPY_FIELDS[a.field]} already written: left alone")
                    continue
                _write(pipeline, row_id, {a.field: _fill(a.value, row, post)},
                       template_sources(a.value, row, post) & agent_fields(pipeline.store, row_id))
                done.append(f"filled {COPY_FIELDS[a.field]}")
            elif a.kind == "add_hashtags":
                have = (post.hashtags or "").split()
                new = [h for h in re.findall(r"#\w+", a.value) if h.lower() not in {x.lower() for x in have}]
                if new:
                    _write(pipeline, row_id, {"hashtags": " ".join(have + new)},
                           {"hashtags"} & agent_fields(pipeline.store, row_id))
                    done.append("added " + " ".join(new))
            elif a.kind == "set_platforms":
                pipeline.update_metadata(row_id, {"target_platforms": parse_platforms(a.value)})
                done.append("platforms: " + ", ".join(Platform.LABELS[p] for p in parse_platforms(a.value)))
            elif a.kind == "set_ticket":
                _write(pipeline, row_id, {a.field: _fill(a.value, row, post)},
                       template_sources(a.value, row, post) & agent_fields(pipeline.store, row_id))
                done.append(f"set {TICKET_FIELDS_SETTABLE[a.field]}")
            elif a.kind == "move":
                if a.field == Stage.SCHEDULED:
                    done.append("not moved: use “Schedule for the next…” to schedule")
                    continue
                pipeline.move(row_id, a.field)
                done.append(f"moved to {a.field}")
            elif a.kind == "schedule":
                weekday, at = parse_when(a.value)
                when = next_occurrence(weekday, at, now)
                targets, _hand = pipeline.auto_targets(post)
                if not targets:
                    done.append("not scheduled: none of its platforms post automatically")
                    continue
                if agent_written_copy(pipeline.store, row_id):
                    # An agent wrote this copy: a rule mustn't become a way around approval.
                    from .scoped_pipeline import ScopedPipeline
                    pending = ScopedPipeline(pipeline, requested_by=f"automation: {rule.name}").schedule(
                        row_id, scheduled_at=when, platforms=targets)
                    done.append(f"waiting for approval (request #{pending.approval_id}): its copy was last "
                                "edited by an AI agent")
                    continue
                pipeline.schedule(row_id, scheduled_at=when, platforms=targets, now=now)
                local = when.astimezone()      # %-d / %-I aren't supported on Windows
                done.append(f"scheduled for {local:%a %b} {local.day}, {local.hour % 12 or 12}:{local:%M %p}")
        except PipelineError as exc:
            reason = "; ".join(exc.problems[:2]) if exc.problems else str(exc)
            done.append(f"couldn't {describe_action(a).split(' (')[0]}: {reason}")
    return done


AGENT_ORIGINS = ("api", "mcp")
# A rule wrote this from text an AI agent supplied (e.g. "{topic}" after an agent set the topic). Stored
# as the revision's origin, so the agent's part is remembered across later rule runs and restarts.
AGENT_INPUT_ORIGIN = "automation:agent-input"
COPY_REVISION_FIELDS = ("title", "description", "tags", "target_platforms", "tiktok_text", "instagram_text",
                        "bluesky_text", "threads_text", "facebook_text", "hashtags")


def is_agent_origin(origin: str | None) -> bool:
    return (origin or "").split(":")[0] in AGENT_ORIGINS or origin == AGENT_INPUT_ORIGIN


def agent_fields(store, row_id: int) -> set[str]:
    """Ticket and copy fields whose current value came from an AI agent: last written by one, or by a
    rule from an agent's text. (A person's later edit to a field clears it; an approved change counts
    as the person's.)"""
    return {f for f, origin in store.latest_field_origins(row_id).items() if is_agent_origin(origin)}


def agent_written_copy(store, row_id: int) -> list[str]:
    """The copy fields (what gets posted) that came from an AI agent, directly or through a rule."""
    return sorted(agent_fields(store, row_id) & set(COPY_REVISION_FIELDS))


def template_sources(template: str, row, post) -> set[str]:
    """The editable fields a template's placeholders actually take their text from for this video.
    ({caption} and {hook} come from the original post, which no one edits here.)"""
    t = row.ticket
    used = set()
    if "{title}" in template and post and post.title:
        used.add("title")
    if "{hashtags}" in template and post and post.hashtags:
        used.add("hashtags")
    if "{topic}" in template and t.topic:
        used.add("topic")
    if "{creator}" in template and t.creator:
        used.add("creator")
    return used


def _write(pipeline, row_id: int, changes: dict, from_agent: set[str]) -> None:
    """A rule's edit; marked as carrying an agent's text when any of its inputs did."""
    if from_agent:
        with pipeline.store.origin(AGENT_INPUT_ORIGIN):
            pipeline.update_metadata(row_id, changes)
    else:
        pipeline.update_metadata(row_id, changes)


def run(pipeline, profile_id: int, trigger: str | None = None, now: datetime | None = None) -> list[dict]:
    """Run every enabled rule on the videos it hasn't handled yet. Returns the log entries."""
    rules = [r for r in load(pipeline.store, profile_id) if r.enabled and (trigger is None or r.trigger == trigger)]
    if not rules:
        return []
    store = pipeline.store
    rows = _on_board(pipeline, profile_id)
    seen = _seen(store, profile_id)
    entries: list[dict] = []
    changed = False
    for rule in rules:
        handled = set(seen.get(rule.id, []))
        for row in rows:
            if row.id in handled:
                continue
            if rule.trigger == "moved" and row.stage != rule.column:
                continue
            handled.add(row.id)                           # once per video, even if it doesn't match
            changed = True
            if not matches(rule, row, store.get_post(row.id)):
                continue
            entries.append(_entry(rule, row, apply(pipeline, rule, row.id, now)))
        seen[rule.id] = sorted(handled)
    if changed:
        _save_seen(store, profile_id, seen)
    _log(store, profile_id, entries)
    return entries


def _on_board(pipeline, profile_id: int) -> list:
    """The profile's videos as the Board shows them (downloaded, or past the Backlog)."""
    folder = pipeline.store.get_profile(profile_id).output_folder
    rows = pipeline.store.load_videos(profile_id)
    for r in rows:
        r.item.refresh_download_status(folder)
    return [r for r in rows if r.item.is_downloaded or r.stage != Stage.BACKLOG]


def matching_existing(pipeline, profile_id: int, rule: Rule) -> list[int]:
    """Videos on the board this rule would act on if run on existing videos now."""
    rows = _on_board(pipeline, profile_id)
    return [r.id for r in rows if (rule.trigger != "moved" or r.stage == rule.column)
            and matches(rule, r, pipeline.store.get_post(r.id))]


def run_on_existing(pipeline, profile_id: int, rule: Rule, now: datetime | None = None) -> list[dict]:
    """On purpose: apply a rule to videos already on the board (those that match)."""
    entries = []
    seen = _seen(pipeline.store, profile_id)
    handled = set(seen.get(rule.id, []))
    for row_id in matching_existing(pipeline, profile_id, rule):
        row = pipeline.store.get_video(row_id)
        entries.append(_entry(rule, row, apply(pipeline, rule, row_id, now)))
        handled.add(row_id)
    seen[rule.id] = sorted(handled)
    _save_seen(pipeline.store, profile_id, seen)
    _log(pipeline.store, profile_id, entries)
    return entries


def _entry(rule: Rule, row, done: list[str]) -> dict:
    from .text import short_title
    return {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "rule": rule.name, "video": row.id,
            "title": short_title(row.item.hook, 60), "done": done or ["nothing to do"]}


# ---------------------------------------------------------------------- trying a rule out
@dataclass
class Preview:
    trigger_matches: bool
    trigger_note: str
    conditions_met: bool
    conditions: list[tuple[str, bool]]
    actions: list[tuple[str, str]]          # (what changes, what it would become)


def preview(pipeline, rule: Rule, row_id: int, now: datetime | None = None,
            column_name=lambda stage: stage) -> Preview:
    """What ``rule`` would do to one video, without changing anything (for "Test this rule")."""
    now = now or datetime.now(timezone.utc)
    row = pipeline.store.get_video(row_id)
    post, _saved = pipeline.post_or_default(row)
    if rule.trigger == "moved":
        here = row.stage == rule.column
        trigger_ok = here
        note = (f"Yes: it's in {column_name(rule.column)}, so it would run when a video is moved there."
                if here else f"Not yet: this video is in {column_name(row.stage)}; the rule runs when a video is "
                             f"moved to {column_name(rule.column)}.")
    else:
        trigger_ok = row.archived_at is None
        note = ("Yes: it runs for videos as they're added to the board." if trigger_ok
                else "No: this video is archived.")
    results = condition_results(rule, row, post)
    met = matches(rule, row, post)
    actions: list[tuple[str, str]] = []
    hashtags = (post.hashtags or "").split()
    for a in rule.actions:
        if a.kind == "fill_copy":
            label = COPY_FIELDS.get(a.field, a.field)
            current = (getattr(post, a.field, "") or "").strip() if a.field in COPY_FIELDS else ""
            actions.append((label, "left alone (already written)" if current else _fill(a.value, row, post)
                            or "(empty template)"))
        elif a.kind == "add_hashtags":
            new = [h for h in re.findall(r"#\w+", a.value) if h.lower() not in {x.lower() for x in hashtags}]
            hashtags += new
            actions.append(("Hashtags", " ".join(hashtags) + ("" if new else "  (nothing new)")))
        elif a.kind == "set_platforms":
            names = ", ".join(Platform.LABELS[p] for p in parse_platforms(a.value))
            actions.append(("Platforms", names or "(none recognised)"))
        elif a.kind == "set_ticket":
            actions.append((TICKET_FIELDS_SETTABLE.get(a.field, a.field), _fill(a.value, row, post)))
        elif a.kind == "move":
            actions.append(("Column", "not moved: use “Schedule for the next…”" if a.field == Stage.SCHEDULED
                            else column_name(a.field)))
        elif a.kind == "schedule":
            when = parse_when(a.value)
            if when is None:
                actions.append(("Schedule", "needs a weekday and time, e.g. Tuesday 09:00"))
            else:
                at = next_occurrence(*when, now=now).astimezone()
                stamp = f"{at:%a %b} {at.day}, {at.hour % 12 or 12}:{at:%M %p}"
                actions.append(("Schedule", f"{stamp}, if the ticket is ready then"))
    return Preview(trigger_ok, note, met, results, actions)


# ---------------------------------------------------------------------- starter recipes
RECIPES = {
    "cross_post": ("Cross-post short-form",
                   "When a TikTok or Instagram video is added, send it to YouTube Shorts and Threads with "
                   "captions written from its own, plus a link-in-bio line."),
    "hashtags": ("Auto-hashtag by topic",
                 "When a caption mentions fitness, tech or a recipe, add that topic's hashtag bundle. Change the "
                 "words and hashtags to fit your niche."),
    "ready_queue": ("Ready queue: Tuesday mornings",
                    "When a ticket is moved to Staging, schedule it for the next Tuesday at 9:00 (only if it's "
                    "ready)."),
}


def recipe(key: str) -> list[Rule]:
    """The rule(s) a starter recipe installs (they apply to videos from now on, like any new rule)."""
    if key == "cross_post":
        return [Rule(name="Cross-post short-form", trigger="added", match="any",
                     conditions=[Condition("source", "TikTok"), Condition("source", "Instagram")],
                     actions=[Action("set_platforms", value="YouTube, Threads"),
                              Action("fill_copy", "title", "{hook}"),
                              Action("fill_copy", "description", "{caption}\n\nLinks and more: in my bio."),
                              Action("fill_copy", "threads_text", "{hook} (link in bio) {hashtags}")])]
    if key == "hashtags":
        bundles = (("fitness", "#fitness #workout #fitnessmotivation"), ("tech", "#tech #gadgets #techtok"),
                   ("recipe", "#recipe #foodtok #easyrecipes"))
        return [Rule(name=f"Hashtags: {word}", trigger="added", conditions=[Condition("caption", word)],
                     actions=[Action("add_hashtags", value=tags)]) for word, tags in bundles]
    if key == "ready_queue":
        return [Rule(name="Ready queue: Tuesday mornings", trigger="moved", column=Stage.STAGING,
                     actions=[Action("schedule", value="Tuesday 09:00")])]
    raise RuleError(f"Unknown recipe: {key}")


def install_recipe(store, profile_id: int, key: str) -> list[Rule]:
    """Add a recipe's rules to the profile (saved like any new rule, so they start from now)."""
    new = recipe(key)
    save(store, profile_id, load(store, profile_id) + new)
    return new
