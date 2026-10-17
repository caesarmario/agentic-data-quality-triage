"""Mock-only tests for the bounded Discord reset helper."""

from __future__ import annotations

import json
import io
from types import SimpleNamespace

import pytest

from scripts import demo_reset_discord as reset


ROOT = reset.ROOT_CHANNEL_IDS[0]
THREAD = "1558000000000000001"
BOT_MESSAGE = "1559000000000000001"
HUMAN_MESSAGE = "1559000000000000002"


def message(message_id: str, channel: str, *, author: str = reset.BOT_ID, webhook: bool = False):
    value = {"id": message_id, "channel_id": channel,
             "author": {"id": author, "bot": author == reset.BOT_ID}, "content": "secret text"}
    if webhook:
        value["webhook_id"] = "1559000000000000099"
    return value


class FakeSession:
    def __init__(self):
        self.calls = []
        self.messages = {root: [] for root in reset.ROOT_CHANNEL_IDS}
        self.messages[THREAD] = []
        self.messages[ROOT] = [message(BOT_MESSAGE, ROOT), message(HUMAN_MESSAGE, ROOT, author="1559000000000000098")]
        self.active_threads = []
        self.archived = {(root, kind): [] for root in reset.ROOT_CHANNEL_IDS for kind in ("public", "joined_private")}
        self.fail_status = {}
        self.delete_timeout_once = False

    def request(self, method, url, **kwargs):
        assert url.startswith(reset.API + "/")
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] == 10
        assert kwargs["headers"]["Authorization"] == "Bot synthetic-secret-token"
        path = url[len(reset.API):]
        params = kwargs.get("params") or {}
        self.calls.append((method, path, params))
        key = (method, path)
        statuses = self.fail_status.get(key, [])
        if statuses:
            status = statuses.pop(0)
            if isinstance(status, Exception):
                raise status
            if isinstance(status, tuple):
                return response(status[0], {"retry_after": status[1]}, {"Retry-After": str(status[1])})
            return response(status, {"retry_after": 0}, {"Retry-After": "0"})
        if path == "/users/@me":
            return response(200, {"id": reset.BOT_ID, "bot": True})
        if path == f"/guilds/{reset.GUILD_ID}":
            return response(200, {"id": reset.GUILD_ID})
        if path == f"/guilds/{reset.GUILD_ID}/threads/active":
            return response(200, {"threads": self.active_threads, "members": []})
        parts = path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == "channels":
            channel_id = parts[1]
            if channel_id in reset.ROOT_CHANNEL_IDS:
                return response(200, {"id": channel_id, "guild_id": reset.GUILD_ID, "type": 0})
            if channel_id == THREAD:
                archived = self.archived[(ROOT, "public")] + self.archived[(ROOT, "joined_private")]
                return response(200, next((t for t in archived if t["id"] == THREAD), self.thread()))
            return response(404)
        if len(parts) == 5 and parts[0] == "channels" and parts[2:4] == ["threads", "archived"]:
            root_id, kind = parts[1], parts[4]
            assert kind == "public"
        elif len(parts) == 7 and parts[0] == "channels" and parts[2:] == ["users", "@me", "threads", "archived", "private"]:
            root_id, kind = parts[1], "joined_private"
        else:
            root_id = kind = None
        if kind:
            threads = self.archived[(root_id, kind)]
            before = params.get("before")
            if before:
                threads = ([t for t in threads if t["thread_metadata"]["archive_timestamp"] < before]
                           if kind == "public" else [t for t in threads if int(t["id"]) < int(before)])
            page = threads[: params["limit"]]
            return response(200, {"threads": page, "members": [], "has_more": len(threads) > len(page)})
        if len(parts) >= 3 and parts[0] == "channels" and parts[2] == "messages":
            channel_id = parts[1]
            items = self.messages[channel_id]
            if len(parts) == 3:
                before = params.get("before")
                filtered = [m for m in items if not before or int(m["id"]) < int(before)]
                return response(200, filtered[: params["limit"]])
            target = next((m for m in items if m["id"] == parts[3]), None)
            if method == "GET":
                return response(200, target) if target else response(404)
            if method == "DELETE":
                if self.delete_timeout_once:
                    self.delete_timeout_once = False
                    self.messages[channel_id] = [m for m in items if m["id"] != parts[3]]
                    raise TimeoutError("synthetic-secret-token")
                if target:
                    self.messages[channel_id] = [m for m in items if m["id"] != parts[3]]
                    return response(204)
                return response(404)
        raise AssertionError(path)

    def thread(self, *, locked=False):
        return {"id": THREAD, "guild_id": reset.GUILD_ID, "parent_id": ROOT,
                "type": 11, "thread_metadata": {"archived": True, "locked": locked,
                "archive_timestamp": "2026-10-01T00:00:00+00:00"}}


def response(status, body=None, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {}, json=lambda: body)


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "synthetic-secret-token")


