"""captcha-gate's CaptchaGate on a session's own tab, outside the navigator: after a page loads and around a press.

The navigator gets the gate inside its run (captcha-gate/fastbrowse_cli.py). Forms are filled and submitted by code
here, not by the navigator, and the captchas that matter most sit on those forms, so the same gate runs on the tab:
it waits for NopeCHA (in the browser) or solves through 2captcha, and holds until the captcha is gone or it gives up.
"""

from __future__ import annotations

import secrets
import sys
from pathlib import Path

from playwright.async_api import Error as PWError, Page


class Captchas:
    def __init__(self, cdp_url: str, gate_dir: Path, twocaptcha_key: str | None):
        if str(gate_dir) not in sys.path:
            sys.path.insert(0, str(gate_dir))
        import captcha_gate

        self.cdp_url = cdp_url
        self.key = twocaptcha_key
        self._mod = captcha_gate

    async def clear(self, page: Page, force_2captcha: bool = False) -> dict | None:
        """Solve whatever captcha is on the page. None when there was none; otherwise what the gate did, with
        `unsolved` listing any it gave up on. `force_2captcha` has 2captcha solve it even when NopeCHA did, for a
        site that rejected NopeCHA's answer."""
        token = f"bbc{secrets.token_hex(4)}"
        try:
            await page.evaluate("t => { document.title = t + ' ' + document.title }", token)
        except PWError:
            return None
        gate = self._mod.CaptchaGate(self.cdp_url, self.key, attach_match=token, force_2captcha=force_2captcha,
                                     log=lambda _msg: None)
        await gate.start()
        try:
            await gate()
        except self._mod.CaptchaUnsolved:
            pass
        finally:
            await gate.close()
            try:
                await page.evaluate("t => { document.title = document.title.replace(t + ' ', '') }", token)
            except PWError:
                pass
        sm = gate.summary()
        if not (sm["solved"] or sm["gave_up"] or sm["unsolved"]):
            return None
        return {k: sm[k] for k in ("solved", "gave_up", "unsolved", "twocaptcha_dollars") if sm.get(k)}
