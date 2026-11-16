"""Watching pages that change on their own: wait_for, record/record_status, evaluate(save_to)."""

import asyncio
import json
from pathlib import Path

# A page that works through states by itself: a progress bar, a status line, a button that becomes enabled, a
# heading swap and an audio element appearing.
JOB = """<title>job</title><h1>Restoring</h1>
<p role=status id=st>queued</p>
<progress id=pg max=100 value=0 aria-label=restore></progress>
<button id=go disabled>Download</button>
<span class=pct>0%</span>
<script>
let n = 0;
const t = setInterval(() => {
  n += 25;
  document.getElementById('pg').value = n;
  document.querySelector('.pct').textContent = n + '%';
  document.getElementById('st').textContent = n < 100 ? 'restoring part=' + n + '&step?x' : 'done';
  if (n >= 100) {
    clearInterval(t);
    document.getElementById('go').disabled = false;
    document.querySelector('h1').textContent = 'Restored';
    const a = document.createElement('audio'); a.src = 'data:audio/wav;base64,UklGRiQAAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQAAAAA='; document.body.append(a);
    window.finished = {at: Date.now(), parts: 4};
  }
}, 700);
</script>"""


async def _job(tools, site, session):
    o = await session(site + "/outcome/live/submit")
    await tools.sessions.get(o["session"]).page.set_content(JOB)
    return o["session"]


async def test_wait_for_condition_selector_text_and_timeout(tools, site, session):
    sid = await _job(tools, site, session)
    r = await tools.wait_for(sid, condition="() => window.finished", timeout=20, poll=0.3)
    assert r["met"] and r["value"]["parts"] == 4 and 1.5 < r["seconds"] < 10
    assert (await tools.wait_for(sid, selector="#go:not([disabled])", timeout=5))["met"]
    assert (await tools.wait_for(sid, text="Restored", timeout=5))["met"]
    miss = await tools.wait_for(sid, text="never shows up", timeout=1.5, poll=0.5)
    assert miss["met"] is False and miss["checks"] >= 2
    assert "error" in await tools.wait_for(sid)


async def test_record_logs_state_changes_and_screenshots(tools, site, session, tmp_path):
    sid = await _job(tools, site, session)
    started = await tools.record(sid, seconds=5, every=1, watch_selectors=[".pct"], interval=0.3,
                                 out_dir=str(tmp_path / "rec"))
    assert started["running"]
    assert "error" in await tools.record(sid, seconds=5)  # one at a time
    assert (await tools.read(sid, max_chars=50))  # other tools keep working meanwhile
    done = await tools.record_status(sid, wait=True)
    assert done["ended"] == "finished" and not done["running"]
    lines = [json.loads(x) for x in (tmp_path / "rec" / "timeline.jsonl").read_text().splitlines()]
    assert "initial" in lines[0] and "final" in lines[-1]
    changes = lines[1:-1]
    assert any("progress" in c for c in changes) and any("status" in c for c in changes)
    assert any(c.get("buttons", {}).get("added") == ["Download"] for c in changes)
    assert any(c.get("headings", {}).get("added") == ["Restored"] for c in changes)
    assert any("media" in c for c in changes)
    assert any(".pct" in c.get("watch", {}) for c in changes)
    # '=', '?' and '&' come through as they are
    assert any("part=50&step?x" in json.dumps(c) for c in changes)
    shots = sorted((tmp_path / "rec" / "shots").glob("*.jpg"))
    assert len(shots) >= 4 and shots[0].read_bytes()[:2] == b"\xff\xd8"


async def test_record_can_be_stopped_early(tools, site, session, tmp_path):
    sid = await _job(tools, site, session)
    await tools.record(sid, seconds=120, every=None, out_dir=str(tmp_path / "rec"))
    await asyncio.sleep(1.5)
    st = await tools.record_status(sid, stop=True)
    assert st["ended"] == "stopped" and st["elapsed"] < 10 and st["screenshots"] == 0
    assert "final" in (tmp_path / "rec" / "timeline.jsonl").read_text().splitlines()[-1]


async def test_evaluate_save_to_writes_whole_result(tools, site, session, tmp_path):
    sid = await _job(tools, site, session)
    out = tmp_path / "big.json"
    r = await tools.evaluate(sid, "() => Array.from({length: 3000}, (_, i) => 'bar:progress=' + i + '&x?y')",
                             save_to=str(out))
    assert r["saved"] == str(out) and r["chars"] > 50000 and len(r["preview"]) == 500
    data = json.loads(out.read_text())
    assert len(data) == 3000 and data[-1] == "bar:progress=2999&x?y"
    assert Path(r["saved"]).is_file()


async def test_file_over_50mb_goes_in_whole(tools, site, session, tmp_path):
    # Playwright refuses files over 50 MB for a browser in another container (Steel); forms.set_large_files
    # streams them in chunks instead. Used above 45 MB everywhere, so it's exercised here on the local Chrome.
    big = tmp_path / "lecture.mp3"
    blob = bytes(range(256)) * (46 * 1024 * 4)  # 46 MiB
    big.write_bytes(blob)
    sid = (await session(site + "/outcome/live/submit"))["session"]
    page = tools.sessions.get(sid).page
    await page.set_content("<form><input type=file name=media></form>"
                           "<script>document.querySelector('input').onchange = e => window.got = e.target.files[0].size"
                           "</script>")
    sv = await tools.survey(sid, page_text=False)
    fid = sv["groups"][0]["fields"][0]["id"]
    r = await tools.fill(sid, {fid: {"file": str(big)}})
    assert r["fields"][fid]["status"] == "filled"
    info = await page.evaluate("async () => { const f = document.querySelector('input').files[0];"
                               " const b = new Uint8Array(await f.slice(f.size - 300).arrayBuffer());"
                               " return [f.name, f.size, f.type, window.got, Array.from(b.slice(-4))]; }")
    assert info == ["lecture.mp3", len(blob), "audio/mpeg", len(blob), [252, 253, 254, 255]]
