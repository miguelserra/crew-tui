"""P7.2a Project Brief and adaptive requirements interview.

The brief is provenance-rich input stored in ``ProjectState.extensions``. The
canonical requirements remain ``RequirementContract``; interview answers and
provenance are metadata used to build it, never a second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable
import hashlib
import json

from state import AcceptanceCriterion, Approval, ProjectState, RequirementContract

REQUIRED_FIELDS = ("project_name", "project_description", "main_objective")

BRIEF_FIELDS = (
    ("project_name", "Project name", "project_overview", "string", True),
    ("project_description", "What do you want to build?", "project_overview",
     "multiline_text", True),
    ("main_objective", "What should the application achieve?",
     "project_overview", "multiline_text", True),
    ("intended_users", "Who will use it?", "project_overview", "multiline_text",
     False),
    ("application_type", "What kind of application?", "application_profile",
     "select_or_free_text", False),
    ("target_platform", "Where should it run?", "application_profile",
     "multi_select_or_free_text", False),
    ("preferred_language", "Preferred programming language",
     "application_profile", "string_or_not_decided", False),
    ("preferred_technologies", "Preferred frameworks or technologies",
     "application_profile", "multiline_text", False),
    ("core_features", "What are the main features?", "functional_scope",
     "multiline_text", False),
    ("main_workflows", "What should users be able to do?", "functional_scope",
     "multiline_text", False),
    ("business_rules", "Business rules or constraints", "functional_scope",
     "multiline_text", False),
    ("data_inputs", "What data will it receive?", "data_and_integrations",
     "multiline_text", False),
    ("data_outputs", "What results should it produce?", "data_and_integrations",
     "multiline_text", False),
    ("data_storage", "Persistent data storage", "data_and_integrations",
     "string_or_not_decided", False),
    ("external_integrations", "External APIs or systems",
     "data_and_integrations", "multiline_text", False),
    ("interface_expectations", "Interface expectations",
     "interface_and_constraints", "multiline_text", False),
    ("non_functional_requirements", "Performance/reliability/security",
     "interface_and_constraints", "multiline_text", False),
    ("deployment_expectations", "Installation or deployment",
     "interface_and_constraints", "multiline_text", False),
    ("additional_constraints", "Other constraints",
     "interface_and_constraints", "multiline_text", False),
)
FIELD_IDS = {f[0] for f in BRIEF_FIELDS}
FIELD_LABELS = {f[0]: f[1] for f in BRIEF_FIELDS}

FIELD_TYPES = {
    "project_name": "CONSTRAINT",
    "project_description": "FUNCTIONAL",
    "main_objective": "FUNCTIONAL",
    "intended_users": "FUNCTIONAL",
    "application_type": "INTERFACE",
    "target_platform": "DEPLOYMENT",
    "preferred_language": "CONSTRAINT",
    "preferred_technologies": "CONSTRAINT",
    "core_features": "FUNCTIONAL",
    "main_workflows": "FUNCTIONAL",
    "business_rules": "BUSINESS_RULE",
    "data_inputs": "DATA",
    "data_outputs": "DATA",
    "data_storage": "DATA",
    "external_integrations": "INTEGRATION",
    "interface_expectations": "INTERFACE",
    "non_functional_requirements": "NON_FUNCTIONAL",
    "deployment_expectations": "DEPLOYMENT",
    "additional_constraints": "CONSTRAINT",
}


def _value_text(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return "; ".join(str(x) for x in value if str(x).strip())
    return str(value or "").strip()


@dataclass
class ProjectBrief:
    values: dict[str, Any] = field(default_factory=dict)
    undecided: list[str] = field(default_factory=list)

    def get(self, field_id: str) -> str:
        return _value_text(self.values.get(field_id))

    def set(self, field_id: str, value: Any) -> None:
        if field_id not in FIELD_IDS:
            raise ValueError(f"campo de brief desconhecido: {field_id}")
        if value is None or _value_text(value) == "":
            self.values.pop(field_id, None)
            return
        if _value_text(value).lower() in ("not decided", "unknown", "tbd"):
            self.mark_undecided(field_id)
            return
        if isinstance(value, (list, tuple)):
            self.values[field_id] = [str(x) for x in value if str(x).strip()]
        else:
            self.values[field_id] = str(value).strip()
        self.undecided = [x for x in self.undecided if x != field_id]

    def mark_undecided(self, field_id: str) -> None:
        if field_id not in FIELD_IDS:
            raise ValueError(f"campo de brief desconhecido: {field_id}")
        self.values.pop(field_id, None)
        if field_id not in self.undecided:
            self.undecided.append(field_id)

    def missing_required(self) -> list[str]:
        out = []
        for field_id in REQUIRED_FIELDS:
            if not self.get(field_id):
                out.append(field_id)
        return out

    def to_dict(self) -> dict[str, Any]:
        return {"values": dict(self.values),
                "undecided": list(dict.fromkeys(self.undecided))}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "ProjectBrief":
        data = data or {}
        values = dict(data.get("values") or {})
        if not values and isinstance(data.get("fields"), dict):
            values = dict(data["fields"])
        for field_id in FIELD_IDS:
            if field_id in data and field_id not in values:
                values[field_id] = data[field_id]
        return cls(values={k: v for k, v in values.items() if k in FIELD_IDS},
                   undecided=[str(x) for x in data.get("undecided") or []])


@dataclass
class InterviewQuestion:
    question_id: str
    category: str
    priority: str
    question: str
    reason: str
    target_requirement: str
    answer_type: str = "free_text"
    options: list[str] = field(default_factory=list)
    default_if_confirmed: str = ""
    blocks_architecture: bool = False
    status: str = "OPEN"  # OPEN|ANSWERED|DEFERRED|SUPERSEDED
    answer: str = ""
    user_confirmed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "InterviewQuestion":
        data = data or {}
        return cls(
            question_id=str(data.get("question_id") or ""),
            category=str(data.get("category") or "Functional ambiguity"),
            priority=str(data.get("priority") or "IMPORTANT").upper(),
            question=str(data.get("question") or ""),
            reason=str(data.get("reason") or ""),
            target_requirement=str(data.get("target_requirement")
                                  or data.get("target") or ""),
            answer_type=str(data.get("answer_type") or "free_text"),
            options=[str(x) for x in data.get("options") or []],
            default_if_confirmed=str(data.get("default_if_confirmed") or ""),
            blocks_architecture=bool(data.get("blocks_architecture", False)),
            status=str(data.get("status") or "OPEN").upper(),
            answer=str(data.get("answer") or ""),
            user_confirmed=bool(data.get("user_confirmed", False)),
        )


@dataclass
class RequirementsInterview:
    questions: list[InterviewQuestion] = field(default_factory=list)
    current_index: int = 0
    revision: int = 0

    def by_id(self, qid: str) -> InterviewQuestion | None:
        return next((q for q in self.questions
                     if q.question_id == qid), None)

    def open_questions(self) -> list[InterviewQuestion]:
        return [q for q in self.questions if q.status == "OPEN"]

    def blocking_open(self) -> list[InterviewQuestion]:
        return [q for q in self.open_questions()
                if q.priority == "BLOCKING" or q.blocks_architecture]

    def to_dict(self) -> dict[str, Any]:
        return {"questions": [q.to_dict() for q in self.questions],
                "current_index": self.current_index,
                "revision": self.revision}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "RequirementsInterview":
        data = data or {}
        return cls(
            questions=[InterviewQuestion.from_dict(q)
                       for q in data.get("questions") or []],
            current_index=int(data.get("current_index") or 0),
            revision=int(data.get("revision") or 0),
        )


def _new_question(qid: str, category: str, priority: str, question: str,
                  reason: str, target: str, *,
                  options: Iterable[str] = (), default: str = "",
                  answer_type: str = "free_text",
                  blocking: bool = False) -> InterviewQuestion:
    return InterviewQuestion(
        question_id=qid, category=category, priority=priority,
        question=question, reason=reason, target_requirement=target,
        answer_type=answer_type, options=list(options),
        default_if_confirmed=default, blocks_architecture=blocking)


def generate_questions(brief: ProjectBrief,
                       interview: RequirementsInterview) -> RequirementsInterview:
    existing = {q.question_id: q for q in interview.questions}
    # Questions whose target became clear from the brief are superseded.
    for q in interview.questions:
        if q.status == "OPEN" and q.target_requirement in FIELD_IDS:
            if brief.get(q.target_requirement):
                q.status = "SUPERSEDED"

    def add(question: InterviewQuestion) -> None:
        old = existing.get(question.question_id)
        if old is not None:
            if old.status == "SUPERSEDED" and not brief.get(
                    old.target_requirement):
                old.status = "OPEN"
            return
        interview.questions.append(question)
        existing[question.question_id] = question

    if not brief.get("core_features") and not brief.get("main_workflows"):
        add(_new_question(
            "q_core_workflows", "User workflow ambiguity", "BLOCKING",
            "What should users be able to do in the application?",
            "No functional workflow is specified, so architecture would have "
            "to invent user behaviour.",
            "core_features", blocking=True,
            answer_type="multiline_text"))
    if not brief.get("acceptance") and not any(
            q.target_requirement == "acceptance" and q.status == "ANSWERED"
            for q in interview.questions):
        add(_new_question(
            "q_acceptance", "Acceptance criteria", "BLOCKING",
            "What observable result shows that the project is successful?",
            "Architecture needs at least one measurable acceptance criterion.",
            "acceptance", blocking=True,
            answer_type="multiline_text"))
    if not brief.get("application_type") and \
            "application_type" not in brief.undecided:
        add(_new_question(
            "q_application_type", "Platform or technology decision", "IMPORTANT",
            "What kind of application should be built?",
            "The application type materially affects the architecture.",
            "application_type", answer_type="select_or_free_text",
            options=["CLI", "TUI", "Web application", "Backend service",
                     "Library", "Automation tool", "Not decided"],
            default="Not decided"))
    if not brief.get("preferred_language") and \
            "preferred_language" not in brief.undecided:
        add(_new_question(
            "q_preferred_language", "Platform or technology decision",
            "IMPORTANT",
            "Do you have a preferred programming language, or should the "
            "architecture recommend one?",
            "A language decision affects dependencies and implementation.",
            "preferred_language", answer_type="string_or_not_decided",
            default="Not decided"))
    if (not brief.get("data_storage")
            and "data_storage" not in brief.undecided
            and (brief.get("data_inputs") or brief.get("data_outputs")
                 or "database" in brief.get("project_description").lower()
                 or "storage" in brief.get("project_description").lower())):
        add(_new_question(
            "q_data_storage", "Data model and persistence", "IMPORTANT",
            "Should the application use a local database, a remote database "
            "or an existing company data system?",
            "Persistence technology affects deployment and data ownership.",
            "data_storage", answer_type="string_or_not_decided",
            default="Not decided"))
    if brief.get("external_integrations"):
        add(_new_question(
            "q_integration_contract", "External API and integration", "BLOCKING",
            "Which external systems must the application call, and what "
            "information must be exchanged?",
            "External interfaces must be explicit before architecture.",
            "external_integrations", blocking=True,
            answer_type="multiline_text"))
    if "api" in brief.get("project_description").lower() and \
            not brief.get("external_integrations"):
        add(_new_question(
            "q_integration_contract", "External API and integration", "BLOCKING",
            "Which external systems or APIs must the application communicate "
            "with?",
            "The description mentions an API but no integration target.",
            "external_integrations", blocking=True,
            answer_type="multiline_text"))
    interview.current_index = next(
        (i for i, q in enumerate(interview.questions) if q.status == "OPEN"), 0)
    return interview


def _field_item_id(field_id: str) -> str:
    return "req_" + field_id


def _question_item_id(qid: str) -> str:
    return "req_" + qid


def normalize(brief: ProjectBrief, interview: RequirementsInterview
              ) -> tuple[RequirementContract, list[dict[str, Any]]]:
    items: list[dict[str, Any]] = []
    for field_id, label, _section, _typ, _required in BRIEF_FIELDS:
        value = brief.get(field_id)
        if not value:
            continue
        items.append({
            "requirement_id": _field_item_id(field_id),
            "requirement_type": FIELD_TYPES.get(field_id, "CONSTRAINT"),
            "description": f"{label}: {value}",
            "status": "CONFIRMED",
            "priority": "MANDATORY" if field_id in REQUIRED_FIELDS else "IMPORTANT",
            "acceptance_criteria": [],
            "source_brief_field": field_id,
            "source_question_id": "",
            "user_confirmed": True,
            "dependencies": [],
            "constraints": [],
        })
    acceptance: list[AcceptanceCriterion] = []
    open_questions: list[str] = []
    for q in interview.questions:
        if q.status == "ANSWERED":
            items.append({
                "requirement_id": _question_item_id(q.question_id),
                "requirement_type": "ACCEPTANCE_CRITERION"
                if q.target_requirement == "acceptance"
                else FIELD_TYPES.get(q.target_requirement, "FUNCTIONAL"),
                "description": f"{q.question} Answer: {q.answer}",
                "status": "CONFIRMED",
                "priority": q.priority,
                "acceptance_criteria": [q.answer]
                if q.target_requirement == "acceptance" else [],
                "source_brief_field": "",
                "source_question_id": q.question_id,
                "user_confirmed": bool(q.user_confirmed),
                "dependencies": [],
                "constraints": [],
            })
            if q.target_requirement == "acceptance":
                acceptance.append(AcceptanceCriterion(
                    id=f"ac{len(acceptance) + 1}", text=q.answer,
                    justification=q.reason))
        elif q.status == "DEFERRED":
            items.append({
                "requirement_id": _question_item_id(q.question_id),
                "requirement_type": FIELD_TYPES.get(q.target_requirement,
                                                    "CONSTRAINT"),
                "description": f"Deferred: {q.question}",
                "status": "DEFERRED",
                "priority": q.priority,
                "acceptance_criteria": [],
                "source_brief_field": "",
                "source_question_id": q.question_id,
                "user_confirmed": True,
                "dependencies": [],
                "constraints": [],
            })
        elif q.status == "OPEN":
            open_questions.append(q.question)
            items.append({
                "requirement_id": _question_item_id(q.question_id),
                "requirement_type": FIELD_TYPES.get(q.target_requirement,
                                                    "CONSTRAINT"),
                "description": q.question,
                "status": "UNRESOLVED",
                "priority": q.priority,
                "acceptance_criteria": [],
                "source_brief_field": "",
                "source_question_id": q.question_id,
                "user_confirmed": False,
                "dependencies": [],
                "constraints": [],
            })
    scope = [brief.get("core_features"), brief.get("main_workflows"),
             brief.get("intended_users")]
    constraints = [brief.get("business_rules"),
                   brief.get("additional_constraints"),
                   brief.get("non_functional_requirements")]
    contract = RequirementContract(
        objective=brief.get("main_objective") or brief.get("project_description"),
        scope_in=[x for x in scope if x],
        constraints=[x for x in constraints if x],
        acceptance=acceptance,
        open_questions=open_questions,
        raw={"p7_2a": {
            "brief": brief.to_dict(),
            "interview": interview.to_dict(),
            "requirements": items,
            "revision": interview.revision,
        }},
    )
    return contract, items


def has_brief(state: ProjectState) -> bool:
    return isinstance(state.extensions.get("p7_2a"), dict)


def brief_of(state: ProjectState) -> ProjectBrief:
    return ProjectBrief.from_dict(
        (state.extensions.get("p7_2a") or {}).get("brief"))


def interview_of(state: ProjectState) -> RequirementsInterview:
    return RequirementsInterview.from_dict(
        (state.extensions.get("p7_2a") or {}).get("interview"))


def install(state: ProjectState, brief: ProjectBrief,
            interview: RequirementsInterview | None = None,
            *, revision: int = 1) -> RequirementsInterview:
    interview = interview or RequirementsInterview(revision=revision)
    interview.revision = revision
    interview = generate_questions(brief, interview)
    state.extensions["p7_2a"] = {
        "brief": brief.to_dict(),
        "interview": interview.to_dict(),
        "revision": revision,
    }
    sync_contract(state)
    return interview


def sync_contract(state: ProjectState) -> tuple[RequirementContract,
                                                list[dict[str, Any]]]:
    brief = brief_of(state)
    interview = interview_of(state)
    contract, items = normalize(brief, interview)
    state.requirements = contract
    state.extensions.setdefault("p7_2a", {})["requirements"] = items
    return contract, items


def answer_question(state: ProjectState, qid: str, answer: str, *,
                    defer: bool = False, revise: bool = False) -> dict[str, Any]:
    brief = brief_of(state)
    interview = interview_of(state)
    question = interview.by_id(qid)
    if question is None:
        raise ValueError(f"pergunta desconhecida: {qid}")
    answer = str(answer or "").strip()
    if defer:
        question.status = "DEFERRED"
        question.answer = answer or "Not decided"
        question.user_confirmed = True
        if question.target_requirement in FIELD_IDS:
            brief.mark_undecided(question.target_requirement)
    else:
        if not answer:
            raise ValueError("resposta vazia")
        target = question.target_requirement
        if target in FIELD_IDS:
            current = brief.get(target)
            if (current and current != answer and not revise
                    and not qid.startswith("q_conflict_")):
                conflict_id = f"q_conflict_{qid}"
                if interview.by_id(conflict_id) is None:
                    interview.questions.append(_new_question(
                        conflict_id, question.category, "BLOCKING",
                        f"The new answer conflicts with the current value of "
                        f"'{FIELD_LABELS.get(target, target)}'. Which is correct?",
                        "Conflicting answers must be clarified before "
                        "architecture.", target, blocking=True,
                        answer_type="free_text"))
                interview.revision += 1
                state.extensions["p7_2a"]["brief"] = brief.to_dict()
                state.extensions["p7_2a"]["interview"] = interview.to_dict()
                sync_contract(state)
                return {"question_id": qid, "conflict": conflict_id,
                        "conflict_question": next(
                            q.to_dict() for q in interview.questions
                            if q.question_id == conflict_id)}
            brief.set(target, answer)
        question.status = "ANSWERED"
        question.answer = answer
        question.user_confirmed = True
        if revise:
            conflict_id = f"q_conflict_{qid}"
            conflict = interview.by_id(conflict_id)
            if conflict is not None and conflict.status == "OPEN":
                conflict.status = "SUPERSEDED"
    interview.revision += 1
    state.extensions["p7_2a"]["brief"] = brief.to_dict()
    state.extensions["p7_2a"]["interview"] = interview.to_dict()
    interview = generate_questions(brief, interview)
    state.extensions["p7_2a"]["interview"] = interview.to_dict()
    sync_contract(state)
    return {"question_id": qid, "status": question.status, "answer": question.answer}


def blockers(state: ProjectState) -> list[str]:
    if not has_brief(state):
        return []
    brief = brief_of(state)
    interview = interview_of(state)
    out = [f"campo obrigatório em falta: {FIELD_LABELS.get(f, f)}"
           for f in brief.missing_required()]
    for q in interview.blocking_open():
        out.append(f"decisão bloqueante em aberto: {q.question_id}")
    return out


def template() -> dict[str, Any]:
    sections: dict[str, list[dict[str, Any]]] = {}
    for field_id, label, section, typ, required in BRIEF_FIELDS:
        sections.setdefault(section, []).append({
            "id": field_id, "label": label, "type": typ,
            "required": required,
        })
    return {"sections": [
        {"id": sid, "fields": fields} for sid, fields in sections.items()
    ], "usage": "/brief field=value; /brief {json}; /brief show"}


def summary(state: ProjectState) -> dict[str, Any]:
    brief = brief_of(state)
    interview = interview_of(state)
    contract = state.requirements
    return {
        "brief": brief.to_dict(),
        "questions": [q.to_dict() for q in interview.questions],
        "blocking": [q.question_id for q in interview.blocking_open()],
        "blockers": blockers(state),
        "requirements": list(contract.to_dict().get("p7_2a", {}).get(
            "requirements", []))
        if contract.raw else [],
        "objective": contract.objective,
        "acceptance": [c.to_dict() for c in contract.acceptance],
        "open_questions": list(contract.open_questions),
        "approval_hash": contract.content_hash(),
    }
