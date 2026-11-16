"""The navigator (fastbrowse + open models) on the test site. Costs a few cents; opt in with AGENT_BROWSER_NAV_TESTS=1."""

import os
from pathlib import Path

import pytest

from browse.navigator import Navigator
from browse.sessions import Sessions
from browse.tools import Tools

pytestmark = pytest.mark.skipif(os.environ.get("AGENT_BROWSER_NAV_TESTS") != "1", reason="set AGENT_BROWSER_NAV_TESTS=1 (uses OpenRouter + Jev)")
KEYS = Path(__file__).resolve().parents[2] / "keys"


@pytest.fixture(scope="module")
async def nav_tools(cdp, tmp_path_factory):
    t = Tools(Sessions(cdp, tmp_path_factory.mktemp("navpackets")), Navigator(cdp, keys_dir=KEYS), None)
    yield t
    await t.sessions.shutdown()


async def test_finds_submit_page_behind_a_menu(nav_tools, site):
    res = await nav_tools.open(site + "/dir/", goal="Go to the page that has the form for submitting a product for a free listing.")
    assert res["navigate"]["status"] == "complete", res
    assert res["navigate"]["now"]["url"].endswith("/dir/submit")
    await nav_tools.close(res["session"])


async def test_answers_a_question_from_the_page(nav_tools, site):
    res = await nav_tools.open(site + "/dir/makers")
    # One fact per call: two-part questions get 'stuck' on whichever part it can't verify first.
    r = await nav_tools.navigate(res["session"], "What does a Featured spot cost per month? Quote the price.")
    assert r["status"] == "complete", r
    assert "99" in r["answer"]
    await nav_tools.close(res["session"])


async def test_reading_a_form_types_nothing(nav_tools, site):
    res = await nav_tools.open(site + "/dir/submit")
    s = res["session"]
    await nav_tools.survey(s, page_text=False)
    r = await nav_tools.navigate(s, "What does the help text next to the Tagline field say? Quote it.")
    assert r["status"] == "complete", r
    assert "search results" in r["answer"].lower()
    chk = await nav_tools.check(s)
    assert "unplanned_text" not in chk, chk
    await nav_tools.close(s)
