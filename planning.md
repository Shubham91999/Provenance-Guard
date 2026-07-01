# Provenance Guard — Planning & System Design

---

## 1. Problem & Design Philosophy

Creative-sharing platforms need to give readers honest context about whether a
piece of text was written by a human or generated with AI — **without** falsely
accusing real creators. Perfect AI detection is an unsolved problem, so the goal
is not a binary verdict but an **honest, uncertainty-aware estimate** paired with
a **fair appeals path**.

**Two principles drive every design decision below:**

1. **False positives are the worst outcome.** Telling a human "your work looks
   AI-generated" is far more damaging on a writing platform than missing an AI
   text. The system is therefore deliberately *cautious about calling something
   AI*: the AI threshold is higher than the human threshold, disagreeing signals
   collapse to "uncertain," and every label is worded as an estimate, not an
   accusation.
2. **Confidence is a UX decision before it is a math problem.** We first decided
   what a score should *mean to a reader* (0.5 = "we genuinely cannot tell"),
   then built the scoring to produce that meaning — not the other way around.

---

## 2. Architecture Narrative (the path a submission takes)

A single piece of text travels through the system like this:

1. A platform sends `POST /submit` with `{ text, creator_id }`.
2. **Rate limiter** (Flask-Limiter) checks the caller hasn't exceeded the limit.
   If they have → `429` and the request stops here.
3. The **submission handler** generates a unique `content_id` (UUID).
4. The text is passed to the **detection pipeline**, which runs **two independent
   signals**:
   - **Signal 1 — Groq LLM classifier** (semantic view): returns an
     AI-likelihood in `[0,1]`.
   - **Signal 2 — Stylometric heuristics** (structural view): returns an
     AI-likelihood in `[0,1]`.
5. The **confidence scorer** combines the two signal scores into one
   `confidence` value in `[0,1]` (probability the text is AI-generated), then
   maps it to an `attribution` category using asymmetric thresholds.
6. The **label generator** turns `(attribution, confidence)` into the
   plain-language **transparency label** a reader would see.
7. The **audit log** (SQLite) records a structured classification event:
   timestamp, `content_id`, `creator_id`, attribution, confidence, both
   individual signal scores, and status.
8. The handler returns a JSON response: `content_id`, `attribution`,
   `confidence`, `signals`, and `label`.

**Appeal flow:** A creator who disputes a result sends `POST /appeal` with
`{ content_id, creator_reasoning }`. The system looks up the content, flips its
status to `under_review`, records the creator's reasoning **alongside the original
decision** in the audit log, and returns a confirmation. No automated
re-classification is performed — a human reviewer works the queue.

---

## 3. Detection Signals

The pipeline uses **two genuinely distinct signals** — one *semantic*, one
*structural*. They fail in different ways, so together they are more informative
than either alone.

### Signal 1 — Groq LLM classifier (semantic / holistic)

- **What it measures:** Whether the text *reads* as AI-generated — tone,
  coherence, "voice," the bland over-polished feel of model output. The model
  (`llama-3.3-70b-versatile`, temperature 0) is prompted to return a JSON object
  with `ai_probability` in `[0,1]` plus a short rationale.
- **Output shape:** a float `llm_score` in `[0,1]` (1 = very AI-like).
- **Why this property differs human vs AI:** LLMs capture semantic and stylistic
  patterns holistically — the "average of the internet" register, hedging, even
  paragraph rhythm — that simple statistics miss.
- **Blind spot:** It is a black box and can be inconsistent run-to-run; it can be
  fooled by lightly edited AI text or by human text that happens to sound
  "generic." It also has no calibrated notion of its own uncertainty.

### Signal 2 — Stylometric heuristics (structural / statistical)

Pure Python, no external libraries. Combines three measurable properties into a
single `stylometric_score` in `[0,1]`:

