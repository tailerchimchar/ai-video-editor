"""The panel's three voices as typed questions about one candidate.

All three share the candidate's `facts` as their state, so Jev answers them in
one request per candidate. The prompts quote the panel rubrics
(`.claude/skills/gaming-expert/SKILL.md`, `viewer-expert/SKILL.md`,
`editing-expert/SKILL.md` + `arsenal.md`) and the mentor's story rules
(`.claude/skills/mentor/SKILL.md`); keep them in sync by hand.
"""

from __future__ import annotations

from ...decisions import BoolQuestion, ChoiceQuestion, ScoreQuestion, TypedQuestion
from .models import Candidate, EffectChoice

_STATE = (
    "The state is one moment of a League of Legends story video (the owner's "
    "game, told chronologically): game minute, the owner's kills and deaths, "
    'team numbers like "4v5" (owner side first), whether the owner\'s team won '
    "it a player down, the longest multikill chain, an objective taken right "
    "after, whether it is the last fight of the game, the enemy champions, "
    "whether the owner's team won, lost or traded the fight evenly (outcome), "
    "and what the owner did in it (owner_role: initiator_kill = the owner "
    "started it with a kill, follow_up, caught_first = the owner was caught "
    "and died first, absent)."
)

_MENTOR = (
    "Mentor rules: story videos should NOT be over-cut; the rhythm of the game "
    "IS the edit, trust the source footage. Owner edits such as 'make this "
    "teamfight cinematic' are additions on top of the approved story, not a "
    "new plan. When the owner's team wins a fight a player down, that fight is "
    "a highlight of the story."
)

GAMING_PROMPT = (
    f"{_STATE}\n\nYou are the gaming-expert, a professional League analyst "
    "(ADC specialist). How good and how game-deciding was this play? Rubric: "
    "1 = enemy threw, 2 = fine, 3 = solid, 4 = coach would clip it, "
    "5 = pro-level. Be stingy with 4 and 5. A fight won a player down, a "
    "multikill or the fight that ends the game rates higher than a lone trade."
)

VIEWER_PROMPT = (
    f"{_STATE}\n\nYou are the viewer-expert: a League player watching this on "
    "YouTube or TikTok. Would a League viewer want this moment to pop (an "
    "effect drawing the eye to it)? You answer 'what should pop'; viewers do "
    "not grade mechanics, so a hype moment can pop even if the play was "
    "ordinary. The killfeed pop is the viewer's confirmation. True = make it pop."
)

EDITOR_PROMPT = (
    f"{_STATE}\n\nYou are the editing-expert. Pick the one effect this moment "
    "should get, from what the renderer can do today. Know where slow-mo helps "
    "and where it kills momentum. A story video gets 3 to 6 effects in "
    "total, so roughly one moment in three gets one: the fights the owner "
    "started or won, the last fight, multikills. Pick none for routine or lost "
    f"moments.\n\n{_MENTOR}"
)

EFFECT_DESCRIPTIONS: dict[str, str] = {
    EffectChoice.none.value: "No effect: let the footage play as recorded.",
    EffectChoice.letterbox_focus.value: (
        "Cinematic bars plus a dimmed frame around the action, for the one "
        "biggest or last fight of the game."
    ),
    EffectChoice.slowmo.value: (
        "Half-speed for at most 2.5 seconds around the deciding kill, for a "
        "clutch kill the viewer must see clearly."
    ),
    EffectChoice.zoom.value: (
        "Zoom in on the centre of the screen (1.3x) for a multikill or a "
        "close-up of a fast outplay."
    ),
}

QUESTIONS_PER_CANDIDATE = 3

GAMING_QUESTION = ScoreQuestion(prompt=GAMING_PROMPT, lo=1, hi=5)
VIEWER_QUESTION = BoolQuestion(prompt=VIEWER_PROMPT)
EDITOR_QUESTION = ChoiceQuestion(
    prompt=EDITOR_PROMPT, options=EffectChoice, descriptions=EFFECT_DESCRIPTIONS
)


def questions_for(candidate: Candidate) -> list[tuple[TypedQuestion, dict]]:
    """gaming, viewer, editor -- in that order, one identical state."""
    state = dict(candidate.facts)
    return [(GAMING_QUESTION, state), (VIEWER_QUESTION, state), (EDITOR_QUESTION, state)]
