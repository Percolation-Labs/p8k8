# Percolate — A Human Insight Marketplace

v1.0 — working name, not final (a prior trademark exists in an adjacent software category, §20; rebrand is a live option, not a blocker)

**One-line description**: AI percolates signal up out of noise (event detection across public data); human insight percolates up out of AI's first pass (a reputation system that separates good insight from noise, over time). The product is the marketplace connecting the two — writers claim stories and build a provable track record, readers subscribe to and rate the experts worth trusting.

Format target: something you read on the toilet or on the train — short, mobile-first, no friction. Not a research report, not a full article.

**Note for agents starting work from this spec**: this document is a reference and design record, not a sequenced task list — sections were added as the design evolved, not in build order. Recommended entry points, matching the sequencing already established in §19 and §22: (1) stand up the environment (§22), (2) build the real scraper against the verified sources (§15), (3) implement entity resolution and corroboration scoring (§16) — this is the actual algorithmic core everything else depends on, (4) establish the Postgres schema for both graphs (§8), (5) run the ABM parameter sweep before trusting the reputation math (§17), (6) build the minimal MVP surface (§13) with Stripe explicitly deferred until a payment flow is actually needed (§18.1). Treat every "open question" in §23 as genuinely open, not resolved by omission.

## 1. Concept

An aggregation platform where AI performs the dull, high-volume work of detecting connections and emerging patterns across business/tech/world events, and humans supply the value that AI cannot: informed opinion, insight, and commentary. Users see a stream of human-generated content, each piece triggered by an AI-detected event.

The human contribution is **not prescribed reporting** — no on-site interviews or original newsgathering required. It is sufficient for a human to offer an informed opinion or take on the event the AI has surfaced. This keeps the "job" cheap to fulfill and removes logistics/dispatch as a bottleneck; the hard problem is routing and trust, not fulfillment.

**Core value proposition: human insight over different time scales.** In a world where AI-generated commentary is free and abundant ("slop"), the platform's differentiated value is a legible, trackable record of *whose insight is actually reliable*, and at what horizon (fast-reactive vs. long-horizon calls). This reframes the product from "AI finds news, humans comment on it" to "a track record of who is actually right, at the timescale that matters to the reader." It also directly resolves the insight-vs-accuracy tension in §6.4: a bold take that's wrong short-term may be right long-term, and timescale turns that from a scoring conflict into a dimension of the track record.

**Informally: this is deslopification.** The AI does the part slop is good at (finding things at scale); the reputation system is the machinery that keeps the output from being slop, because slop is cheap precisely because no one's track record is on the line for it. The literal shape of this, detailed in §16.5, is a trust flow: primary source → traceable expert review → reader trust → commissioned further review — every step traceable back to a source, never to a paraphrase of one.

Two free-standing halves of the product:
- **The marketplace**: writers claim triggered stories (gigs), build a category-scoped reputation, and readers can subscribe to and rate individual experts.
- **The free aggregation layer**: an "idea generator" — a curated feed of detected patterns and relationships between events/entities/topics, available without payment, that doubles as top-of-funnel discovery for the marketplace and as a standalone trends product in its own right.

**Design principle for this spec: don't lock in mechanisms that have no supporting data yet.** Where a design choice is genuinely undecidable without real usage data (e.g., how time horizon gets assigned — see §10), the right move is to leave the options on the table, ship the simplest version that lets both be tested, and let implementation/iteration decide. Theoretical elegance is not a tiebreaker in the absence of data.

## 2. Core Loop

1. AI continuously scans public data streams (§15) and detects events, clusters, or emerging connections worth commentary.
2. A detected event is tagged with a category and topic/entity links (§8.2) and surfaced as an open "claim" opportunity.
3. The event is classified — fresh scoop, ongoing story with a new angle, or zeitgeist-echo (§3) — before being offered to writers.
4. Qualified humans (per category reputation) claim the story and submit opinion/commentary.
5. Submissions are rated by peers and by AI (§6).
6. Rated content streams to readers, ranked by quality/reputation signals; readers can subscribe to specific experts and rate them directly.
7. Where events later resolve (an outcome becomes knowable), AI performs a retrospective accuracy audit that feeds back into contributor reputation (§6.3).

## 3. Scoop Classification & the Zeitgeist Knowledge Base

Not every detected event is the same kind of opportunity, and treating them identically undersells the good ones and oversells the bad ones. Before an event is offered to writers as a claimable story, it goes through a classification step — and a separate, standing layer interprets *why* smaller stories matter even when they aren't scoops at all.

### 3.1 Pre-Flight Check: Is This Actually New?

