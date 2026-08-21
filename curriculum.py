"""Curriculum registry used by the learner subject picker.

The registry deliberately stores coverage metadata. Subject buttons can be
released before chapter ingestion is complete, while the UI can still tell a
learner whether a curriculum is an official imported syllabus or a baseline
subject map awaiting source verification.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CurriculumSubject:
    slug: str
    name: str
    group: str


PRIMARY = {
    1: ("English", "Hindi", "Mathematics", "Environmental Studies"),
    2: ("English", "Hindi", "Mathematics", "Environmental Studies"),
    3: ("English", "Hindi", "Mathematics", "Environmental Studies"),
    4: ("English", "Hindi", "Mathematics", "Environmental Studies"),
    5: ("English", "Hindi", "Mathematics", "Environmental Studies"),
}
MIDDLE = {
    6: ("English", "Hindi", "Mathematics", "Science", "Social Science", "Computer Science"),
    7: ("English", "Hindi", "Mathematics", "Science", "Social Science", "Computer Science"),
    8: ("English", "Hindi", "Mathematics", "Science", "Social Science", "Computer Science"),
}
SECONDARY = {
    9: ("English", "Hindi", "Mathematics", "Science", "Social Science", "Information Technology"),
    10: ("English", "Hindi", "Mathematics", "Science", "Social Science", "Information Technology"),
}
SENIOR = {
    11: ("English", "Physics", "Chemistry", "Mathematics", "Biology", "Accountancy", "Business Studies", "Economics", "History", "Political Science", "Geography", "Computer Science", "Psychology", "Sociology"),
    12: ("English", "Physics", "Chemistry", "Mathematics", "Biology", "Accountancy", "Business Studies", "Economics", "History", "Political Science", "Geography", "Computer Science", "Psychology", "Sociology"),
}
STATE_LANGUAGES = {
    "Andhra Pradesh": "Telugu",
    "Bihar": "Bengali",
    "Gujarat": "Gujarati",
    "Karnataka": "Kannada",
    "Kerala": "Malayalam",
    "Maharashtra": "Marathi",
    "Odisha": "Odia",
    "Tamil Nadu": "Tamil",
    "Telangana": "Telugu",
    "Uttar Pradesh": "Hindi",
    "West Bengal": "Bengali",
}


def _slug(value: str) -> str:
    return "-".join(value.casefold().replace("&", "and").split())


def subjects_for(standard: int, board: str, state: str | None = None) -> list[CurriculumSubject]:
    """Return subject buttons for the selected learner profile.

    This is the first registry layer. Official chapter/source imports will
    replace the baseline coverage metadata without changing the API contract.
    """
    groups = PRIMARY if standard <= 5 else MIDDLE if standard <= 8 else SECONDARY if standard <= 10 else SENIOR
    names = list(groups.get(standard, ()))
    if board == "State Board" and state:
        language = STATE_LANGUAGES.get(state)
        if language and language not in names:
            names.insert(1, language)
    group = "Primary" if standard <= 5 else "Middle school" if standard <= 8 else "Secondary" if standard <= 10 else "Senior secondary"
    return [CurriculumSubject(_slug(name), name, group) for name in names]


def curriculum_payload(standard_label: str | None, board: str | None, state: str | None) -> dict:
    try:
        standard = int((standard_label or "").split()[-1])
    except (ValueError, IndexError):
        return {"ready": False, "subjects": [], "message": "Choose your class and board first."}
    if standard not in range(1, 13) or board not in {"CBSE", "State Board"}:
        return {"ready": False, "subjects": [], "message": "Choose a supported class and board first."}
    if board == "State Board" and not state:
        return {"ready": False, "subjects": [], "message": "Choose your State Board state first."}
    subjects = subjects_for(standard, board, state)
    return {
        "ready": True,
        "standard": standard_label,
        "board": board,
        "state": state if board == "State Board" else None,
        "coverage": "subject-map",
        "coverage_label": "Subject map ready; chapter sources are being verified",
        "subjects": [subject.__dict__ for subject in subjects],
    }


def subject_for_profile(subject_name: str | None, standard_label: str | None, board: str | None, state: str | None) -> CurriculumSubject | None:
    if not subject_name:
        return None
    available = subjects_for(int((standard_label or "").split()[-1]), board or "", state)
    return next((subject for subject in available if subject.name.casefold() == subject_name.strip().casefold()), None)
