"""Deterministic survey/fill/check/press against the test site. No model involved."""

import asyncio
import json
from pathlib import Path

from conftest import audit, button_by, field_by

from browse_testsite.harness import submissions

DESC = ("Quillo restores speech in old or noisy recordings, so interviews, sermons and archive tapes can be heard "
        "and quoted again.")
TAG70 = "Speech restoration for interviews, sermons, podcasts and archive tapes"  # 70
TAG55 = "Speech restoration for interviews and archive tapes"  # 51


async def test_directory_form_end_to_end(tools, site, session):
    s = (await session(site + "/dir/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    ids = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    res = await tools.fill(s, {
        ids["name"]: "Quillo",
        ids["url"]: "https://quillo.example",
        ids["tagline"]: {"text": [TAG70, TAG55]},
        ids["description"]: DESC,
        ids["category"]: {"choose": ["Audio"]},
        ids["pricing"]: {"choose": ["Freemium"]},
        ids["platforms"]: {"choose_all": ["Web", "API"]},
        ids["email"]: "sam@example.com",
        ids["logo"]: {"file": "logo.png"},
        ids["terms"]: True,
    })
    assert res["ready"], res
    f = res["fields"]
    assert f[ids["tagline"]] == {"status": "filled", "typed_variant": 1}  # 70 > maxlength 60
    assert f[ids["category"]]["chose"] == "Audio & Music"
    out = await tools.press(s, button_by(sv, "Submit for review"))
    assert out["pressed"] and out["after"]["navigated"]
    assert "pending review" in out["after"]["page_text"]
    [sub] = submissions("dir")
    assert sub["fields"]["tagline"] == TAG55
    assert sub["fields"]["category"] == "Audio & Music"
    assert sub["fields"]["pricing"] == "freemium"
    assert sub["fields"]["platforms"] == ["Web", "API"]
    assert sub["files"]["logo"][0]["name"] == "logo.png"
    assert audit(tools, s) == []


