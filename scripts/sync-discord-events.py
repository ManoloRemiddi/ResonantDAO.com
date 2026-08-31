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
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

API_VERSION = "v10"
DEFAULT_GUILD_ID = "1423225702135894111"  # ResonantDAO community (Augmentatism)
FEED_SCHEMA = "resonantdao/discord-events@1"
DESCRIPTION_MAX = 500
GRACE_PERIOD = timedelta(minutes=10)  # keep events that just ended

ENTITY_KINDS_BY_INT = {1: "stage", 2: "voice", 3: "external"}
ENTITY_KINDS_BY_NAME = {"STAGE_INSTANCE": "stage", "VOICE": "voice", "EXTERNAL": "external"}
STATUS_BY_INT = {1: "SCHEDULED", 2: "ACTIVE", 3: "COMPLETED", 4: "CANCELLED"}
KEEP_STATUSES = {"SCHEDULED", "ACTIVE"}


def normalize_status(value):
    """Discord's REST payloads send status as an int; some paths use the name."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return STATUS_BY_INT.get(value)
    if isinstance(value, str):
        upper = value.strip().upper()
        return upper if upper in STATUS_BY_INT.values() else None
    return None


def normalize_kind(value):
    if isinstance(value, bool):
        return "external"
    if isinstance(value, int):
        return ENTITY_KINDS_BY_INT.get(value, "external")
    if isinstance(value, str):
        return ENTITY_KINDS_BY_NAME.get(value.strip().upper(), "external")
    return "external"


def normalize_privacy(value):
    """2 / "GUILD_ONLY" is the only level these events can carry."""
    if value is None:
        return None  # absent: tolerate rather than drop everything
    if isinstance(value, bool):
        return "other"
    if isinstance(value, int):
        return "guild" if value == 2 else "other"
    if isinstance(value, str):
        return "guild" if value.strip().upper() == "GUILD_ONLY" else "other"
    return "other"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    sys.exit(1)


def http_get_scheduled_events(guild_id: str, token: str) -> list:
    """Fetch scheduled events, honoring Discord's retry_after on 429.

    GitHub Actions runners share outbound IPs, so Discord's per-IP limiter can
    reject a request through no fault of this repo. Retry a few times, and
    treat a still-limited result as a skipped tick rather than a broken deploy:
    the previously synced feed stays in place and the page ignores past events.
    """
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
    attempts = 5
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
            if not isinstance(payload, list):
                fail(f"unexpected API payload type: {type(payload).__name__}")
            return payload
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:300]
            if exc.code == 429:
                if attempt < attempts:
                    delay = 3.0
                    match = re.search(r'"retry_after"\s*:\s*([0-9.]+)', body)
                    if match:
                        delay = max(2.0, float(match.group(1)) + 1.0)
                    print(
                        f"rate limited by Discord (attempt {attempt}/{attempts}); "
                        f"waiting {delay:.1f}s",
                        file=sys.stderr,
                    )
                    time.sleep(delay)
                    continue
                print(
                    f"::warning::Discord rate limited this runner after {attempts} "
                    "attempts; keeping the previously synced feed.",
                    file=sys.stderr,
                )
                sys.exit(0)
            hints = {
                401: "token is missing, revoked, or not a bot token (must start with 'Bot ' in header, raw token in the secret)",
                403: "bot lacks access: is it a member of this guild, and does it have View Channels (and ideally Manage Events) permission?",
                404: "guild not found: check GUILD_ID, or the bot is not in that guild",
            }
            fail(f"Discord API returned HTTP {exc.code}. {hints.get(exc.code, '')} Body: {body}")
        except urllib.error.URLError as exc:
            if attempt < attempts:
                print(
                    f"network error ({exc.reason}); retrying in 3s "
                    f"(attempt {attempt}/{attempts})",
                    file=sys.stderr,
                )
                time.sleep(3)
                continue
            fail(f"could not reach Discord API: {exc.reason}")
    fail("unreachable: exhausted fetch attempts")


def image_url(event: dict) -> "str | None":
    """Discord returns `image` as a hash; `cover_image` (legacy) as a URL."""
    cover = event.get("cover_image")
    if isinstance(cover, str) and cover.startswith("http"):
        return cover
    image_hash = event.get("image")
    if isinstance(image_hash, str) and image_hash:
        # cards render ~300px wide; 512 stays sharp at 2x DPR and weighs a
        # quarter of 1024
        return (
            f"https://cdn.discordapp.com/guild-events/{event['id']}/"
            f"{image_hash}.png?size=512"
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

    status = normalize_status(event.get("status"))
    if status not in KEEP_STATUSES:
        return None, f"status={event.get('status')!r} -> {status!r} (kept: {sorted(KEEP_STATUSES)})"

    # GUILD_ONLY is the only privacy level these events can carry. Tolerate
    # the field being absent rather than silently dropping every event; only an
    # explicit non-guild value is rejected.
    privacy = normalize_privacy(event.get("privacy_level"))
    if privacy == "other":
        return None, f"privacy_level={event.get('privacy_level')!r} is not guild-public"

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
    kind = normalize_kind(entity_type)
    location = None
    metadata = event.get("entity_metadata") or {}
    if isinstance(metadata, dict):
        location = metadata.get("location")
    if not location:
        location = {"stage": "Discord Stage", "voice": "Discord voice"}.get(kind)

    return {
        "id": str(event["id"]),
        "name": str(event.get("name", "")).strip(),
        "description": clean_description(event.get("description")),
        "image": image_url(event),
        "start": event.get("scheduled_start_time"),
        "end": event.get("scheduled_end_time"),
        "status": status,
        "kind": kind,
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


def describe_recurrence(event: dict) -> str:
    """Compact summary of Discord's recurrence_rule, for the diagnose log."""
    rule = event.get("recurrence_rule")
    if not isinstance(rule, dict) or not rule:
        return "one-off"
    frequency = rule.get("frequency")
    names = {0: "NONE", 1: "DAILY", 2: "WEEKLY", 3: "MONTHLY"}
    label = names.get(frequency, f"freq={frequency!r}")
    parts = [f"{label} x{rule.get('interval', 1)}"]
    if rule.get("by_weekday"):
        parts.append(f"weekday={rule['by_weekday']}")
    if rule.get("by_monthday"):
        parts.append(f"monthday={rule['by_monthday']}")
    end = rule.get("end")
    if isinstance(end, dict):
        parts.append(f"until={end.get('until') or end.get('count')}")
    exceptions = event.get("guild_scheduled_event_exceptions")
    if exceptions:
        parts.append(f"exceptions={len(exceptions) if isinstance(exceptions, list) else '?'}")
    return "recurring(" + " ".join(parts) + ")"


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
        name = str(event.get("name", ""))[:40]
        detail = (
            f"start={event.get('scheduled_start_time')} end={event.get('scheduled_end_time')} "
            f"status={event.get('status')!r} {describe_recurrence(event)}"
        )
        if reason is None:
            print(f"  KEEP   {name!r} {detail}")
        else:
            print(f"  DROP   {name!r} -> {reason} | {detail}")


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
