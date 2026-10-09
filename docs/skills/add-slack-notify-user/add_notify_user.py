#!/usr/bin/env python3
"""Add a Slack user to a Turnstone schedule's `notify_targets`.

A scheduled run (e.g. the `ai-gateway-daily-usage` schedule) relays its report
to the destinations listed in its `notify_targets`. Adding a user means:

  1. resolving the Turnstone username to that user's Slack DM route
     (`D<conversation_id>:U<slack_user_id>`), read straight from the DB, and
  2. appending that route to the schedule's `notify_targets` via the admin API.

This script does both, in one step, with `--dry-run` to preview without writing.

It is meant to run inside the `turnstone-server` pod, where `TURNSTONE_DB_URL`
and `TURNSTONE_API_KEY` are set and the console API is reachable. Run it like::

    kubectl -n turnstone exec sts/turnstone-server -c server -- \
      sh -c 'base64 -d | python3' </dev/stdin <<'PYEOF'
    <paste the base64 of this file here>
    PYEOF

…or `cat add_notify_user.py | base64` it and pipe. See SKILL.md for the helper.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request

DEFAULT_SCHEDULE = "ai-gateway-daily-usage"
DEFAULT_API = os.environ.get("TURNSTONE_API_BASE", "http://turnstone-console:8090")

# -- DB (psycopg, available in the pod) -------------------------------------
try:
    import psycopg
except ImportError:  # pragma: no cover - psycopg is always present in the pod venv
    psycopg = None


# -- DB: resolve a username -> Slack DM route --------------------------------
SLACK_ROUTE_SQL = (
    "SELECT channel_id FROM channel_routes WHERE channel_type='slack' "
    "AND split_part(channel_id,':',2)=%s AND split_part(channel_id,':',3)=''"
)


def resolve_slack_route(cur, *, username=None, slack_id=None, route=None):
    """Return the base (non-thread) Slack DM route for a user, or None.

    Precedence: explicit `route` (full D..:U..) wins, then `slack_id` (raw
    U-identifier) is matched directly; otherwise `username` is walked
    users -> channel_users -> channel_routes.
    """
    if route:
        return route
    if slack_id:
        row = cur.execute(SLACK_ROUTE_SQL, (slack_id,)).fetchone()
        return row[0] if row else None

    if not username:
        return None
    uid = cur.execute("SELECT user_id FROM users WHERE username=%s", (username,)).fetchone()
    if not uid:
        return None  # unknown username
    slack = cur.execute(
        "SELECT channel_user_id FROM channel_users "
        "WHERE user_id=%s AND channel_type='slack'",
        (uid[0],),
    ).fetchone()
    if not slack:
        return None  # username exists but has no Slack link
    row = cur.execute(SLACK_ROUTE_SQL, (slack[0],)).fetchone()
    return row[0] if row else None


def list_slack_users(cur):
    """Yield (username, display_name, slack_id, route) for every linked Slack user."""
    rows = cur.execute(
        """
        SELECT u.username, u.display_name, cu.channel_user_id AS slack_id,
               cr.channel_id AS route
        FROM channel_users cu
        JOIN users u ON u.user_id = cu.user_id
        LEFT JOIN channel_routes cr
              ON cr.channel_type='slack'
             AND split_part(cr.channel_id,':',2) = cu.channel_user_id
             AND split_part(cr.channel_id,':',3) = ''
        WHERE cu.channel_type = 'slack'
        ORDER BY COALESCE(u.display_name, u.username)
        """
    ).fetchall()
    return [
        {"username": r[0], "display_name": r[1], "slack_id": r[2], "route": r[3]}
        for r in rows
    ]


def find_schedule(cur, *, name=None, task_id=None):
    if task_id:
        row = cur.execute(
            "SELECT task_id FROM scheduled_tasks WHERE task_id=%s", (task_id,)
        ).fetchone()
        return row[0] if row else None
    if not name:
        return None
    row = cur.execute(
        "SELECT task_id FROM scheduled_tasks WHERE name=%s", (name,)
    ).fetchone()
    return row[0] if row else None


# -- admin API (urllib; only stdlib needed) ----------------------------------
def api_get_schedule(base_url, task_id, key):
    url = f"{base_url}/v1/api/admin/schedules/{task_id}"
    with urllib.request.urlopen(
        urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"}),
        timeout=30,
    ) as resp:
        return json.loads(resp.read().decode())


def api_update_targets(base_url, task_id, targets, key, dry_run=False):
    url = f"{base_url}/v1/api/admin/schedules/{task_id}"
    payload = json.dumps({"notify_targets": targets}).encode()
    if dry_run:
        return {"dry_run": True, "notify_targets": targets}
    req = urllib.request.Request(
        url,
        data=payload,
        method="PUT",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def _connect(url):
    """psycopg connect; normalise the in-pod `postgresql+psycopg://` DSN."""
    return psycopg.connect(url.replace("postgresql+psycopg://", "postgresql://", 1))


def main():
    ap = argparse.ArgumentParser(description="Add a Slack user to a schedule's notify_targets.")
    ap.add_argument("--username", help="Turnstone username to add (resolved to their Slack DM route).")
    ap.add_argument("--slack-id", help="Raw Slack user id (U...) to add, bypassing username lookup.")
    ap.add_argument("--route", help="Full Slack DM route (D...:U...) to add, bypassing all lookup.")
    ap.add_argument("--schedule-name", default=DEFAULT_SCHEDULE, help="Schedule name to update.")
    ap.add_argument("--task-id", help="Schedule task id (overrides --schedule-name).")
    ap.add_argument("--api-url", default=DEFAULT_API, help="Admin API base (default $TURNSTONE_API_BASE).")
    ap.add_argument("--api-key", default=os.environ.get("TURNSTONE_API_KEY", ""), help="Admin API key.")
    ap.add_argument("--db-url", default=os.environ.get("TURNSTONE_DB_URL", ""), help="Database URL.")
    ap.add_argument("--list-users", action="store_true", help="List all linked Slack users + routes, then exit.")
    ap.add_argument("--dry-run", action="store_true", help="Show the change without applying it.")
    args = ap.parse_args()

    if not args.db_url:
        print("error: TURNSTONE_DB_URL not set (run inside the pod)", flush=True)
        return 2

    # list mode: discovery aid
    if args.list_users:
        with _connect(args.db_url) as conn:
            for u in list_slack_users(conn.cursor()):
                print(f"  {u['username']:<12} {u['display_name']:<18} {u['slack_id']:<14} {u['route']}")
        return 0

    # resolve target
    with _connect(args.db_url) as conn:
        task_id = find_schedule(conn.cursor(), name=args.schedule_name, task_id=args.task_id)
        if not task_id:
            print(f"error: no schedule named {args.schedule_name!r}"
                  f" (or --task-id given)", flush=True)
            return 2
        route = resolve_slack_route(
            conn.cursor(), username=args.username, slack_id=args.slack_id, route=args.route
        )
        if not route:
            print(
                f"error: could not resolve a Slack route for "
                f"username={args.username!r} slack_id={args.slack_id!r} route={args.route!r}",
                flush=True,
            )
            print("hint: use --list-users to see known Slack-linked users.", flush=True)
            return 2

        cur = conn.cursor()
        current = json.loads(
            cur.execute("SELECT notify_targets FROM scheduled_tasks WHERE task_id=%s", (task_id,))
            .fetchone()[0]
        )
        current_ids = {t.get("channel_id") for t in current if t.get("channel_id")}

    if route in current_ids:
        print(f"{route} is already a notify target — nothing to do.", flush=True)
        print("current targets:", current, flush=True)
        return 0

    updated = current + [{"channel_type": "slack", "channel_id": route}]
    print(f"schedule : {args.schedule_name} ({task_id})")
    print(f"user     : {args.username or args.slack_id or args.route}")
    print(f"route    : {route}")
    print(f"current  : {sorted(current_ids)}")
    print(
        f"action   : {'WOULD ADD' if args.dry_run else 'ADDING'} -> "
        f"{sorted({t.get('channel_id') for t in updated})}",
        flush=True,
    )

    if not args.dry_run:
        if not args.api_key:
            print("error: TURNSTONE_API_KEY not set (needed to PUT)", flush=True)
            return 2
        resp = api_update_targets(
            args.api_url, task_id, updated, args.api_key, dry_run=False
        )
        got = {t.get("channel_id") for t in (resp.get("notify_targets") or [])}
        ok = route in got
        print(f"result   : {'OK - route in notify_targets' if ok else 'VERIFICATION FAILED'}", flush=True)
        if not ok:
            print("actual notify_targets:", resp.get("notify_targets"), flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
