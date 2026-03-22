# Codex Usage Monitor

Local tool for making heavy Codex use easier to understand and steer.

It is no longer mainly framed as a strong “advisor.” Its best use is as a decision cockpit on top of Codex's native usage board: where are your tokens going, which projects or task types are consuming them, where does usage pressure spike, and how much of the interpretation is measured versus inferred?

It reads your existing local Codex session data from `~/.codex`, then helps you answer questions like:

- which projects are actually consuming most of my Codex usage?
- how concentrated is my usage in `xhigh`, agents, or planning?
- when does usage pressure spike?
- which sessions are disproportionately expensive?
- how much of the story is hard telemetry versus heuristic interpretation?

## What It Uses

- thread metadata from `~/.codex/state_5.sqlite`
- archived or live rollout JSONL files from `~/.codex/archived_sessions` and `~/.codex/sessions`
- optional manual ratings stored locally in:
  - `tmp/codex_usage/ratings.jsonl`

The tool does not need any external API.

It also infers project scope from:

- file paths mentioned in tool calls
- `apply_patch` targets
- command arguments and workdirs

The served browser view is one main monitor page. Internally, the script can still generate stricter or more inference-heavy report modes when you use the CLI.

## Commands

List recent completed sessions:

```bash
python3 scripts/codex_usage_monitor.py list --days 21 --limit 20
```

Generate the main report:

```bash
python3 scripts/codex_usage_monitor.py report --days 21 --limit 30
```

Generate a stricter factual report:

```bash
python3 scripts/codex_usage_monitor.py report --days 21 --limit 30 --mode basic
```

Generate the more inference-heavy report:

```bash
python3 scripts/codex_usage_monitor.py report --days 21 --limit 30 --mode hybrid
```

Generate the main dashboard and serve it on your machine or local network:

```bash
python3 scripts/codex_usage_monitor.py serve --days 21 --limit 30
```

By default this serves `tmp/codex_usage/index.html` on `0.0.0.0:8765`, prints both a local URL and any detected LAN URL, and rebuilds the page from fresh Codex data at most once every 120 seconds while the server is running.

If you want faster live updates while you keep checking the same page, use a shorter refresh interval:

```bash
python3 scripts/codex_usage_monitor.py serve --days 21 --limit 30 --refresh-seconds 60
```

For an always-on local monitor, this repo also includes:

- startup wrapper: `scripts/run_codex_usage_monitor.sh`
- launchd agent template: `launchd/com.example.codex-usage-monitor.plist`

The launchd setup uses port `8769` by default so it stays separate from ad hoc test servers you may start manually.

## Share Safely

The code is the easy part to share. The risky part is sharing real output.

Safe to share:

- `scripts/codex_usage_monitor.py`
- `scripts/run_codex_usage_monitor.sh`
- a generic launchd template
- docs and setup instructions

Avoid publishing unless you intentionally sanitize them first:

- `tmp/codex_usage/latest-report.md`
- `tmp/codex_usage/latest-report.json`
- `tmp/codex_usage/index.html`
- `tmp/codex_usage/ratings.jsonl`
- `tmp/codex_usage/launchd.stdout.log`
- `tmp/codex_usage/launchd.stderr.log`

Those files can contain your real session titles, inferred project names, local activity patterns, and other personal working context.

If you want to post screenshots publicly, the simplest safe rule is:

- do not use screenshots from your real data
- either blur the session tables and project labels or generate a demo screenshot from fake data

If sensitive data is ever committed by mistake, GitHub says cleanup can require history rewriting and coordination with other clones, so prevention is much easier than cleanup.

Default outputs:

- Main served page: `tmp/codex_usage/index.html`
- Main markdown report: `tmp/codex_usage/latest-report.md`
- Main JSON export: `tmp/codex_usage/latest-report.json`
- Additional CLI mode outputs are still written when you explicitly use those modes.

## The Important Shift

You do not need to rate anything if you mainly want a usage monitor.

If the report cannot find enough same-project evidence, it should tell you that plainly instead of bluffing.

The main value over Codex's native usage board should be:

- project mix
- task mix
- setup mix
- pressure visibility
- daily burn visibility
- heavy-session visibility
- explicit uncertainty labels

## How To Use It Well

Best pattern:

1. Run `report` or `serve`.
2. If you want a page you can keep revisiting, prefer `serve` and leave it running.
3. Check `Measurement Confidence` and `Usage Reference` first.
4. Look at which projects are being inferred well versus which sessions stay `workspace:root`, `workspace:shared`, or `multi-project`.
5. Use `Project Usage`, `Daily Usage`, and `Heaviest Sessions` to understand where Codex is really being spent.
6. Use the `Advisory Read` only after you understand the harder telemetry above it.
7. Change only one habit at a time for a few sessions:
   - `medium` vs `xhigh`
   - with plan vs without plan
   - with agents vs solo
8. Revisit the same served page or rerun the report.

## Important Caveat

Without manual ratings, the tool still works as a usage monitor. It uses a proxy quality score based on:

- completion signals
- final-answer shape
- write actions
- verification commands
- obvious failure language

That proxy is good enough for early monitoring and tentative project-aware guesses, but not for strong confidence. If you ever use manual ratings later, treat them as sparse anchor labels rather than busywork on every session.
