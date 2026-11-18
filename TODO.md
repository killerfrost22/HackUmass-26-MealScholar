# MealScholar TODO

> **November 17, 2026:** accounts/login and the UMass Dining integration (campus menus, dining
> halls, favorites, alerts, hall cams, MongoDB Atlas, SMTP email) were removed; nutrition now
> comes only from USDA FoodData Central and the app keeps one local diary without sign-in. Items
> below about accounts, email, campus menus, halls, favorites, cams or Atlas are obsolete.

Updated November 14, 2026 after a live-site audit starting at 22:22 UTC. Owners follow `AGENTS.md`; deadlines follow `TIMELINE.md`.
Section 0 is the current website priority order. Sections 1–6 retain the existing project backlog, with confirmed stale items reconciled. An unchecked verification task is not a confirmed defect.
Tick a box only when its acceptance criteria are observed. Post contract and deployment changes on the board.

## 0. Full-site audit: findings and execution order

**Scope:** [public website](https://mealscholar.health/), all eight main content sections, hero, navigation and footer; [linked app](https://app.mealscholar.health/) Today, Menu, dish facts, Week, Settings, Alerts and installation entry. Reviewed rendered Chromium light/dark surfaces, mobile menu, FAQ disclosures, carousel keyboard controls, reduced-motion behavior, HTTP responses, metadata, deployed scripts and backend contracts.

**Coverage and limits**

| Area | Observed | Not established by this audit |
|---|---|---|
| Responsive website | No document-level horizontal overflow at 320, 375, 768, 1440, 1920 and 2560 px; screenshots at representative mobile, tablet and desktop sizes | Every device/browser combination; component overflow still exists at tablet width (AUD-13) |
| App | Mobile at 320/390 px and desktop at 1440 px; live menu populated; dish facts include serving size and allergen warning; Today/Week empty states, settings and alerts opened | An authenticated user's populated diary, account onboarding or a complete photo-to-result flow |
| Accessibility | axe-core 4.10.3 WCAG A/AA snapshots, headings, disclosure controls, keyboard carousel and modal dismissal/focus return | WCAG conformance: landing contrast checks had incomplete results over layered backgrounds; screen readers and physical touch devices still need review |
| Technical delivery | Direct HTTP/HTTPS header requests; working new hosts and legacy 302 redirects; manifest, crawler directives and source review | Real-user Core Web Vitals, delivered email, billing controls, backups or hourly scraper operation |
| Safety | No meals uploaded, accounts created, emails sent, preferences saved, data deleted or Gemini calls made | Production write paths were deliberately not exercised because guest storage is shared |

The first browser responses loaded assets stamped `1960e30`; deployments continued during the audit (later response modification time: 22:28 UTC). These are timestamped observations, not a promise that production will remain unchanged.

**Keep what works:** distinctive food-photo/range visual identity; consistent typography and theme controls; one marketing H1 with clear section headings; skip links and native FAQ disclosures; carousel arrow-key announcements and reduced-motion handling; visible nutrient ranges, optional-goal and medical-advice caveats; opt-in location; explicit serving-size/allergen caveat in campus dish facts. No JavaScript exceptions or broken loaded marketing images were observed. The manifest and Home Screen instructions exist: lack of a service worker is not, by itself, a broken install feature, and the site does not promise offline use.

### P0 — block wider public use

- [ ] **AUD-01 · Harsh (deployment) + Joseph (auth/data): stop sharing guests' private data.**
  - **Evidence:** live [`/api/status`](https://app.mealscholar.health/api/status) reported `auth_required: false`; a fresh unsigned-in browser received 200 from `/api/me/prefs` and `/api/days/2026-11-14`, while `/api/auth/me` returned 401. `auth.py:592–598` returns the same anonymous diary owner, `diary.py:40–44` maps that owner to `user_id = ''`, and `prefs.py:403–405` maps guests to the same `"local"` preferences. This is not per-device guest storage.
  - **Impact:** guests share meals, goals, diet/allergen preferences and learned portions. The visible “Clear history” and “Delete all my data” actions target that shared data; the “on this device” settings copy is misleading. No destructive request was made to demonstrate it.
  - **Done when:** public production requires authentication; unsigned diary/preferences requests return 401; two signed-in test accounts cannot read, correct or erase each other's data. Record a deliberate retention/purge decision for existing anonymous rows and `"local"` prefs; never assign them to a new registrant. Until then, keep the writable demo private rather than presenting shared storage as a private diary.

- [ ] **AUD-02 · Harsh: enforce HTTPS on both new hosts before enabling accounts.**
  - **Evidence:** direct requests at 22:30 UTC to `http://mealscholar.health/` and `http://app.mealscholar.health/` returned **200**, not redirects. Neither HTTPS root sent `Strict-Transport-Security`.
  - **Impact:** an explicitly opened HTTP URL can serve the app over plaintext; secure cookies, geolocation and installation features also depend on secure transport.
  - **Done when:** both HTTP hosts redirect permanently to the equivalent HTTPS path/query; HTTPS responses send an intentional HSTS policy. Enable subdomain coverage only after confirming affected subdomains support TLS. Verify sign-in and phone upload still work. This is a Cloudflare/deployment change: coordinate with Harsh, not an unapproved workflow edit.

- [ ] **AUD-03 · Joseph + Harsh: bound photo-analysis abuse and model spend.**
  - **Audit evidence (before #119):** the analysis endpoint had no request limiter; the existing auth limiter did not protect Gemini analysis. Public sign-in was off. This was established from source/configuration, not a production load test.
  - **Done when:** photo analysis and model-backed answer/refinement have per-account and per-network limits, return actionable 429/`Retry-After` responses when exhausted, and do not call Gemini after rejection. Configure enforceable API/model quotas and billing alerts; alerts alone are not a spending cap. Confirm the deployed limits and quota owner on the board.
  - [x] **Joseph · #119 server enforcement:** photos and clarifying answers share sliding limits of 10 attempts per account and 100 per network per 10 minutes. Guests are network-limited too; IPv6 /64 grouping and trusted-tunnel identity reuse the existing auth rules. Refusals return 429/`Retry-After` before model work; the photo quota is checked before reading upload bytes. Tests cover both endpoints, account/network separation, expiry, guest mode, forwarded-header spoofing and no model calls after rejection. API/auth tests: 99 passed; targeted Ruff checks passed; the actual isolated CLI server returned 429 for both routes at exhaustion.
  - [ ] **Harsh + Joseph:** configure and confirm enforceable provider quotas and billing alerts on the board. The in-process counters reset on restart and are per worker, not a project-wide spending cap. This is why AUD-03 remains open.

### P1 — core promise, trust and deployment correctness

- [ ] **AUD-04 · Harsh (app) + Joseph (contract): connect the app to the campus-menu backend.**
  - **Audit evidence (before #121):** the browser omitted the hall and rendered all matches as USDA, leaving the backend campus path unreachable from uploads. That implementation gap is now fixed; the remaining acceptance work below is still open.
  - **Done when:** capture lets the user confirm a dining hall without requiring location permission, sends its `location_id`, and renders `source: "campus"` versus `"usda"` truthfully, including hall, dish and serving basis. Location denial/off-campus/no matching dish must retain a usable USDA path. Counted campus portions need a servings-preserving correction control: sending grams for a counted dish can switch it to USDA. Keep photo-time work in section 3 connected to this flow.
  - **Proof:** an isolated real campus meal produces campus-sourced main nutrients; an unmatched/off-campus meal produces an explicitly labeled USDA fallback; corrections preserve or visibly change provenance. Do not close on backend tests alone. If this cannot land before judging, remove/qualify the campus-first promises before presenting them.
  - [x] **Harsh · #121 integration:** browser uploads now send hall and local photo time, and source-aware result cards distinguish campus labels from USDA. Hall context uses nearby commons, Menu choice or main hall. **Still required:** isolated real-photo/fallback/correction proof, explicit hall-context review and counted-serving behavior; a merge does not establish those results.

- [ ] **AUD-05 · Harsh + Joseph: finish the SMTP/account go-live cutover, including frontend capability detection.**
  - **Audit evidence (before #123):** the app inferred sending capability from provider names and treated SMTP as a placeholder. Production provider/flag settings and delivered-email behavior were not established by the audit.
  - **Done when:** the app recognizes the actual sending provider; production uses SMTP and required authentication; a team-owned address receives working verification and reset links on `https://app.mealscholar.health/`. Confirm the public URL, secure cookies, development outbox disabled and demo verification bypass disabled. Never turn on public auto-verification as a substitute for delivered email.
  - **Separate claim:** do not promise emailed favorites alerts merely because SMTP works. Confirm the digest is scheduled and restricted to verified, opted-in recipients, or keep its unavailable wording accurate.
  - [x] **Harsh · #123 capability:** the server now exposes `email_sending` and the app uses it for availability wording; account mail is multipart and a password-changed notice exists. Delivered verification/reset email, required-auth deployment, public link origin and scheduled digest checks remain open.

- [ ] **AUD-06 · Harsh + Joseph: publish privacy/retention information and disclose processing at capture.**
  - **Evidence:** neither marketing navigation/footer nor the app links a privacy notice. Photos are processed by Google Gemini; opt-in coordinates can go to public OpenStreetMap Nominatim (`context.py`); sessions retain IP/user-agent information; Cloudflare injects a live analytics beacon. “Only the place name is saved” describes diary storage, not all processing.
  - **Done when:** both sites link a plain-language notice identifying the operator/contact, providers, data sent, data stored, retention and deletion process. Distinguish photo/EXIF handling, place names, account/health preferences, sessions and analytics. Add concise capture/location disclosures before transmission. State actual provider policies, not an unsupported “nothing is stored anywhere” or medical-compliance claim.
  - Account/meal deletion exists in source; verify its isolation and cascade under AUD-01. Data export is a later product decision, not a falsely claimed existing feature.
  - **Retention update (#122/#126):** logged meal photos are now retained as stripped JPEGs and thumbnails under the data directory, served through the meal owner's API and removed with meal/history/account deletion. README/submission wording is corrected; the actual site notice and capture disclosure must describe this, the shared-guest risk and any cam-preview retention. AUD-06 remains open.

- [x] **AUD-07 · Joseph: make app response protections consistent (#112).**
  - **Evidence:** direct app-root responses lacked `X-Content-Type-Options`, `Referrer-Policy` and a framing restriction. The landing already sends `nosniff` and `strict-origin-when-cross-origin`, but neither root declares a framing restriction.
  - **Done when:** one documented layer applies `nosniff`, an intentional referrer policy, and `frame-ancestors 'none'` or equivalent anti-framing protection. Keep any broader CSP compatible with the inline theme boot, uploaded-photo previews and deployed analytics; do not ship a policy that silently breaks capture, location or installation.
  - **Completed:** ASGI response middleware now sends `nosniff`, `strict-origin-when-cross-origin` and `X-Frame-Options: DENY` without imposing a script/image/camera CSP. Verified locally on HTML, CSS, unauthorized API replies and 404s, plus a real browser launch. API/metadata regression run: 27 passed; targeted Ruff lint and format checks passed. Deployment-layer HTTPS/HSTS remains AUD-02, not part of this completion.

- [ ] **AUD-08 · Harsh: revalidate the app HTML, not just its versioned assets.**
  - **Evidence:** the landing root sends `Cache-Control: no-cache`; the app root has no explicit `Cache-Control`. Both already stamp JS/CSS URLs with a commit. Cached HTML can still point a phone at an old set of versioned assets.
  - **Done when:** app HTML has an explicit revalidation policy, private JSON responses are not publicly cached, and a normal reload after a deploy receives the new asset version without requiring users to know about hard reloads. Preserve effective static-asset caching.

- [ ] **AUD-09 · Harsh + Joseph: back up SQLite, retained meal photos and Atlas.**
  - **Evidence:** meals/learned portions are in `MEALSCHOLAR_HOME/diary.sqlite`, accounts/preferences in Atlas, and #122 adds retained JPEGs/thumbnails under `MEALSCHOLAR_HOME/photos/`. An Atlas-only backup cannot restore a diary with its photos.
  - **Done when:** scheduled SQLite-consistent, photo-directory and Atlas backups have documented retention/access controls. Restore an account with its diary, photos and preferences in isolation. A bare copy of a live SQLite file without transaction/WAL consistency is not sufficient proof.

- [ ] **AUD-10 · Harsh + Joseph: align correction and goal copy with actual behavior.**
  - **Evidence:** landing FAQ 04 promises a swap “from the campus menu or USDA”; the correction API/sheet currently selects USDA foods, not campus dishes. A campus result's USDA match may supply only micronutrients, so it must not be presented as the source of all nutrition. The app still contains “No targets” copy even though user-chosen goals exist.
  - **Done when:** either campus-dish swapping works end to end or the FAQ clearly describes the supported USDA correction. Explain provenance changes, retain ranges and micronutrient estimate labels, and replace unconditional “No targets” with optional/user-chosen goal wording. Do not advertise “one tap” where a search-and-confirm flow is required.

**Still-open account-security work:** section 5's account-enumeration response remains open. Password-hash and decoded-image-limit tasks are completed below; neither closes the remaining P0 deployment gates.

### P2 — demonstration, usability, accessibility and discovery

- [ ] **AUD-11 · Harsh: give counts, coverage and refresh claims one dated source.**
  - **Evidence:** the landing hard-codes 24,191 menu items, 2,236 dishes, 13,648 USDA foods, a November 10–28 window and November 14 samples. “Refreshed every hour” is repeated, but the public locations response exposes no last-successful-refresh field. The prior board count differed; the audit did not independently count Mongo documents or verify the scraper.
  - **Done when:** define and document each count/query and date, reuse the same snapshot across landing/app/submission, and show a last-successful refresh or explicitly dated snapshot. Label the November 14 sample dishes as samples, not permanently live menus. Qualify “every dish” where source nutrition is absent/rejected. The main-nutrient summary should name all nine consistently, including saturated fat.

- [ ] **AUD-12 · Harsh + Gabby: replace or remove the non-playing demo promises.**
  - **Evidence:** three “Coming soon” placeholders with play-shaped artwork remain at [`#videos`](https://mealscholar.health/#videos), and a hero carousel card promotes them. No recording plays.
  - **Done when:** publish real photo→ranges, clarification and correction→Today clips with working controls, phone playback, useful captions/transcripts and suitable posters; otherwise remove the empty section, hero promotion and navigation links together. Do not leave controls that look like playable media.

- [ ] **AUD-13 · Harsh: fix tablet hero-card action overflow and touch sizing.**
  - **Evidence:** at 768 px, the active `.fan-card` measured 220 px wide with 249 px scroll width; its 184 px action row had 231 px scroll width and “Learn more” extended about 28 px past the card. The active links are 40 px tall, below the project's 44 px touch-target rule. Document-level overflow checks miss this component clipping.
  - **Done when:** both actions fit inside every active card across the tablet breakpoint and 320–2560 px range, labels remain readable, targets meet the project's 44×44 px rule, and keyboard focus stays visible. Preserve the existing carousel's announcements, pause behavior and reduced-motion support rather than replacing working interaction unnecessarily.

- [ ] **AUD-14 · Harsh: simplify the first-photo journey before asking for installation.**
  - **Evidence:** the mobile hero and sticky dock emphasize “Add to Home Screen”; the destination `?install=1` immediately opens an installation sheet before any meal is analyzed. The app's first Today view then leads with a location prompt and repeats the no-meals message in two regions.
  - **Recommendation, not a measured conversion regression:** make “Open/Try the app” the mobile primary action, keep installation optional/secondary or after first value, and make the first photo action the main empty-state focus. Location remains optional; do not force a permission prompt or installation to try a meal. Preserve the desktop phone handoff/QR path.

- [ ] **AUD-15 · Harsh + Gabby: make operator, support and pilot scope discoverable.**
  - **Evidence:** the site presents UMass branding and a pilot claim, but no contact/support/team link is offered in the footer; “campus” is generic above the fold although the integration is UMass-specific.
  - **Done when:** users can find a monitored support/report-a-wrong-match route and who operates the project. State UMass Amherst coverage, the off-campus USDA fallback and the pilot's limitations. Make institutional relationship/endorsement wording explicit and supportable; do not imply an official partnership just from the logo.

- [ ] **AUD-16 · Harsh: make the campus-data link lead to a usable source or menu.**
  - **Evidence:** the footer's “Campus dining menus” link points to `#umass`, a static three-dish sample on the same marketing page, unlike the genuine external USDA/OpenStreetMap source links.
  - **Done when:** a clearly labeled link opens the app's real Menu view and the data attribution points to the actual campus source. Users should not have to infer that a sample card is the full menu browser.

- [ ] **AUD-17 · Harsh: close the manual accessibility gaps left by automated checks.**
  - **Evidence:** sampled landing and Settings scans had no confirmed A/AA violations, but many landing contrast nodes were **incomplete**, not passes. At 390×844, axe flagged the app footer's FoodData Central link for target size/partial obstruction near the fixed dock; this needs manual confirmation, not an unqualified conformance claim.
  - **Done when:** verify light/dark text and UI contrast over image/gradient backgrounds; test visible keyboard focus and screen-reader names/ranges; confirm footer links and final content remain reachable above the dock at mobile sizes and zoom. Include VoiceOver/Safari and a second screen reader/browser. Track any reproduced defect separately; keep existing skip links, native disclosures, modal focus return and reduced-motion behavior.

- [ ] **AUD-18 · Harsh + Gabby: complete share-preview and canonical metadata.**
  - **Evidence:** the marketing head has a title, description and basic `og:title`/`og:description`/`og:type`, but no canonical URL, `og:url`, share image or Twitter card. A share in a judging/team chat therefore has no intentional image.
  - **Done when:** the canonical and social URL use `https://mealscholar.health/`, a suitable deployed absolute `og:image` returns 200 with an image content type, and a large-image card previews correctly. Existing artwork outside `web/` is not automatically publicly deployed. Do not add invented reviews/ratings to structured data.

- [ ] **AUD-19 · Harsh: define crawler policy deliberately.**
  - **Evidence:** both [`robots.txt`](https://mealscholar.health/robots.txt) responses contain only Cloudflare's content-signals comments; the marketing [`sitemap.xml`](https://mealscholar.health/sitemap.xml) returns 404. Missing a sitemap is low impact for this single-page site, not a launch blocker.
  - **Done when:** marketing discovery/canonical policy is intentional; if adding a sitemap, serve it and reference it in robots. Set an explicit indexing policy for app/account surfaces and keep APIs/private data out of search. Robots/noindex are not authentication or a fix for AUD-01. Check the edge-served result because Cloudflare manages part of this response.

- [ ] **AUD-20 · Harsh: establish a mobile performance baseline before optimizing.**
  - **Evidence:** the app ships a single roughly 246 KB JS file and 152 KB CSS file before compression; fonts are preloaded and text compression/edge caching work. These file sizes are not proof of poor real-user performance. Remote PageSpeed attempts were rate-limited; no Lighthouse score or field Core Web Vitals result was obtained.
  - **Done when:** record a throttled mobile trace covering initial use, menu loading and photo-analysis waiting states; measure LCP, CLS and interaction latency, network transfer and model time separately. Optimize demonstrated bottlenecks, preserve responsive image selection/lazy loading, and extend cache lifetimes only for genuinely versioned immutable assets. Avoid a framework/build rewrite justified only by source-file size.

- [ ] **AUD-21 · Harsh + Joseph + Gabby: finish the domain/documentation cutover (#55).**
  - **Audit evidence:** the new hosts worked with temporary legacy redirects, while the documentation still described older implementation states. Subsequent README/submission corrections are recorded below; edge/email-origin and filming-document checks are separate.
  - **Done when:** README/configuration instructions describe current implementation versus enabled deployment, including SMTP settings; all intended app/site/video/submission links and QR codes use the new hosts; account emails use the new app origin. Preserve legacy path/query redirects, make them permanent when the cutover is settled, and retire obsolete “wait for rename/UI merge” instructions. Do not describe backend-only campus support as a working app feature.
  - [x] **Joseph · #117/#128/#126 documentation:** README and submission now cover new domains, current campus/time integration, real SMTP capability, retained stripped meal photos and all three storage sets. The obsolete conditional campus-story block is removed. Film-plan, account-email origin and permanent edge-redirect work still keep AUD-21 open.

- [ ] **AUD-22 · Harsh: provide helpful non-API not-found recovery.**
  - **Evidence:** an unknown app path answers raw `{"detail":"Not Found"}` JSON. Hash routes work and real 404 status codes are present; this is recovery polish, not broken main navigation.
  - **Done when:** an unknown browser-page URL offers a clear return to the app/website while retaining status 404. API failures remain appropriately structured JSON, not an HTML catch-all or a false 200.

- [ ] **AUD-23 · Harsh + Gabby: prove the phone capture/install experience on actual devices.**
  - **Evidence:** the manifest, icons, Home Screen sheet and iOS/Android instructions are present. The manifest forces portrait orientation; the maskable and normal 512 px icons are identical. Native camera, HEIC selection, actual OS installation and icon masking were not verified in desktop Chromium emulation.
  - **Done when:** test iPhone Safari and Android Chrome: take/choose a photo, HEIC where supported, denied location, cancelled analysis, malformed/over-limit upload, flaky connection, Home Screen install/relaunch and icon safe-area appearance. Remove a non-essential portrait lock and verify landscape usability. Keep unsupported/offline behavior honest; an offline feature is not required by the current promise. Run writable checks only after AUD-01 is closed or in an isolated staging diary.

**Suggested order:** AUD-01–03 first; then AUD-04/05 and section 1's end-to-end smoke; then privacy, response/cache correctness and backup/restore. Finish the real demo media and tablet defect before judging. SEO/performance/404 polish must not displace privacy or the core campus promise. Existing section 5 range-calibration work remains essential before treating the displayed ranges as validated accuracy.


## 1. Merge chain and cleanup

The base chain (#7, #11, #13 and #15) is merged. GitHub checked during the live-site audit: #48 (app UI) merged at 16:19 UTC and #41 (rename) at 16:50 UTC on November 14. #106 (campus pipeline, photo time, HEIC and SMTP) merged at 22:19 UTC. Do not use the earlier open-PR list as current status.

- [x] **Joseph**: rebase #7 on `main` and fix the `AGENTS.md` conflict. Run `uv run pytest` and `uvx ruff@0.17.5 check`, then merge. Delete the branch on merge so GitHub moves #11 to `main`.
- [x] **Joseph**: review #11 (login). Done before it merged: a security review found no code bugs, and Harsh confirmed the three server settings (`MONGODB_URI`, `MEALSCHOLAR_PUBLIC_URL`, `MEALSCHOLAR_DEV_OUTBOX` unset). Its follow-ups are under "Before real users" in section 5.
- [x] Merge #11, #13 and #15 back to back, in that order.
- [ ] **Harsh + Joseph**: finish an isolated, authenticated end-to-end smoke test: sign up, receive verification email, onboard, upload, answer the question, correct a match, then check Today and Week. During the audit the live app still reported `auth_required: false` and `email_provider: outbox`; successful read-only API requests do not close this task.
- [x] **Joseph**: the rest of Harsh's #7 review. Coordinates are kept out of the access log and meal notes (#30). Photos that won't decode get 413/415 instead of 500, and error replies no longer show server paths or Gemini's raw errors (#32).
- [x] **Joseph**: landing page dark mode (#16), regenerated after Harsh's landing rework. Merged as #45, with a System, Light, Dark toggle.
- [x] **Harsh + Joseph**: app dark mode is live. The audit exercised Settings → Appearance → Dark and rendered the app at mobile and desktop sizes; System, Light and Dark are present. The old post-#48 patch dependency is obsolete.
- [x] **Joseph**: add `test_pictures/` to `.gitignore`. The photos carry GPS EXIF (see `BENCHMARK.md`). Done in #62. Reversed: the six photos are committed with EXIF stripped, and the gitignore line is gone.
- [x] **Harsh + Joseph**: land the rename to `mealscholar` (#41). GitHub confirms it merged on November 14; the current app source is `src/mealscholar/site/` and the live app responds. The old instruction to keep #41 rebased is obsolete.

## 2. Campus-menu promise and data accuracy

**Current state:** #106 implemented the campus backend and #121 connected the browser hall/time inputs and source-aware labels. The code path is integrated; AUD-04 remains open for real-photo, fallback, hall-context and counted-serving/correction acceptance proof, not another backend hookup.

- [x] **Joseph + Harsh**: implement backend campus matching (#106).
  - [x] Select the menu day/meal from photo time, including late-night handling. The hall is an explicit `location_id`, not inferred automatically from location or saved halls.
  - [x] Give Gemini menu dish-name candidates; match the returned `menu_item` against the menu. The implementation uses names, not the originally proposed dish-id output.
  - [x] Use campus label nutrients when the match passes checks; otherwise use USDA. Parse weighted serving sizes and support estimated serving counts for counted dishes.
  - [x] Add provenance: `"campus"` or `"usda"`, with `dish` and meal-level `menu`. #121 also renders campus-versus-USDA labels in the app; remaining acceptance proof is AUD-04.
  - [x] Add backend coverage for menu hit/miss/unavailable data, late-night selection and corrections (`tests/test_pipeline.py`, `tests/test_web.py`). Existing tests were inspected, not rerun in this documentation audit.
- [ ] **Joseph**: rerun the dining-photo benchmark against the integrated #121 path. Report campus hits, fallbacks, portion accuracy, source changes on correction and before/after error, not just whether a campus label appeared.
- [x] **Harsh**: hourly menu refresh was confirmed on the board at 7:53 AM. This remains a prior operational assertion; freshness visibility and current counts are tracked in **AUD-11**.
- [x] **Joseph + Harsh**: remove the old “no calorie targets” statements from README/insights and the app footer/header. Remaining unconditional “No targets” UI wording is tracked in **AUD-10**.

Remaining landing-copy fallback, count reconciliation and demo recordings have one checklist each: **AUD-04**, **AUD-11** and **AUD-12**.

## 3. Accuracy (Joseph)

These come from the recommendations in `BENCHMARK.md`, ordered by measured impact.

- [ ] Prompt: skip food cut off by the edge of the photo unless it's the subject. This fixes the drink-photo pizza and the `IMG_0583` glass.
- [ ] Refine prompt: "update existing items, never duplicate one". Add a test for the duplicate dressing.
- [ ] Prompt: the question is one sentence. Split composite salads into their parts.
- [ ] Lookup misses still open:
  - "un-sweetened" tea splits into two words and matches sweetened tea
  - pepperoni thin crust loses a tiebreak to "extra cheese"
  - a whole salad matches "Salad dressing, NFS"
- [ ] Let Gemini choose among the top USDA candidates with structured output.
- [ ] After every lookup change, post the before/after from `uv run python bench/report.py --rematch`. CI's Lookup regression check (#22) already fails PRs that make the lookup worse. When a change beats `REMATCH_LIMITS` in `bench/report.py`, raise the limits in the same PR.
- [ ] **Harsh + Joseph**: verify phone-local diary dates and late-night menu choice near midnight. #106 accepts offset-bearing `taken_at` and #121 now sends the browser's local photo time and hall; do not reopen that completed integration. Real-device/time-boundary acceptance remains to be observed.
- [x] Calorie ranges too wide (#54). Meal and day totals now combine the foods' ranges as partly correlated errors instead of adding every low and every high. Meal #7 goes from 945–1,455 to 1,010–1,369 kcal. The refine prompt narrows the items an answer pins down, and the omelette no longer matches a fried egg. Defining low and high as the 10th and 90th percentile was tried and not kept. See "Range width" in `BENCHMARK.md`.
- [ ] **Harsh**: on the result card, lead with the likely kcal and show the range smaller, the way Today's `hero__num` does (#54). Today the big number is the range (`totalHtml`, `kcalRangeHtml`).

## 4. Demo and submission

| When (EST) | Task | Owner |
|---|---|---|
| Sat 2:00 PM | MVP works end to end on app.mealscholar.health (historical checkpoint; full authenticated smoke remains open) | All |
| Sat 6:00 PM | Cut anything that can't be demoed by midnight | Harsh |
| Sat 11:00 PM | Submission draft: done early in `submissions/devpost.md` (#28). Review it before pasting it into Devpost | Harsh + Gabby |
| Sun 12:00 AM | **Initial submission** | Harsh |
| Sun 2:00 AM | Feature freeze, then bug fixes only | All |
| Sun 7:00 AM | Record the demo video (script in `video/`), take screenshots, finish the README | Gabby |
| Sun 8:30 AM | **Final submission** (30 min buffer) | Harsh |
| Sun 12:30 PM | **Judging** | All |

- [x] Prize tracks, decided in `submissions/devpost.md` (Harsh can still change them). Devpost allows only one [HACKUMASS] category, and every project is considered for the grand prizes automatically.
  - [HACKUMASS] category: **Best UMass Hack**. Best UI/UX, Most Impactful AI Hack and Best Pitch are [HACKUMASS] categories too, so we can't also enter them.
  - Sponsor opt-ins, which don't count toward that limit: **[MLH] Best Use of Gemini API** (talking points in `BENCHMARK.md`) and **Best Use of MongoDB Atlas**.
- [x] Gabby acquired **mealscholar.health** (registered at Porkbun, 10:51 AM EST).
  - Finish the remaining cutover work under **AUD-21**; both new origins already serve, while #55 remains open.
  - [x] **Gabby**: switch nameservers to Cloudflare. The audit's DNS lookup returned `treasure.ns.cloudflare.com` and `mario.ns.cloudflare.com`.
  - [x] **Harsh + Gabby**: the submission's GoDaddy Registry answer names `mealscholar.health` and distinguishes Porkbun (registrar) from the registry. Confirm the actual Devpost form matches the corrected draft when submitting.
- [ ] **Harsh**: make the repo public before submitting, or drop the code link. Judges get a 404 on a private repo. Harsh's Claude says nothing secret is in the repo.
- [ ] **Joseph or Harsh**: put the numeric Gemini project number in `submissions/devpost.md`. We only have the project ID (`gen-lang-client-…`). The draft explains where to find the number.
- [x] **Joseph · #128/#126**: the campus-menu submission story reflects #121's integrated browser path and no longer has conditional replacement instructions. Real-photo source/correction proof remains AUD-04, and submission-form completion is separate.
- [ ] **Gabby**: add the video link (YouTube, Vimeo or Youku) and the gallery images to the Devpost form.
- [x] **Joseph**: improve the Devpost thumbnail's gallery-size hierarchy (#100). Use `brand/devpost-thumbnail.png` (1800×1200, 3:2); its editable HTML keeps the real salad result and makes the calorie range the primary number.
- [ ] The final deadline is 9:00 AM on hackumass.com but 10:00 AM on Devpost. Go by 9:00 AM.
- [ ] A demo account with a few days already logged, so Today and This week aren't empty.
- Public demo verification bypass/outbox configuration is tracked in **AUD-05**. Do **not** enable `MEALSCHOLAR_DEMO_VERIFY_LINK` on the public app as a shortcut to sign-up.
- [ ] Fallbacks: a recorded video, and the faster model (`MEALSCHOLAR_MODEL=gemini-3.6-flash MEALSCHOLAR_THINKING=minimal`) if Gemini is slow on venue Wi-Fi.
- [ ] Charge phones. Test the demo on venue Wi-Fi and on cellular.
- [ ] **Everyone filming**: reload `app.mealscholar.health` on each filming phone and confirm the current UI before recording. Asset versions are stamped, but HTML revalidation still needs **AUD-08**; do not rely on an old cached tab or old-domain recording instructions.

## 5. Continuous development (keep doing this after the hackathon)

- [x] **Joseph**: add a test workflow (`uv run pytest` on `ubuntu-latest`). The **Tests** workflow (#62) runs on every pull request and push to `main`, next to Ruff (lint and format) and the Lookup regression check (#22). The tests use a fixture USDA database, and the Mongo tests skip without `MEALSCHOLAR_TEST_MONGODB_URI`.
- [ ] **Harsh**: branch protection on `main`: require Lint and Tests to pass. Every push to `main` is a live deploy. GitHub's rulesets API says this needs GitHub Pro while the repo is private, so it may have to wait for the public/private decision in section 4.
- [ ] Keep stacks of PRs at most two deep, and merge the base the same day. #7 sat for 7 hours and blocked three PRs.
- [ ] Every PR includes tests and a clean Ruff run, plus screenshots for UI changes. Post on the board when a contract changes (an API, a schema, an env var).
- [ ] Before real users:
  - [x] Implement a real email sender. #106 added SMTP support; this is no longer a placeholder-only implementation. Production activation and delivered verification/reset email checks remain open: the live audit still saw `email_provider: outbox`.
  - Server request limits are implemented in **#119**; provider quota/billing confirmation remains under **AUD-03**. Anonymous diary isolation is still **AUD-01**.
  - [ ] Rotate the Gemini key and the Mongo connection string if they were shared in chat.
  - Account/preferences **and SQLite diary** backup/restore are tracked in **AUD-09**; diaries are not stored in Atlas.
  - [ ] Move hosting off a personal Mac mini.
  - [ ] From the #11 security review:
    - [x] Require a confirmed email before the unsolicited favorites digest (`dining.py`). Retain this guard when SMTP is enabled; delivered-email/digest go-live checks are **AUD-05**.
    - [ ] Stop revealing existing accounts through sign-up's 409 response; define a uniform public response and an appropriate email path.
    - [x] Scope forgot-password throttling by network and by `(network, email)`; the current `auth.py` already implements this. The old global per-email quota task is obsolete.
    - [x] **Joseph · #119:** raise new-password scrypt to N=2^17, r=8, p=1, with a 256 MiB OpenSSL memory ceiling and the existing four-operation concurrency bound (about 128 MiB per hash). Existing N=2^14 hashes still verify from their stored parameters; password changes/resets use the stronger cost. API/auth tests: 99 passed, including legacy verification and real hashing.
  - [x] **Joseph · #112**: enforce a 50-megapixel ceiling in the shared photo loader before pixel decoding. The web API returns 413 with lower-resolution guidance. Regression coverage includes a 60 MP image below Pillow's default warning threshold; existing JPEG/HEIC paths remain covered. API/metadata tests: 27 passed.
- [ ] Weigh 10 real meals on a kitchen scale, so the benchmark has real error bars instead of estimates. Then check that the narrower meal ranges from #54 still contain the weighed amounts, and retune `compute.SHARED_ERROR`.
- [x] Run `bench/report.py --rematch` in CI as a regression check: the Lookup regression workflow (#22), with the USDA download cached.

## 6. Later

- [ ] Allergen and diet flags on logged meals. The dining data already has allergens and diet tags, and onboarding saves the user's own.
- [ ] Send all photos of one meal in a single Gemini request, so items at the edges are counted once.
- [ ] v2: chain restaurant menus, barcode scanning.
- [ ] v3: processing level (NOVA), backfill from the camera roll.