def test_discover_sanitizes_and_scopes():
    session = FakeSession()
    session.messages[ROOT][1]["author"].pop("bot")
    session.messages[ROOT].append(message("1559000000000000003", ROOT, webhook=True))
    session.active_threads = [{**session.thread(), "thread_metadata": {"archived": False, "locked": False}}]
    session.messages[THREAD] = [message("1559000000000000004", THREAD)]
    result = reset.discover(session)
    assert result["bot_identity"] == {"id": reset.BOT_ID}
    assert result["thread_ids"] == [THREAD]
    assert result["channels"][0]["message_ids"] == [BOT_MESSAGE]
    assert result["channels"][0]["human_count"] == 1
    assert result["channels"][0]["human_message_ids"] == [HUMAN_MESSAGE]
    assert result["thread_coverage"] == reset.THREAD_COVERAGE
    assert result["thread_coverage"]["private_archived_threads"] == "joined_only"
    assert result["channels"][-1]["message_ids"] == ["1559000000000000004"]
    assert "secret" not in json.dumps(result)
    assert all(method == "GET" for method, _, _ in session.calls)


def test_message_and_archived_thread_pagination():
    session = FakeSession()
    session.messages[ROOT] = [message(str(1559000000000000300 - i), ROOT) for i in range(101)]
    session.archived[(ROOT, "public")] = [
        {**session.thread(), "id": str(1558000000000000300 - i),
         "thread_metadata": {"archived": True, "locked": False,
                             "archive_timestamp": f"2026-10-01T00:{(199-i)//60:02d}:{(199-i)%60:02d}+00:00"}}
        for i in range(101)
    ]
    # The fake supports one real thread channel; return metadata for all archived IDs.
    original = session.request
    def with_archived_channels(method, url, **kwargs):
        path = url[len(reset.API):]
        for thread in session.archived[(ROOT, "public")]:
            if path == f"/channels/{thread['id']}":
                session.calls.append((method, path, kwargs.get("params") or {}))
                return response(200, thread)
        if path.endswith("/messages") and path.split("/")[2] in {t["id"] for t in session.archived[(ROOT, "public")]}:
            session.calls.append((method, path, kwargs.get("params") or {}))
            return response(200, [])
        return original(method, url, **kwargs)
    session.request = with_archived_channels
    result = reset.discover(session)
    assert len(result["thread_ids"]) == 101
    assert len(result["channels"][0]["message_ids"]) == 101
    assert sum(path.endswith("/messages") and path.startswith(f"/channels/{ROOT}/") for _, path, _ in session.calls) == 2


def test_discovery_fails_closed_on_page_cap(monkeypatch):
    monkeypatch.setattr(reset, "MAX_PAGES", 1)
    session = FakeSession()
    session.messages[ROOT] = [message(str(1559000000000000300 - i), ROOT) for i in range(100)]
    with pytest.raises(reset.ResetError):
        reset.discover(session)


def test_discovery_rejects_archived_private_access_and_locked_target():
    session = FakeSession()
    session.fail_status[("GET", f"/channels/{ROOT}/users/@me/threads/archived/private")] = [403]
    with pytest.raises(reset.ResetError):
        reset.discover(session)
    session = FakeSession()
    session.archived[(ROOT, "public")] = [session.thread(locked=True)]
    session.messages[THREAD] = [message("1559000000000000004", THREAD)]
    with pytest.raises(reset.ResetError):
        reset.discover(session)


def test_joined_private_archived_paginates_by_thread_id():
    session = FakeSession()
    session.archived[(ROOT, "joined_private")] = [
        {**session.thread(), "id": str(1558000000000000300 - i), "type": 12}
        for i in range(101)
    ]
    original = session.request

    def with_joined_channels(method, url, **kwargs):
        path = url[len(reset.API):]
        for thread in session.archived[(ROOT, "joined_private")]:
            if path == f"/channels/{thread['id']}":
                session.calls.append((method, path, kwargs.get("params") or {}))
                return response(200, thread)
        if path.endswith("/messages") and path.split("/")[2] in {t["id"] for t in session.archived[(ROOT, "joined_private")]}:
            session.calls.append((method, path, kwargs.get("params") or {}))
            return response(200, [])
        return original(method, url, **kwargs)

    session.request = with_joined_channels
    result = reset.discover(session)
    assert len(result["thread_ids"]) == 101
    joined_calls = [(path, params) for _, path, params in session.calls
                    if path == f"/channels/{ROOT}/users/@me/threads/archived/private"]
    assert len(joined_calls) == 2
    assert joined_calls[1][1]["before"] == session.archived[(ROOT, "joined_private")][99]["id"]
    assert not any("/threads/archived/private" in path and "/users/@me/" not in path for _, path, _ in session.calls)


def test_apply_rechecks_author_scope_and_preserves_humans():
    session = FakeSession()
    session.messages[ROOT][1]["author"].pop("bot")
    snapshot = reset.discover(session)
    assert snapshot["channels"][0]["human_message_ids"] == [HUMAN_MESSAGE]
    session.messages[ROOT][0]["author"] = {"id": "1559000000000000098", "bot": False}
    with pytest.raises(reset.ResetError):
        reset.delete_manifest(snapshot, session)
    assert all(method != "DELETE" for method, _, _ in session.calls)
    session.messages[ROOT][0] = message(BOT_MESSAGE, ROOT)
    assert reset.delete_manifest(snapshot, session) == {"deleted": 1, "already_missing": 0}
    assert [m["id"] for m in session.messages[ROOT]] == [HUMAN_MESSAGE]