- **(a) Burstiness — sentence-length coefficient of variation.** Humans write in
  bursts (a long sentence, then a short one); AI text tends toward uniform
  sentence lengths. **Low variation → more AI-like.** This is the strongest of
  the three (weight 0.5).
- **(b) AI-cliché / transition-phrase density.** AI writing over-uses connectors
  and hedges ("furthermore," "moreover," "it is important to note,"
  "in conclusion"). High density → more AI-like (weight 0.3).
- **(c) Average sentence length.** Very uniform, long sentences skew AI; short,
  choppy, casual sentences skew human (weight 0.2).
- **What it measures:** the *shape* of the writing, independent of meaning.
- **Why it differs:** AI decoding optimizes for fluent, low-variance,
  well-connected prose; human writing is lumpier and more idiosyncratic.
- **Blind spot:** Naturally formal or academic human writing (and writing by
  non-native English speakers) is uniform and connector-heavy, so it can be
  **falsely flagged as AI**. This is exactly why stylometry is the *lower-weighted*
  signal and why signal disagreement forces "uncertain" (see §5).

### Why these two together

One is semantic, one is structural — genuinely independent axes. When they
**agree**, confidence is high. When they **disagree**, that disagreement is itself
information: we treat it as uncertainty rather than trusting the more aggressive
signal, which protects human writers from the stylometric blind spot.

---

## 4. Confidence Scoring & Combination

- Each signal outputs an AI-likelihood in `[0,1]`.
- **Combined confidence** = weighted average, LLM weighted higher because it is
  the more reliable signal and stylometry has a known false-positive blind spot:

  ```
  confidence = 0.65 * llm_score + 0.35 * stylometric_score
  ```

- If the LLM signal is unavailable (API error / no key), the system **degrades
  gracefully** to stylometric-only and flags the result as degraded, rather than
  failing the request.

### What the number means (uncertainty representation)

`confidence` is the estimated **probability the text is AI-generated**:

| Value | Meaning to a reader |
|-------|---------------------|
| near **1.0** | strongly looks AI-generated |
| **0.5** | genuine coin-flip — we cannot tell |
| near **0.0** | strongly looks human-written |

*Certainty of the verdict* is therefore the **distance from 0.5**. A 0.51 is
almost no evidence; a 0.95 is strong evidence. The label wording reflects this.

### Attribution thresholds (asymmetric — protects humans)

The AI band is intentionally **narrower/harder to reach** than the human band, so
borderline content falls into "uncertain" rather than being called AI:

| Combined confidence | Attribution | Label variant |
|---------------------|-------------|---------------|
| `>= 0.70` | `likely_ai` | High-confidence AI |
| `0.35 – 0.70` (exclusive of 0.70) | `uncertain` | Uncertain |
| `< 0.35` | `likely_human` | High-confidence human |

### Disagreement rule (extra false-positive protection)

If the two signals disagree by more than **0.40** (`abs(llm - stylo) > 0.40`),
the attribution is forced to **`uncertain`** regardless of the combined score. We
do not issue a confident verdict when a semantic and a structural view of the
same text conflict.

### How we validate the score is meaningful

Run the four labeled test inputs (clearly-AI, clearly-human, formal-human,
lightly-edited-AI) and confirm: (1) clearly-AI scores materially higher than
clearly-human; (2) borderline inputs land in the middle band, not pinned to 0/1;
(3) each of the three label variants is reachable. Both signal scores are printed
separately so we can see which signal drives a surprising result.

---

## 5. Transparency Label Design

Three variants, keyed off `attribution`. Every variant is written as an
**estimate, not an accusation**, names that automated detection can be wrong, and
(for the AI/uncertain cases) points the creator to the appeal path. The estimated
percentage is interpolated into the body so 0.51 and 0.95 read differently within
the same variant.

