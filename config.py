"""Central configuration for Provenance Guard.

All tunable design decisions live here so the detection/scoring behaviour matches
planning.md in one place. See planning.md §4 for the reasoning behind these values.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Groq / LLM signal ------------------------------------------------------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

# --- Signal combination weights (planning.md §4, §S1) -----------------------
# The base verdict is a weighted blend of the two strong, BIDIRECTIONAL signals
# (LLM = semantic, stylometry = structural). The LLM is weighted higher because
# it is the more reliable signal; stylometry has a known false-positive blind
# spot on formal/non-native human writing. If the LLM is unavailable, stylometry
# is used alone (graceful degradation).
LLM_WEIGHT = 0.65
STYLOMETRIC_WEIGHT = 0.35

# ENSEMBLE 3rd signal (planning.md §S1): repetition is a ONE-SIDED confirmatory
# signal. High redundancy is evidence FOR AI, but low redundancy is NOT evidence
# for a human (humans also write non-repetitively), so it can only add up to this
# much confidence toward AI — it never pulls a verdict toward human.
REPETITION_MAX_BOOST = 0.15

# A signal "votes AI" if its score is at or above this line (planning.md §S1).
VOTE_LINE = 0.50

# --- Attribution thresholds (asymmetric — protects human writers) -----------
# The AI band is intentionally harder to reach than the human band so borderline
# content falls into "uncertain" rather than being called AI.
AI_THRESHOLD = 0.70          # confidence >= this  -> likely_ai
HUMAN_THRESHOLD = 0.35       # confidence <  this  -> likely_human
# between the two (inclusive of HUMAN_THRESHOLD, exclusive of AI_THRESHOLD) -> uncertain

# --- Disagreement rule (extra false-positive protection) --------------------
# If the two signals disagree by more than this, force "uncertain".
DISAGREEMENT_THRESHOLD = 0.40

# --- Short-text handling ----------------------------------------------------
# Below this word count, stylometrics are essentially noise, so we widen toward
# "uncertain" instead of issuing a confident verdict.
MIN_RELIABLE_WORDS = 40

# --- Stylometric sub-metric weights (planning.md §3, Signal 2) --------------
BURSTINESS_WEIGHT = 0.50
CLICHE_WEIGHT = 0.30
SENTENCE_LENGTH_WEIGHT = 0.20

# --- Rate limiting (documented in README) -----------------------------------
RATE_LIMIT = "10 per minute;100 per day"

# --- Storage ----------------------------------------------------------------
DB_PATH = os.getenv("PROVENANCE_DB", "provenance.db")

# Attribution category constants
LIKELY_AI = "likely_ai"
LIKELY_HUMAN = "likely_human"
UNCERTAIN = "uncertain"
