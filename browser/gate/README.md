# Captcha gate

Captchas are solved in code before the browsing agent's next step, so it never has to notice, wait for, or work
around one. Used by the shared browser MCP (`..`). Given `attach_match`, a gate watches only the attached tab and the tabs it opens (followed through `openerId`), because
other agents work in other windows of the same Chrome at the same time.

Two solvers:

- **NopeCHA**, the extension loaded in Steel (`../../steel/README.md`). It solves in the browser: reCAPTCHA v2
  checkboxes, hCaptcha, Turnstile, and Cloudflare's "Just a moment..." page.
- **2captcha**, an API (`aitools/keys/2captcha.key`). It solves from a sitekey or an image, and the gate puts
  the answer into the page. It covers what NopeCHA cannot (GeeTest v4, MTCaptcha, image-text captchas), and backs
  up NopeCHA on reCAPTCHA and Turnstile.

## Run a task by hand

```sh
cd ~/git/aitools/browser
.venv/bin/python gate/run.py "Click the Check button and report the result." \
  --start https://2captcha.com/demo/geetest-v4 --authorize
```

`run.py` drives Steel at `ws://localhost:<--steel>/` (3103 by default). It reads keys from the environment, or
else from `aitools/keys` (`$AGENT_BROWSER_KEYS`). `--json` prints fastbrowse's full result plus everything the
gate did; `--trace FILE` appends the gate's record and fastbrowse's own trace; `--no-gate` runs plain fastbrowse.

## From the browser MCP

- **The navigator** runs `fastbrowse_cli.py`: `run.py`'s setup behind fastbrowse's own flags (`TASK --cdp-url
  --target-match --context --max-steps --json --trace --authorize`), printing fastbrowse's `--json` result plus
  `gate`. With `--target-match` the run attaches to the session's window, and the gate watches that window and
  its popups only.
- **The form tools** run the gate on the session's tab after each page load and around each press. A press refuses
  while a captcha is unsolved unless Claude passes `override_reason`. `agent-browser-mcp --no-captchas` turns
  this part off.

## What `run.py` sets up

