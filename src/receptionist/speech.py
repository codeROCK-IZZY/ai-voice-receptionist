"""A speech-recognition error channel for the evaluation.

Real phone speech recognition mishears in predictable ways: numbers that sound alike (fifteen / fifty), homophones
(four / for, two / to), similar words (massage / message), dropped short words and stray fillers. Running a real
recogniser over thousands of synthetic calls would need recorded audio; this channel applies those error types at a
chosen word error rate, so the dialogue can be measured at 0%, 10% and 20% WER.
"""

from __future__ import annotations

import random

CONFUSIONS: dict[str, list[str]] = {
    "fifteen": ["fifty"],
    "fifty": ["fifteen"],
    "thirteen": ["thirty"],
    "thirty": ["thirteen", "dirty"],
    "fourteen": ["forty"],
    "forty": ["fourteen"],
    "sixteen": ["sixty"],
    "seventeen": ["seventy"],
    "four": ["for", "or"],
    "two": ["to", "too"],
    "eight": ["ate", "a"],
    "one": ["won", "on"],
    "ten": ["then"],
    "massage": ["message"],
    "follow": ["hollow", "fellow"],
    "assessment": ["a session", "assessments"],
    "tuesday": ["tuesdays", "choose day"],
    "thursday": ["thursdays", "first day"],
    "friday": ["fried day"],
    "monday": ["mondays", "one day"],
    "wednesday": ["when's day"],
    "saturday": ["saturdays"],
    "morning": ["mourning"],
    "afternoon": ["after noon"],
    "book": ["look", "brook"],
    "cancel": ["council", "cancer"],
    "yes": ["yeah", "yet"],
    "no": ["know", "now"],
    "half": ["have"],
    "past": ["passed"],
    "quarter": ["water"],
    "name": ["nay"],
    "appointment": ["a pointment", "appointments"],
    "next": ["neck", "text"],
    "second": ["seconds"],
    "first": ["furs"],
    "the": ["a", "uh"],
}
FILLERS = ["uh", "um", "erm", "like", "so"]


class Channel:
    def __init__(self, wer: float, seed: int = 0) -> None:
        self.wer = wer
        self.rng = random.Random(seed)

    def hear(self, text: str) -> str:
        if self.wer <= 0:
            return text
        out: list[str] = []
        for word in text.split():
            bare = word.lower().strip(",.?!")
            if self.rng.random() >= self.wer:
                out.append(word)
                continue
            r = self.rng.random()
            if bare in CONFUSIONS and r < 0.7:
                out.append(self.rng.choice(CONFUSIONS[bare]))
            elif r < 0.85:
                continue  # dropped
            else:
                out.extend([self.rng.choice(FILLERS), word])  # inserted filler
        return " ".join(out)