**High-confidence AI (`likely_ai`):**
> **⚠︎ Likely AI-Generated**
> Our automated analysis suggests this content was most likely created with AI
> assistance (estimated {pct}% likelihood). This is an automated estimate, not a
> certainty — detection tools can be wrong. If you are the creator and believe
> this is inaccurate, you can appeal this assessment.

**High-confidence human (`likely_human`):**
> **✓ Likely Human-Written**
> Our automated analysis found no strong signs of AI generation, so this content
> most likely reflects original human writing (estimated {pct}% likelihood of AI
> generation). This is an automated estimate, not a guarantee of authorship.

**Uncertain (`uncertain`):**
> **◦ Origin Uncertain**
> We could not confidently determine whether this content was written by a human
> or generated with AI (estimated {pct}% likelihood of AI generation). We are
> showing this honestly rather than guessing. Please treat the authorship as
> unverified. If you are the creator, you can add context by appealing.

---

## 6. Appeals Workflow

- **Who can appeal:** the creator of the content (identified via `content_id`;
  in a real deployment this would be auth-gated to the owning `creator_id`).
- **What they provide:** `content_id` and free-text `creator_reasoning`
  (e.g., "I am a non-native speaker; my formal style is not AI").
- **What the system does on receipt:**
  1. Look up the content by `content_id` (404 if unknown).
  2. Update its status to **`under_review`**.
  3. Write an **appeal event** to the audit log, stored alongside the original
     classification (same `content_id`), including the creator's reasoning and a
     timestamp.
  4. Return a confirmation `{ content_id, status: "under_review", message }`.
- **No automated re-classification** — the appeal enters a human review queue.
- **What a reviewer sees in the queue:** for each appealed item — the original
  attribution, combined confidence, both individual signal scores, the original
  text, the creator's reasoning, and timestamps for both the classification and
  the appeal. (Surfaceable via `GET /log`, filtered to `status = under_review`.)

---

## 7. Anticipated Edge Cases (specific failure modes)

1. **Formal / academic human writing or non-native English.** Uniform sentence
   lengths and heavy use of connectors ("furthermore," "the literature suggests")
   make the *stylometric* signal read it as AI. Mitigation: stylometry is
   down-weighted (0.35), the AI threshold is high (0.70), and if the LLM
   disagrees the disagreement rule forces "uncertain." This is the primary
   false-positive risk the design targets.
2. **Short poems / repetitive verse with simple vocabulary.** Deliberate
   repetition and short, even lines crater lexical diversity and burstiness, so
   heuristics may score it AI-like even though it is a genuine human creative
   choice. Mitigation: very short texts carry low statistical reliability, so the
   scorer widens toward "uncertain" for short inputs rather than issuing a
   confident AI verdict.
3. **Very short submissions (a sentence or two).** Neither signal has enough
   material; stylometrics are essentially noise. Mitigation: minimum-length
   handling pushes short inputs toward "uncertain."
4. **Lightly edited AI text.** A human pass over AI output blurs both signals —
   this *should* land mid-range, and the "uncertain" band is designed to catch
   exactly this case honestly rather than guessing.

---

## Architecture

