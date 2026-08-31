# ResonantDAO.com

Public static website for ResonantDAO.

This repository is intended for GitHub Pages hosting at `resonantdao.com`.

## Pages

- `index.html` - simple ResonantDAO overview and registration entry point.
- `register/index.html` - Phantom wallet sign-in and visible NFT credential steps.
- `about/index.html` - concise DAO purpose and membership rules.
- `whitepaper/index.html` - public ResonantDAO whitepaper page.

## Events calendar (Discord sync)

`/events/` shows two things:

1. **Upcoming live sessions** — dated events read from the community Discord
   server's Scheduled Events, refreshed automatically.
2. **The weekly rhythm** — a hand-maintained table of the ten recurring calls.

Discord is the source of truth: create each event as a Scheduled Event in the
server and it appears on the site within about an hour, no code or deploy
needed. Visitors see every time in their own browser timezone.

### How the sync works

```
Discord Scheduled Events API
        ^  bot token (server-side only, never shipped to the browser)
        |
.github/workflows/sync-discord-events.yml   (every 30 min + manual)
        |
scripts/sync-discord-events.py              (sanitizes to public fields)
        |
events/discord-events.json                  (committed -> Pages rebuilds)
        |
events/index.html                           (fetches it, renders cards locally)
```

### One-time setup

1. Create a Discord application + bot at
   <https://discord.com/developers/applications> and copy its **token**.
2. Invite the bot to the server (OAuth2 URL Generator, `bot` scope, with
   **View Channels** and **Manage Events**). It only needs read access.
3. Store the token as a repository secret, never in a file:

   ```bash
   gh secret set DISCORD_BOT_TOKEN --repo ManoloRemiddi/ResonantDAO.com
   ```

4. Optionally override the server id with a repository variable
   `DISCORD_GUILD_ID` (default: the Augmentatism guild, `1423225702135894111`):

   ```bash
   gh variable set DISCORD_GUILD_ID --repo ManoloRemiddi/ResonantDAO.com --body "<guild id>"
   ```

Then run the workflow once from the Actions tab ("Run workflow") or wait for
the next 30-minute tick. If the token is missing or the bot cannot see the
server, the run fails with a readable hint and the site keeps showing the
weekly table.

### Privacy note

The feed carries only what Discord already shows publicly in the server: title,
description, cover, start/end, venue label, and how many people RSVP'd. It
contains no member ids, no channel ids, and no credentials.