The retail-tariff example tested against real data (see §15.1's sourcing evidence) showed the failure mode directly: a genuinely detected cross-company pattern turned out to be a story Forbes, CNBC, and several research shops had already covered in depth, because the underlying source (SEC 8-Ks) is filed simultaneously with press-covered earnings calls. Detecting a pattern is not the same as detecting it *first* — and the platform should never claim otherwise on a source that structurally can't produce lead time.

Before surfacing a flagged event, the system runs a live check, not an assumption:
- **A dated news/web search** for existing coverage of the same underlying fact pattern.
- **A social-commentary check** — is anyone already discussing this on the subculture/social sources in the sourcing plan (§15)?

Based on what that check finds, every flagged event gets classified into one of three states, and the framing offered to writers differs accordingly:
- **Fresh scoop** — no meaningful prior coverage found. Genuine lead time. Framed to writers as "you're first."
- **Ongoing story, new angle** — the broader story is already covered, but this specific data point, filing, or connection isn't. Framed as "here's what's missing from the existing coverage," not as a scoop.
- **Saturated** — already thoroughly covered from the same angle. Deprioritized or dropped rather than offered as if it were fresh.

This classification should itself be built on the same rigorous backtest discipline as the reputation algorithm (§17): lead-time claims are a per-source empirical property (§15's finding that trial registries and patent filings plausibly have real lead time, while earnings-linked filings structurally don't), not a platform-wide marketing claim.

**Note (v1 build scope): the live search-based pre-flight check above (Tavily or similar) is explicitly out of scope for the first PR.** v1 implements the three-way classification using algorithmic signals already available in the pipeline (corroboration score, source-type diversity, entity-resolution confidence — §16.3/§16.4) rather than an external search call. Wiring in a live freshness search is a fast, isolated follow-up once this pipeline is proven.

### 3.2 The Subtler Case: Small Stories That Echo the Zeitgeist

Not every valuable story is a scoop or even a fresh angle on an ongoing one. A small, individually minor event can be worth surfacing precisely *because* it's a fresh data point in a larger, ongoing debate — an echo of the zeitgeist rather than news in its own right. A single obscure company's earnings call mentioning reshoring, or one small university's AI-policy statement, may be unremarkable alone but becomes worth a take when framed as "another data point in the US-China AI race debate."

This requires the platform to maintain a standing **zeitgeist knowledge base**: a maintained (not one-shot) store of the major ongoing narrative themes of the moment (in 2026: AI vs. China/US competition, tariffs and trade, geopolitical conflict, among others), each entry holding:
- The theme itself.
- **The main debate** — the actual competing positions people hold, not a flattened single narrative. For "AI vs. China," for instance, the debate genuinely has multiple credible sides (a national-security race the US must win at speed, versus a view that the race framing itself accelerates risk and export-control approaches are counterproductive) — the knowledge base should hold the *structure* of that disagreement, not resolve it.
- Which side(s) of the debate a newly detected small event provides evidence for or against.

When a small, otherwise-unremarkable event is detected, it gets checked against this knowledge base: does it echo one of the standing debates? If so, it can be surfaced with that framing ("here's a small but real data point on side X of the AI-race debate") even though it would never pass as a scoop or a fresh angle on its own. This is a **third content category**, distinct from both scoop types in §3.1 — not competing with them, but filling the gap where most of the platform's actual volume likely lives, since fresh scoops and even good ongoing-story angles are comparatively rare next to the steady stream of minor events that quietly reinforce or complicate a bigger debate.

**Open question**: how the zeitgeist knowledge base itself gets maintained and kept current — whether it's periodically regenerated by an LLM synthesis pass over recent high-volume coverage, hand-curated, or some blend — is deferred to §22, since it needs real operational experience to answer well rather than upfront design.

**Note (v1 build scope): a minimal version of this knowledge base (a small, hand-seeded set of standing themes) is in-scope as a nice-to-have if time allows, but the core v1 deliverable is §3.1's classification plus the detection pipeline (§16) — don't let §3.2 block the PR.**

## 4. Roles

- **AI (event detector)**: identifies and tags events/connections worth commentary. Always active, not reputation-gated.
- **Contributor ("writer")**: claims and submits opinion/commentary in response to a triggered event.
- **Reviewer**: rates/votes on other contributors' submissions. Review rights are earned, not granted upfront.
- **Reader**: browses the feed, subscribes to specific writers, and rates experts directly.
- Contributor and reviewer are **not separate tracks** — one symmetric score per user per category governs both the right to have your own work weighted and the right to review others' work (see §7).

## 5. Categories

- Every AI-detected event is tagged with a category (e.g., fintech, AI/ML, geopolitics, consumer tech, macro).
- Category tagging is **event-derived**, not self-declared by contributors — a user does not claim "I am a fintech expert"; their fintech reputation accrues only from participation in fintech-tagged events.
- Reputation is scoped **per category**, not global. A user's overall profile is the union of their category-level scores.
- Cross-category spillover (e.g., partial credit from "AI/tech" into "AI policy") is an explicit **v2 consideration**, not part of the initial design — categories are fully siloed at launch to keep the trust model simple.

### 5.1 The Admission Rule: Trigger-Type, Not Topic

Topic domain (fintech, AI/ML, geopolitics, etc.) is one axis; the other, more fundamental axis is **trigger-type** — the actual governing rule for what belongs on the platform at all, regardless of topic. Content earns its place only if it has an identifiable triggering event. Personal, evergreen, untriggered content (generic self-improvement essays, life-lessons writing with no external stimulus) is out of scope **on every topic**, not because those topics are uninteresting, but because they have no primary source to trace (§16.5) and no accuracy to audit (§6) — the trust-flow mechanism simply doesn't operate on them.

Four trigger types, crossing all topic categories:
- **Breaking event** — genuinely new and time-sensitive; the default case the core loop (§2) already describes.
- **Zeitgeist echo** — a small, individually minor event that resonates with an ongoing polarized debate (§3.2). "AI bashing," for instance, isn't a separate category — it's a zeitgeist-echo instance of the AI-race debate, still stimulated by a real triggering event, not generic commentary.
- **Anniversary/memorial** — a calendar-triggered commemoration of a historical event (e.g., "N years since X"). Distinct from the other three in one important way: it's **predictable in advance**, not reactive — anniversary dates are known ahead of time, so this is the one trigger type the detection pipeline can schedule for rather than only react to. Primary sources are the original historical record, still traceable per §16.5.
- **Triggered reflection** — a personal or reflective angle is admissible, but only when gated by an actual identifiable external trigger (a new study, an anniversary, a fresh event that reopens the topic) — never a generic, untriggered personal essay. This is a narrow carve-out, not a reopening of personal-development content broadly.

## 6. Reputation & Trust Model

### 6.1 Principle

Single, symmetric, per-category reputation score. The same score that a user earns from producing well-rated commentary is the score that grants them review/voting rights over others' commentary in that category. No separate "contributor score" vs. "reviewer score."

### 6.2 Composition

Score is a blended function of three inputs, with weights that shift over time and per category:

```
score(user, category) =
    w_human(t)      × peer_rating_component
  + w_ai_bootstrap(t) × ai_quality_rating
  + w_ai_outcome      × outcome_accuracy_rating   [only when/if the event resolves]
```

- **peer_rating_component**: ratings from other users who already hold reputation in that category. Higher-reputation raters carry more weight (Stack Overflow / PageRank-style) — this is the recursive-trust mechanic implemented over the social/reputation graph (§9.1).
- **ai_quality_rating**: AI's own assessment of the quality/insight of a submission. This is the bootstrap mechanism — see §5.3.
- **outcome_accuracy_rating**: a retrospective AI-driven audit of whether a take's implied prediction or claim held up once the underlying event resolved. This is a **permanent** AI role, not a bootstrap crutch, and continues to apply even in fully human-dominated categories.

### 6.3 AI Weight as Visible, Reducible Bootstrap

Each category displays a transparent ratio, e.g.:

> Fintech: 62% human-weighted · 38% AI-weighted

- On category creation, `w_ai_bootstrap` starts high (AI does most of the quality judging, since no humans yet hold reputation there).
- As real contributors participate and accumulate genuine peer-endorsed reputation, `w_human` rises and `w_ai_bootstrap` falls proportionally. The ratio is **not manually toggled by the platform** — it is a direct function of accumulated, peer-validated human reputation in that category.
- The ratio is designed to be **understood as a call to action**, not a hidden flaw: users can see when a category is still AI-dominated and are implicitly incentivized to build it toward human dominance. Early movers who help flip an AI-dominated category into a human-dominated one receive outsized reputation gains for doing so.
- The ratio must be resistant to gaming by raw volume alone — it should track accumulated *trusted* reputation, not submission count, so a category cannot be "flipped" by a flood of low-quality participation.
- **The ratio can regress.** If active reviewers in a category go dormant or leave, `w_human` can fall back and `w_ai_bootstrap` rise again. This is intentional — the ratio functions as a live health indicator for the category, not a one-time achievement.
- `w_ai_outcome` is **not part of this ratio** and does not go to zero even at 100% human-weighted quality scoring. The UI must distinguish these clearly: "human-weighted" refers only to real-time quality/insight judging; outcome auditing is a separate, permanent, always-on AI function. This distinction should be explicit in-product to avoid appearing contradictory.

### 6.4 Divergence Between Insight and Accuracy

A well-argued, bold take that turns out wrong should not necessarily score identically to a hedge-everything take that happens to be safely correct. Quality/insight rating (peer + AI bootstrap) and outcome-accuracy rating are kept as **distinguishable signals** rather than collapsed into one number — e.g., surfaced to users as something like a separate "insight" mark and "track record" mark, even though both roll into the same underlying category score.

## 7. Content Stream

- Users see a stream of human-submitted commentary, each item attributed to the AI-detected event that triggered it.
- Ranking within the stream should favor high-reputation contributors and high-peer-rated submissions, with visibility into the category's current AI/human weighting where relevant.

## 8. Data Model — The Two Graphs

The product is fundamentally graph-shaped, and there are **two distinct graphs**, not one — conflating them would tangle reputation math with content/topic modeling unnecessarily.

### 8.1 The Social/Reputation Graph

- **Vertices**: users.
- **Edges**: ratings — `rater → contributor`, weighted, timestamped, scoped to a category, carrying the rating value.
- **What runs over it**: the recursive-trust reputation computation (§6.2 — a rater's weight depends on their own accumulated reputation, PageRank-style), and the Phase 0 collusion-detection work (§18 — identifying tightly-connected reciprocal-rating cliques as a graph-structural signature, not just pairwise counting).
- This graph is the one the ABM (§18) is a simulation of.

**(v1 build scope: this graph is out of scope for the first PR — no reputation math, no ratings, no ABM. §8.2 is the graph this PR builds.)**

### 8.2 The Event/Entity Graph ("Topics and Experts")

- **Vertices**: events, entities (companies, people, products), topics/categories, and writers.
- **Edges**: "event mentions entity," "entity relates to entity," "event follows/resolves a prior event," "writer claimed/covered event," and (derived) "writer is a top voice on topic X" (the last one computed from the writer's accumulated reputation on events linked to that topic, not asserted directly).
- **What runs over it**: the event-clustering/deduplication work (§15 — entity resolution across sources), the "idea generator" pattern/trend digest (the free aggregation layer in §1), and topic/expert discovery pages (a topic page shows related events plus the writers with the strongest track record on that graph).
- **Reader actions map onto this graph directly**: subscribing to an expert is an edge from reader to writer; rating an expert is an edge that also feeds back into §8.1's reputation graph.

Keeping these separate matters: the reputation graph is about *trust between people*, and the event/entity graph is about *relationships between things in the world*. A writer's position in one is derived from, but not identical to, their position in the other.

**This is the core graph for v1: events, entities, topics, and the edges between them, populated from real ingested sources.**

## 9. Tech Stack

### 9.1 Database — Postgres, with a clear-eyed view of graph support

Postgres is the right default, not a compromise — both graphs above can live in one database without a second system to sync.

- **Today (building now)**: model both graphs as ordinary vertex/edge relational tables (e.g., `users`, `ratings(rater_id, contributor_id, category, weight, ts)`, `events`, `entities`, `event_entity_links`, `event_relations`). Query fixed-depth relationships with plain joins; use `WITH RECURSIVE` CTEs for variable-depth traversal (e.g., "topics related to this topic within N hops"). This is proven, if more verbose than a dedicated graph query language.
- **SQL/PGQ (Postgres 19, in beta now, GA expected ~Sept–Oct 2026)**: adds `CREATE PROPERTY GRAPH` and `GRAPH_TABLE` — a standards-based (SQL:2023 / ISO 9075-16) graph pattern-matching query layer defined as a **read-only view over existing relational tables**. Critically, this means **no data migration is needed to adopt it later** — build the vertex/edge tables correctly now, and SQL/PGQ becomes a nicer query syntax on top of the same schema once it's GA, not a rearchitecture.
- **What SQL/PGQ does *not* give you**: it's a query interface, not an analytics engine. No built-in PageRank, no community/clique detection, no weighted centrality — exactly the operations both graphs above actually need (reputation propagation, collusion-clique detection, topic/expert centrality). This is confirmed by Postgres's own documentation and community writeups as of the 19 beta.
- **Where Rust extensions come in**: this gap is exactly where a custom Postgres extension earns its place. `pgrx` (the Rust framework for building Postgres extensions) lets these graph algorithms — reputation propagation, collusion-clique detection, centrality for topic/expert ranking — run as compiled, memory-safe, in-process SQL functions, rather than round-tripping the graph out to an external service on every computation. `pgrx` is used in production despite being pre-1.0, and does not depend on SQL/PGQ's maturity at all — it can be built against any current Postgres version today.
- `pgvector`, already planned for entity/event similarity matching (§15), lives in the same Postgres instance for embeddings either.

**Net effect**: one Postgres instance, ordinary tables today, SQL/PGQ query syntax adopted for free once GA, and a small Rust extension doing the handful of actual graph algorithms nothing else in this stack provides. This keeps the system fast and simple rather than introducing a separate graph database (Neo4j-style) purely for a feature Postgres will soon cover natively for querying, and can cover today via a custom extension for the algorithmic pieces.

**(v1 build note: this p8k8 codebase already has a working REM query system — LOOKUP/SEARCH/FUZZY/TRAVERSE/SQL — over Postgres with a `graph_edges` JSONB-based traversal mechanism, per the project's own CLAUDE.md. Use the existing ontology/REM infrastructure rather than building new vertex/edge tables from scratch — model events/entities/topics as CoreModel-derived schemas per existing conventions, and use `rem_traverse` for graph walks. No Rust extension, no SQL/PGQ — that's future-scale infra this PR does not need.)**

### 9.2 API

A minimal REST/JSON API is sufficient — no need for GraphQL's flexibility given how narrow the MVP surface is (§13). Core endpoints, deliberately matching the MVP's minimal screen set plus the marketplace actions layered on top:

- `GET /feed` — the main scroll, reputation-ranked.
- `POST /events/{id}/claim` — a writer claims an open triggered story.
- `POST /events/{id}/takes` — submit a markdown take (+ optional image) against a claimed or open event.
- `POST /takes/{id}/comments` — comment on a take.
- `POST /takes/{id}/ratings` — rate a take (peer rating, feeds §6.2 and the social graph in §8.1).
- `GET /experts/{id}` — an expert's profile: track record, insight vs. accuracy marks (§6.4), topics they're strong in (derived from §8.2).
- `POST /experts/{id}/subscribe` — a reader subscribes to a specific expert.
- `POST /experts/{id}/rate` — a reader rates an expert directly (distinct from rating a take — feeds overall standing).
- `GET /topics/{id}` — a topic page: related events plus top experts on that topic, both derived by walking the event/entity graph (§8.2).

This list is intentionally small. Payments, gig-claim countdown timers, and the AI/human ratio display (§6.3) are additive once the core loop above is proven, per the MVP philosophy in §13.

**(v1 build scope: none of the marketplace endpoints above — claim/takes/comments/ratings/subscribe/experts — are in scope for this PR. Build a read-only equivalent of `GET /feed` and `GET /topics/{id}` — i.e., a way to browse the detected, interpreted events and walk the graph — since that's the "actual interesting feeds and graph views" deliverable. No writer/marketplace actions yet.)**

A second, distinct API surface exists for the enterprise revenue engine (§20.3) — the detection/scoop-classification pipeline and the reputation/verification methodology, exposed as standalone, versioned, independently rate-limited services for other companies to integrate. This is not an extension of the consumer endpoints above; its customers are engineering teams at other companies, not readers or writers, and it should be designed, versioned, and rate-limited separately from the consumer API later.

**(Out of scope for v1.)**

### 9.3 Frontend

No single framework is prescribed — "any modern, nice stack" is an explicit instruction, and the frontend is the least architecturally risky part of this system. Two things worth holding to regardless of framework choice:
- **Mobile-first, not mobile-adapted** — the stated use case ("read on the toilet or on the train") means the primary surface is a phone screen with a spotty connection; whatever stack is chosen should treat that as the default render target, not an afterthought.
- **Minimal and fast over feature-rich** — matching the MVP philosophy (§14): a plain markdown-rendering feed, not a heavy app-shell. Server-rendering the feed (for fast first paint on mobile networks) paired with a lightweight client for the interactive bits (claim, rate, subscribe) is a reasonable default shape, whichever specific framework implements it.

**(Out of scope for v1 — this PR is backend/data pipeline only. No frontend.)**

## 10. Content Sourcing & Distribution (framing — see §15 for the verified, tested plan)

- **Sourcing**: "interesting" events are most realistically found via anomaly/correlation detection over existing streams — not simple aggregation. The differentiated signal is "these unrelated things just started moving together," not "here is an article."
- **Distribution**: building a standalone destination app is the highest-risk path (attention competition). More realistic wedges: an overlay/annotation layer on top of existing feeds, or a syndicated digest (newsletter/Slack) surfacing top reputation-weighted takes per category into channels people already use. The reputation engine is the product; the front-end can live wherever attention already exists.
- **Why people use it**: for readers, the pitch is a filter against AI-generated slop — legible human insight with a visible track record (precedent: people already pay for Stratechery/Matt Levine specifically for human insight, not facts). For contributors, a public, category- and horizon-scoped track record is portable career capital — a stronger signal than follower count, and a plausible discovery/hiring signal.

## 11. Time Scale — Open Implementation Question (deliberately unresolved)

The core value prop (§1) requires every take to carry some notion of the horizon it's judged against. Two candidate mechanisms exist, and **neither is selected** — this should be resolved empirically once there's usage data, not decided upfront:
- **Event-level horizon**: AI assigns a horizon to the event itself (e.g., "fast-moving story" vs. "slow-burn structural") and all takes on that event inherit the same clock.
- **Take-level horizon**: each contributor tags their own horizon per submission, so two people can comment on the same event at different timescales.

Implementation approach: ship a version where both are possible (AI proposes a default horizon per event, contributor can override per take), instrument which path people actually use and which produces better-calibrated track records, and let that data — not a priori reasoning — decide whether to converge on one mechanism later.

**(Out of scope for v1 — no takes, no contributors yet in this PR's scope.)**

## 12. Stickiness / Marketplace Dynamics

**(This entire section — §12.1, §12.2, §12.3 — describes the writer-gig and reader-subscription marketplace dynamics. Out of scope for v1; included here only for context on where the detection core fits into the larger product.)**

Stickiness is not a passive engagement mechanic (streaks, notifications-for-their-own-sake) — it comes from the platform functioning as a genuine two-sided, real-time marketplace: writers claiming/accepting gigs, and readers subscribing to and rating experts (and, eventually, paying to access insight in the moment). This is closer to Uber's transactional dynamics than to a content-feed engagement loop.

## 13. MVP App Design — Minimal Surface Area

**(Out of scope for v1 — this is the marketplace app UI. This PR is the data/detection pipeline underneath it.)**

Design model: Stack Overflow — plain, content-first, no unnecessary UI decoration. The goal is the fewest possible screens/options for v1, not a feature-complete app.

- **Content format**: simple markdown articles with images. Comment-style — short, opinionated write-ups, not long-form journalism. Low friction to write is the priority; a rich WYSIWYG editor is unnecessary — plain markdown input with image embed is sufficient.
- **Auth**: email + one-time code. No passwords, no social login, no profile setup wizard.
- **Core screens/actions, and nothing beyond this for v1**:
  1. **Scroll** — the main feed of triggered events and human takes.
  2. **Write** — respond to an open AI-triggered event with a markdown take (+ optional image).
  3. **Comment** — respond to someone else's take.
- Everything else in this spec (reputation display, the AI/human ratio bar, horizon tagging, subscriptions, payments) should be treated as **progressive additions layered onto this minimal core**, not part of the initial screen set. The MVP should be usable and coherent even before any of those systems are visible in the UI — reputation and weighting can run in the background and surface later once there's enough real usage to justify the added complexity.
- **Note for build sequencing**: Stripe (and Stripe Connect for writer payouts, §18.1) is not needed for the minimal core above, but is a real, non-optional dependency once any subscription or payout flow (§20.1) goes live — flag it early rather than discovering it late in the build.

## 14. Core Technical Risk Areas (at scale)

Three distinct engineering problems, each with its own failure mode:

1. **The scraper (sourcing at scale)** — foundational; weak input signal undermines everything downstream. Challenges: rate limits/ToS across many sources, deduplication of the same event surfacing with different phrasing across feeds, and entity resolution (recognizing "Acme Corp," "$ACME," "Acme" as the same node so connections can be drawn — this is the entity-resolution step feeding the event/entity graph in §8.2). Closer to a data-engineering problem than an AI problem.
2. **The algorithm** — two sub-problems:
   - *Detection*: separating genuine correlation from noise; false positives will erode contributor trust quickly if the job board fills with uninteresting flagged events.
   - *Reputation/trust math*: the recursive-trust mechanic (§6) is well-understood in principle (PageRank, Stack Overflow) but tuning — decay rates, how fast `w_human` climbs, gaming resistance — is where it breaks under real adversarial use. Should be simulated/backtested before launch, not just shipped and observed live.
3. **Stickiness (the app/marketplace layer)** — the least proven of the three and the most disconnected from the other two: the scraper and algorithm can be excellent and still produce a marketplace nobody transacts in daily. Addressed structurally in §12 (gig-claim loop + subscription/rating loop) rather than via passive engagement mechanics.

**(v1 build focus: risk area #1, the scraper, plus the entity-resolution/corroboration half of risk area #2 — detection. Not the reputation/trust math half of #2, and not #3 at all.)**

## 15. Verified Sourcing Strategy

The mainstream-media-scraping approach (RSS from TechCrunch, Reuters, etc.) hit real blocking (403s) and real dedup failure (two independently-reported real stories on the same event scored only 67/100 similarity even on the most generous fuzzy-matching metric — string-similarity dedup is not sufficient; genuine event linking needs semantic/LLM-based or embedding-based matching, not lexical matching, feeding into the entity-resolution step of §8.2).

The better path is **structured, open, non-obvious sources that were never designed to block programmatic access**. Every source below was tested live (not assumed) against the real endpoint.

### 15.1 First Pass

| Source | Status (tested live) | Recency confirmed | Coverage | Auth needed |
|---|---|---|---|---|
| SEC EDGAR full-text search | ✅ Works | Filings from the current week confirmed | Corporate/finance events, US public companies | None — just a User-Agent header |
| arXiv API | ✅ Works (must use `https`, not `http`) | Papers from 2 days prior confirmed | Research/tech leading indicators | None |
| HN Firebase API | ✅ Works well | Story ~2 hours old confirmed | Dev/tech/startup subculture | None |
| Federal Register API | ✅ Works | Entries for the current date confirmed | US regulatory/policy events | None |
| openFDA (recalls) | ✅ Works | Entries within the past ~2 weeks confirmed | Consumer/food/drug safety events | None |
| Wikipedia RecentChanges API | ✅ Works | Edits seconds old confirmed | Extremely broad, noisy — general world-event proxy via edit velocity | None |
| Lobste.rs RSS | ✅ Works | Current front page confirmed | Smaller, higher-signal dev/infra subculture than HN | None |
| PyPI recent-updates RSS | ✅ Works | Real-time package releases confirmed | Software/dev ecosystem signal | None |
| GH Archive (bulk hourly dumps) | ✅ Works | ~1 hour latency | All public GitHub activity | None |

| Source | Status (tested live) | Recency confirmed | Coverage | Auth needed |
|---|---|---|---|---|
| GitHub Events API (live) | ⚠️ Rate-limited to 60 req/hr unauthenticated — exhausted almost immediately | N/A | Same as above, live instead of hourly | Needs a token to be usable at all |
| UK Companies House API | ⚠️ Requires free API key | Untested | UK corporate filings | Free key required |
| GDELT DOC API | ❌ 503 upstream on every retry (3 attempts) | Untestable at time of writing | Would be very broad world-news coverage if reliable | None, but reliability itself is the risk — supplementary, not critical-path |
| Reddit (RSS route) | ❌ 403, blocked outright | N/A | Subculture/forum signal | Needs the real OAuth Reddit API, not RSS scraping |
| Bluesky (public search API) | ❌ 403 | N/A | Social velocity signal | Needs a different endpoint or app-level auth |
| Mastodon public timeline | ❌ 422 (public timelines opt-in per-instance, disabled on the instance tested) | N/A | Social velocity signal | Needs an instance with public timelines enabled, or authenticated access |
| USPTO PatentsView API | ⚠️ Connection failed (likely query-encoding, not the API itself) | Untested | Patent filings, early tech-direction signal | Needs retest with corrected formatting |

**Domain-skew warning**: GitHub, HN, Lobste.rs, arXiv, and PyPI are all developer/tech-insider sources — they answer "what's happening in software" well and "what's happening in the world" barely at all. GitHub specifically should be scoped as a *tech-category-only* input, not part of the general "world events" backbone; it was over-represented in the original pass because it had a clean free API, not because it's a considered source of general-interest events.

### 15.2 Bootstrap Budget Allocation ($200/month)

| Source | Official commercial cost | Cheaper real route found | Verdict at $200/mo |
|---|---|---|---|
| Reddit | Official commercial tier: ~$12,000/month minimum. No mid-tier exists. | Third-party APIs (flat ~$19/month, or pay-per-call ~$0.002/read) deliver equivalent monitoring at a fraction of the cost | **Use a third-party provider, not Reddit direct.** Budget ~$20–40/month. |
| X/Twitter | Official pay-per-use: ~$0.005/read; legacy ~$200/mo Basic tier closed to new signups | Third-party read-only APIs run ~$0.05–0.15 per 1,000 reads — 30–100x cheaper | **Use a third-party provider for reads.** Budget ~$50–80/month. |
| Crunchbase | Entry API tier ~$49–99/month | No meaningfully cheaper equivalent for verified, structured funding data | **Defer** until budget scales past ~$500/month; SEC EDGAR is a partial substitute meanwhile. |
| GDELT | Free | — | Free, but unreliable in testing — supplementary only. |
| Companies House (UK) | Free (key required) | — | Free — register for the key. |

**Recommended $200/month bootstrap split**: ~$60/month third-party Reddit, ~$80/month third-party X/Twitter, ~$60/month buffer held back rather than pre-committed.

**How this changes things**: the free-tier backbone already covers corporate/finance, research, regulatory, and dev-subculture events at zero cost — the entire $200/month budget goes toward the one gap free sources can't fill: social/subculture velocity signal. Crunchbase-quality funding data stays out of reach at this budget — a real, named gap, not a rounding error. Next meaningful tier is ~$500–1,000/month (Crunchbase entry + GitHub token become affordable); the ~$12,000+/month tier is only relevant if Reddit's official commercial access becomes necessary for compliance/reliability reasons.

### 15.3 Second Pass — Widening Further (global macro, agriculture, climate, entertainment/gaming culture)

| Source | Status (tested live) | Domain covered |
|---|---|---|
| World Bank API | ✅ Works, zero auth | Global (not US-centric) macro/economic indicators across ~200 countries |
| Steam Charts API | ✅ Works, zero auth | Gaming — a genuine consumer-entertainment subculture distinct from developer culture |
| US Census Bureau API | ⚠️ Needs a free key | US retail/demographic/economic detail beneath BLS's headline numbers |
| FRED | ⚠️ Needs a free key | US macro/financial-markets data |
| NOAA Climate Data API | ⚠️ Needs a free token | Climate/extreme weather — leading indicator for agriculture, energy, insurance, supply-chain stories |
| USDA NASS Quick Stats | ⚠️ Needs a free key | Agriculture — zero coverage anywhere else in the plan |

World Bank fixes a real gap: every source verified before it was US-centric, quietly making "world events" mean "American events." NOAA and USDA close out agriculture and climate entirely.

### 15.4 Third Pass — Maximizing Free Coverage

| Source | Status (tested live) | Domain covered |
|---|---|---|
| Frankfurter (forex rates) | ✅ Works, zero auth | Currency markets |
| CoinGecko | ✅ Works, zero auth | Crypto markets — a distinct online subculture from dev-tech and mainstream finance |
| NWS live weather alerts | ✅ Works, zero auth | Real-time disaster/extreme-weather events (25 live alerts confirmed at test time) |
| FEC (campaign finance) | ✅ Works (shared `DEMO_KEY`, request a free real key for production) | Political/campaign-finance events |
| FAA NAS Status | ✅ Works, zero auth | Real-time transportation/logistics disruption |

Worth a retry rather than abandoning: FBI Crime Data API (likely a path issue), GlobeNewswire RSS (503 at test time, plausibly transient), USPTO PatentsView (query-encoding issue).

### 15.5 Budget Tiers — Where Money Actually Buys Coverage

Combining all passes, paid access should be reserved for the two categories with no free alternative: real-time social/subculture chatter (Reddit, X) and licensed business data (Crunchbase-grade funding info). Everything else is covered free once ~5 registration keys (Census, FRED, NOAA, USDA, EIA) are obtained.

- **$0/month**: the entire free backbone above — broad, not tech-myopic, multi-domain, largely real-time.
- **$200/month (bootstrap)**: per §15.2 — Reddit + X third-party read access.
- **$1,000/month**: priority order — (1) scale up Reddit/X volume, (2) Crunchbase entry tier, (3) a paid GitHub Events token / higher-volume GH Archive processing for the tech-category layer specifically, (4) buffer.
- **Beyond $1,000/month**: a genuine mainstream-news wire API becomes the best next spend, since it was never free-solvable, unlike everything above.

**Core finding across all passes**: this sourcing strategy scales almost entirely on free, registration-only government/public-data APIs, not paid data vendors. The paid budget's job is narrow — subculture social signal — not "more sources" in general.

**(v1 build scope: pick a small, high-value subset of the zero-auth, ✅-verified free sources above for a first working pipeline — do not attempt all of them, and do not build any paid/third-party Reddit or X integration in this PR. A sensible v1 starting set, chosen for domain diversity and zero auth friction: SEC EDGAR full-text search, arXiv, HN Firebase API, Federal Register API, Wikipedia RecentChanges API. This covers corporate/finance, research, dev/tech, US regulatory, and general-world-event-proxy domains without needing any API keys at all. Add more sources incrementally after this is proven, per this section's own "low hanging fruit first" logic.)**

## 16. The Detection Core — Master Sources, Entity Resolution, Corroboration, and the Graph→Threshold→LLM Pipeline

This is the actual algorithmic core the rest of the spec depends on: a master reference of primary sources, the two processes (entity resolution, corroboration scoring) that turn raw multi-source noise into a trustworthy graph, and the pipeline that decides which graph nodes are worth an LLM's attention. Restated simply: **algorithmic processes build and score the graph; only nodes that clear a threshold get passed to an LLM to interpret and write up.** The algorithm does the cheap, high-volume filtering; the LLM does the expensive, low-volume synthesis — matching the cost-optimized routing logic already established in §18.3.

### 16.1 Master Source Reference Table

Consolidating every source verified live across §15.1–§15.5 into one reference, organized by domain rather than by which pass discovered it:

| Domain | Sources | Auth |
|---|---|---|
| Corporate/finance (regulatory) | SEC EDGAR, Federal Register, UK Companies House | Free (Companies House needs a key) |
| Corporate/finance (market) | World Bank, FRED, Frankfurter (forex), CoinGecko (crypto) | Free (FRED needs a key) |
| Finance/markets (social) | StockTwits (public API, zero auth, confirmed live) | Free |
| Health/pharma | ClinicalTrials.gov, openFDA | Free |
| Legal/litigation | CourtListener | Free (needs an account) |
| Macro/labor economy | BLS, US Census Bureau (EIA — energy) | Free (Census, EIA need keys) |
| Agriculture/climate | USDA NASS Quick Stats, NOAA Climate Data, NWS live alerts | Free (USDA, NOAA need keys; NWS is fully open) |
| Transportation/logistics | FAA NAS Status | Free |
| Political/campaign finance | FEC | Free (shared demo key; request a real one for production) |
| Dev/tech subculture (tech-category-only, per §15.1's domain-skew warning) | HN, Lobste.rs, PyPI, GH Archive, arXiv, GitHub Events (needs a token) | Free |
| General public attention (cross-domain) | Wikipedia Pageviews, Wikipedia RecentChanges | Free |
| Entity crosswalk/resolution | Wikidata (see §16.2) | Free |
| Consumer subculture | Steam Charts (gaming) | Free |
| Curated social — crypto, geopolitics/conflict OSINT, cyber threat intel | Telegram public channel previews (`t.me/s/<channel>`, confirmed live, zero auth) | Free |
| Curated social — tech/research/journalism/policy commentary | Bluesky `getAuthorFeed` for specific known handles (confirmed live, zero auth — note: broad search is blocked, specific-handle following is not) | Free |
| Curated social — breaking news, tech/VC, politics, sports commentary | X/Twitter, third-party read API only — narrower scope than originally budgeted | Paid |
| Supplementary, unreliable | GDELT (commercial-use-clear per its terms, but 503s on every live test — treat as supplementary only) | Free |
| Confirmed dead — do not pursue | Nitter (offline per its own metadata) and XCancel (received a cease-and-desist from X Corp and shut down, confirmed live) — the free-scraping-workaround category for X is being actively and successfully shut down, not a stable option | N/A |
| Paid, no free tier exists | Reddit (third-party), Crunchbase (deferred) | Paid, per §15.2 budget |

**Telegram ToS note** — stay in the "Apple News space," not the "training scraper" space: Telegram's official API terms explicitly prohibit using its data to "train, fine-tune, or otherwise develop" AI/ML models — the same category of clause the New York Times uses to block AI-training scrapers. This targets a specific activity (building or improving a model on the platform's content), not inference-time use of an already-trained model to organize and summarize content for delivery — the same thing Apple News and Artifact already do openly, uncontroversially, at scale. Percolate's actual activity (detection/clustering/summarization via Claude/Gemini/Kimi at inference time, never training or fine-tuning on Telegram content) sits in the Apple News category, and the platform's own terms and any future legal review should say so explicitly, rather than leave the distinction implicit. This positioning should hold as a standing constraint, not a one-time argument: if Percolate ever moves toward fine-tuning a custom model on ingested source content, this reasoning stops applying and the question needs revisiting.

**Revised X/Twitter framing**: the original $50-80/month budget line (§15.2) was sized for *broad* keyword/velocity monitoring. Per the platform-to-category mapping above, X's genuinely distinctive value is narrower — specific trusted journalists, VCs, and analysts, not broad search — which is a curated-following problem, the same shape as the Bluesky/Telegram approach. Curated following of a small handle list via a cheap third-party pay-per-read API is realistically a few dollars a month, not $50-80, with StockTwits closing the finance-specific slice for free, the residual paid need for X is smaller than originally scoped.

**(v1 build scope: only the free, zero-auth §15.1 first-pass sources listed in the v1 starting set above — SEC EDGAR, arXiv, HN Firebase, Federal Register, Wikipedia RecentChanges. No Telegram, Bluesky, StockTwits, or paid sources in this PR.)**

### 16.2 Entity Resolution — A Tiered, Cost-Aware Process

The core problem, demonstrated live earlier (§15.1): the same real-world entity ("OpenAI," "Open AI," a specific SEC CIK number, a stock ticker) appears differently across sources, and lexical/fuzzy matching alone fails on real examples (the Cursor/SpaceX case scored only 67/100 similarity despite being the same story). Entity resolution should be a **cascading, cost-ordered process**, escalating only when a cheaper tier fails — matching the cost-optimized LLM routing philosophy in §18.3:

1. **Canonical registry match (cheapest, first)**: maintain a canonical entity table (part of the event/entity graph, §8.2) keyed by external IDs already present in the sources themselves — SEC CIK numbers, stock tickers, Wikidata QIDs. Exact or alias-table match against this registry resolves the large majority of cases for free, no model call needed.
2. **Wikidata crosswalk (newly identified, free, zero auth)**: Wikidata's API, verified live, returns disambiguated entities with **over 100 structured properties per entity** — including external-ID crosswalks that can link a company's legal name, aliases, and identifiers across systems. This is a genuinely useful, previously unlisted resource specifically for entity resolution, distinct from its earlier appearance (§14) as a general-attention signal — same data source, different job.
3. **Fuzzy/embedding-based matching (fallback)**: for entities not yet in the canonical registry, semantic embedding similarity (not lexical string-matching, which already failed the live test) to propose a probable match, flagged for confirmation rather than auto-accepted.
4. **LLM resolution (most expensive, last resort)**: only for genuinely ambiguous cases the first three tiers can't resolve — routed to the cheapest capable model per §18.3's routing table, since this should be a small minority of cases if the registry and Wikidata tiers are doing their job.

**(v1 build scope: implement tiers 1–3 — canonical registry match, Wikidata crosswalk, embedding fallback via pgvector, already a dependency in this codebase. Tier 4 LLM resolution is fine to include too since the LLM interpretation step is already in scope for this PR — but it should genuinely be the last resort, not the default path.)**

### 16.3 Corroboration Scoring — Independence, Not Volume

The tradecraft principle (§19): corroboration only counts when sources are **structurally independent**, not merely numerous. Two outlets both quoting the same press release is one source, not two — this is precisely why the retail-tariff pattern (§3.1) failed the scoop test despite "42 sources" superficially looking like strong corroboration.

Concrete scoring approach:
- Every source in the master table (§16.1) is tagged with a **source-type category** (regulatory filing, trial registry, patent, independent-source press release, independent journalism, social chatter).
- A candidate event's corroboration score counts **distinct source-type categories** confirming the same entity-cluster within a time window, not raw mention count. Three news outlets citing one filing scores as weakly as a single source; one filing plus one independent trial-registry update plus one independent court filing scores as genuinely strong, because they could not plausibly all derive from the same original document.
- This score is a direct input to the graph edges in §16.4, and to the pre-flight scoop-classification check in §3.1 — a low corroboration score is itself a reason to classify an event as unproven, independent of whether a news search finds prior coverage.

### 16.4 The Graph → Threshold → LLM Pipeline

Restating the core loop (§2) with this machinery made explicit:

1. **Raw ingestion** produces noisy, duplicate-heavy candidate events from the master sources (§16.1) — this stage is expected to be messy ("AI builds crap" in the sense of high recall, low precision).
2. **Entity resolution (§16.2) and corroboration scoring (§16.3)** run algorithmically — no LLM call yet — to cluster raw items into candidate event nodes on the event/entity graph (§8.2), each carrying a corroboration score and resolved entity links.
3. **An algorithmic threshold** (combining corroboration score, source reliability — e.g., GDELT's demonstrated unreliability should carry a lower weight per §15.1 — and entity-resolution confidence) filters the graph down to the event nodes actually worth attention. This is the expensive-work gate: only nodes clearing it proceed.
4. **Only thresholded nodes go to an LLM interpreter**, which performs the scoop/saturation classification (§3.1, algorithmic-signal version per this build's v1 scope note), zeitgeist framing (§3.2, optional for v1), and ultimately writes the report/digest content — the expensive, low-volume step, deliberately kept small by the algorithmic pre-filter in step 3.

This structure is also what makes the cost model in §18.3 realistic: the LLM never sees the full raw volume, only the pre-filtered, algorithmically-scored subset — the entity resolution and corroboration scoring in §16.2/§16.3 are what keep LLM spend proportional to genuinely interesting events, not raw ingestion volume.

**(This is the v1 core deliverable: raw ingestion from the chosen v1 sources → entity resolution → corroboration scoring → threshold → LLM interpretation/write-up, stored as scored, linked nodes on the event/entity graph, and queryable as a feed + graph views.)**

### 16.5 Two More Tradecraft Techniques as Scored Fields, and the Trust Flow

Corroboration scoring (§16.3) is one of three concrete tradecraft techniques worth stating explicitly as fields the pipeline actually computes, not just principles:
- **Primary-source-first tracing**: every event carries a link to the actual primary document (the filing, the trial record, the patent), not a paraphrase of one — already the instinct behind pulling from EDGAR/ClinicalTrials.gov/CourtListener directly rather than news coverage of them (§15). Made explicit as a field, this is both a real differentiator against AlphaSense/CB Insights (§21) and a direct legal-risk mitigant (§14) — a claim traceable to a primary document is defensible in a way a paraphrase isn't.
- **Absence as a signal**: several master-table sources (§16.1) have predictable per-entity cadences — a company's recurring filing schedule, a trial's expected update rhythm. A baseline per entity lets the pipeline flag when an entity breaks its *own* normal pattern (a missing filing, a stalled update) — a subtler, harder-to-replicate signal than presence-detection, and one the competitive scan (§21) didn't find any existing player doing.

**The trust flow this is really building toward**: a primary source is reviewed by a writer, whose review links back to that primary source (via the tracing field above); a reader's trust in that review is earned specifically by the writer's track record at *primary-source review itself*, not general commentary quality — which is exactly what §6's reputation score should be measuring once tradecraft (§19) is codified into it. That trust, once established, is what lets a reader **commission or recommend a specific primary-source review from a specific trusted analyst** (§12.2's request-a-take mechanic) rather than just consuming whatever's in the feed. Deslopification, in this framing, isn't a slogan — it's the literal shape of the pipeline: primary source → traceable expert review → reader trust → commissioned further review, at every step traceable back to a source rather than a paraphrase of one.

**(v1 build scope: implement primary-source-first tracing as a required field on every event node — this is straightforward and high-value given the chosen v1 sources are all primary-source APIs already. "Absence as a signal" is a nice-to-have, not required for v1.)**

### 16.6 Curating a Who's Who, and Scoring Uniqueness (Not Just Reliability)

**(Out of scope for v1 — this section is about curating social/handle-based sources, which are themselves out of scope per §16.1's v1 note.)**

## 17. Phase 0 — Simulation-Driven Development (ongoing track, not a solved deliverable)

**(Out of scope for v1 — this is the reputation-graph ABM, which depends on §8.1/§6, both out of scope for this PR.)**

Before the reputation algorithm (§6) is treated as production-ready, or used in any "cannot be gamed" marketing claim, it is stress-tested in an agent-based model (ABM) of the social/reputation graph (§8.1). This is an ongoing process initiated alongside product build, not a one-time task to close out.

## 18. Infrastructure & LLM Processing Costs

### 18.1 Infra Cost Tiers (database, workers, queue, storage)

**(Payments/Stripe row is explicitly out of scope for v1 — no payment flow exists yet.)**

### 18.2 Model Options — Cost Comparison (per million tokens, USD, direct provider pricing)

No privacy/data-handling constraint applies (the platform reprocesses public data only), so routing is decided purely on cost and fitness for each job.

| Model | Input | Output | Context | Fit |
|---|---|---|---|---|
| Claude Haiku 4.5 | $1.00 | $5.00 | 200K | Baseline cheap-tier reference |
| Claude Sonnet 5 | $2.00 (→$3 Sept '26) | $10.00 (→$15) | 1M | Baseline mid-tier reference |
| Claude Opus 5 | $5.00 | $25.00 | 1M | Highest-quality option, reserved for cases where output quality directly matters |
| Gemini 3.1 Flash-Lite | $0.25 | $1.50 | 1M | Cheapest viable option for high-volume, low-complexity jobs |
| Gemini 3.5 Flash | $1.50 | $9.00 | 1M | Mid-tier, generally not cost-competitive with Kimi K2.6 here |
| Gemini 3.1 Pro | $2.00 | $12.00 | 200K (doubles above) | Frontier Gemini tier |
| Kimi K2.5 | $0.60 | $3.00 | 262K | Open-weight, cheap |
| Kimi K2.6 | $0.95 | $4.00 | 262K | Best cost/capability tradeoff for mid-complexity synthesis; cached input ~$0.16 (~83% off) |
| Kimi K3 | $3.00 | $15.00 | 1M | Frontier Kimi tier, priced near Claude Sonnet/Opus |

### 18.3 Cost-Optimized Routing & Recalculated LLM Spend

Routing purely on cost per job:
- Entity tagging (high volume, low complexity) → Gemini 3.1 Flash-Lite
- Event clustering/relationship detection (feeds the event/entity graph, §8.2) → Kimi K2.6
- Writer-facing pattern/idea digest (the free aggregation layer, §1) → Kimi K2.6; revisit if output quality on this specific surface proves inadequate, as it's the one job the end user directly sees
- AI-bootstrap quality rating (high volume, low complexity) → Gemini 3.1 Flash-Lite

Recalculated bootstrap-tier LLM cost (~200 events/day, ~50 submissions/day, 1 category, batch discount applied to all non-realtime jobs):

| Job | Volume | Model | Est. cost/month |
|---|---|---|---|
| Entity tagging | 200 items/day | Gemini 3.1 Flash-Lite | ~$1.65 |
| Event clustering | 4 runs/day | Kimi K2.6 (batched) | ~$8.50 |
| Writer digest | 1 run/day, 1 category | Kimi K2.6 (batched) | ~$4.20 |
| Quality rating | 50 submissions/day | Gemini 3.1 Flash-Lite | ~$0.75 |
| **Total** | | | **~$15/month** |

Roughly 3.5x cheaper than routing entirely through Claude models (~$54/month). At Growth tier (multiple categories, ~10x volume): roughly $70-115/month vs. ~$250-400/month single-provider.

**(v1 build note: this codebase's existing p8/agentic/adapter and pydantic-ai routing already supports model selection per agent/schema — use that rather than building new routing logic. The entity-tagging and event-clustering jobs above map naturally onto this PR's entity-resolution and detection-pipeline steps; pick reasonably-costed models available via the project's existing provider config rather than necessarily the exact models named here, which may not all be configured in this deployment.)**

**(§19 Tradecraft Systemization research plan, §20 Business Model, §21 Competitive Landscape are context/reference only — not build scope for this or any near-term PR. Summarized: §19 proposes studying Bellingcat/First Draft verification methodology and eventually interviewing high-reputation writers to codify tradecraft into a checklist; §20 is the pricing/legal/revenue model for the marketplace and API licensing; §21 is competitive analysis vs. AlphaSense, CB Insights, Exploding Topics, and Estimize.)**

## 22. Development Environment & Deployment Workflow

**(Reference only — this p8k8 repo already has its own environment, deployment (Hetzner K8s, per this repo's CLAUDE.md), and the GitHub Action + auto-deploy pipeline set up in this same session. Do not stand up the Hetzner/Orca/tmux environment described in the original spec — build within this existing p8k8 codebase and its existing deploy pipeline instead.)**

## 23. Open Questions / Deferred to Later Iterations

(Preserved verbatim from the original spec for reference — none of these block the v1 scope defined above, since v1 deliberately avoids the reputation/marketplace/business-model machinery most of these questions concern.)

- Exact decay function for cross-category reputation spillover (v2).
- How ties are broken when an event's "resolution" is ambiguous or never cleanly resolves.
- Precise formula for how much accumulated peer-validated reputation is required to shift the human/AI ratio by a given increment.
- Anti-gaming mechanics beyond volume-resistance — see §17 for progress and open tradeoffs, and §9.1 for where graph-based collusion detection will actually run.
- UI/UX for the transparent ratio indicator and the insight-vs-track-record distinction.
- Final choice of horizon-assignment mechanism (§11), once usage data exists.
- Final distribution wedge (§10) — standalone app vs. overlay vs. syndicated digest.
- Final pricing/payout model — preliminary model now drafted (§20.1): writer monetization via Direct Expert Subscription + Percolate All-Access, with Percolate taking only a minimal processing-cost cut on both — this is not Percolate's revenue engine (§20). Still open: the exact minimal cut, and validating the reputation-weighted payout formula against real usage.
- Whether fast-horizon (revenue) and long-horizon (trust) takes need materially different UX treatments.
- At what point (usage/scale threshold) reputation display, ratio bar, horizon tagging, and gig-claim flow, and payments graduate from background systems into surfaced UI (§13).
- ABM parameter sweep and the graph-based collusion detection Rust extension (§17, §9.1) — ongoing.
- Specific choice of frontend framework (§9.3) — deliberately left open.
- Exact schema for the vertex/edge tables underlying both graphs (§8) — to be finalized when implementation starts, informed by which SQL/PGQ features are stable by GA.
- How the zeitgeist knowledge base (§3.2) itself gets maintained and kept current — LLM synthesis pass, hand-curation, or a blend — deferred pending operational experience.
- Precisely how the pre-flight scoop/saturation check (§3.1) integrates into the cost model (§18) — it adds a search step per candidate event, which should be budgeted once real event volume is known.
- Whether Bellingcat/First Draft-style tradecraft (§19) transfers meaningfully to fast-reactive commentary, or needs substantially different scaffolding — an empirical question for the retrospective-interview research plan, not a design decision to make now.
- Exact minimal processing-cost cut for writer subscriptions (§20.1) — deferred pending real conversion/payout data.
- Ideal customer profile for the API/process-licensing product (§20.3/§20.4) — which companies would integrate rather than build their own, and at what price point — unanswered and the most urgent open item blocking near-term revenue.
- Confirmation of Percolate's legal entity structure (§20.2) with actual counsel, not this spec.
- Direct competitive validation against Estimize specifically (§21.2) — reaching out to understand their actual retention/accuracy mechanics in more depth than public sources show, given how close a precedent it is.
- Whether Exploding Topics' "community of trendspotters" has any non-public reputation mechanism worth studying before assuming it's purely decorative (§21.3) — the public-facing material doesn't show one, but that's not conclusive.
- Exact timing for Stripe/Stripe Connect integration (§18.1, §13) — not needed for the MVP core, but should be scheduled deliberately once subscriptions or payouts are actually being built, not left until the last minute.
- The exact algorithmic threshold formula combining corroboration score, source reliability, and entity-resolution confidence (§16.4) — needs real event volume to calibrate, same discipline as every other threshold in this spec.
- Whether Wikidata's crosswalk coverage (§16.2) is comprehensive enough for smaller/private entities, or only reliably populated for large, well-known ones — untested at the individual-entity level so far, only confirmed working in principle.
- Whether the uniqueness/delta scoring approach (§16.6) produces a meaningfully different curation priority than reliability alone once real data exists — untested, and worth validating before treating it as load-bearing.
- Whether the anniversary/memorial trigger type (§5.1) is worth a dedicated scheduled-lookahead component in the pipeline (§16.4), since it's the one trigger type that's predictable in advance rather than reactive — not yet built, just identified.
- Whether the "Apple News, not training scraper" legal positioning (§16.1) needs to be formalized in Percolate's own published terms explicitly, or is sufficient as an internal legal-review talking point — pending the real solicitor conversation already flagged (§20.2).
