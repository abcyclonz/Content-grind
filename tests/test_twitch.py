import json

import httpx
import pytest

from network.twitch import auth
from network.twitch.api import Helix
from network.twitch.eventsub import ChatListener, parse_notification


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_device_login_polls_until_authorized(tmp_path):
    polls = {"n": 0}

    def handler(req: httpx.Request):
        if req.url.path == "/oauth2/device":
            body = dict(httpx.QueryParams(req.content.decode()))
            assert body["scopes"] == "user:read:chat" and "client_secret" not in body
            return httpx.Response(200, json={"device_code": "dev", "user_code": "ABCD", "interval": 5,
                                             "expires_in": 1800,
                                             "verification_uri": "https://www.twitch.tv/activate"})
        if req.url.path == "/oauth2/token":
            body = dict(httpx.QueryParams(req.content.decode()))
            assert body["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
            polls["n"] += 1
            if polls["n"] < 3:
                return httpx.Response(400, json={"status": 400, "message": "authorization_pending"})
            return httpx.Response(200, json={"access_token": "A1", "refresh_token": "R1",
                                             "scope": ["user:read:chat"], "token_type": "bearer"})
        if req.url.path == "/oauth2/validate":
            return httpx.Response(200, json={"login": "mybot", "user_id": "42", "expires_in": 9000})
        raise AssertionError(req.url)

    shown = []
    path = tmp_path / "tok.json"
    token = auth.device_login("cid", show=shown.append, client=_client(handler), sleep=lambda s: None,
                              path=path)
    assert "ABCD" in shown[0]
    assert (token.login, token.user_id, polls["n"]) == ("mybot", "42", 3)
    assert json.loads(path.read_text())["refresh_token"] == "R1"


def test_refresh_saves_new_one_time_refresh_token(tmp_path):
    path = tmp_path / "tok.json"
    auth.save_token(auth.TwitchToken("A1", "R1", ["user:read:chat"], "42", "mybot", validated_at=0), path)

    def handler(req):
        if req.url.path == "/oauth2/validate":
            return httpx.Response(401, json={"status": 401, "message": "invalid access token"})
        body = dict(httpx.QueryParams(req.content.decode()))
        assert body == {"client_id": "cid", "grant_type": "refresh_token", "refresh_token": "R1"}
        return httpx.Response(200, json={"access_token": "A2", "refresh_token": "R2", "scope": []})

    tm = auth.TokenManager("cid", path=path, client=_client(handler))
    assert tm.access_token() == "A2"           # hourly validation failed -> refreshed
    saved = auth.load_token(path)
    assert (saved.refresh_token, saved.user_id) == ("R2", "42")


def test_not_logged_in(tmp_path):
    with pytest.raises(auth.TwitchAuthError, match="twitch-login"):
        auth.TokenManager("cid", path=tmp_path / "missing.json")


class StubTokens:
    user_id = "42"

    def __init__(self):
        self.refreshed = 0

    def access_token(self):
        return "tok"

    def refresh(self):
        self.refreshed += 1


def test_helix_streams_and_401_retry():
    calls = []

    def handler(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(401)
        assert req.headers["Client-Id"] == "cid"
        assert req.url.params.get_list("user_login") == ["a", "b"]
        return httpx.Response(200, json={"data": [{"user_login": "b", "title": "GTA RP", "game_name": "GTA V"}]})

    tokens = StubTokens()
    helix = Helix("cid", tokens, client=_client(handler))
    assert helix.live_streams(["a", "b"]) == {"b": {"user_login": "b", "title": "GTA RP", "game_name": "GTA V"}}
    assert tokens.refreshed == 1


NOTIFICATION = {
    "metadata": {"message_id": "m1", "message_type": "notification",
                 "subscription_type": "channel.chat.message"},
    "payload": {"event": {
        "broadcaster_user_login": "test_streamer", "chatter_user_login": "viewer1",
        "message": {"text": "KEKW KEKW", "fragments": [
            {"type": "emote", "text": "KEKW"}, {"type": "text", "text": " "},
            {"type": "emote", "text": "KEKW"}]}}},
}


def test_parse_notification():
    m = parse_notification(NOTIFICATION, 123.0)
    assert (m.broadcaster_login, m.text, m.emote_count, m.received_at) == ("test_streamer", "KEKW KEKW", 2, 123.0)


class FakeWS:
    def __init__(self, frames):
        self.frames = list(frames)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def recv(self, timeout=None):
        if not self.frames:
            raise TimeoutError
        return json.dumps(self.frames.pop(0))


class FakeHelix:
    def __init__(self):
        self.subscribed, self.unsubscribed = [], []

    def subscribe_chat(self, session_id, bid):
        self.subscribed.append((session_id, bid))
        return f"sub-{bid}"

    def unsubscribe(self, sid):
        self.unsubscribed.append(sid)


def test_listener_subscribes_dedupes_and_follows_reconnect():
    welcome = lambda sid: {"metadata": {"message_type": "session_welcome"},
                           "payload": {"session": {"id": sid, "keepalive_timeout_seconds": 30}}}
    sessions = {
        "wss://first": [welcome("S1"), NOTIFICATION, NOTIFICATION,  # duplicate delivery
                        {"metadata": {"message_type": "session_reconnect"},
                         "payload": {"session": {"reconnect_url": "wss://second"}}}],
        "wss://second": [welcome("S2"), dict(NOTIFICATION, metadata=dict(NOTIFICATION["metadata"], message_id="m2"))],
    }
    got, opened = [], []
    helix = FakeHelix()

    def connector(url, **kw):
        opened.append(url)
        return FakeWS(sessions[url])

    listener = ChatListener(helix, got.append, url="wss://first", connector=connector)
    listener.want("1001", "test_streamer")
    url, keep = listener._session("wss://first", False)
    assert (url, keep) == ("wss://second", True)
    listener._stop.set()  # end the second session after its frames
    listener._stop.clear()

    # second session: subscriptions carry over, so no new subscribe call
    def run_second():
        listener_frames = sessions["wss://second"]
        ws = FakeWS(listener_frames)
        listener.connector = lambda url, **kw: ws
        import threading
        t = threading.Timer(0.5, listener._stop.set)
        t.start()
        listener._session("wss://second", True)
        t.cancel()

    run_second()
    assert helix.subscribed == [("S1", "1001")]
    assert [m.text for m in got] == ["KEKW KEKW", "KEKW KEKW"]  # m1 once, then m2


def test_listener_drops_unwanted_subscriptions():
    helix = FakeHelix()
    listener = ChatListener(helix, lambda m: None)
    listener.want("1", "a")
    listener._sync_subscriptions("S")
    listener.drop("1")
    listener._sync_subscriptions("S")
    assert helix.unsubscribed == ["sub-1"]
