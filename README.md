# Provenance Guard

A backend system that any creative-sharing platform can plug into to classify
submitted text as **AI-generated** or **human-written**, score **confidence** in
that classification, surface a plain-language **transparency label** to readers,
and handle **appeals** from creators who believe they were misclassified.

The guiding principle is **honesty over false certainty**. Perfect AI detection is
an unsolved problem, so Provenance Guard is built to *acknowledge uncertainty* and
to *protect human creators from false accusations*, not to hand down binary
verdicts.

> Full system design (written before implementation) lives in
> [planning.md](planning.md).

---

## Table of Contents

- [Quick start](#quick-start)
- [API](#api)
- [Architecture overview](#architecture-overview)
- [Detection signals](#detection-signals)
- [Confidence scoring](#confidence-scoring)
- [Transparency label](#transparency-label)
- [Appeals workflow](#appeals-workflow)
- [Rate limiting](#rate-limiting)
- [Audit log](#audit-log)
- [Stretch features](#stretch-features)
- [Known limitations](#known-limitations)
- [Spec reflection](#spec-reflection)
- [AI usage](#ai-usage)

---

## Quick start

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Provide your Groq key (never committed — .env is gitignored).
# Create a .env file in the repo root containing:
#   GROQ_API_KEY=gsk_...
# Optional overrides: GROQ_MODEL=llama-3.3-70b-versatile , PROVENANCE_DB=provenance.db

python app.py                      # serves on http://localhost:5000
```

Then open **http://localhost:5000/** for the interactive web UI (paste content →
Analyze → see the color-coded label, confidence bar, per-signal breakdown, and an
inline appeal form). The same endpoints are also usable directly via `curl` (see
[API](#api)).

If `GROQ_API_KEY` is missing or the Groq API fails, the system **degrades
gracefully** to the stylometric signal only and flags the result as `degraded`
rather than erroring.

---

## API

| Method | Path | Purpose |
|--------|------|---------|
| `GET`  | `/` | Interactive web UI (paste content, analyze, appeal) |
| `POST` | `/submit` | Classify text (or image metadata). Body: `{ "text", "creator_id", ["content_type", "metadata"] }` |
| `POST` | `/appeal` | Contest a classification. Body: `{ "content_id", "creator_reasoning" }` |
| `GET`  | `/log`    | Recent audit-log entries. Optional `?limit=N&status=under_review` |
| `GET`  | `/health` | Liveness + whether the LLM signal is configured |
| `POST` | `/verify-human` | *(stretch)* Earn a Verified-Human certificate |
| `GET`  | `/certificate/<creator_id>` | *(stretch)* Check a creator's certificate |
| `GET`  | `/analytics` | *(stretch)* Platform metrics as JSON |
| `GET`  | `/dashboard` | *(stretch)* Minimal HTML analytics view |

### Example `POST /submit`

```bash
curl -s -X POST http://localhost:5000/submit \
  -H "Content-Type: application/json" \
  -d '{"text": "The sun dipped below the horizon, painting the sky in hues of amber and rose. I sat on the porch, coffee in hand, watching the neighborhood slowly go quiet.", "creator_id": "test-user-1"}'
```

Response:

```json
{
  "content_id": "95da71a6-5421-4cf9-8151-5be4fc0108e7",
  "content_type": "text",
  "attribution": "likely_ai",
  "confidence": 0.748,
  "signals": { "llm_score": 0.8, "stylometric_score": 0.653, "repetition_score": 0.034 },
  "votes": { "ai": 2, "human": 1, "detail": { "llm": "ai", "stylometric": "ai", "repetition": "human" } },
  "label": {
    "title": "⚠︎ Likely AI-Generated",
    "body": "Our automated analysis suggests this content was most likely created with AI assistance (estimated 75% likelihood)...",
    "variant": "likely_ai",
    "ai_likelihood_pct": 75
  },
  "notes": { "degraded": false, "forced_uncertain": false, "reason": "confidence 0.75 mapped via thresholds" }
}
```

---

## Architecture overview

**The path a submission takes, end to end:**

1. A platform calls `POST /submit` with `{ text, creator_id }`.
2. The **rate limiter** (Flask-Limiter) checks the caller is within limits; over
   the limit returns `429` and stops.
3. The **submission handler** mints a unique `content_id` (UUID).
4. The text runs through **two independent detection signals**:
   - **Signal 1 — Groq LLM** (semantic view) → `llm_score` in `[0,1]`.
   - **Signal 2 — Stylometry** (structural view) → `stylometric_score` in `[0,1]`.
5. The **confidence scorer** blends the two into one `confidence` (probability the
   text is AI-generated), then maps it to an `attribution`
   (`likely_ai` / `uncertain` / `likely_human`) using **asymmetric thresholds**
   plus a **disagreement rule**.
6. The **label generator** turns `(attribution, confidence)` into the
   plain-language transparency label a reader sees.
7. The **audit log** (SQLite) records a structured classification event.
8. The JSON response returns `content_id`, `attribution`, `confidence`, both
   signal scores, and the label.

**Appeal path:** `POST /appeal` looks up the content by `content_id`, flips its
status to `under_review`, and writes the creator's reasoning to the audit log
alongside the original decision — for a human reviewer. No automated
re-classification.

A full ASCII diagram of both flows is in
[planning.md → Architecture](planning.md#architecture).

---

## Detection signals

Two **genuinely distinct** signals — one *semantic*, one *structural* — that fail
in different ways, so the combination is more informative than either alone.

### Signal 1 — Groq LLM classifier (semantic / holistic)

- **Measures:** whether the text *reads* as AI-generated — tone, voice, coherence,
  the bland "average-of-the-internet" register of model output. `llama-3.3-70b-versatile`
  at temperature 0 is asked to return a JSON `ai_probability` in `[0,1]`.
- **Why I chose it:** it captures holistic, semantic patterns that pure statistics
  can't — hedging, generic phrasing, paragraph rhythm.
- **What it misses:** it's a black box, can be inconsistent, and can be fooled by
  lightly edited AI text or by human writing that simply sounds generic. It has no
  calibrated sense of its own uncertainty.

### Signal 2 — Stylometric heuristics (structural / statistical)

Pure Python, no external libraries. Blends three measurable properties into one
score (weights in `config.py`):

| Sub-metric | What it captures | Weight |
|------------|------------------|--------|
| **Burstiness** (sentence-length coefficient of variation) | Humans write in bursts (long then short); AI is uniform. Low variation → AI-like. | 0.50 |
| **AI-cliché / transition density** | AI over-uses "furthermore," "moreover," "it is important to note." High density → AI-like. | 0.30 |
| **Average sentence length** | Long, uniform sentences skew AI; short, choppy ones skew human. | 0.20 |

- **Why I chose it:** it measures the *shape* of writing independent of meaning, a
  completely different axis from the LLM.
- **What it misses:** naturally formal/academic human writing and non-native
  English speakers produce uniform, connector-heavy prose, so stylometry can
  **falsely flag them as AI**. This is precisely why it is the *lower-weighted*
  signal and why signal disagreement forces "uncertain" (see below).

### Signal 3 — Lexical redundancy *(stretch: ensemble)*

- **Measures:** how much the text *repeats itself* — a third axis, distinct from
  meaning (LLM) and sentence shape (stylometry). Metrics: **distinct-bigram ratio**
  (unique bigrams ÷ total) and **top-token dominance** (frequency share of the most
  common content word).
- **Why:** AI text tends to reuse phrasing and cycle a few connectives → low bigram
  variety → higher AI-likelihood.
- **What it misses:** repetitive human verse/refrains also look redundant. Because
  low redundancy is *not* evidence of humanity, this signal is used **one-sided**
  (see [Ensemble detection](#stretch-features)).

---

## Confidence scoring

### How the signals combine

Each signal outputs an AI-likelihood in `[0,1]`. The combined confidence is a
weighted average, with the LLM weighted higher because stylometry has a known
false-positive blind spot:

```
confidence = 0.65 * llm_score + 0.35 * stylometric_score
```

`confidence` is the estimated **probability the text is AI-generated**:
`1.0` = strongly AI, **`0.5` = genuine coin-flip / cannot tell**, `0.0` = strongly
human. The *certainty of a verdict* is therefore the distance from 0.5 — a 0.51 is
almost no evidence, a 0.95 is strong evidence, and the label wording reflects that.

### Attribution thresholds (asymmetric — this is the false-positive defense)

A false positive (calling a human's work AI) is the worst outcome on a writing
platform, so the AI band is **deliberately harder to reach** than the human band:

| Combined confidence | Attribution | Label variant |
|---------------------|-------------|---------------|
| `>= 0.70` | `likely_ai` | High-confidence AI |
| `0.35 – < 0.70` | `uncertain` | Uncertain |
| `< 0.35` | `likely_human` | High-confidence human |

**Disagreement rule:** if the two signals disagree by more than `0.40`, attribution
is forced to `uncertain` regardless of the blended score — we never issue a
confident verdict when the semantic and structural views conflict. **Short-text
rule:** submissions under 40 words can't reach `likely_ai` (too little material to
be confident).

### How I validated it's meaningful

I ran four deliberately chosen inputs (clearly AI, clearly human, formal human,
lightly edited AI) through the *full two-signal pipeline* and printed each signal
separately. Scores spread across the full range (0.18 → 0.75), all three label
variants are reachable, and — critically — the **formal-human** paragraph the LLM
alone rated 0.70 lands in *uncertain*, not *AI*, because stylometry pulled it down
and the asymmetric threshold protected the writer.

### Two example submissions with noticeably different confidence

**High-confidence AI (confidence `0.748` → `likely_ai`):**

> "Artificial intelligence represents a transformative paradigm shift in modern
> society. It is important to note that while the benefits of AI are numerous, it
> is equally essential to consider the ethical implications. Furthermore,
> stakeholders across various sectors must collaborate to ensure responsible
> deployment."

| Signal | Score |
|--------|-------|
| LLM (semantic) | 0.80 |
| Stylometric (structural) | 0.653 (cliché density maxed on "furthermore / it is important to note") |
| **Combined confidence** | **0.748** → **⚠︎ Likely AI-Generated** |

**Lower-confidence / uncertain (confidence `0.628` → `uncertain`):**

> "The relationship between monetary policy and asset price inflation has been
> extensively studied in the literature. Central banks face a fundamental tension
> between their mandate for price stability and the unintended consequences of
> prolonged low interest rates on equity and real estate valuations."

| Signal | Score |
|--------|-------|
| LLM (semantic) | 0.70 |
| Stylometric (structural) | 0.493 |
| **Combined confidence** | **0.628** → **◦ Origin Uncertain** |

This pair is the whole point: real, formal *human* writing (economics prose) does
**not** get branded AI — it lands in the honest "uncertain" band, a meaningfully
different label and score from the clearly-AI example.

For reference, the clearly-human casual sample scored **0.183 → ✓ Likely
Human-Written** (LLM 0.20, stylometric 0.15).

---

## Transparency label

Three variants, keyed off `attribution`. Every variant is written as an
**estimate, not an accusation**, states plainly that automated detection can be
wrong, and (for AI/uncertain) points the creator to the appeal path. The estimated
percentage is interpolated into the body so a 0.51 and a 0.95 read differently
within the same variant.

### High-confidence AI (`likely_ai`)

> **⚠︎ Likely AI-Generated**
> Our automated analysis suggests this content was most likely created with AI
> assistance (estimated {pct}% likelihood). This is an automated estimate, not a
> certainty — detection tools can be wrong. If you are the creator and believe
> this is inaccurate, you can appeal this assessment.

### High-confidence human (`likely_human`)

> **✓ Likely Human-Written**
> Our automated analysis found no strong signs of AI generation, so this content
> most likely reflects original human writing (estimated {pct}% likelihood of AI
> generation). This is an automated estimate, not a guarantee of authorship.

### Uncertain (`uncertain`)

> **◦ Origin Uncertain**
> We could not confidently determine whether this content was written by a human
> or generated with AI (estimated {pct}% likelihood of AI generation). We are
> showing this honestly rather than guessing. Please treat the authorship as
> unverified. If you are the creator, you can add context by appealing.

`{pct}` is replaced with the actual percentage at runtime (e.g., 75% in the AI
example above). Implementation: `generate_label()` in [scoring.py](scoring.py).

---

## Appeals workflow

- **Who:** the creator of the content (identified by `content_id`; in production
  this would be auth-gated to the owning `creator_id`).
- **What they provide:** `content_id` and free-text `creator_reasoning`.
- **What the system does on receipt:**
  1. Look up the content (`404` if unknown).
  2. Update its status to **`under_review`**.
  3. Append an **appeal event** to the audit log, stored alongside the original
     classification (same `content_id`), with the reasoning and a timestamp.
  4. Return a confirmation.
- **No automated re-classification** — the appeal enters a human review queue.

**Example:**

```bash
curl -s -X POST http://localhost:5000/appeal \
  -H "Content-Type: application/json" \
  -d '{"content_id": "a067c26a-562c-4266-a1d6-e11e5c5a1329", "creator_reasoning": "I wrote this myself. I am a non-native English speaker and my style may appear more formal than typical."}'
```

Response:

```json
{
  "content_id": "a067c26a-562c-4266-a1d6-e11e5c5a1329",
  "status": "under_review",
  "message": "Appeal received. This content is now under review by a human moderator. The original classification and your reasoning have been logged.",
  "original_attribution": "likely_human",
  "original_confidence": 0.183
}
```

**What a reviewer sees** — `GET /log?status=under_review` returns exactly the
appealed items with the original attribution, confidence, both signal scores, the
creator's reasoning, and both timestamps:

```json
{
  "entries": [
    {
      "event_type": "appeal",
      "content_id": "a067c26a-562c-4266-a1d6-e11e5c5a1329",
      "creator_id": "test-user-1",
      "attribution": "likely_human",
      "confidence": 0.183,
      "llm_score": 0.2,
      "stylometric_score": 0.15,
      "status": "under_review",
      "appeal_reasoning": "I wrote this myself... non-native English speaker...",
      "timestamp": "2026-07-01T02:16:13.049Z"
    }
  ]
}
```

---

## Rate limiting

Applied to `POST /submit` via Flask-Limiter (`memory://` storage), keyed by client
IP:

```
10 per minute; 100 per day
```

**Reasoning for these specific numbers:**

- **10 / minute** — a genuine creator submits their *own* finished work; even an
  active writer editing and re-checking a piece rarely needs more than a handful of
  submissions in a minute. 10 leaves comfortable headroom for legitimate
  re-submissions while a script trying to brute-force/fingerprint the detector hits
  the wall almost immediately. It also caps Groq API cost per client, since every
  submission triggers an LLM call.
- **100 / day** — an upper bound on realistic single-creator daily volume (even a
  prolific writer or a small team on one account). An adversary trying to flood the
  system or farm the classifier across many texts is stopped well before doing
  damage, while normal users will essentially never see this limit.

Two tiers are used together so a burst is throttled by the per-minute limit and
sustained abuse is throttled by the per-day limit.

**Evidence** — 12 rapid requests against a fresh window (limit 10/min):

```
request 1 -> 200
request 2 -> 200
request 3 -> 200
request 4 -> 200
request 5 -> 200
request 6 -> 200
request 7 -> 200
request 8 -> 200
request 9 -> 200
request 10 -> 200
request 11 -> 429
request 12 -> 429
```

The first 10 succeed; the 11th and 12th return `429 Too Many Requests`.

---

## Audit log

Every attribution decision and every appeal is written to a structured SQLite
audit log (`audit_log` table; a `content` table tracks current status). Each entry
captures: timestamp, `content_id`, `creator_id`, attribution, combined confidence,
**both individual signal scores**, status, appeal reasoning (if any), and a
`details` JSON blob with per-sub-metric stylometry and the LLM's rationale. Surface
it via `GET /log`.

**Sample (`GET /log`) — 3 real entries: two classifications and one appeal.**

```json
{
  "entries": [
    {
      "id": 3,
      "event_type": "appeal",
      "content_id": "a067c26a-562c-4266-a1d6-e11e5c5a1329",
      "creator_id": "test-user-1",
      "attribution": "likely_human",
      "confidence": 0.183,
      "llm_score": 0.2,
      "stylometric_score": 0.15,
      "status": "under_review",
      "appeal_reasoning": "I wrote this myself from personal experience. I am a non-native English speaker and my writing style may appear more formal than typical.",
      "timestamp": "2026-07-01T02:16:13.049Z"
    },
    {
      "id": 2,
      "event_type": "classification",
      "content_id": "95da71a6-5421-4cf9-8151-5be4fc0108e7",
      "creator_id": "test-user-2",
      "attribution": "likely_ai",
      "confidence": 0.748,
      "llm_score": 0.8,
      "stylometric_score": 0.653,
      "status": "classified",
      "appeal_reasoning": null,
      "details": {
        "signals": {
          "llm": { "available": true, "model": "llama-3.3-70b-versatile",
                   "reasoning": "The text's overly formal tone, generic phrases, and lack of personal voice suggest AI generation." },
          "stylometric": { "burstiness_ai": 0.579, "cliche_ai": 1.0, "sentence_length_ai": 0.317,
                           "type_token_ratio": 0.884, "word_count": 43, "sentence_count": 3 }
        },
        "scoring": { "degraded": false, "forced_uncertain": false, "reason": "confidence 0.75 mapped via thresholds" }
      },
      "timestamp": "2026-07-01T02:16:13.042Z"
    },
    {
      "id": 1,
      "event_type": "classification",
      "content_id": "a067c26a-562c-4266-a1d6-e11e5c5a1329",
      "creator_id": "test-user-1",
      "attribution": "likely_human",
      "confidence": 0.183,
      "llm_score": 0.2,
      "stylometric_score": 0.15,
      "status": "classified",
      "appeal_reasoning": null,
      "timestamp": "2026-07-01T02:16:12.673Z"
    }
  ]
}
```

Note how entry `id: 3` logs the appeal **alongside** the original decision data for
the same `content_id` as `id: 1`, and flips status to `under_review`.

---

## Stretch features

All four stretch features are implemented. Designs were written into
[planning.md → Stretch Features](planning.md#stretch-features-design--written-before-building-each)
before the code.

### 1. Ensemble detection (3 signals, weighting + voting)

The pipeline runs **three distinct signals** and combines them two ways:

- **Weighted score:** base verdict = `0.65*LLM + 0.35*stylometry`, plus a
  **one-sided repetition boost** (up to `+0.15`). Repetition is one-sided by
  design: high redundancy is evidence *for* AI, but low redundancy is **not**
  evidence for a human, so it can raise confidence toward AI but never pull a
  verdict toward human. This choice matters — an earlier naive 3-way average
  wrongly demoted a clearly-AI sample from `likely_ai` (0.748) to `uncertain`
  (0.603) because that sample happened not to be repetitive; the one-sided design
  fixed it while still letting repetition flag genuinely repetitive text (a
  repetitive test string boosted 0.455 → 0.542).
- **Voting view:** each signal casts a vote (`ai` if score ≥ 0.5). The response
  and audit log report the tally, e.g. `"votes": {"ai": 2, "human": 1, "detail":
  {"llm": "ai", "stylometric": "ai", "repetition": "human"}}`.

The `/submit` response now includes `signals.repetition_score` and `votes`.

### 2. Provenance certificate ("Verified Human" credential)

A creator can earn a **Verified Human** badge through an extra verification step.

- `POST /verify-human` with `{ creator_id, attestation, writing_sample }`. The
  attestation must exactly match *"I certify this account's work is my own original
  human writing."* and the sample must be ≥ 20 words (a lightweight stand-in for a
  real identity/liveness check). A certificate is issued and stored.
- `GET /certificate/<creator_id>` checks status (`200` verified / `404` not).
- When a certified creator submits, the label carries a `creator_badge`:
  **"✔ Verified Human Creator"**. Crucially, the badge reflects *creator identity*,
  shown **independently** of the content verdict — a verified human can still post
  AI text, so the badge adds provenance context without overriding the
  classification.

```json
"label": {
  "title": "✓ Likely Human-Written", "...": "...",
  "creator_badge": { "text": "✔ Verified Human Creator",
    "note": "This creator has completed identity verification. The badge reflects the creator's verified status, not this specific piece of content." }
}
```

### 3. Analytics dashboard

`GET /analytics` (JSON) and `GET /dashboard` (HTML) surface platform metrics:

- **Detection patterns** — count + percentage per attribution.
- **Appeal rate** — appeals ÷ classifications.
- **Extra metric #1** — average confidence overall and per attribution.
- **Extra metric #2** — degraded (no-LLM) rate, and verified-human creator count.

Sample `GET /analytics`:

```json
{
  "total_classifications": 5,
  "detection_patterns": {
    "likely_ai": {"count": 1, "percent": 20.0},
    "likely_human": {"count": 3, "percent": 60.0},
    "uncertain": {"count": 1, "percent": 20.0}
  },
  "appeals": {"count": 1, "appeal_rate_percent": 20.0},
  "average_confidence": {"overall": 0.48,
    "by_attribution": {"likely_ai": 0.99, "uncertain": 0.755, "likely_human": 0.218}},
  "degraded_rate_percent": 0.0,
  "verified_human_creators": 1
}
```

### 4. Multi-modal support (image metadata)

`POST /submit` accepts `content_type: "image_metadata"` alongside the default
`"text"`. For image metadata the body carries the image's `text` (caption/
alt-text) plus a `metadata` object. Two things happen:

1. The **caption** runs through the normal 3-signal text pipeline.
2. A **metadata provenance check** scans for authoritative AI markers
   (generator software like Midjourney/DALL·E/Stable Diffusion, or C2PA-style
   `digital_source_type` / `ai_generated: true`).

**Hard provenance beats soft inference:** if the metadata explicitly declares AI
generation, attribution is set to `likely_ai` at `0.99` and the label adds a
`provenance_note`. Otherwise the caption's text verdict stands. This mirrors how
real Content Credentials (C2PA) should override statistical guessing.

```
image_metadata + {"software": "Midjourney v6"}  -> likely_ai (0.99), provenance_note set
image_metadata + {"software": "Adobe Photoshop"} -> caption's text verdict stands
```

---

## Known limitations

The system will predictably misclassify some content, tied directly to properties
of the two signals:

1. **Formal / academic human writing and non-native English speakers.** These
   produce uniform sentence lengths and connector-heavy prose, so the *stylometric*
   signal reads them as AI-like. In testing, the economics paragraph scored
   stylometric 0.49 and the LLM 0.70 — without the safeguards it would trend toward
   an AI verdict. It is held at *uncertain* only because stylometry is
   down-weighted (0.35) and the AI threshold is high (0.70). This is the primary
   false-positive risk and the reason for the entire asymmetric design.
2. **Short poems / repetitive verse with simple vocabulary.** Deliberate repetition
   and short, even lines crater burstiness and lexical diversity, so the heuristics
   read a genuine human creative choice as AI-like. The short-text rule (no
   `likely_ai` under 40 words) mitigates but does not eliminate this.
3. **Lightly edited AI text.** A human pass over AI output blurs both signals; this
   *should* land mid-range, and the "uncertain" band exists to catch it honestly
   rather than guess — but the LLM sometimes reads it as fully human (in testing the
   edited-AI sample scored LLM 0.20 → `likely_human`), which is a genuine false
   negative.

The honest takeaway: detection is imperfect by nature, so the real safeguards are
uncertainty-aware labels and a working appeals path — not a claim of accuracy.

---

## Spec reflection

**One way the spec helped:** Writing `planning.md` *before* code forced me to
answer "what should a confidence score of 0.5 mean to a user?" up front. Deciding
that 0.5 = "we genuinely cannot tell" (and that certainty is distance from 0.5)
before writing any math is what produced the asymmetric thresholds and the
disagreement rule. Had I coded first, I would likely have built a naive binary flip
at 0.5 and only discovered the false-positive problem while trying to word the
label.

**One way the implementation diverged:** The spec did not originally include a
**short-text rule**. During Milestone 4 testing, very short inputs produced
noisy, over-confident stylometric scores, so I added a rule (`< 40 words` cannot be
`likely_ai`) that the plan didn't anticipate. I also added **graceful degradation**
to stylometry-only when the LLM is unavailable — the spec assumed the LLM would
always be present, but a real system shouldn't hard-fail on an upstream API error.
Both changes are now reflected back in `config.py` and `planning.md`.

---

## AI usage

I used an AI coding assistant during implementation. Two specific instances:

1. **Stylometric signal generation.** I directed the assistant to generate a
   stylometric scoring function from the planning.md Signal 2 spec (burstiness +
   cliché density + sentence length → one `[0,1]` score). It produced a reasonable
   skeleton, but its initial burstiness mapping was too aggressive — nearly every
   multi-sentence text scored high. **I revised** the coefficient-of-variation
   normalization (mapping cv ≥ 0.9 → 0 rather than a steeper curve) and re-weighted
   the sub-metrics so burstiness (0.50) dominated cliché density (0.30), after
   verifying against the four labeled test inputs.

2. **Confidence scoring / thresholds.** I asked the assistant to generate the
   `combine_scores` logic. Its first version implemented a simple weighted average
   with a symmetric 0.5 cutoff — which silently contradicted the false-positive
   asymmetry in my spec. **I overrode** it to use the asymmetric thresholds
   (AI ≥ 0.70, human < 0.35) and added the **disagreement rule** (force
   `uncertain` when signals differ by > 0.40), which the generated code omitted
   entirely. I verified all three label variants were reachable afterward, and
   confirmed the formal-human sample correctly stayed out of the AI band.

In both cases the AI accelerated the boilerplate but produced scoring that drifted
from the spec's intent; the value came from checking the generated thresholds
against planning.md and correcting them before wiring them in.
