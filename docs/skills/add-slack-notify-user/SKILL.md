---
name: add-slack-notify-user
description: >-
  Use this skill when the user wants to add or remove a Slack user from a
  Turnstone scheduled run's notification targets (notify_targets) — e.g.
  "also send the daily usage report to Jacob", "add me to the report
  notifications", "who's on the daily usage Slack notify list", or "make the
  report go to X and Y too". It resolves a Turnstone username to that user's
  Slack DM route and updates the schedule's notify_targets via the admin API.
version: 1.0.0
---

# Adding a Slack user to a scheduled run's notifications

## What's actually going on

A scheduled run (like `ai-gateway-daily-usage`) relays its report to every
destination in its **`notify_targets`**. That column lives on the
`scheduled_tasks` table in the Turnstone DB as a JSON array of
`{"channel_type": "slack", "channel_id": "D…:U…"}` objects — one per Slack DM
route. The skill (`ai-gateway-usage`) only *produces* the report; the
**schedule + delivery pipeline** is what sends it.

So "add a user to the report" = append that user's Slack DM route to the
schedule's `notify_targets`. No skill change, no code change — just the schedule
row.

The daily usage schedule is:

| name | task id |
|---|---|
| `ai-gateway-daily-usage` | `6a082afb2035440ea82b3508a49c7a56` |

## The one command

The helper script `add_notify_user.py` does the whole thing: it resolves a
username → their Slack DM route (from the DB), then appends that route to the
schedule's `notify_targets` (deduped) and verifies the write. Run it **inside the
`turnstone-server` pod**, where `TURNSTONE_DB_URL` and `TURNSTONE_API_KEY` are
set and the console API is reachable.

```bash
# add a user by Turnstone username
base64 -i docs/skills/add-slack-notify-user/add_notify_user.py | \
  kubectl --context rp-tools-nonprod-ue1 -n turnstone exec -i sts/turnstone-server -c server -- \
  sh -c 'base64 -d | python3 - --username jsims' </dev/stdin

# preview without writing
… | … sh -c 'base64 -d | python3 - --username jsims --dry-run' </dev/stdin

# list everyone already Slack-linked, with their route
… | … sh -c 'base64 -d | python3 - --list-users' </dev/stdin
```

(Paste the `… | …` line; it's the same `base64 -i … | kubectl exec …` invocation
each time.)

### Arguments

| flag | meaning |
|---|---|
| `--username NAME` | Turnstone username to resolve and add. |
| `--slack-id U…` | Raw Slack user id, bypassing username lookup. |
| `--route D…:U…` | Full DM route, bypassing all lookup. |
| `--schedule-name` | Schedule to update (default `ai-gateway-daily-usage`). |
| `--task-id` | Override the schedule id. |
| `--list-users` | Print every linked Slack user + route, then exit. |
| `--dry-run` | Show the change without writing. |

## What to tell the user afterward

The script prints the schedule, the resolved route, the previous set of
targets, and the result (`OK - route in notify_targets` after a real write, or
`already a notify target — nothing to do` if they're already in). Report that
back. Remind them the change only affects **future** runs (the next scheduled
fire, or a manual trigger) — nothing is resent retroactively.

## Data model (why the lookup works)

Three tables connect a person to a DM route; the join is `username →
`users.user_id` → `channel_users.channel_user_id` (the Slack `U…` id) →
`channel_routes.channel_id` (`D…:U…`).

```sql
SELECT u.username, u.display_name, cu.channel_user_id AS slack_id,
       cr.channel_id        AS route
FROM channel_users cu
JOIN users u        ON u.user_id   = cu.user_id
LEFT JOIN channel_routes cr
       ON cr.channel_type  = 'slack'
      AND split_part(cr.channel_id, ':', 2) = cu.channel_user_id   -- U…
      AND split_part(cr.channel_id, ':', 3) = ''                   -- base DM only, not a thread
WHERE cu.channel_type = 'slack'
ORDER BY COALESCE(u.display_name, u.username);
```

Two things the query guards on:

- **Base DM route, not a thread.** Slack DM routes have a 2-part form
  (`D<conv>:U<user>`); a threaded reply appends a 3rd part
  (`D<conv>:U<user>:<ts>`). `split_part(x, ':', 3) = ''` selects only the base
  DM so we never schedule a threaded channel.
- **No Slack link yet?** The user exists but hasn't linked Slack
  (`channel_users` empty for them) → the script errors and points at
  `--list-users`. A user only receives the report after they've linked the
  Turnstone Slack app (which is what creates the `channel_users` +
  `channel_routes` rows).

## Updating the target list

The API only *accepts* the full `notify_targets` array (it doesn't append one
entry), so the script reads the current list, appends the new route, and PUTs
the whole thing back. It dedups by exact `channel_id` and verifies the route is
present in the API's response after the write.

## Related

- The report's own delivery mechanics (render, Slack formatting) live in the
  `ai-gateway-usage` skill and the Slack channel adapter
  (`turnstone/channels/slack/…`).
- To *inspect* the current notify targets: the script's `--list-users` output,
  or read `scheduled_tasks.notify_targets` directly.
