"""Bounded, bot-message-only Discord demo cleanup. No project logger imports."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from typing import Any

import requests


API = "https://discord.com/api/v10"
GUILD_ID = "1557744199745540137"
ROOT_CHANNEL_IDS = ("1557750137672179732", "1557943799072694282", "1557943838067261560")
BOT_ID = "1476800539747553463"
MAX_PAGES = 20
MAX_THREADS = 300
MAX_MESSAGES = 2000
MAX_REQUESTS = 3000
MAX_RETRIES = 3
THREAD_COVERAGE = {
    "active_threads": "accessible",
    "public_archived_threads": "accessible",
    "private_archived_threads": "joined_only",
}


class ResetError(Exception):
    """A fixed, safe-to-display reset failure."""


def _fail() -> None:
    raise ResetError("Discord reset aborted: scope, access, or completeness could not be verified")


def _id(value: Any) -> str:
    if not isinstance(value, str) or not value.isdecimal() or not 15 <= len(value) <= 22:
        _fail()
    return value


class _Client:
    def __init__(self, session: requests.Session | None):
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token or "\n" in token or "\r" in token:
            _fail()
        self.session = session if session is not None else requests.Session()
        self.headers = {"Authorization": f"Bot {token}", "Accept": "application/json"}
        self.calls = 0

    def request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
        if method not in {"GET", "DELETE"} or not path.startswith("/") or "//" in path:
            _fail()
        for attempt in range(MAX_RETRIES):
            self.calls += 1
            if self.calls > MAX_REQUESTS:
                _fail()
            try:
                response = self.session.request(
                    method, API + path, headers=self.headers, params=params,
                    timeout=10, allow_redirects=False,
                )
            except Exception:
                if method == "DELETE":
                    # The caller must read back an uncertain DELETE before retrying.
                    return 0, None
                if attempt + 1 == MAX_RETRIES:
                    _fail()
                continue
            status = response.status_code
            if status == 429:
                try:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after is None:
                        retry_after = response.json().get("retry_after")
                    delay = float(retry_after)
                except Exception:
                    _fail()
                if not math.isfinite(delay) or not 0 <= delay <= 30 or attempt + 1 == MAX_RETRIES:
                    _fail()
                time.sleep(delay)
                continue
            if 500 <= status <= 599:
                if method == "DELETE":
                    return 0, None
                if attempt + 1 == MAX_RETRIES:
                    _fail()
                continue
            if status not in {200, 204, 404}:
                _fail()
            if status in {204, 404}:
                return status, None
            try:
                return status, response.json()
            except Exception:
                _fail()
        _fail()

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        status, body = self.request("GET", path, params=params)
        if status != 200:
            _fail()
        return body


def _identity(client: _Client) -> None:
    user = client.get("/users/@me")
    if not isinstance(user, dict) or user.get("id") != BOT_ID or user.get("bot") is not True:
        _fail()
    guild = client.get(f"/guilds/{GUILD_ID}")
    if not isinstance(guild, dict) or guild.get("id") != GUILD_ID:
        _fail()


def _channel(client: _Client, channel_id: str, parent_id: str | None) -> dict[str, Any]:
    channel = client.get(f"/channels/{channel_id}")
    if not isinstance(channel, dict) or channel.get("id") != channel_id or channel.get("guild_id") != GUILD_ID:
        _fail()
    if parent_id is None:
        if channel_id not in ROOT_CHANNEL_IDS or channel.get("type") not in (0, 5):
            _fail()
    elif channel.get("parent_id") != parent_id or channel.get("type") not in (10, 11, 12):
        _fail()
    return channel


def _owned(message: Any, channel_id: str) -> bool:
    if not isinstance(message, dict):
        _fail()
    _id(message.get("id"))
    if message.get("channel_id") != channel_id or not isinstance(message.get("author"), dict):
        _fail()
    author = message["author"]
    if "webhook_id" in message and message["webhook_id"] is not None:
        return False
    return author.get("id") == BOT_ID and author.get("bot") is True


def _messages(client: _Client, channel_id: str) -> tuple[list[str], list[str]]:
    target_ids: list[str] = []
    human_ids: list[str] = []
    before: str | None = None
    seen: set[str] = set()
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"limit": 100}
        if before:
            params["before"] = before
        page = client.get(f"/channels/{channel_id}/messages", params)
        if not isinstance(page, list) or len(page) > 100:
            _fail()
        for message in page:
            message_id = _id(message.get("id") if isinstance(message, dict) else None)
            if message_id in seen or (before and int(message_id) >= int(before)):
                _fail()
            seen.add(message_id)
            if len(seen) > MAX_MESSAGES:
                _fail()
            if _owned(message, channel_id):
                if message.get("thread") is not None:
                    _fail()
                target_ids.append(message_id)
            elif message["author"].get("bot", False) is False and message.get("webhook_id") is None:
                human_ids.append(message_id)
        if len(page) < 100:
            return target_ids, human_ids
        before = _id(page[-1]["id"])
    _fail()


def _archived(client: _Client, root_id: str, kind: str) -> list[dict[str, Any]]:
    if kind not in {"public", "joined_private"}:
        _fail()
    result: list[dict[str, Any]] = []
    before: str | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, Any] = {"limit": 100}
        if before:
            params["before"] = before
        path = (f"/channels/{root_id}/threads/archived/public" if kind == "public" else
                f"/channels/{root_id}/users/@me/threads/archived/private")
        page = client.get(path, params)
        if not isinstance(page, dict) or not isinstance(page.get("threads"), list) or not isinstance(page.get("has_more"), bool):
            _fail()
        threads = page["threads"]
        if len(threads) > 100 or (page["has_more"] and not threads):
            _fail()
        if kind == "joined_private":
            ids = [_id(t.get("id") if isinstance(t, dict) else None) for t in threads]
            if (len(ids) != len(set(ids)) or
                    any(int(a) <= int(b) for a, b in zip(ids, ids[1:])) or
                    (before and ids and int(ids[0]) >= int(before))):
                _fail()
        result.extend(threads)
        if len(result) > MAX_THREADS:
            _fail()
        if not page["has_more"]:
            return result
        if kind == "public":
            timestamps = [t.get("thread_metadata", {}).get("archive_timestamp") if isinstance(t, dict) else None for t in threads]
            next_before = timestamps[-1]
            if not isinstance(next_before, str) or not next_before or next_before == before or timestamps.count(next_before) != 1:
                _fail()
        else:
            next_before = ids[-1]
        before = next_before
    _fail()


def discover(session: requests.Session | None = None) -> dict[str, Any]:
    """Return a sanitized, explicitly scoped discovery snapshot or fail closed."""
    client = _Client(session)
    _identity(client)
    roots = {root: _channel(client, root, None) for root in ROOT_CHANNEL_IDS}
    active = client.get(f"/guilds/{GUILD_ID}/threads/active")
    if not isinstance(active, dict) or not isinstance(active.get("threads"), list):
        _fail()
    threads: dict[str, dict[str, Any]] = {}
    for thread in active["threads"]:
        if not isinstance(thread, dict):
            _fail()
        if thread.get("parent_id") in roots:
            threads[_id(thread.get("id"))] = thread
    if len(active["threads"]) > MAX_THREADS:
        _fail()
    for root in roots:
        for kind in ("public", "joined_private"):
            for thread in _archived(client, root, kind):
                if not isinstance(thread, dict) or thread.get("parent_id") != root:
                    _fail()
                thread_id = _id(thread.get("id"))
                if thread_id in threads and threads[thread_id] != thread:
                    _fail()
                threads[thread_id] = thread
                if len(threads) > MAX_THREADS:
                    _fail()
    channels: list[dict[str, Any]] = []
    thread_ids = sorted(threads, key=int)
    total_messages = 0
    for channel_id, parent_id in [(root, None) for root in ROOT_CHANNEL_IDS] + [
        (thread_id, threads[thread_id]["parent_id"]) for thread_id in thread_ids
    ]:
        channel = _channel(client, channel_id, parent_id)
        if parent_id is not None and not isinstance(channel.get("thread_metadata", {}).get("locked"), bool):
            _fail()
        target_ids, human_ids = _messages(client, channel_id)
        total_messages += len(target_ids) + len(human_ids)
        if total_messages > MAX_MESSAGES:
            _fail()
        if parent_id is not None and target_ids and channel.get("thread_metadata", {}).get("locked") is True:
            _fail()
        if parent_id is None and set(target_ids).intersection(thread_ids):
            _fail()
        channels.append({"channel_id": channel_id, "parent_id": parent_id,
                         "message_ids": target_ids, "human_count": len(human_ids),
                         "human_message_ids": human_ids})
    return {"bot_identity": {"id": BOT_ID}, "guild_id": GUILD_ID,
            "channels": channels, "thread_ids": thread_ids,
            "thread_coverage": THREAD_COVERAGE.copy()}


def _validate_snapshot(snapshot: Any) -> list[tuple[str, str | None, list[str]]]:
    if not isinstance(snapshot, dict) or set(snapshot) != {"bot_identity", "guild_id", "channels", "thread_ids", "thread_coverage"}:
        _fail()
    if (snapshot["bot_identity"] != {"id": BOT_ID} or snapshot["guild_id"] != GUILD_ID or
            snapshot["thread_coverage"] != THREAD_COVERAGE):
        _fail()
    entries = snapshot["channels"]
    thread_ids = snapshot["thread_ids"]
    if not isinstance(entries, list) or not isinstance(thread_ids, list) or len(thread_ids) > MAX_THREADS:
        _fail()
    for thread_id in thread_ids:
        _id(thread_id)
    if len(set(thread_ids)) != len(thread_ids):
        _fail()
    result: list[tuple[str, str | None, list[str]]] = []
    seen_channels: set[str] = set()
    seen_messages: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"channel_id", "parent_id", "message_ids", "human_count", "human_message_ids"}:
            _fail()
        channel_id = _id(entry["channel_id"])
        parent = entry["parent_id"]
        if parent is None:
            if channel_id not in ROOT_CHANNEL_IDS:
                _fail()
        elif parent not in ROOT_CHANNEL_IDS or channel_id not in thread_ids:
            _fail()
        ids = entry["message_ids"]
        human_ids = entry["human_message_ids"]
        if (channel_id in seen_channels or not isinstance(ids, list) or
                not isinstance(human_ids, list) or type(entry["human_count"]) is not int or
                entry["human_count"] != len(human_ids)):
            _fail()
        seen_channels.add(channel_id)
        for message_id in ids:
            _id(message_id)
            if message_id in seen_messages or message_id in thread_ids:
                _fail()
            seen_messages.add(message_id)
        for human_id in human_ids:
            _id(human_id)
            if human_id in seen_messages:
                _fail()
            seen_messages.add(human_id)
        result.append((channel_id, parent, ids))
    if seen_channels != set(ROOT_CHANNEL_IDS) | set(thread_ids) or len(seen_messages) > MAX_MESSAGES:
        _fail()
    return result


def _read_target(client: _Client, channel_id: str, message_id: str) -> bool:
    status, message = client.request("GET", f"/channels/{channel_id}/messages/{message_id}")
    if status == 404:
        return False
    if status != 200 or not _owned(message, channel_id) or message["id"] != message_id or message.get("thread") is not None:
        _fail()
    return True


def delete_manifest(snapshot: dict[str, Any], session: requests.Session | None = None) -> dict[str, int]:
    """Delete only revalidated explicit bot messages; never delete channels or threads."""
    entries = _validate_snapshot(snapshot)
    client = _Client(session)
    _identity(client)
    counts = {"deleted": 0, "already_missing": 0}
    for channel_id, parent_id, ids in entries:
        channel = _channel(client, channel_id, parent_id)
        if ids and parent_id is not None and channel.get("thread_metadata", {}).get("locked") is not False:
            _fail()
        for message_id in ids:
            if not _read_target(client, channel_id, message_id):
                counts["already_missing"] += 1
                continue
            for attempt in range(MAX_RETRIES):
                status, _ = client.request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
                if status == 204:
                    counts["deleted"] += 1
                    break
                if status == 404:
                    counts["already_missing"] += 1
                    break
                if status != 0:
                    _fail()
                if not _read_target(client, channel_id, message_id):
                    counts["deleted"] += 1
                    break
                if attempt + 1 == MAX_RETRIES:
                    _fail()
            else:
                _fail()
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded Discord demo-message reset")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--discover", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        output = discover() if args.discover else delete_manifest(json.load(sys.stdin))
    except Exception:
        print(json.dumps({"error": "Discord reset aborted: scope, access, or completeness could not be verified"}))
        return 1
    print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
