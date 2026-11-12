"""Voice profiles as files: move a creator's voice between computers, profiles or shared databases.

A voice file is plain JSON with the fields of Settings → Brand voice (name, tone, avoid, hashtags,
hashtag_style, emoji, cta, footer, guidance). Settings → Brand voice has Import… and Export…; the same from a
terminal:

    python -m reelay.voices import voices/hey-this-is-andrew.json --profile "Hey This Is Andrew" --use
    python -m reelay.voices export "Hey, This Is Andrew" --profile "Hey This Is Andrew" > my-voice.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .models import EMOJI_LEVELS, BrandProfile

FIELDS = ("name", "tone", "avoid", "hashtags", "hashtag_style", "emoji", "cta", "footer", "guidance")
MAX_FIELD = 10_000


class VoiceFileError(Exception):
    """A plain-language reason a voice file can't be used."""


def to_dict(brand: BrandProfile) -> dict:
    return {"reelay_voice": 1, **{f: getattr(brand, f) for f in FIELDS}}


def from_dict(data: dict, profile_id: int) -> BrandProfile:
    if not isinstance(data, dict) or data.get("reelay_voice") != 1:
        raise VoiceFileError("That isn't a Reelay voice file.")
    values = {}
    for f in FIELDS:
        value = data.get(f, "")
        if not isinstance(value, str):
            raise VoiceFileError(f"“{f}” must be text.")
        if len(value) > MAX_FIELD:
            raise VoiceFileError(f"“{f}” is longer than {MAX_FIELD:,} characters.")
        values[f] = value.strip()
    if not values["name"]:
        raise VoiceFileError("The voice has no name.")
    if values["emoji"] and values["emoji"] not in EMOJI_LEVELS:
        raise VoiceFileError(f"Emoji must be one of: {', '.join(EMOJI_LEVELS)}.")
    return BrandProfile(profile_id=profile_id, **values)


def import_voice(store, profile_id: int, path: Path | str, use: bool = False) -> BrandProfile:
    """Add the voice to the profile, or update the profile's voice of the same name."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise VoiceFileError(f"Couldn't read {Path(path).name}: {exc}") from None
    brand = from_dict(data, profile_id)
    existing = next((b for b in store.list_brand_profiles(profile_id) if b.name == brand.name), None)
    if existing is not None:
        brand.id, brand.created_at, brand.active = existing.id, existing.created_at, existing.active
    brand.active = brand.active or use
    return store.save_brand_profile(brand)


def export_voice(brand: BrandProfile, path: Path | str) -> None:
    Path(path).write_text(json.dumps(to_dict(brand), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    from .store import Store
    from .dblocation import current_path
    parser = argparse.ArgumentParser(prog="python -m reelay.voices", description="Import or export a voice profile")
    sub = parser.add_subparsers(dest="cmd", required=True)
    imp = sub.add_parser("import", help="Add a voice file to a profile")
    imp.add_argument("file")
    imp.add_argument("--profile", required=True, help="The Reelay profile's name")
    imp.add_argument("--use", action="store_true", help="Make it the voice in use")
    exp = sub.add_parser("export", help="Print a profile's voice as JSON")
    exp.add_argument("name")
    exp.add_argument("--profile", required=True)
    args = parser.parse_args(argv)
    store = Store(current_path())
    try:
        profile = next((p for p in store.list_profiles() if p.name == args.profile), None)
        if profile is None:
            print(f"No profile named “{args.profile}”.", file=sys.stderr)
            return 1
        if args.cmd == "import":
            try:
                brand = import_voice(store, profile.id, args.file, use=args.use)
            except VoiceFileError as exc:
                print(exc, file=sys.stderr)
                return 1
            print(f"Imported “{brand.name}” into {profile.name}{' (in use)' if brand.active else ''}.")
            return 0
        brand = next((b for b in store.list_brand_profiles(profile.id) if b.name == args.name), None)
        if brand is None:
            print(f"{profile.name} has no voice named “{args.name}”.", file=sys.stderr)
            return 1
        print(json.dumps(to_dict(brand), indent=2, ensure_ascii=False))
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