Everything here was measured on the 2captcha demos (see [Results](#results)); each line says why.

| Setting | Value | Why |
|---|---|---|
| LLM, every purpose | `minimax/minimax-m3.1-flash` (open weights) | It takes images, so `recover` and `verify` (which send screenshots) need no second model. Gate on, every demo twice: 19/22 passed, against 16/22 for GLM-5.3 planning + MiniMax reading + GLM-5.3-Flash, which also overran fastbrowse's 1,600-token output cap on `verify`. |
| OpenRouter routing | `openrouter_route.py`: MiniMax on ModelRun at low reasoning effort, then OpenRouter's own routing | See below. |
| `bot_check_above` | 0.97 (fastbrowse: 0.70) | With the gate on, fastbrowse's own bot-check verdict was mostly Jev misreading a page about captchas, or a solved widget, as a wall. A captcha the gate cannot solve stops the run itself. |
| `barren_reads` | 3 (fastbrowse: 2) | A reader sometimes returns nothing the first time; one more read is about $0.001 |
| Limits | 30 steps, 600s; no dollar cap by default | OpenRouter sometimes returns a call without its cost, and fastbrowse then stops a dollar-capped run as `budget_exceeded`. `--max-dollars` still sets one. |
| Agent prompt | nothing about captchas | A line saying "captchas are handled by code" made Jev call pages bot walls: 7 of 11 runs blocked with it |
| Retry | once, on `blocked` after the gate solved a captcha on that origin | |

**Why the provider matters.** fastbrowse's reader must cite the page block behind each piece of evidence
(`cite: {first, last}`), or fastbrowse discards it and the run ends `stuck` on a page that shows the answer.
fastbrowse's schema lets `cite` be null, so strict JSON cannot force a citation. The same open-weight model served by
different OpenRouter providers (different quantizations and inference stacks) differs widely at it. Replaying real
fastbrowse reader requests:

| Model and provider | Cited correctly | Time |
|---|---|---|
| MiniMax M3.1 Flash on ModelRun, low reasoning | 8/8 | median 3.5s |
| MiniMax M3.1 Flash on InferenceNet (OpenRouter's usual pick) | 3/8, at any effort | median 5-9s |
| MiniMax M3.1 Flash on SambaNova | 12/12 | ~68s a read, past fastbrowse's 30s per call |
| GLM-5.3 on Wafer | 3/3 | |
| GLM-5.3 on InferenceNet, Morph, DeepInfra, Together | 1/3 each | |
| GLM-5.3 on Fireworks, Parasail | 0/3 | |
| GLM-5.3 on Z.AI, Baidu, Novita | rejected the request | |

For MiniMax the difference is reasoning: providers that let it reason cite correctly, and InferenceNet runs it with
none. OpenRouter routes mostly by price, so without a preference a run got a different provider for each call, and
results swung between rounds (8/11 passed in one, 4/11 in the next, with the same setup).

fastbrowse's OpenRouter preset sets `provider` itself and refuses an override, so `run.py` points fastbrowse's
`custom` endpoint at `openrouter_route.py`, a small proxy in its own process. For each call it tries the preferred
provider alone for 10s, then lets OpenRouter route it. A provider can accept a call and never answer, and OpenRouter
only falls back on errors. Each attempt requires providers that support the JSON schema, as fastbrowse's preset does.
The proxy adds the low reasoning effort fastbrowse would send itself, and asks OpenRouter to report each call's cost
so fastbrowse can meter it. Set `OPENROUTER_ROUTE_LOG=<file>` to log every prompt and reply. The log holds page
text, so delete it after debugging.

## How the gate works

Pass a gate as fastbrowse's `check_access` hook:

```python
from captcha_gate import CaptchaGate

async with CaptchaGate("ws://localhost:3103/", twocaptcha_key) as gate:
    result = await run_task(task, start=url, cdp_url="ws://localhost:3103/", check_access=gate)
print(gate.summary())   # who solved what, how long it took, 2captcha spend, anything left unsolved
```

fastbrowse awaits `check_access` before every observation, read, screenshot, navigation and action, about 3-5
times per step and concurrently. On each call the gate does the following:

1. **Scan** each tab the run opened (tabs open before the gate started are left alone) for reCAPTCHA v2, hCaptcha,
   Turnstile, Cloudflare's challenge page, GeeTest v3/v4, MTCaptcha and image-text captchas. With nothing found it
   returns in milliseconds. Concurrent calls share one scan.
2. **Hold** while one is unsolved. The agent cannot observe or act, so it cannot submit early. On a page under 10s
   old whose captcha library is loading but has not drawn its widget, it holds up to 8s for the widget to appear.
3. **Route** it:

   | Captcha | Solver |
   |---|---|
   | reCAPTCHA v2 checkbox / Enterprise, Turnstile | NopeCHA for 30s (`nopecha_wait`), then 2captcha |
   | hCaptcha | NopeCHA only (2captcha no longer documents hCaptcha) |
   | Cloudflare "Just a moment..." | NopeCHA only, 60s (2captcha needs a proxy for it) |
   | GeeTest v3/v4, MTCaptcha, image text | 2captcha straight away |
   | reCAPTCHA invisible / v3 | not held: their tokens are made when the form submits |
   | Question captchas ("what is 3 + 4?") | the agent answers them like any other field |

4. **Hand the answer to the page.** A token goes into the widget's hidden field. The gate also calls the site's
   callback and overrides `getResponse` / `getValidate` / `getVerifiedToken`, so pages that ask the widget get the
   token too. GeeTest keeps its state in closures, so a script the gate adds to each tab (`INIT_JS`) catches the
   captcha objects as the library creates them, and catches Turnstile's and hCaptcha's render callbacks the same
   way. An image answer is typed into its field.
5. **Keep captcha remnants out of the agent's view.** These all made fastbrowse end runs `blocked`:
   - reCAPTCHA and hCaptcha keep their image-challenge frame on every page that uses them, hidden off screen.
     fastbrowse reads into it anyway and offered Jev "Get an audio challenge" and "Get a new challenge". Only
     `display:none` takes a frame out of fastbrowse's view, so the gate mirrors the provider's own hiding with it, and
     undoes it the moment the provider shows the challenge (for NopeCHA to solve).
   - After a 2captcha solve the widget still looks unsolved (GeeTest's "Click to verify", MTCaptcha's text box). The
     gate hides it and puts "Captcha solved" in its place. The page only reads the token. The note goes when a
     button is pressed or the form submitted, since it sits where pages put their result and the reader quoted it as
     the result message. If a reCAPTCHA token expires, the widget comes back and the gate solves it again.
6. **Stop the run when it cannot solve one.** After 2captcha fails twice, its budget is spent, or a hold passes
   `max_hold` (240s), the gate raises `CaptchaUnsolved` from the hook. fastbrowse ends the run, and `run.py` reports
   it as `blocked`, naming the captcha and the reason, rather than letting the agent wander until it is `stuck`.

Limits per gate: `max_hold` 240s per call, `max_solves` 10 and `max_dollars` $0.10 for 2captcha.

## Test it

```sh
.venv/bin/python gate/selftest.py recaptcha-v2 normal mtcaptcha geetest-v4   # the gate alone, no agent (from browser/)
```

`selftest.py` opens each demo, lets the gate solve it, clicks Check in code and prints the page's verdict. A pass
there and a failure in `bench.py` means the agent side is the problem, not the solving.

`bench.py` (not included in this repo) asks the agent to "Click the Check button on this form and report the exact result message the page
shows". It never mentions the captcha, as a real task would not. A pass is a `complete` run whose answer quotes the
demo's success message. Results go to `results/`.

## Results

Gate on, `bench.py`'s task, 2captcha demos. "Plain" is fastbrowse without the gate. 11 runs per row, except
round 9 (each demo twice, 22 runs).

| Round | Setup | Passed | `blocked` | `stuck` / other |
|---|---|---|---|---|
| 1 | plain, GLM-5.3 | 4/11 | 3 | 4 |
| 1 | gate, with a captcha line in the prompt | 2/11 | **7** | 2 |
| 3 | + reCAPTCHA challenge frame hidden, prompt line removed, `bot_check_above` 0.97, gate stops unsolvable runs | 3/11 | 1 | 7 |
| 6 | + MiniMax reader (unrouted) | 8/11 | 1 | 2 |
| 7 | same as 6 (run-to-run noise) | 4/11 | 1 | 6 |
| 7 | plain, same models | 1/11 | 3 | 7 (mostly reported the page's failure message) |
| 9A | gate, routed: GLM-5.3 + MiniMax reader + GLM-5.3-Flash | 16/22 | 2 | 4 |
| **9B** | **gate, routed: MiniMax M3.1 Flash everywhere (the default)** | **19/22** | **2** | **1** |

In round 9B the only `blocked` runs were GeeTest v3, which 2captcha cannot solve; the gate stopped each in about 30s
and named the reason. The other failure was a page that stopped answering fastbrowse's own CDP command. Per demo
in 9B, both runs passed for reCAPTCHA v2, Enterprise, Invisible, v3, Turnstile, Normal (image), Text, MTCaptcha and
GeeTest v4, and one of two for reCAPTCHA v2 Callback. A run cost about $0.007 in model calls (median 22s), plus
$0.001-0.003 for each 2captcha solve.

Gate alone (`selftest.py`, no agent): reCAPTCHA v2 (NopeCHA, 9-17s), image text (2captcha, ~6s, $0.001), MTCaptcha
(2captcha, 15-125s, $0.00145) and GeeTest v4 (2captcha, ~11s, $0.003) all got "Captcha is passed successfully!".

Round 10 reran the demos where the "Captcha solved" note was quoted, after it was made to go on submit
(reCAPTCHA v2, GeeTest v4, MTCaptcha, all via 2captcha): 3/3 passed, each quoting the page's own message.


## Known gaps

- **GeeTest v3**: 2captcha answered `ERROR_CAPTCHA_UNSOLVABLE` on the demo's challenge every time. GeeTest v3
  challenges are single use, and the retry reuses the same one. v3 is GeeTest's deprecated version. The gate stops
  the run as `blocked` in about 30s.
- **Click, rotate, Lemin, KeyCaptcha**: not routed. 2captcha has APIs for them, and the gate can grow routes when a
  bot meets one.
- **Cloudflare challenge page and hCaptcha without NopeCHA**: no fallback.
- **reCAPTCHA v3 score**: this browser scored 0.3. Sites that require 0.5+ reject it. 2captcha can mint high-score
  v3 tokens, but injecting them is site-specific (the token comes from `grecaptcha.execute` at submit time).
- **A captcha unrelated to the task** (a newsletter form in a footer) is solved too, costing a few seconds and,
  for 2captcha types, a solve. If it cannot be solved, the run stops `blocked`.
- **reCAPTCHA v2 Callback demo**: the page submits from the widget's callback and has no Check button, so a run
  that follows the bench task literally can end `stuck` looking for one, after the captcha is passed. This is a
  bench-task artefact; the gate alone passes it (NopeCHA, ~21s).
- **Turnstile on the 2captcha demo** uses Cloudflare's always-pass test sitekey, so its pass proves little.
- In this Chromium build, a script added for new documents only runs once the Page domain is enabled on that session.
  `_session` does that; without it the GeeTest hooks never install.
