#!/usr/bin/env python3
"""Sync public Discord Scheduled Events into a sanitized JSON feed.

Runs on a schedule in GitHub Actions (the "tiny sync layer" for the static
site). Reads upcoming events from the Discord API using a bot token kept in
the DISCORD_BOT_TOKEN secret, keeps only public-facing fields, and writes an
atomically-replaced JSON file that the events page fetches (same-origin).

Public fields kept per event:
  id, name, description (trimmed plain text), image (CDN URL),
  scheduled_start_time, scheduled_end_time, status, entity_type,
  location, user_count, url (https://discord.com/events/<guild>/<event>)

No tokens, channel ids, or creator data ever reach the feed.

Usage:  python3 scripts/sync-discord-events.py [output.json] [--diagnose]
Env:    DISCORD_BOT_TOKEN (required), GUILD_ID (optional, hard default)
Stdlib only; no dependencies to install.

--diagnose logs why each raw record was kept or dropped (public fields only,
never the token) so filter mismatches are visible in the Actions log.
"""

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API_VERSION = "v10"
DEFAULT_GUILD_ID = "1423225702135894111"  # ResonantDAO community (Augmentatism)
FEED_SCHEMA = "resonantdao/discord-events@1"
DESCRIPTION_MAX = 500
GRACE_PERIOD = timedelta(minutes=10)  # keep events that just ended

ENTITY_TYPES = {1: "stage", 2: "voice", 3: "external"}
KEEP_STATUSES = {"SCHEDULED", "ACTIVE"}


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def http_get_scheduled_events(guild_id: str, token: str) -> list:
    url = (
        f"https://discord.com/api/{API_VERSION}/guilds/{guild_id}/scheduled-events"
        "?with_user_count=true"
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bot {token}",
            "User-Agent": "ResonantDAO-site-sync (https://resonantdao.com)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        hints = {
            401: "token is missing, revoked, or not a bot token (must start with 'Bot ' in header, raw token in the secret)",
            403: "bot lacks access: is it a member of this guild, and does it have View Channels (and ideally Manage Events) permission?",
            404: "guild not found: check GUILD_ID, or the bot is not in that guild",
            429: "rate limited: retry later",
        }
        fail(f"Discord API returned HTTP {exc.code}. {hints.get(exc.code, '')} Body: {body}")
    except urllib.error.URLError as exc:
        fail(f"could not reach Discord API: {exc.reason}")
    if not isinstance(payload, list):
        fail(f"unexpected API payload type: {type(payload).__name__}")
    return payload


def image_url(event: dict) -> "str | None":
    """Discord returns `image` as a hash; `cover_image` (legacy) as a URL."""
    cover = event.get("cover_image")
    if isinstance(cover, str) and cover.startswith("http"):
        return cover
    image_hash = event.get("image")
    if isinstance(image_hash, str) and image_hash:
        return (
            f"https://cdn.discordapp.com/guild-events/{event['id']}/"
            f"{image_hash}.png?size=1024"
        )
    return None


def clean_description(raw) -> "str | None":
    if not isinstance(raw, str):
        return None
    text = re.sub(r"\s+", " ", raw).strip()
    if not text:
        return None
    if len(text) > DESCRIPTION_MAX:
        text = text[: DESCRIPTION_MAX - 1].rstrip() + "\u2026"
    return text