```
                          POST /submit  { text, creator_id }
                                     │
                                     ▼
                         ┌───────────────────────┐
                         │   Rate Limiter         │  429 if over limit
                         │   (Flask-Limiter)      │──────────────► (stop)
                         └───────────┬───────────┘
                                     │ text
                                     ▼
                         ┌───────────────────────┐
                         │  Submission Handler    │  make content_id (UUID)
                         └───────────┬───────────┘
                                     │ raw text
                 ┌───────────────────┴───────────────────┐
                 ▼                                         ▼
     ┌───────────────────────┐               ┌───────────────────────┐
     │ Signal 1: Groq LLM     │               │ Signal 2: Stylometry   │
     │ (semantic)             │               │ (structural)           │
     │  → llm_score [0,1]     │               │  → stylometric_score   │
     └───────────┬───────────┘               └───────────┬───────────┘
                 │  llm_score                             │ stylometric_score
                 └───────────────────┬───────────────────┘
                                     ▼
                         ┌───────────────────────┐
                         │  Confidence Scorer     │  weighted blend +
                         │                        │  asymmetric thresholds +
                         │  → confidence,         │  disagreement rule
                         │    attribution         │
                         └───────────┬───────────┘
                                     │ (attribution, confidence)
                                     ▼
                         ┌───────────────────────┐
                         │  Label Generator       │  → transparency label text
                         └───────────┬───────────┘
                                     │ full result
                        ┌────────────┴────────────┐
                        ▼                          ▼
             ┌───────────────────┐     ┌───────────────────────┐
             │  Audit Log (SQLite)│     │  JSON Response         │
             │  classification evt│     │  content_id,attribution│
             └───────────────────┘     │  confidence,signals,   │
                                        │  label                 │
                                        └───────────────────────┘

        APPEAL FLOW
        POST /appeal { content_id, creator_reasoning }
                 │
                 ▼
        ┌───────────────────┐   status → "under_review"    ┌────────────────┐
        │  Appeal Handler    │ ───────────────────────────► │ Content store  │
        │                    │   appeal event + reasoning   │  (SQLite)      │
        │                    │ ───────────────────────────► │ Audit Log      │
        └─────────┬─────────┘                               └────────────────┘
                  │ { content_id, status: under_review, message }
                  ▼
              JSON Response
```

**Narrative:** In the submission flow, `POST /submit` passes text through the rate
limiter, then to two independent detectors (Groq LLM = semantic, stylometry =
structural); their scores are blended into one confidence value, mapped to an
attribution via asymmetric thresholds, turned into a transparency label, logged,
and returned. In the appeal flow, `POST /appeal` looks up the content by
`content_id`, flips its status to `under_review`, and records the creator's
reasoning in the audit log alongside the original decision for a human reviewer.

---

## AI Tool Plan

For each implementation milestone: which spec sections feed the AI tool, what to
ask it to generate, and how to verify the output.

### M3 — Submission endpoint + Signal 1
- **Spec provided:** §3 (Detection Signals — Signal 1), §2 (Architecture
  narrative), the Architecture diagram, and the API contract.
- **Ask it to generate:** the Flask app skeleton with a `POST /submit` route stub,
  and the Groq LLM signal function returning `llm_score` in `[0,1]`.
- **How to verify:** call the signal function directly on a few inputs and inspect
  the returned float before wiring it into the route; confirm the route returns
  `content_id` + attribution + placeholder confidence/label; confirm the signal
  function's signature matches "returns a float score," not a binary flag.

### M4 — Signal 2 + confidence scoring
- **Spec provided:** §3 (Signal 2), §4 (Confidence Scoring), the diagram.
- **Ask it to generate:** the stylometric signal function (burstiness + cliché
  density + avg sentence length → one score) and the `combine_scores` logic with
  the exact weights, thresholds, and disagreement rule.
- **How to verify:** confirm the generated thresholds match §4 *exactly* (AI tools
  drift here); run the 4 labeled inputs and check clearly-AI ≫ clearly-human, that
  borderline inputs land mid-band, and print both signal scores separately.

### M5 — Production layer
- **Spec provided:** §5 (Label variants), §6 (Appeals workflow), the diagram.
- **Ask it to generate:** the label-generation function mapping
  `(attribution, confidence)` to the three variant texts, and the `POST /appeal`
  endpoint.
- **How to verify:** ask it to print all three label variants and confirm the text
  matches §5 verbatim; confirm an appeal updates status to `under_review` and
  writes an appeal event; confirm rate limiting returns `429` after the limit.

---

## Stretch Features (design — written before building each)

All four stretch features are implemented. Designs below were written before the
corresponding code, per the milestone instruction to update planning.md first.

### S1 — Ensemble detection (3+ signals with documented weighting + voting)