def test_apply_404_429_and_unknown_delete_readback(monkeypatch):
    monkeypatch.setattr(reset.time, "sleep", lambda _: None)
    session = FakeSession()
    snapshot = reset.discover(session)
    session.fail_status[("DELETE", f"/channels/{ROOT}/messages/{BOT_MESSAGE}")] = [429]
    assert reset.delete_manifest(snapshot, session) == {"deleted": 1, "already_missing": 0}
    assert reset.delete_manifest(snapshot, session) == {"deleted": 0, "already_missing": 1}
    session = FakeSession()
    snapshot = reset.discover(session)
    session.delete_timeout_once = True
    assert reset.delete_manifest(snapshot, session) == {"deleted": 1, "already_missing": 0}
    assert sum(method == "DELETE" for method, _, _ in session.calls) == 1


def test_three_second_retry_after_is_accepted_without_real_sleep(monkeypatch):
    sleeps = []
    monkeypatch.setattr(reset.time, "sleep", sleeps.append)
    session = FakeSession()
    snapshot = reset.discover(session)
    session.fail_status[("DELETE", f"/channels/{ROOT}/messages/{BOT_MESSAGE}")] = [(429, 3)]
    assert reset.delete_manifest(snapshot, session) == {"deleted": 1, "already_missing": 0}
    assert sleeps == [3.0]
    assert [m["id"] for m in session.messages[ROOT]] == [HUMAN_MESSAGE]


@pytest.mark.parametrize("delay", [31, float("inf"), float("nan"), -1])
def test_unsafe_retry_after_aborts_without_sleep_or_delete(monkeypatch, delay):
    sleeps = []
    monkeypatch.setattr(reset.time, "sleep", sleeps.append)
    session = FakeSession()
    snapshot = reset.discover(session)
    session.fail_status[("DELETE", f"/channels/{ROOT}/messages/{BOT_MESSAGE}")] = [(429, delay)]
    with pytest.raises(reset.ResetError):
        reset.delete_manifest(snapshot, session)
    assert sleeps == []
    assert [m["id"] for m in session.messages[ROOT]] == [BOT_MESSAGE, HUMAN_MESSAGE]


def test_snapshot_scope_and_token_redaction(capsys, monkeypatch):
    session = FakeSession()
    snapshot = reset.discover(session)
    snapshot["guild_id"] = "1559000000000000099"
    with pytest.raises(reset.ResetError) as exc:
        reset.delete_manifest(snapshot, session)
    assert "synthetic-secret-token" not in str(exc.value)
    monkeypatch.setattr(reset, "discover", lambda: (_ for _ in ()).throw(RuntimeError("synthetic-secret-token")))
    monkeypatch.setattr("sys.argv", ["demo_reset_discord", "--discover"])
    assert reset.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"error": "Discord reset aborted: scope, access, or completeness could not be verified"}
    assert "synthetic-secret-token" not in output


def test_apply_rejects_foreign_guild_and_thread_starter():
    session = FakeSession()
    snapshot = reset.discover(session)
    original = session.request

    def foreign_channel(method, url, **kwargs):
        if url == reset.API + f"/channels/{ROOT}":
            return response(200, {"id": ROOT, "guild_id": "1559000000000000099", "type": 0})
        return original(method, url, **kwargs)

    session.request = foreign_channel
    with pytest.raises(reset.ResetError):
        reset.delete_manifest(snapshot, session)
    assert all(method != "DELETE" for method, _, _ in session.calls)

    session = FakeSession()
    snapshot = reset.discover(session)
    session.messages[ROOT][0]["thread"] = {"id": BOT_MESSAGE}
    with pytest.raises(reset.ResetError):
        reset.delete_manifest(snapshot, session)
    assert all(method != "DELETE" for method, _, _ in session.calls)


def test_get_timeout_is_bounded_and_redacted():
    session = FakeSession()
    session.fail_status[("GET", "/users/@me")] = [
        TimeoutError("synthetic-secret-token") for _ in range(reset.MAX_RETRIES)
    ]
    with pytest.raises(reset.ResetError) as exc:
        reset.discover(session)
    assert "synthetic-secret-token" not in str(exc.value)
    assert len(session.calls) == reset.MAX_RETRIES


def test_apply_cli_reads_snapshot_stdin_and_emits_only_json(monkeypatch, capsys):
    snapshot = reset.discover(FakeSession())
    monkeypatch.setattr("sys.argv", ["demo_reset_discord", "--apply"])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(snapshot)))
    monkeypatch.setattr(reset, "delete_manifest", lambda value: {"deleted": len(value["channels"][0]["message_ids"]), "already_missing": 0})
    assert reset.main() == 0
    assert json.loads(capsys.readouterr().out) == {"deleted": 1, "already_missing": 0}
