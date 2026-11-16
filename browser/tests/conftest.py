import base64
import os
import tempfile
from pathlib import Path

# Keep the tests' sessions out of the real registry the janitor reads (set before browse.registry is imported).
os.environ["AGENT_BROWSER_REGISTRY"] = tempfile.mkdtemp(prefix="browse-registry-")

import pytest

from browse.audit import foreign_values, load_packets
from browse.sessions import Sessions
from browse.tools import Tools
from browse_testsite.app import LOG
from browse_testsite.harness import chrome, testsite

# 1x1 PNG
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==")


@pytest.fixture(scope="session")
def site():
    with testsite() as base:
        yield base


@pytest.fixture(scope="session")
def cdp():
    with chrome() as ws:
        yield ws


@pytest.fixture(scope="session")
def assets(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("assets")
    (d / "logo.png").write_bytes(PNG)
    (d / "shots").mkdir()
    (d / "shots" / "app.png").write_bytes(PNG)
    return d


@pytest.fixture(scope="session")
async def tools(cdp, assets, tmp_path_factory):
    state = tmp_path_factory.mktemp("packets")
    t = Tools(Sessions(cdp, state, agent="tests"), None, assets)
    yield t
    await t.sessions.shutdown()


@pytest.fixture(autouse=True)
def clear_log():
    LOG.clear()
    yield


@pytest.fixture
async def session(tools):
    opened = []

    async def _open(url):
        res = await tools.open(url)
        opened.append(res["session"])
        return res

    yield _open
    for sid in opened:
        await tools.close(sid)


def field_by(survey: dict, **match) -> dict:
    for g in survey["groups"]:
        for f in g["fields"]:
            if all(f.get(k) == v for k, v in match.items()):
                return f
    raise AssertionError(f"no field {match} in {[f.get('name') or f.get('label') for g in survey['groups'] for f in g['fields']]}")


def button_by(survey: dict, text: str) -> str:
    for g in survey["groups"]:
        for b in g["buttons"]:
            if text.lower() in b["text"].lower():
                return b["id"]
    raise AssertionError(f"no button {text!r}")


def audit(tools: Tools, *sids: str, ignore=()) -> list:
    """Every text value the site received, checked against what these sessions' plans said."""
    events = load_packets([tools.sessions.get(sid).packet.path for sid in sids])
    bad = []
    for entry in LOG:
        bad += foreign_values(entry["fields"], events, ignore=ignore)
    return bad
