"""Equal-employment self-disclosure questions.

Collected, not parsed -- none of this is in the resume. Kept dependency-free so both the
schema (`models.EqualEmployment`) and the form template read the same source.

Every question carries DECLINE. These are protected characteristics; refusing to answer has
to be a storable answer rather than a blank, or "unanswered" and "declined" become the same
state and the form can never be completed honestly.
"""

from dataclasses import dataclass

DECLINE = "Prefer not to say"


@dataclass(frozen=True)
class Question:
    id: str
    label: str
    choices: tuple[str, ...]
    multi: bool = False

    @property
    def options(self) -> tuple[str, ...]:
        return (*self.choices, DECLINE)


QUESTIONS: tuple[Question, ...] = (
    Question("work_authorization_us", "Are you authorized to work in the US?", ("Yes", "No")),
    Question("disability", "Do you have a disability?", ("Yes", "No")),
    Question("gender", "What is your gender?", ("Male", "Female", "Non-binary")),
    Question(
        "visa_sponsorship",
        "Will you now or in the future require sponsorship for employment visa status?",
        ("Yes", "No"),
    ),
    Question("lgbtq", "Do you identify as LGBTQ+?", ("Yes", "No")),
    Question("veteran", "Are you a veteran?", ("Yes", "No")),
    Question(
        "race",
        "How would you identify your race?",
        (
            "American Indian or Alaska Native",
            "Asian",
            "Black or African American",
            "Native Hawaiian or Other Pacific Islander",
            "White",
            "Two or More Races",
        ),
    ),
    Question("hispanic_latino", "Are you Hispanic or Latino?", ("Yes", "No")),
    Question(
        "sexual_orientation",
        "How would you describe your sexual orientation? (mark all that apply)",
        ("Heterosexual", "Gay", "Lesbian", "Bisexual", "Asexual", "Queer"),
        multi=True,
    ),
    Question("pronouns", "What are your pronouns?", ("He/Him", "She/Her", "They/Them")),
)

BY_ID = {q.id: q for q in QUESTIONS}
