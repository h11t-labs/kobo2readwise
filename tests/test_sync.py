"""Tests for the kobo2readwise proxy.

The most important assertion here is the trust guarantee: the Readwise token
must never leak into a response body or into logs.
"""

import logging

import httpx
import respx
from fastapi.testclient import TestClient

from app import READWISE_URL, app

client = TestClient(app)


def test_healthz_ok():
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_index_served():
    resp = client.get("/")
    assert resp.status_code == 200
    assert "kobo2readwise" in resp.text


def test_about_page_served():
    resp = client.get("/about.html")
    assert resp.status_code == 200
    assert "About" in resp.text


def test_sync_without_token_is_400():
    resp = client.post("/sync", json={"highlights": [{"text": "hello"}]})
    assert resp.status_code == 400


def test_sync_without_highlights_is_400():
    resp = client.post("/sync", json={"token": "abc", "highlights": []})
    assert resp.status_code == 400


@respx.mock
def test_sync_success_forwards_and_counts():
    route = respx.post(READWISE_URL).mock(return_value=httpx.Response(200, json={}))
    resp = client.post(
        "/sync",
        json={"token": "secret-token", "highlights": [{"text": "a"}, {"text": "b"}]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"synced": 2}

    # Token is forwarded to Readwise in the Authorization header, nowhere else.
    assert route.called
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Token secret-token"


@respx.mock
def test_sync_upstream_401_maps_to_401():
    respx.post(READWISE_URL).mock(return_value=httpx.Response(401, json={"detail": "nope"}))
    resp = client.post("/sync", json={"token": "bad", "highlights": [{"text": "a"}]})
    assert resp.status_code == 401


@respx.mock
def test_token_never_leaks_to_response_or_logs(caplog):
    respx.post(READWISE_URL).mock(return_value=httpx.Response(200, json={}))
    token = "super-secret-do-not-leak-1234567890"
    with caplog.at_level(logging.DEBUG):
        resp = client.post("/sync", json={"token": token, "highlights": [{"text": "a"}]})

    assert resp.status_code == 200
    assert token not in resp.text
    assert token not in caplog.text
