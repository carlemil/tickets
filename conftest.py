"""Fixtures for the whole suite: uv run pytest

Everything shares one process on purpose. `core.DB_PATH` is a module global that
`core.connect()` re-reads on every call, so pointing it at a temp file redirects the
database for the test, the TestClient and the in-thread uvicorn server alike — which is
what lets a browser test and a direct MCP tool call meet on the same card. That is also
why this suite must not be run under pytest-xdist.
"""

import socket
import threading
import time

import pytest

import core


@pytest.fixture(autouse=True)
def db(tmp_path):
    """A fresh database per test. Autouse, so no test can reach the real tickets.db."""
    core.DB_PATH = str(tmp_path / "t.db")
    return core.DB_PATH


def pytest_configure(config):
    config.addinivalue_line("markers", "no_home: start with no projects configured")


@pytest.fixture(autouse=True)
def home(db, request):
    """Every card needs a configured project, and a fresh database has none. Most tests
    just need one to exist, so "Home" does unless the test is marked no_home."""
    if "no_home" not in request.keywords:
        core.create_project("Home")
    return "Home"


@pytest.fixture
def client():
    """Drives the custom HTTP routes in-process. No context manager: the routes sit
    outside the /mcp mount, so they need neither the lifespan nor an MCP session."""
    from starlette.testclient import TestClient  # imports httpx2 internally, never httpx

    import app

    return TestClient(app.app)


@pytest.fixture(scope="session")
def server():
    """Real uvicorn on a free port, in a thread, for the browser to talk to."""
    import uvicorn

    import app

    with socket.socket() as s:  # ask the OS for a free port, then hand it to uvicorn
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app.app, host="127.0.0.1", port=port,
                                        log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not srv.started:
        assert thread.is_alive() and time.monotonic() < deadline, "server did not start"
        time.sleep(0.01)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(5)


@pytest.fixture(scope="session")
def browser():
    """System Chrome via Playwright. Skips rather than fails when either is absent, so a
    checkout without the dev group still gets a green gate from the other four files."""
    api = pytest.importorskip("playwright.sync_api")
    with api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except Exception as e:  # no Chrome installed, or a channel mismatch
            pytest.skip(f"no usable Chrome: {e}")
        yield b
        b.close()


@pytest.fixture
def page(browser, server):
    """A page on the board. Any uncaught JS error or console
    error fails the test that caused it — the board is one file of hand-written DOM code
    and a silent TypeError there is exactly the kind of bug these tests exist to catch."""
    pg = browser.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    # Subresource failures ("Failed to load resource: …") are not board bugs — the board
    # ships no favicon, so every load logs one 404. Real JS exceptions arrive on
    # "pageerror", and a broken API call still fails whatever the test asserts about state.
    pg.on("console", lambda m: errors.append(f"{m.text} @ {(m.location or {}).get('url')}")
          if m.type == "error" and not m.text.startswith("Failed to load resource")
          else None)
    pg.goto(server)
    pg.wait_for_selector("#board .lane")
    # Count in-flight api() calls. Every panel edit PATCHes and then re-renders the panel,
    # replacing its field nodes, and the write lands in the database before the browser
    # re-renders -- so a test that waits only on the database can type into a node the
    # re-render is about to discard. wait_saved() waits for this to reach zero as well.
    pg.evaluate("""() => {
        const real = window.api;
        window.__inflight = 0;
        window.api = (...a) => {
            window.__inflight++;
            return real(...a).finally(() => { window.__inflight--; });
        };
    }""")
    yield pg
    pg.close()
    assert not errors, errors