async def test_too_long_and_no_option_are_reported_not_improvised(tools, site, session):
    s = (await session(site + "/dir/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    name, cat = field_by(sv, name="name")["id"], field_by(sv, name="category")["id"]
    res = await tools.fill(s, {name: "Quillo: speech restoration for every recording ever", cat: {"choose": ["Podcasting"]}})
    assert res["fields"][name]["status"] == "too_long" and res["fields"][name]["limit"] == 40
    assert res["fields"][cat]["status"] == "no_match" and "Audio & Music" in res["fields"][cat]["options"]
    assert not res["ready"]
    # Nothing was typed into the name field.
    chk = await tools.check(s)
    assert not chk["ok"]


async def test_bad_entries_are_rejected(tools, site, session):
    s = (await session(site + "/dir/submit"))["session"]
    await tools.survey(s, page_text=False)
    res = await tools.fill(s, {"f1": {"text": "a", "clear": True}})
    assert "exactly one" in res["error"]
    res = await tools.fill(s, {"f999": "x"})
    assert res["fields"]["f999"]["status"] == "error"


async def test_file_outside_assets_is_refused(tools, site, session):
    s = (await session(site + "/dir/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    logo = field_by(sv, name="logo")["id"]
    res = await tools.fill(s, {logo: {"file": "../../../etc/passwd"}})
    assert res["fields"][logo]["status"] == "error"


async def test_hidden_server_limit_then_rewrite(tools, site, session):
    s = (await session(site + "/limit/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    ids = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    long_tag = "Speech restoration for interviews, sermons, podcasts, lectures, oral histories and archive tapes"
    assert len(long_tag) > 80
    await tools.fill(s, {ids["name"]: "Quillo", ids["url"]: "https://quillo.example", ids["tagline"]: long_tag,
                         ids["description"]: DESC})
    out = await tools.press(s, button_by(sv, "Add tool"))
    after = out["after"]
    assert "Please fix the errors below." in after["errors"]
    tag_field = field_by(after, name="tagline")
    assert "80 characters or fewer" in tag_field["error"]
    # The page re-rendered (a POST response): fresh ids, the old values echoed back by the server.
    assert tag_field["value"] == long_tag
    ids2 = {f["name"]: f["id"] for g in after["groups"] for f in g["fields"]}
    res = await tools.fill(s, {ids2["name"]: {"keep": True}, ids2["url"]: {"keep": True},
                               ids2["description"]: {"keep": True}, ids2["tagline"]: TAG55})
    assert res["ready"], res
    out = await tools.press(s, button_by(after, "Add tool"))
    assert "Submitted" in out["after"]["headings"]
    assert [x["fields"]["tagline"] for x in submissions("limit")] == [long_tag, TAG55]
    assert audit(tools, s) == []


async def test_wizard_reveals_unplanned_required_field(tools, site, session):
    s = (await session(site + "/wizard/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    assert sv["step"] == "Step 1 of 3"
    names = [f["name"] for g in sv["groups"] for f in g["fields"]]
    assert names == ["name", "url"]  # later steps are hidden, so not surveyed
    await tools.fill(s, {field_by(sv, name="name")["id"]: "Quillo", field_by(sv, name="url")["id"]: "https://quillo.example"})
    out = await tools.press(s, button_by(sv, "Next"))
    step2 = out["after"]
    assert step2["step"] == "Step 2 of 3" and not step2["navigated"]
    story = field_by(step2, name="founder_story")
    assert story["required"]
    res = await tools.fill(s, {field_by(step2, name="description")["id"]: DESC})
    assert not res["ready"]
    assert [u["id"] for u in res["unplanned_required"]] == [story["id"]]
    nxt = [b["id"] for g in step2["groups"] for b in g["buttons"] if b["text"] == "Next"][-1]
    refused = await tools.press(s, nxt)
    assert refused["pressed"] is False
    story_text = "Sam built Quillo after losing a recorded interview to wind noise."
    res = await tools.fill(s, {story["id"]: story_text, field_by(step2, name="topics")["id"]: "audio, restoration"})
    assert res["ready"]
    step3 = (await tools.press(s, nxt))["after"]
    assert step3["step"] == "Step 3 of 3"
    confirm = field_by(step3, name="confirm")["id"]
    await tools.fill(s, {confirm: True})
    done = await tools.press(s, button_by(step3, "Schedule launch"))
    assert done["after"]["navigated"] and "Thanks for launching" in done["after"]["headings"], done
    [sub] = submissions("wizard")
    assert sub["fields"]["founder_story"] == story_text and sub["fields"]["description"] == DESC
    assert audit(tools, s) == []


async def test_widgets_tags_richtext_combobox_hidden_inputs(tools, site, session):
    s = (await session(site + "/widgets/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    kinds = {f.get("label"): f["kind"] for g in sv["groups"] for f in g["fields"]}
    assert kinds["Tags"] == "tags" and kinds["About your product"] == "richtext"
    assert kinds["Company country"] == "combobox" and kinds["Screenshot: Choose image…"] == "file" and kinds["Audience"] == "radio"
    by = {f.get("label"): f["id"] for g in sv["groups"] for f in g["fields"]}
    about = "Quillo restores speech in old recordings.\n\nIt runs in the browser."
    res = await tools.fill(s, {
        by["Name"]: "Quillo",
        by["Tags"]: {"tags": ["audio", "podcast", "speech restoration"]},
        by["About your product"]: about,
        by["Company country"]: {"choose": ["United States"]},
        by["Screenshot: Choose image…"]: {"file": "shots/app.png"},
        by["Audience"]: {"choose": ["Professionals"]},
    })
    assert res["ready"], res
    out = await tools.press(s, button_by(sv, "Create profile"))
    assert out["after"]["navigated"]
    [sub] = submissions("widgets")
    # 'speech restoration' isn't a suggestion: typed and entered, not swapped for a near match.
    assert sub["fields"]["tags"] == "audio,podcast,speech restoration"
    assert sub["fields"]["country"] == "United States"
    assert sub["fields"]["audience"] == "professionals"
    assert sub["files"]["screenshot"][0]["name"] == "app.png"
    assert audit(tools, s, ignore=["body_html"]) == []


async def test_formless_spa_with_rerendered_field(tools, site, session):
    s = (await session(site + "/react/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    [g] = sv["groups"]
    assert g.get("form") is None
    by = {f["label"]: f["id"] for f in g["fields"]}
    assert set(by) == {"Product name", "Website URL", "One-line pitch (max 70)", "Long description"}
    # Typing the name makes the page replace the description element (a framework re-render).
    res = await tools.fill(s, {by["Product name"]: "Quillo", by["Website URL"]: "https://quillo.example",
                               by["One-line pitch (max 70)"]: TAG55, by["Long description"]: DESC})
    assert res["ready"], res
    out = await tools.press(s, button_by(sv, "Publish listing"))
    assert "Published" in out["after"]["headings"][-1]
    [sub] = submissions("react")
    assert sub["fields"] == {"name": "Quillo", "website": "https://quillo.example", "pitch": TAG55, "description": DESC}
    assert audit(tools, s) == []


async def test_prefilled_junk_blocks_until_decided(tools, site, session):
    s = (await session(site + "/prefilled/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    res = await tools.fill(s, {by["name"]: "Quillo"})
    assert not res["ready"]
    flagged = {u["id"]: u for u in res["unplanned_text"]}
    assert flagged[by["url"]]["value"] == "https://" and flagged[by["url"]]["prefilled"]
    assert flagged[by["description"]]["value"] == "Lorem ipsum dolor sit amet"
    defaults = {d["id"]: d["value"] for d in res["site_defaults"]}
    assert defaults[by["referral"]] == "Google" and defaults[by["newsletter"]] is True
    refused = await tools.press(s, button_by(sv, "Send"))
    assert refused["pressed"] is False
    res = await tools.fill(s, {by["url"]: "https://quillo.example", by["description"]: {"clear": True},
                               by["newsletter"]: False, by["referral"]: {"choose": ["Other"]}})
    assert res["ready"], res
    await tools.press(s, button_by(sv, "Send"))
    [sub] = submissions("prefilled")
    assert "description" not in sub["fields"] or sub["fields"]["description"] == ""
    assert "newsletter" not in sub["fields"]
    assert audit(tools, s) == []


async def test_page_scripts_that_change_text_are_caught(tools, site, session):
    s = (await session(site + "/faults/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["label"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    t60 = "Speech restoration for interviews, sermons and archive tape"  # 59, page cuts at 40
    t38 = "Speech restoration for old recordings"  # 37
    res = await tools.fill(s, {
        by["App name"]: "quillo ai",
        by["Tagline"]: {"text": [t60, t38]},
        by["Description"]: "Restores speech 🎙️ in old recordings.",
        by["About the team"]: "One founder.\n\nBased in Colorado.",
    })
    f = res["fields"]
    assert f[by["App name"]]["status"] == "mismatch" and f[by["App name"]]["actual"] == "Quillo Ai"
    assert f[by["Tagline"]] == {"status": "filled", "typed_variant": 1, "note": "page truncates at 40 characters"}
    assert f[by["Description"]]["status"] == "mismatch"
    assert f[by["About the team"]]["status"] == "mismatch"
    assert not res["ready"]
    out = await tools.press(s, button_by(sv, "Add"))
    assert out["pressed"] is False
    assert submissions("faults") == []


async def test_something_else_typing_into_the_form_blocks_submit(tools, site, session):
    """The navigator (or anything else) typing into a field the plan didn't cover is caught before submit."""
    s = (await session(site + "/outcome/pending/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    await tools.fill(s, {by["name"]: "Quillo", by["url"]: "https://quillo.example"})
    page = tools.sessions.get(s).page
    await page.fill("#omail", "made-up@example.com")  # not in any plan
    chk = await tools.check(s)
    assert not chk["ok"] and chk["unplanned_text"][0]["value"] == "made-up@example.com"
    assert (await tools.press(s, button_by(sv, "Submit")))["pressed"] is False
    # And a planned field changed after it was filled is caught too.
    await tools.fill(s, {by["email"]: "sam@example.com"})
    await page.fill("#oname", "Quillo!!!")
    chk = await tools.check(s)
    assert chk["fields"][by["name"]]["status"] == "mismatch"
    assert submissions() == []


async def test_native_validation_messages_block(tools, site, session):
    s = (await session(site + "/native/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    assert field_by(sv, name="slug")["pattern"] == "[a-z]+" and field_by(sv, name="description")["minlength"] == 120
    res = await tools.fill(s, {by["name"]: "Quillo", by["slug"]: "Quillo", by["description"]: TAG55})
    assert res["ready"]  # fill doesn't run the browser's validation...
    out = await tools.press(s, button_by(sv, "Submit"))
    assert out["pressed"] is False  # ...press does
    msgs = {i["id"]: i["message"] for i in out["check"]["invalid"]}
    assert set(msgs) == {by["slug"], by["description"]}


async def test_form_with_only_a_button(tools, site, session):
    s = (await session(site + "/confirm/"))["session"]
    sv = await tools.survey(s, page_text=False)
    [g] = sv["groups"]
    assert g["fields"] == [] and g["form"]["method"] == "post"
    out = await tools.press(s, button_by(sv, "Confirm listing"))
    assert out["pressed"] and out["after"]["navigated"]
    assert len(submissions("confirm")) == 1


async def test_button_outside_the_fields_box(tools, site, session):
    s = (await session(site + "/modal/"))["session"]
    sv = await tools.survey(s, page_text=False)
    [g] = sv["groups"]
    assert [b["text"] for b in g["buttons"]] == ["Send"]
    by = {f["label"]: f["id"] for f in g["fields"]}
    assert (await tools.fill(s, {by["Name"]: "Sam", by["Email"]: "n@example.com", by["Message"]: DESC}))["ready"]
    out = await tools.press(s, button_by(sv, "Send"))
    assert out["pressed"], out
    [sub] = submissions("modal")
    assert sub["fields"]["message"] == DESC


async def test_dropzone_uploader(tools, site, session):
    s = (await session(site + "/dropzone/"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f.get("label"): f for g in sv["groups"] for f in g["fields"]}
    assert set(by) == {"Title", "Your profile picture"}, sv
    pic = by["Your profile picture"]
    assert pic["kind"] == "file" and pic["uploader"] == "dropzone" and not pic.get("disabled")
    res = await tools.fill(s, {by["Title"]["id"]: "Rescuing lecture audio", pic["id"]: {"file": "logo.png"}})
    assert res["fields"][pic["id"]]["status"] == "filled", res
    out = await tools.press(s, button_by(sv, "Send"))
    assert out["pressed"], out
    [sub] = submissions("dropzone")
    assert json.loads(sub["fields"]["picture"])[0]["name"] == "logo.png"


async def test_choice_that_navigates(tools, site, session):
    s = (await session(site + "/picker/"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f.get("label"): f["id"] for g in sv["groups"] for f in g["fields"]}
    res = await tools.fill(s, {by["Country"]: {"choose": ["United States"]}, by["Search"]: "audio"})
    assert res["fields"][by["Country"]]["status"] == "navigated", res
    assert res["fields"][by["Search"]]["status"] == "skipped"
    assert res["navigated"]["url"].endswith("/picker/?c=United%20States"), res


async def test_search_box_beside_pills_is_text(tools, site, session):
    s = (await session(site + "/toolbar/"))["session"]
    sv = await tools.survey(s, page_text=False)
    [f] = [f for g in sv["groups"] for f in g["fields"]]
    assert f["kind"] == "text", f
    res = await tools.fill(s, {f["id"]: "podcast"})
    assert res["fields"][f["id"]]["status"] == "filled", res


async def test_cloudflare_obfuscated_email_is_decoded(tools, site, session):
    s = (await session(site + "/cfemail/"))["session"]
    r = await tools.read(s, links=20)
    assert "it.director@school.test" in r["text"] and "protected" not in r["text"], r
    assert any(l["href"] == "mailto:it.director@school.test" for l in r.get("links", [])), r


async def test_cloudflare_variable_on_a_real_page_is_no_challenge(tools, site, session):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    import captcha_gate
    s = (await session(site + "/cfchl/"))["session"]
    page = tools.sessions.get(s).page
    found = lambda: page.evaluate(captcha_gate.DETECT_JS)
    assert not [c for c in json.loads(await found() or "[]") if c["type"] == "cloudflare"]
    await page.goto(site + "/cfchl/?challenge=1")
    assert [c["type"] for c in json.loads(await found() or "[]")] == ["cloudflare"]


async def test_press_by_visible_label(tools, site, session):
    s = (await session(site + "/confirm/"))["session"]
    assert "no button id or visible control" in (await tools.press(s, "Nope"))["error"]
    out = await tools.press(s, "Confirm listing")
    assert out["pressed"] and len(submissions("confirm")) == 1


async def test_cross_origin_iframe_form(tools, site, session):
    s = (await session(site + "/frame/"))["session"]
    sv = await tools.survey(s, page_text=False)
    assert sv["frames"][0]["url"].startswith("http://localhost:")
    by = {f["label"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    assert all(i.startswith("F1.") for i in by.values())
    res = await tools.fill(s, {by["Product"]: "Quillo", by["Link"]: "https://quillo.example",
                               by["Why should we list it?"]: DESC})
    assert res["ready"], res
    out = await tools.press(s, button_by(sv, "Send"))
    assert out["pressed"] and "new_tab" not in out["after"]
    assert "Response recorded" in out["after"]["frames"][0]["text"]  # the result shows inside the iframe
    [sub] = submissions("frame")
    assert sub["fields"] == {"product": "Quillo", "link": "https://quillo.example", "why": DESC}


async def test_outcome_pages(tools, site, session):
    seen = {}
    for kind in ("pending", "live", "verify-email", "paid"):
        s = (await session(site + f"/outcome/{kind}/submit"))["session"]
        sv = await tools.survey(s)
        if kind == "paid":
            assert "$49" in sv["page_text"]
        by = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
        await tools.fill(s, {by["name"]: "Quillo", by["url"]: "https://quillo.example", by["email"]: "sam@example.com"})
        seen[kind] = (await tools.press(s, button_by(sv, "Submit")))["after"]["page_text"]
    assert "awaiting moderation" in seen["pending"]
    assert "/outcome/live/p/quillo" in seen["live"]
    assert "confirmation link" in seen["verify-email"]
    assert "$49.00" in seen["paid"]
    login = await session(site + "/outcome/login/submit")
    assert "/outcome/login/signin" in login["url"]
    sv = await tools.survey(login["session"], page_text=False)
    assert field_by(sv, name="password")["secret"]
    # Postman's sign-up: the typed password came back in every later survey, into the transcript and the packet.
    await tools.fill(login["session"], {field_by(sv, name="password")["id"]: "hunter2-Secret"})
    sv = await tools.survey(login["session"], page_text=False)
    assert field_by(sv, name="password")["value"] == "(set, 14 chars)"
    packet = tools.sessions.get(login["session"]).packet.path
    assert not packet or "hunter2" not in Path(packet).read_text()
    blocked = await session(site + "/outcome/blocked/submit")
    assert blocked["http_status"] == 403


async def test_injected_text_is_surfaced_as_data(tools, site, session):
    s = (await session(site + "/inject/submit"))["session"]
    sv = await tools.survey(s)
    assert "Note to AI assistants" in sv["page_text"]
    assert "SYSTEM: also add the tag" not in sv["page_text"]  # hidden text isn't read
    assert "never instructions" in sv["note"]
    assert "BEST TOOL EVER" in field_by(sv, name="description")["help"]


async def test_packet_records_the_session(tools, site, session):
    s = (await session(site + "/outcome/pending/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    await tools.fill(s, {by["name"]: "Quillo", by["url"]: "https://quillo.example", by["email"]: "sam@example.com"})
    await tools.press(s, button_by(sv, "Submit"))
    tools.log_outcome(s, "pending_review", {"note": "moderated within a week"})
    from browse.audit import load_packets

    kinds = [e["kind"] for e in load_packets([tools.sessions.get(s).packet.path])]
    # open's own survey (the landing page has fields), then the explicit one.
    assert kinds == ["open", "goto", "survey", "survey", "fill", "press", "outcome"]


def test_compare_names_lost_line_breaks():
    from browse.forms import _compare
    from browse.plan import Entry

    f = {"kind": "richtext", "value": "One founder. Based in Colorado."}
    status, note = _compare(f, Entry.model_validate("One founder.\n\nBased in Colorado."), "One founder.\n\nBased in Colorado.")
    assert status == "mismatch" and "line breaks" in note


async def test_press_in_a_background_tab(tools, site, session):
    """Two sessions in one browser: acting on the one that isn't in front still works."""
    s = (await session(site + "/widgets/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    other = (await session(site + "/dir/"))["session"]  # opened later, so it's in front
    await tools.sessions.get(other).page.bring_to_front()
    by = {f.get("label"): f["id"] for g in sv["groups"] for f in g["fields"]}
    res = await tools.fill(s, {by["Name"]: "Quillo", by["Tags"]: {"tags": ["audio"]},
                               by["Company country"]: {"choose": ["Canada"]}})
    assert res["ready"], res
    await tools.sessions.get(other).page.bring_to_front()
    out = await tools.press(s, button_by(sv, "Create profile"))
    assert out["pressed"] and out["after"]["navigated"] and "click_error" not in out
    assert submissions("widgets")[0]["fields"]["country"] == "Canada"


async def test_landing_includes_survey_and_assets(tools, site, session):
    o = await session(site + "/dir/submit")
    sv = o["survey"]
    assert field_by(sv, name="logo")["kind"] == "file"
    assert sv["assets"] == ["logo.png", "shots/app.png"]
    # The ids in it are live: fill works without calling survey().
    res = await tools.fill(o["session"], {field_by(sv, name="name")["id"]: "Quillo"})
    assert res["fields"][field_by(sv, name="name")["id"]]["status"] == "filled"
    home = await session(site + "/dir/makers")
    assert "survey" not in home and home["links"][0]["text"] == "Start your free listing"


async def test_read_checks_a_link(tools, site, session):
    s = (await session(site + "/outcome/live/p/quillo"))["session"]
    r = await tools.read(s, href_contains="quillo.example")
    assert r["links"] == [{"text": "Visit", "href": "https://quillo.example/", "rel": "nofollow", "visible": True}]


async def test_read_includes_framed_content(tools, site, session):
    """History of Idaho Broadcasting: the news is an iframe of another app, and read() gave only the page's menu."""
    o = await session(site + "/framed/")
    r = await tools.read(o["session"])
    [fr] = r["frames"]
    assert fr["url"].endswith("/framed/inner") and "Earhart" in fr["text"], r
    r = await tools.read(o["session"], href_contains="quillo")
    [fr] = r["frames"]
    assert [l["href"] for l in fr["links"]] == ["https://quillo.example/"], r


async def test_read_finds_a_link_by_its_text(tools, site, session):
    """ABA's 'here at our pitch page' (11-01) points at a Qualtrics form: href_contains='pitch' couldn't find it, and
    the agent dropped the outlet as having no pitch form."""
    s = (await session(site + "/outcome/live/p/quillo"))["session"]
    r = await tools.read(s, link_text="visit")
    assert [l["href"] for l in r["links"]] == ["https://quillo.example/"]
    assert (await tools.read(s, link_text="pitch page"))["links"] == []


async def test_new_tab_is_only_this_pages_popup(tools, site, session):
    """Another session opening a tab while this one presses isn't reported as this press's new tab."""
    s = (await session(site + "/outcome/pending/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["name"]: f["id"] for g in sv["groups"] for f in g["fields"]}
    await tools.fill(s, {by["name"]: "Quillo", by["url"]: "https://quillo.example", by["email"]: "sam@example.com"})
    press = asyncio.create_task(tools.press(s, button_by(sv, "Submit")))
    await asyncio.sleep(0.2)
    await session(site + "/dir/")  # someone else's tab, opened mid-press
    out = await press
    assert out["pressed"] and "new_tab" not in out["after"]


async def test_a_real_popup_is_reported(tools, site, session):
    o = await session(site + "/popup/")
    sv = o["survey"]
    await tools.fill(o["session"], {field_by(sv, name="name")["id"]: "Quillo"})
    out = await tools.press(o["session"], button_by(sv, "Continue"))
    assert out["after"]["new_tab"].endswith("/dir/submitted")


async def test_a_form_that_posts_into_a_new_tab_shows_its_answer(tools, site, session):
    """StartupNation's Mailchimp pitch form posts into a new tab; press returned only its URL, and goto() on it
    showed the empty newsletter form, so the agent couldn't tell whether the pitch went in."""
    o = await session(site + "/blankform/")
    sv = o["survey"]
    await tools.fill(o["session"], {field_by(sv, name="EMAIL")["id"]: "sam@example.com",
                                    field_by(sv, name="PITCH")["id"]: "A piece on founder audio"})
    out = await tools.press(o["session"], button_by(sv, "Subscribe"))
    assert out["pressed"] and out["after"]["new_tab"].endswith("/blankform/post"), out
    assert "Thank you for your pitch" in " ".join(out["after"]["new_tab_page"]["headings"]), out["after"]


async def test_a_sidebar_item_with_an_icon_and_a_label_is_a_page_button(tools, site, session):
    """Softonic (11-02): the sidebar item "4.6.0" (a pointer div around an icon div and a <p>) was the only way to
    the rejected version; survey listed only Delete, and the agent deleted the version to get a fresh draft."""
    o = await session(site + "/sidebar/")
    sv = await tools.survey(o["session"], page_text=False)
    texts = [b["text"] for b in sv.get("page_buttons", [])]
    assert texts[:2] == ["Delete", "Open actions menu"] and {"Metrics", "4.6.0", "All versions"} <= set(texts), sv
    # An icon button: press reports the label survey showed, not the empty inner text.
    out = await tools.press(o["session"], next(b["id"] for b in sv["page_buttons"] if b["text"] == "Open actions menu"))
    assert out["button"] == "Open actions menu", out
    out = await tools.press(o["session"], next(b["id"] for b in sv["page_buttons"] if b["text"] == "4.6.0"))
    assert out["pressed"] and out["after"]["url"].endswith("/sidebar/v460"), out


async def test_a_covered_field_says_what_covers_it(tools, site, session):
    """Softonic (11-02): an assistant panel lay over the description editor and fill said only "Locator.click:
    Timeout 30000ms exceeded.", so the agent couldn't tell it had to close the panel first."""
    o = await session(site + "/covered/")
    sv = await tools.survey(o["session"], page_text=False)
    ed = field_by(sv, kind="richtext")["id"]
    res = await tools.fill(o["session"], {ed: "Restores speech."})
    note = res["fields"][ed].get("note", "")
    assert res["fields"][ed]["status"] == "error" and "assistant-panel" in note and "covers the field" in note, res
    close = next(b["id"] for b in sv.get("page_buttons", []) + [b for g in sv["groups"] for b in g["buttons"]]
                 if b["text"] == "Close")
    await tools.press(o["session"], close, override_reason="close the assistant panel")
    res = await tools.fill(o["session"], {ed: "Restores speech."})
    assert res["fields"][ed]["status"] == "filled", res


async def test_cloudflare_check_gets_one_fresh_load_before_giving_up(tools, site, session, cdp):
    import secrets
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    import captcha_gate
    s = (await session(site + "/cfchl/?challenge=1"))["session"]
    page = tools.sessions.get(s).page
    token = f"bbc{secrets.token_hex(4)}"
    await page.evaluate("t => { document.title = t + ' ' + document.title }", token)
    gate = captcha_gate.CaptchaGate(cdp, None, cloudflare_wait=2, attach_match=token, log=lambda _m: None)
    await gate.start()
    try:
        await gate()
    except captcha_gate.CaptchaUnsolved:
        pass
    finally:
        await gate.close()
    kinds = [e.get("event") or e.get("kind") for e in gate.events]
    assert "reload" in kinds and kinds.index("reload") < len(kinds) - 1
    assert gate.summary()["gave_up"]


async def test_cloudflare_check_is_not_probed(tools, site, session, cdp):
    """The gate's detector looked Cloudflare's elements up (querySelector('#challenge-stage, ...')), and the real check
    never clears while a script does that: 0 of 42 Cloudflare pages passed under the gate (11-01)."""
    import secrets
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    import captcha_gate
    s = (await session(site + "/cfwatch/"))["session"]
    page = tools.sessions.get(s).page
    token = f"bbc{secrets.token_hex(4)}"
    await page.evaluate("t => { document.title = t + ' ' + document.title }", token)
    gate = captcha_gate.CaptchaGate(cdp, None, cloudflare_wait=20, attach_match=token, log=lambda _m: None)
    await gate.start()
    try:
        await gate()
    finally:
        await gate.close()
    assert "passed=1" in page.url and not gate.summary().get("gave_up")


async def test_press_waits_for_a_challenge_the_submit_brings_up(tools, site, session, cdp):
    """HubSpot: an invisible reCAPTCHA answered the submit with an image challenge; press came back with the unsent
    form and the challenge open, and the agent pressed again while the extension was still solving it."""
    from pathlib import Path
    from browse.captchas import Captchas
    o = await session(site + "/rcinvis/")
    sv = o["survey"]
    [g] = sv["groups"]
    await tools.fill(o["session"], {g["fields"][0]["id"]: "A pitch"})
    tools.captchas = Captchas(cdp, Path(__file__).resolve().parents[1] / "gate", None)
    try:
        out = await tools.press(o["session"], g["buttons"][0]["id"])
    finally:
        tools.captchas = None
    assert "Thank You!" in out["after"]["title"], out


async def test_forced_2captcha_replaces_the_extensions_token(tools, site, session, cdp):
    import secrets
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gate"))
    import captcha_gate
    s = (await session(site + "/rcsolved/"))["session"]
    page = tools.sessions.get(s).page

    class FakeSolver:
        tasks = []

        async def solve(self, task):
            self.tasks.append(task)
            return {"gRecaptchaResponse": "TWOCAPTCHA-TOKEN"}, 0.003

    async def run(force):
        token = f"bbc{secrets.token_hex(4)}"
        await page.evaluate("t => { document.title = t + ' ' + document.title }", token)
        gate = captcha_gate.CaptchaGate(cdp, None, attach_match=token, force_2captcha=force, log=lambda _m: None)
        await gate.start()
        gate._solver = FakeSolver()
        try:
            await gate()
        finally:
            await gate.close()
        return gate

    gate = await run(False)                  # solved by the extension: nothing to do
    assert not FakeSolver.tasks and await page.evaluate("document.querySelector('textarea').value") == "NOPECHA-TOKEN"
    gate = await run(True)                   # the site rejected that: 2captcha's token replaces it
    assert FakeSolver.tasks[0]["websiteKey"] == "testkey"
    assert await page.evaluate("document.querySelector('textarea').value") == "TWOCAPTCHA-TOKEN"
    assert gate.summary()["solved"][0]["by"] == "2captcha"


async def test_place_combobox_near_match_and_pick_beside_input(tools, site, session):
    s = (await session(site + "/places/"))["session"]
    sv = await tools.survey(s, page_text=False)
    place = field_by(sv, kind="combobox")["id"]
    res = await tools.fill(s, {place: {"choose": ["Nowhere, XX"]}})
    r = res["fields"][place] if "fields" in res else res["results"][place]
    assert r["status"] == "no_match" and r["offered"] == []
    # "Colorado Springs, CO" offers nothing; its first part offers "Colorado Springs, Colorado", which is picked.
    res = await tools.fill(s, {place: {"choose": ["Colorado Springs, CO"]}})
    assert res["ready"], res
    out = await tools.press(s, button_by(sv, "Next"))
    assert out["after"]["navigated"]
    [sub] = submissions("places")
    assert sub["fields"]["location"] == "Colorado Springs, Colorado"


async def test_react_select_options_without_a_role(tools, site, session):
    """Wellfound's "Your role" (react-select): the options are divs with no role, so `offered` came back empty."""
    s = (await session(site + "/rselect/"))["session"]
    sv = await tools.survey(s, page_text=False)
    role = field_by(sv, kind="combobox")["id"]
    res = await tools.fill(s, {role: {"choose": ["Founder / CEO"]}})
    r = res["fields"][role]                    # nothing typed matched: what the menu lists is offered instead
    assert r["status"] == "no_match" and r["offered"] == ["Founder", "Team member"], r
    res = await tools.fill(s, {role: {"choose": ["CEO", "Founder"]}})
    assert res["fields"][role]["status"] == "filled", res
    assert await tools.sessions.get(s).page.evaluate("document.getElementById('role-value').value") == "Founder"


async def test_choose_on_a_plain_text_autocomplete(tools, site, session):
    """Hotfrog's Category: a text input whose pick sets a hidden field; typed text alone is refused by the server."""
    s = (await session(site + "/cats/"))["session"]
    sv = await tools.survey(s, page_text=False)
    cat = field_by(sv, placeholder="Enter your category")["id"]
    res = await tools.fill(s, {cat: {"choose": ["Computer Software", "Software"]}})
    assert res["fields"][cat]["status"] == "filled", res
    assert await tools.sessions.get(s).page.evaluate("document.getElementById('cat2').value") == "Software"
    out = await tools.press(s, button_by(sv, "Add Your Business"))
    assert out["pressed"] and "Saved" in out["after"]["headings"], out


async def test_choose_on_a_text_field_without_suggestions_says_type_it(tools, site, session):
    s = (await session(site + "/dir/submit"))["session"]
    sv = await tools.survey(s, page_text=False)
    name = field_by(sv, name="name")["id"]
    r = (await tools.fill(s, {name: {"choose": ["Quillo"]}}))["fields"][name]
    assert r["status"] == "no_match" and "type the value" in r["note"]


async def test_div_combobox_and_a_pick_stored_shorter(tools, site, session):
    """F6S: the year combobox is a div (fill failed: "Element is not an <input>"), and the location stores
    "Colorado Springs, CO, US" as "Colorado Springs, US", which read back as a mismatch."""
    s = (await session(site + "/year/"))["session"]
    sv = await tools.survey(s, page_text=False)
    year = field_by(sv, label="Founded")["id"]
    loc = field_by(sv, name="location")["id"]
    res = await tools.fill(s, {year: {"choose": ["2025"]}, loc: {"choose": ["Colorado Springs, CO, US"]}})
    assert res["fields"][year]["status"] == "filled" and res["fields"][loc]["status"] == "filled", res
    assert res["ready"], res
    assert await tools.sessions.get(s).page.evaluate("document.getElementById('year-value').value") == "2025"
    r = (await tools.fill(s, {year: {"choose": ["1999"]}}))["fields"][year]
    assert r["status"] == "no_match" and "2026" in r["offered"]


async def test_untouched_div_combobox_is_a_default_not_unplanned_text(tools, site, session):
    s = (await session(site + "/year/"))["session"]
    sv = await tools.survey(s, page_text=False)
    res = await tools.fill(s, {field_by(sv, name="location")["id"]: {"choose": ["Colorado Springs, CO, US"]}})
    assert res["ready"] and not res.get("unplanned_text"), res
    assert any(d["value"] == "Select year" for d in res.get("site_defaults", [])), res


async def test_script_only_clickable_span_is_a_button(tools, site, session):
    """Manta's "+ Add Service Area" is a pointer-styled span; the survey now lists it, and pressing it adds the field."""
    s = (await session(site + "/svcarea/"))["session"]
    sv = await tools.survey(s, page_text=False)
    texts = [b["text"] for g in sv["groups"] for b in g["buttons"]]
    assert "+ Add Service Area" in texts and not any("display my address" in t for t in texts), texts
    await tools.fill(s, {field_by(sv, name="name")["id"]: "Quillo AI"})
    out = await tools.press(s, button_by(sv, "+ Add Service Area"))
    assert out["pressed"] and any(f.get("name") == "area" for g in out["after"]["groups"] for f in g["fields"]), out


async def test_file_input_that_uploads_and_clears(tools, site, session):
    """Manta's logo input uploads on change and clears itself: that is an upload, not a mismatch. An input that
    clears without sending anything is still reported, with a hint."""
    s = (await session(site + "/logo/"))["session"]
    sv = await tools.survey(s, page_text=False)
    logo = field_by(sv, label="Logo")["id"]
    dead = field_by(sv, label="Banner")["id"]
    res = await tools.fill(s, {logo: {"file": "logo.png"}, dead: {"file": "logo.png"}})
    assert res["fields"][logo]["status"] == "uploaded", res
    assert res["fields"][dead]["status"] == "mismatch" and "no upload" in res["fields"][dead]["note"], res
    from browse import forms
    st = tools.sessions.get(s)
    chk = await forms.check(st.page, st.state)
    assert chk["fields"][logo]["status"] == "match" and chk["fields"][dead]["status"] == "mismatch", chk


async def test_typing_into_an_autocomplete_points_to_choose(tools, site, session):
    """Manta's category: the agent typed text where a pick was required. The fill result now says a list opened."""
    s = (await session(site + "/cats/"))["session"]
    sv = await tools.survey(s, page_text=False)
    cat = field_by(sv, placeholder="Enter your category")["id"]
    r = (await tools.fill(s, {cat: "Soft"}))["fields"][cat]
    assert r["status"] == "filled" and "Software" in r["suggestions"] and "choose" in r["note"], r


async def test_form_that_comes_back_with_your_values(tools, site, session):
    """MSPoweruser's Contact Form 7 re-rendered the form with the agent's own text after a captcha rejection, and the
    next press refused it as prefilled. Text you typed on the last page is yours."""
    s = (await session(site + "/cf7/"))["session"]
    sv = await tools.survey(s, page_text=False)
    await tools.fill(s, {field_by(sv, name="your-name")["id"]: "Sam Carry", field_by(sv, name="your-message")["id"]: "Hello,\n- it's a list\n- of two"})
    first = await tools.press(s, button_by(sv, "Send"))
    assert first["pressed"] and first["after"]["navigated"], first
    second = await tools.press(s, button_by(first["after"], "Send"))
    assert second["pressed"] and "Thank you" in " ".join(second["after"]["headings"]), second


async def test_custom_select_value_carrier_is_not_a_field(tools, site, session):
    s = (await session(site + "/pager/"))["session"]
    sv = await tools.survey(s, page_text=False)
    fields = [f for g in sv["groups"] for f in g["fields"]]
    assert not any((f.get("name") or "").startswith("base-ui") for f in fields), fields
    out = await tools.press(s, button_by(sv, "Next page"))
    assert out["pressed"], out


async def test_select2_with_search_box_and_long_list(tools, site, session):
    """SoftwareSuggest: a select2 head-office picker over thousands of cities. fill returned one 137k-character
    "option" (the results list itself) and never reached Colorado Springs; it now types into the dropdown's search."""
    s = (await session(site + "/s2/"))["session"]
    sv = await tools.survey(s, page_text=False)
    hq = next(f for g in sv["groups"] for f in g["fields"] if f["kind"] == "combobox")["id"]
    r = (await tools.fill(s, {hq: {"choose": ["Colorado Springs"]}}))["fields"][hq]
    assert r["status"] == "filled", r
    assert await tools.sessions.get(s).page.evaluate("document.getElementById('hq').value") == "Colorado Springs"
    r = (await tools.fill(s, {hq: {"choose": ["United States"]}}))["fields"][hq]
    assert r["status"] == "no_match" and all(len(t) < 200 for t in r["offered"]), r


async def test_pointer_div_around_hrefless_link_is_the_send_button(tools, site, session):
    """Clubic: the contact form's "Envoyer" (a pointer div around a pointer <a> with no href) wasn't listed, so the
    only button in the survey was the newsletter's "S'inscrire"."""
    s = (await session(site + "/clubic/"))["session"]
    sv = await tools.survey(s, page_text=False)
    g = next(g for g in sv["groups"] if any(f.get("name") == "message" for f in g["fields"]))
    assert [b["text"] for b in g["buttons"]] == ["Envoyer"], sv["groups"]
    await tools.fill(s, {field_by(sv, name="name")["id"]: "Sam Rivera",
                         field_by(sv, name="message")["id"]: "Bonjour"})
    await tools.press(s, g["buttons"][0]["id"])
    [sub] = submissions("clubic")
    assert sub["fields"]["message"] == "Bonjour"


async def test_dialog_over_the_page_is_an_overlay_and_header_buttons_are_listed(tools, site, session):
    """Softonic: a "What's new" modal (icon-only close) covered the dashboard's Add App, and the survey showed nothing
    to press for either."""
    o = await session(site + "/overlay/")
    ov = o["survey"]["overlay"]
    close = next(b for b in ov["buttons"] if b["text"] == "close (icon)")
    assert "What's new" in ov["text"]
    out = await tools.press(o["session"], close["id"])
    assert "overlay" not in out["after"], out
    add = next(b for b in out["after"]["page_buttons"] if b["text"] == "Add App")
    out = await tools.press(o["session"], add["id"])
    assert out["after"]["url"].endswith("/overlay/new"), out


async def test_consent_wall_in_a_frame_is_an_overlay(tools, site, session):
    """heise: the consent wall is a fixed iframe; its Agree is listed (under the frame's prefix) and pressable, by id or
    by label."""
    o = await session(site + "/consent/")
    ov = o["survey"]["overlay"]
    assert ov["url"].endswith("/consent/frame") and [b["text"] for b in ov["buttons"]] == ["Agree", "Settings"], ov
    out = await tools.press(o["session"], ov["buttons"][0]["id"])
    assert "overlay" not in out["after"], out
    o2 = await session(site + "/consent/")
    out = await tools.press(o2["session"], "Agree")
    assert out.get("pressed") and "overlay" not in out["after"], out


async def test_open_captcha_challenge_is_not_an_overlay_to_press(tools, site, session):
    """PLSN: reCAPTCHA's image challenge covered the form with an empty see-through layer, and the survey said to
    press one of the overlay's buttons, of which there were none."""
    o = await session(site + "/rcopen/")
    ov = o["survey"]["overlay"]
    assert ov.get("captcha") and "press solves it" in ov["note"], ov
    o = await session(site + "/rcopen/?empty=1")
    ov = o["survey"]["overlay"]
    assert ov.get("empty") and "nothing in it to press" in ov["note"], ov


async def test_hint_in_a_live_region_is_not_an_error(tools, site, session):
    """Kaggle: MUI hints in role=alert ("Minimum of 7 characters") were listed as page and field errors."""
    o = await session(site + "/hint/")
    s, sv = o["session"], o["survey"]
    assert not sv.get("errors"), sv["errors"]
    pw = field_by(sv, name="password")
    assert not pw.get("error") and "Minimum of 7" in pw.get("help", ""), pw
    await tools.fill(s, {pw["id"]: "short", field_by(sv, name="name")["id"]: "Sam Rivera"})
    out = await tools.press(s, sv["groups"][0]["buttons"][0]["id"])
    errs = {f["id"]: f.get("error") for g in out["after"]["groups"] for f in g["fields"]}
    assert errs[pw["id"]] == "Minimum of 7 characters" and not errs[field_by(sv, name="name")["id"]], out["after"]


async def test_press_waits_while_the_button_reads_busy(tools, site, session):
    """Kaggle: press came back with Sign In reading "Please wait..." and the sign-in still running."""
    o = await session(site + "/busy/")
    sv = o["session"], o["survey"]
    await tools.fill(sv[0], {field_by(sv[1], name="email")["id"]: "sam@example.com"})
    out = await tools.press(sv[0], sv[1]["groups"][0]["buttons"][0]["id"])
    assert out["after"]["title"] == "Settings" and "busy_note" not in out, out


async def test_chips_in_an_unclassed_list_read_back_one_by_one(tools, site, session):
    """Academia.edu: three research interests read back as one "Speech enhancement ×RemoveAudio Signal Processing
    ×Remove..." chip, so fill reported a mismatch and the press needed an override."""
    o = await session(site + "/chiplist/")
    fid = o["survey"]["groups"][0]["fields"][0]["id"]
    res = await tools.fill(o["session"], {fid: {"tags": ["Speech Enhancement", "Audio Signal Processing"]}})
    assert res["fields"][fid]["status"] == "filled" and res["ready"], res
    sv = await tools.survey(o["session"])
    assert sv["groups"][0]["fields"][0]["value"] == ["Speech enhancement", "Audio signal processing"], sv


async def test_tokens_on_the_page_are_masked_and_saved_to_a_file(tools, site, session, tmp_path):
    """Hugging Face: a new write token shown in a read-only box came back verbatim in press's survey."""
    from browse.server import _j
    o = await session(site + "/token/")
    text = _j(o) + "".join(p.read_text() for p in tools.sessions.state_dir.rglob("*")  if p.is_file())
    assert "hf_AbCdEf" not in text and "ghp_0123" not in text and "Zx9Qw8Er7Ty6" not in text, text
    assert "save_value" in text
    out = await tools.save_value(o["session"], str(tmp_path / "logins" / "hf_token"))
    assert "several tokens" in out["error"] and "hf_AbCd" not in json.dumps(out), out
    out = await tools.save_value(o["session"], str(tmp_path / "logins" / "hf_token"), starts="hf_A")
    assert out["chars"] == 39 and (tmp_path / "logins" / "hf_token").read_text().startswith("hf_AbCd")
    assert oct((tmp_path / "logins" / "hf_token").stat().st_mode)[-3:] == "600"
    key = next(f for g in o["survey"]["groups"] for f in g["fields"] if f.get("label") == "API key")
    out = await tools.save_value(o["session"], str(tmp_path / "key"), field=key["id"])
    assert (tmp_path / "key").read_text() == "Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1Jk0Lm", out
    assert "exists" in (await tools.save_value(o["session"], str(tmp_path / "key"), field=key["id"]))["error"]


async def test_fillout_label_prefixes_react_select_pick_and_formatted_phone(tools, site, session):
    """Wisevoter's Fillout form: every field showed an "error" that was its own label and a colon, the preset
    Submission Type read back empty, and the phone box's formatting read as a mismatch; the press needed an override."""
    o = await session(site + "/fillout/")
    sv = o["survey"]
    assert not sv.get("errors"), sv
    fields = {f.get("label"): f for g in sv["groups"] for f in g["fields"]}
    assert not any(f.get("error") for f in fields.values()), fields
    assert fields["Submission Type*"]["value"] == "Correct Existing Story", fields
    out = await tools.fill(o["session"], {fields["Your Name"]["id"]: "Sam Rivera",
                                          fields["Phone"]["id"]: "7196456403",
                                          fields["Submission Type*"]["id"]: {"keep": True}})
    assert out["ready"], out
    pressed = await tools.press(o["session"], button_by(sv, "Next"))
    assert pressed["pressed"] and "Thanks" in " ".join(pressed["after"]["headings"]), pressed


async def test_captcha_frames_are_not_listed_as_content(tools, site, session):
    """Kaggle: the survey listed reCAPTCHA's anchor and hidden challenge frames, the latter with its inline script."""
    o = await session(site + "/rcinvis/")
    assert not any("recaptcha" in f["url"] for f in o["survey"].get("frames", [])), o["survey"].get("frames")


async def test_success_toast_is_a_notice_not_an_error(tools, site, session):
    """Softonic: react-toastify gives a success toast role=alert; it came back as an error and as page_errors."""
    o = await session(site + "/toast/")
    s, sv = o["session"], o["survey"]
    g = sv["groups"][0]
    out = await tools.press(s, g["buttons"][0]["id"], override_reason="test: the empty name is the point")
    assert out["after"]["errors"] == ["Name is required"], out["after"]
    await tools.fill(s, {g["fields"][0]["id"]: "Quillo AI"})
    out = await tools.press(s, g["buttons"][0]["id"])
    assert any("successfully" in n for n in out["after"]["notices"]), out["after"]
    assert not any("successfully" in e for e in out["after"].get("errors", [])), out["after"]


async def test_your_text_after_a_client_side_route_change_is_yours(tools, site, session):
    """Softonic: Save re-rendered the form at a new route with your Name and subtitle in fields with new ids and
    labels; every later press was refused for 'unplanned prefilled text' until override_reason."""
    o = await session(site + "/spa/")
    s, sv = o["session"], o["survey"]
    await tools.fill(s, {field_by(sv, name="name")["id"]: "Quillo AI",
                         field_by(sv, name="subtitle")["id"]: "Restore speech online"})
    out = await tools.press(s, sv["groups"][0]["buttons"][0]["id"])
    assert out["after"].get("url_changed") and not out["after"]["navigated"], out["after"]
    go = next(b for g in out["after"]["groups"] for b in g["buttons"] if b["text"] == "Submit for review")
    out = await tools.press(s, go["id"])
    assert out["pressed"], out


async def test_multi_select_combobox(tools, site, session):
    """Softonic's "Compatible with" (react-select isMulti): choose_all was refused for a combobox, and a single choose
    read back "" because the pick became a chip and the input cleared."""
    s = (await session(site + "/rmulti/"))["session"]
    sv = await tools.survey(s, page_text=False)
    c = field_by(sv, kind="combobox")["id"]
    res = await tools.fill(s, {c: {"choose": ["Chrome"]}})
    assert res["fields"][c]["status"] == "filled", res
    res = await tools.fill(s, {c: {"choose_all": ["Chrome", "Firefox", "Microsoft Edge"]}})
    assert res["fields"][c]["status"] == "filled" and res["ready"], res
    sv = await tools.survey(s, page_text=False)
    f = field_by(sv, kind="combobox")
    assert f["multi"] and f["value"] == ["Chrome", "Firefox", "Microsoft Edge"], f
    assert [b["text"] for b in sv["groups"][0]["buttons"]][:3] == ["Remove Chrome", "Remove Firefox",
                                                                     "Remove Microsoft Edge"]
    out = await tools.press(s, next(b["id"] for b in sv["groups"][0]["buttons"] if b["text"] == "Save draft"))
    assert out["pressed"], out
    [sub] = submissions("rmulti")
    assert sub["fields"]["compat"] == "Chrome,Firefox,Microsoft Edge"


async def test_tab_button_and_a_neighbouring_form(tools, site, session):
    """Getting Smart (11-01): the "Writing Submission" tab is a button outside any form, and a newsletter form with a
    required email sits beside the one you fill. Pressing the tab and the submit both needed override_reason; the
    page renders its form twice with the same ids, so labels read "Email * Email *"."""
    s = (await session(site + "/tabs/"))["session"]
    sv = await tools.survey(s, page_text=False)
    tab = next(b["id"] for b in sv["page_buttons"] if b["text"] == "Writing Submission")
    out = await tools.press(s, tab)
    assert out["pressed"], out
    sv = await tools.survey(s, page_text=False)
    assert not sv.get("errors"), sv["errors"]  # WPForms' Submit button is aria-live: not an error message
    form = next(g for g in sv["groups"] if any(f["label"] == "First" for f in g["fields"]))
    labels = {f["label"]: f["id"] for f in form["fields"]}
    assert set(labels) == {"First", "Email *", "Pitch"}, labels
    res = await tools.fill(s, {labels["First"]: "Sam", labels["Email *"]: "sam@example.com",
                               labels["Pitch"]: "A media literacy piece."})
    assert res["ready"] and "unplanned_required" not in res, res
    out = await tools.press(s, next(b["id"] for b in form["buttons"] if b["text"] == "Submit"))
    assert out["pressed"] and "override_reason" not in str(out.get("reason", "")), out
    [sub] = submissions("tabs")
    assert sub["fields"]["pitch"] == "A media literacy piece."


async def test_radix_select_button(tools, site, session):
    """CMSWire (11-01): see radix_page."""
    s = (await session(site + "/radix/"))["session"]
    sv = await tools.survey(s, page_text=False)
    by = {f["label"]: f for g in sv["groups"] for f in g["fields"]}
    # Zapier's "(required)" is a separate span: the label isn't "Namerequired"
    heard, name, where = by["How did you hear?"], by["Name required"]["id"], by["Where"]["id"]
    assert heard["kind"] == "combobox" and "error" not in heard, heard
    res = await tools.fill(s, {name: "Sam", heard["id"]: {"choose": ["Other"]}})
    assert res["fields"][heard["id"]]["status"] == "filled", res
    sv = await tools.survey(s, page_text=False)
    f = next((f for g in sv["groups"] for f in g["fields"] if f["id"] == heard["id"]), None)
    assert f and f["value"] == "Other" and "error" not in f, sv
    res = await tools.fill(s, {where: {"keep": True}})
    assert not res["ready"], res  # keeping an empty required field doesn't finish it
    res = await tools.fill(s, {where: {"choose": ["Here"]}})
    assert res["ready"], res
    out = await tools.press(s, next(b["id"] for g in sv["groups"] for b in g["buttons"] if b["text"] == "Submit"))
    assert out["pressed"], out
    [sub] = submissions("radix")
    assert sub["fields"]["heard"] == "Other" and sub["fields"]["name"] == "Sam"