def parse_discord_time(value) -> "datetime | None":
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def filter_event(event: dict, now: datetime) -> "tuple[dict | None, str | None]":
    """Return (payload, None) when the event should publish, else (None, reason)."""
    if not isinstance(event, dict) or "id" not in event:
        return None, "record has no id"

    status = event.get("status")
    if status not in KEEP_STATUSES:
        return None, f"status={status!r} (kept: {sorted(KEEP_STATUSES)})"

    # 2 = GUILD_ONLY, the only level Discord currently defines for these.
    # Tolerate the field being absent rather than silently dropping every
    # event; only an explicit non-guild value is rejected.
    privacy = event.get("privacy_level")
    if privacy is not None and privacy != 2:
        return None, f"privacy_level={privacy!r}"

    start = parse_discord_time(event.get("scheduled_start_time"))
    if start is None:
        return None, f"unusable scheduled_start_time={event.get('scheduled_start_time')!r}"
    end = parse_discord_time(event.get("scheduled_end_time"))

    reference = end or start
    if reference < now - GRACE_PERIOD:
        return None, (
            "already over (ends "
            f"{(end or start).strftime('%Y-%m-%d %H:%M')} UTC, now {now.strftime('%Y-%m-%d %H:%M')} UTC)"
        )

    entity_type = event.get("entity_type")
    location = None
    metadata = event.get("entity_metadata") or {}
    if isinstance(metadata, dict):
        location = metadata.get("location")
    if not location:
        location = {
            "stage": "Discord Stage",
            "voice": "Discord voice",
        }.get(ENTITY_TYPES.get(entity_type, "external"))

    return {
        "id": str(event["id"]),
        "name": str(event.get("name", "")).strip(),
        "description": clean_description(event.get("description")),
        "image": image_url(event),
        "start": event.get("scheduled_start_time"),
        "end": event.get("scheduled_end_time"),
        "status": status,
        "kind": ENTITY_TYPES.get(entity_type, "external"),
        "location": location,
        "interested": event.get("user_count") or 0,
    }, None


def sanitize_event(event: dict, guild_id: str, now: datetime) -> "dict | None":
    payload, _ = filter_event(event, now)
    if payload:
        payload["url"] = f"https://discord.com/events/{guild_id}/{payload['id']}"
    return payload


def build_feed(events: list, guild_id: str, now: datetime) -> dict:
    sanitized = (sanitize_event(event, guild_id, now) for event in events)
    kept = [event for event in sanitized if event and event["name"]]
    kept.sort(key=lambda event: event["start"] or "")
    return {
        "schema": FEED_SCHEMA,
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "guild_id": guild_id,
        "count": len(kept),
        "events": kept,
    }


def diagnose(events: list, now: datetime) -> None:
    """Log per-record keep/drop decisions. Public fields only, never secrets."""
    print(f"--- diagnose: {len(events)} raw record(s); now = {now.isoformat()} ---")
    if events and isinstance(events[0], dict):
        print("fields on first record:", ", ".join(sorted(events[0].keys())))
    for event in events:
        if not isinstance(event, dict):
            print(f"  DROP   non-object record: {event!r}")
            continue
        _, reason = filter_event(event, now)
        name = str(event.get("name", ""))[:44]
        if reason is None:
            print(f"  KEEP   {name!r} start={event.get('scheduled_start_time')}")
        else:
            print(f"  DROP   {name!r} -> {reason}")


def write_atomic(path: str, feed: dict) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(feed, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    output = args[0] if args else "events/discord-events.json"
    diagnose_mode = "--diagnose" in sys.argv or os.environ.get("SYNC_DIAGNOSE") == "1"

    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        fail(
            "DISCORD_BOT_TOKEN is not set. Add it as a repository secret "
            "(Settings > Secrets and variables > Actions), then re-run this workflow."
        )
    guild_id = os.environ.get("GUILD_ID", DEFAULT_GUILD_ID).strip() or DEFAULT_GUILD_ID

    now = datetime.now(timezone.utc)
    raw_events = http_get_scheduled_events(guild_id, token)
    if diagnose_mode:
        diagnose(raw_events, now)
    feed = build_feed(raw_events, guild_id, now)
    write_atomic(output, feed)

    print(
        f"synced {feed['count']} upcoming event(s) "
        f"from {len(raw_events)} raw record(s) -> {output}"
    )
    if not feed["count"] and raw_events:
        print(
            "note: nothing published. Re-run with --diagnose (or check the "
            "diagnose lines above) to see why each event was skipped; most "
            "often the events have already ended or are cancelled."
        )


if __name__ == "__main__":
    main()
