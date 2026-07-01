"""Standalone test harness for the detection signals + scoring (planning.md §4).

Run:  .venv/bin/python test_signals.py
Prints each signal separately so we can see which one drives a result.
If GROQ_API_KEY is set, exercises both signals; otherwise stylometry-only
(degraded mode).
"""

import detection
import scoring

SAMPLES = {
    "clearly_ai": (
        "Artificial intelligence represents a transformative paradigm shift in "
        "modern society. It is important to note that while the benefits of AI "
        "are numerous, it is equally essential to consider the ethical "
        "implications. Furthermore, stakeholders across various sectors must "
        "collaborate to ensure responsible deployment."
    ),
    "clearly_human": (
        "ok so i finally tried that new ramen place downtown and honestly? "
        "underwhelming. the broth was fine but they put WAY too much sodium in "
        "it and i was thirsty for like three hours after. my friend got the "
        "spicy version and said it was better. probably won't go back unless "
        "someone drags me there"
    ),
    "borderline_formal_human": (
        "The relationship between monetary policy and asset price inflation has "
        "been extensively studied in the literature. Central banks face a "
        "fundamental tension between their mandate for price stability and the "
        "unintended consequences of prolonged low interest rates on equity and "
        "real estate valuations."
    ),
    "borderline_edited_ai": (
        "I've been thinking a lot about remote work lately. There are genuine "
        "tradeoffs — flexibility and no commute on one side, isolation and "
        "blurred work-life boundaries on the other. Studies show productivity "
        "varies widely by individual and role type."
    ),
}


def main():
    for name, text in SAMPLES.items():
        llm_score, llm_detail = detection.llm_signal(text)
        stylo_score, stylo_detail = detection.stylometric_signal(text)
        rep_score, rep_detail = detection.repetition_signal(text)
        result = scoring.combine_scores(
            llm_score, stylo_score, stylo_detail["word_count"], rep_score
        )
        label = scoring.generate_label(result["attribution"], result["confidence"])

        print("=" * 72)
        print(f"INPUT: {name}")
        llm_str = f"{llm_score:.3f}" if llm_score is not None else "N/A (degraded)"
        print(f"  Signal 1 (LLM semantic)     : {llm_str}")
        print(f"  Signal 2 (stylometric)      : {stylo_score:.3f}")
        print(f"     burstiness_ai={stylo_detail['burstiness_ai']} "
              f"cliche_ai={stylo_detail['cliche_ai']} "
              f"sent_len_ai={stylo_detail['sentence_length_ai']} "
              f"ttr={stylo_detail['type_token_ratio']} "
              f"words={stylo_detail['word_count']}")
        print(f"  Signal 3 (repetition)       : {rep_score:.3f}"
              f"  (distinct2={rep_detail.get('distinct_bigram_ratio')} "
              f"dominance={rep_detail.get('top_token_dominance')})")
        print(f"  Votes                       : {result['votes']['ai']} ai / "
              f"{result['votes']['human']} human  {result['votes']['detail']}")
        print(f"  Combined confidence         : {result['confidence']:.3f}")
        print(f"  Attribution                 : {result['attribution']}")
        print(f"  Reason                      : {result['reason']}")
        print(f"  Label                       : {label['title']}")
    print("=" * 72)


if __name__ == "__main__":
    main()