- **What changes:** the pipeline is upgraded from 2 signals to **3 distinct
  signals** combined by both a **weighted score** and a **majority vote**.
- **Third signal — Lexical redundancy (`repetition_signal`).** A genuinely new
  axis from the first two: it measures *how much the text repeats itself*, not its
  meaning (LLM) or sentence shape (stylometry). Metrics: **distinct-bigram ratio**
  (unique bigrams / total bigrams) and **top-token dominance** (frequency share of
  the most common non-trivial word). AI text tends to reuse phrasing and cycle a
  small set of connective words → low bigram diversity / higher dominance →
  higher AI-likelihood. **Blind spot:** repetitive human verse/refrains score
  redundant too, so it carries the *lowest* weight.
- **Weighting (documented):** `confidence = 0.50*LLM + 0.30*stylometry +
  0.20*repetition`. The LLM stays dominant; the two heuristics are supporting
  votes.
- **Voting view:** each signal casts a vote (`ai` if its score ≥ 0.5, else
  `human`). The response and audit log report the tally (e.g., `2 ai / 1 human`)
  alongside the weighted score. The disagreement rule generalizes to
  *max − min spread across the three signals*: spread > 0.40 → forced `uncertain`.

### S2 — Provenance certificate ("Verified Human" credential)

- **Idea:** a creator can earn a **Verified Human** credential through an extra
  verification step; once earned, their submissions carry a badge.
- **Verification step (`POST /verify-human`):** creator supplies `creator_id`, a
  signed **attestation** string ("I certify this account's work is my own original
  human writing"), and a short **writing sample**. The system requires the exact
  attestation text and a sample of sufficient length (a lightweight stand-in for a
  real identity/liveness check), then issues a certificate.
- **Storage:** a `certificates` table (`cert_id`, `creator_id`, `issued_at`,
  `method`, `status=active`).
- **Display:** when a certified creator submits, the `/submit` response and the
  label include a `creator_badge`: **"✔ Verified Human Creator"** plus a short
  note. The badge is about *creator identity*, shown independently of the automated
  content verdict (a verified human can still submit AI text — the badge does not
  override the classification, it adds provenance context).
- **Endpoints:** `POST /verify-human` (earn), `GET /certificate/<creator_id>`
  (check).

### S3 — Analytics dashboard

- **Endpoint `GET /analytics`** returns platform-wide metrics computed from the
  audit log + content store:
  - **Detection patterns:** total classifications and a breakdown by attribution
    (`likely_ai` / `uncertain` / `likely_human`) with percentages.
  - **Appeal rate:** appeals ÷ classifications.
  - **Extra metric #1 — average confidence** overall and per attribution.
  - **Extra metric #2 — degraded rate** (share of classifications made without the
    LLM signal) and **verified-human creator count**.
- **`GET /dashboard`** renders the same numbers as a minimal HTML page for humans.

### S4 — Multi-modal support (second content type)

- **Second content type:** **image metadata + caption**. `POST /submit` accepts an
  optional `content_type` (`"text"` default, or `"image_metadata"`).
- **For `image_metadata`:** the body carries the image's `text` (caption/alt-text/
  description) plus a `metadata` object (e.g., generator software, C2PA-style
  fields). Two things happen:
  1. The **caption** runs through the normal 3-signal text pipeline.
  2. A **metadata provenance check** scans for authoritative AI-generation markers
     (`software`/`tool` containing "Midjourney", "DALL", "Stable Diffusion",
     "Firefly", etc., or explicit `ai_generated: true` / C2PA `digital_source_type`).
- **Combination rule:** hard provenance beats soft inference — if metadata
  explicitly declares AI generation, attribution is set to `likely_ai` with high
  confidence and the label notes the metadata source. Otherwise the caption's
  text-pipeline verdict stands. This models how real provenance (C2PA/Content
  Credentials) should override statistical guessing.
