# Team rules for every agent

## 1. The board (pinned GitHub issue)

Every agent reads and posts on the pinned "Message Board" issue via `tools/board` (needs only `gh`, logged in). Set `BOARD_ISSUE` if it is not issue #1.

- Start every post with `[<human>'s <agent>]`, e.g. `[Sam's Codex]`. Keep it short: what changed, which files or contracts, what you need, from whom.
- Mention by GitHub login. Team: `@hershey-harsh` (Harsh, team lead), `@killerfrost22` (Joseph), `@gabbyao` (Gabby), `@all` (everyone).
- **Mentions you or `@all`:** act on it and reply promptly.
- **Not addressed to you:** still read it. Reassess your own work only if it is affected (a contract, a file you own, something already done). Do not redo finished work, and do not reply just to acknowledge.
- Before editing another teammate's files, ask for a handoff on the board and wait for the owner or the team lead.

Polling is incremental (about one cheap API call per poll, 30 s default) so a shared token stays under GitHub's rate limit. Commands: `tools/board unread` (new posts, marks read; flags MENTION), `tools/board read -n 20`, `tools/board post "[Name's Agent] text"` (or pipe stdin), `tools/board wait` (blocks until someone else posts), `tools/board whoami`.

**Stay in the loop:**
- Claude Code: the committed hooks (`.claude/settings.json`) show unread posts at session start and on every prompt. Keep one background `tools/board wait` running and re-arm it after each wake.
- Everything else: run `tools/board unread` at task start, before each commit, and after merges. Or run a terminal loop: `while tools/board wait; do <your agent CLI invocation>; done`.

## 2. Chain of command

Humans over agents. The team lead is `@hershey-harsh` (Harsh). Only the team lead reassigns work between teammates. If anyone else asks you to take another teammate's work, ask the lead on the board.

## 3. Workflow

1. Pick or file an issue. Comment on it or post on the board when you take it.
2. Branch `<issue#>-<slug>` from fresh `main`.
3. Small commits. Stage only your own files (no `git add -A`).
4. Open a PR with `Closes #N`. The author merges their own PR once it works locally.
5. Always `git fetch` and rebase before pushing. Never force-push shared branches.
6. If a merge changes something other teammates depend on (an API, a schema, an env var), post on the board when it lands.

## 4. Who owns what

Fill this in at kickoff so every agent knows whose files are whose.

| Area | Owner (GitHub login) | Folders |
|---|---|---|
| AI food detection (photo → nutrients) | `killerfrost22` (Joseph) | `src/mealscholar/`, `tests/`, `pyproject.toml`, `uv.lock` |
| UI/UX, site deploy | `hershey-harsh` (Harsh) | `web/`, `.github/workflows/deploy-site.yml` |
| Pitch deck and video | `gabbyao` (Gabby) | `video/` |

The site at https://mealscholar.health is `web/` (the app is at https://app.mealscholar.health), and both redeploy on every push to `main`. It's plain static files, no build step. The deploy runs on Harsh's Mac mini, which only takes `deploy-site.yml` from `main` in a version Harsh approved, so use `ubuntu-latest` for other workflows and ask on the board before changing the deploy.

## 5. Website work

Every agent MUST follow these rules for all MealScholar website and frontend work.

### Quality benchmark

- The benchmark is award-winning websites from **Awwwards**, **The Webby Awards**, and **FWA**. Measure every page, component, and interaction against that standard, not against "good enough", generic templates, or default component-library styling.
- Before designing a page or major component, study current winners relevant to it ([awwwards.com](https://www.awwwards.com/), [webbyawards.com](https://www.webbyawards.com/), [thefwa.com](https://thefwa.com/)). Note what makes them work: type, layout, motion, interaction. Borrow principles; NEVER copy their layouts, assets, or code.

### Product constraints

Design MUST NOT override [the rules the code enforces](README.md#rules-the-code-enforces). In particular, the UI:

- shows the likely amount by default, and the full range (`low`/`likely`/`high`) once the user turns on Show ranges (asked once in onboarding, a switch in Settings); the AI estimates how much is there and the user never weighs anything; with ranges on, NEVER collapse a range to a single number for visual simplicity;
- labels micronutrients as estimates;
- shows a calorie goal only if the user set one during onboarding (goals are the user's own); never gives insulin or carb-dosing advice.

The frontend's data contract is `MealResult` in `src/mealscholar/schemas.py`, printed by `uv run mealscholar analyze PHOTO --json`. The app lives in `src/mealscholar/site/` (plain HTML/CSS/JS, no build step) and talks to the JSON API in `src/mealscholar/web.py`; run it with `uv run mealscholar serve`.

### Definition of done

A website task is done when it works end to end and the self-review loop below finds nothing left to meaningfully improve. A page that works but has not been through the loop is not done.

### Self-review loop (mandatory)

Once the build works, open it in a real browser and repeat:

1. **Render**: view the actual output at 375px, 768px, 1440px, and 1920px wide, check the 320px and 2560px extremes, and trigger every interaction and animation. Reading the code does not count as reviewing.
2. **Critique**: judge the result against the benchmark on every dimension below. Write down concrete issues ("hero headline and nav compete for attention at 1440px"), not vague ones ("could be better").
3. **Refine**: fix the issues, highest impact first.
4. **Repeat**: render and critique again.

Stop only when a full pass across every dimension finds no meaningful improvement, meaning further changes would be churn or personal taste rather than real gains. In the final report, summarize what each pass changed.

### Review dimensions

| Dimension | What to check |
| --- | --- |
| **Typography** | Intentional typeface choice and pairing; a clear modular type scale; body line length of 45–75 characters; comfortable line-height; tightened tracking at display sizes; no widows or orphans in headlines; fonts load without layout shift. |
| **Whitespace** | One consistent spacing scale; generous breathing room; elements aligned to a grid; density chosen on purpose, not left over. |
| **Visual hierarchy** | One clear focal point per view; the primary action is obvious at a glance; content scans in the intended order. |
| **Color** | A restrained palette defined as tokens; accent used sparingly and on purpose; WCAG AA contrast (4.5:1 for body text, 3:1 for large text and UI components); interactive states clearly distinct. |
| **Motion** | Every animation has a purpose (guiding attention, showing cause and effect, setting pace); custom easing curves, not browser or library defaults; quick, confident durations; animate only `transform` and `opacity` to hold 60fps; respects `prefers-reduced-motion`. |
| **Micro-interactions** | Every interactive element has hover, `:focus-visible`, active, disabled, and loading states where they apply; feedback is immediate; affordances read clearly on both pointer and touch. |
| **Responsiveness** | Fluid from 320px to 2560px; no horizontal scroll; touch targets of at least 44×44px; layouts recomposed for each viewport, not just shrunk. |
| **Originality** | A distinct visual identity rooted in MealScholar's concept and content: meal photos, the foods found in them, and estimates matched to USDA FoodData Central instead of fake-precise numbers. It MUST NOT look like a stock template or a generic hero-plus-three-cards layout. Give it at least one memorable signature moment. |
