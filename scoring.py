"""Confidence scoring and transparency-label generation (planning.md §4, §5)."""

import config


def combine_scores(llm_score, stylometric_score, word_count, repetition_score=None):
    """Combine the signal scores into one confidence + attribution.

    ENSEMBLE (planning.md §S1): weighted blend of up to three distinct signals
    (LLM, stylometry, repetition) plus a majority-vote tally. If the LLM is
    unavailable the heuristic weights are renormalized (graceful degradation).

    Returns a dict:
      {
        "confidence": float in [0,1],   # probability the text is AI-generated
        "attribution": likely_ai | uncertain | likely_human,
        "degraded": bool,               # True if LLM was unavailable
        "forced_uncertain": bool,       # True if disagreement/short-text rule fired
        "reason": str,
        "votes": {"ai": int, "human": int, "detail": {...}},
      }
    """
    degraded = llm_score is None

    # --- Base verdict: weighted blend of the two strong bidirectional signals.
    if degraded:
        base = stylometric_score  # graceful degradation
    else:
        base = (
            config.LLM_WEIGHT * llm_score
            + config.STYLOMETRIC_WEIGHT * stylometric_score
        )

    # --- Ensemble 3rd signal: one-sided confirmatory repetition boost.
    boost = 0.0
    if repetition_score is not None and repetition_score > config.VOTE_LINE:
        # Map repetition (0.5..1.0) -> (0..REPETITION_MAX_BOOST).
        boost = config.REPETITION_MAX_BOOST * (
            (repetition_score - config.VOTE_LINE) / (1.0 - config.VOTE_LINE)
        )
    confidence = max(0.0, min(1.0, base + boost))

    # Vote tally: each active signal votes AI if its score >= VOTE_LINE.
    contributing = {"stylometric": stylometric_score}
    if not degraded:
        contributing["llm"] = llm_score
    if repetition_score is not None:
        contributing["repetition"] = repetition_score
    vote_detail = {name: ("ai" if s >= config.VOTE_LINE else "human")
                   for name, s in contributing.items()}
    ai_votes = sum(1 for v in vote_detail.values() if v == "ai")
    human_votes = len(vote_detail) - ai_votes
    votes = {"ai": ai_votes, "human": human_votes, "detail": vote_detail}

    forced_uncertain = False
    reason = ""

    # Base attribution from asymmetric thresholds.
    if confidence >= config.AI_THRESHOLD:
        attribution = config.LIKELY_AI
    elif confidence < config.HUMAN_THRESHOLD:
        attribution = config.LIKELY_HUMAN
    else:
        attribution = config.UNCERTAIN

    # Disagreement rule: don't issue a confident verdict when the two BIDIRECTIONAL
    # signals conflict (repetition is one-sided, so it's excluded from the spread).
    scores = [stylometric_score] + ([] if degraded else [llm_score])
    spread = (max(scores) - min(scores)) if len(scores) > 1 else 0.0
    if len(scores) > 1 and spread > config.DISAGREEMENT_THRESHOLD:
        if attribution != config.UNCERTAIN:
            forced_uncertain = True
            reason = (
                f"signals disagree (spread {spread:.2f} > "
                f"{config.DISAGREEMENT_THRESHOLD}) -> forced uncertain"
            )
        attribution = config.UNCERTAIN

    # Short-text rule: too little material to be confident -> widen to uncertain.
    if word_count < config.MIN_RELIABLE_WORDS and attribution == config.LIKELY_AI:
        forced_uncertain = True
        reason = (
            f"only {word_count} words (< {config.MIN_RELIABLE_WORDS}); "
            f"too short for a confident AI verdict -> forced uncertain"
        )
        attribution = config.UNCERTAIN

    if not reason:
        reason = f"confidence {confidence:.2f} mapped via thresholds"

    return {
        "confidence": round(confidence, 3),
        "attribution": attribution,
        "degraded": degraded,
        "forced_uncertain": forced_uncertain,
        "reason": reason,
        "votes": votes,
    }


def generate_label(attribution, confidence, verified_human=False):
    """Map (attribution, confidence) to the transparency label (planning.md §5).

    The estimated percentage is interpolated so 0.51 and 0.95 read differently
    within the same variant. Returns {title, body, variant, ai_likelihood_pct}.

    If ``verified_human`` (planning.md §S2), a creator_badge is attached. The
    badge is about *creator identity* and is shown independently of the automated
    content verdict — it does not override the classification.
    """
    pct = round(confidence * 100)

    if attribution == config.LIKELY_AI:
        title = "⚠︎ Likely AI-Generated"
        body = (
            f"Our automated analysis suggests this content was most likely "
            f"created with AI assistance (estimated {pct}% likelihood). This is "
            f"an automated estimate, not a certainty — detection tools can be "
            f"wrong. If you are the creator and believe this is inaccurate, you "
            f"can appeal this assessment."
        )
    elif attribution == config.LIKELY_HUMAN:
        title = "✓ Likely Human-Written"
        body = (
            f"Our automated analysis found no strong signs of AI generation, so "
            f"this content most likely reflects original human writing (estimated "
            f"{pct}% likelihood of AI generation). This is an automated estimate, "
            f"not a guarantee of authorship."
        )
    else:  # uncertain
        title = "◦ Origin Uncertain"
        body = (
            f"We could not confidently determine whether this content was written "
            f"by a human or generated with AI (estimated {pct}% likelihood of AI "
            f"generation). We are showing this honestly rather than guessing. "
            f"Please treat the authorship as unverified. If you are the creator, "
            f"you can add context by appealing."
        )

    label = {
        "title": title,
        "body": body,
        "variant": attribution,
        "ai_likelihood_pct": pct,
    }

    if verified_human:
        label["creator_badge"] = {
            "text": "✔ Verified Human Creator",
            "note": (
                "This creator has completed identity verification. The badge "
                "reflects the creator's verified status, not this specific "
                "piece of content."
            ),
        }

    return label
