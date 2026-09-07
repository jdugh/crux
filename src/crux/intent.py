"""The approved scope: what the human actually asked for, verbatim.

Append-only, never rewritten, never summarised by a model.  A summary would put a
layer of interpretation between what you said and what drift is measured against,
which is exactly the bug this is meant to catch.

Fed by silent hooks: UserPromptSubmit for your prompts, PostToolUse on
AskUserQuestion for your answers, and the decisions ledger for what you settled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths, state

PROMPT = "prompt"
ANSWER = "answer"
DECISION = "decision"
PLAN = "plan"


def ledger_path(session_id: str) -> Path:
    return paths.session_dir(session_id) / "intent.jsonl"


def _next_seq(session_id: str) -> int:
    return len(read_all(session_id)) + 1


def append(session_id: str, kind: str, **fields: Any) -> Dict[str, Any]:
    record = {"t": kind, "ts": state.now_iso(), "seq": _next_seq(session_id)}
    record.update(fields)
    paths.append_jsonl(ledger_path(session_id), record)
    return record


def record_prompt(session_id: str, text: str) -> Dict[str, Any]:
    return append(session_id, PROMPT, text=text)


def record_answer(session_id: str, question: str, chosen: str,
                  notes: str = "", decision: Optional[str] = None,
                  header: str = "") -> Dict[str, Any]:
    return append(session_id, ANSWER, question=question, chosen=chosen,
                  notes=notes, decision=decision, header=header)


def record_decision(session_id: str, decision_id: str, status: str,
                    title: str) -> Dict[str, Any]:
    return append(session_id, DECISION, id=decision_id, status=status, title=title)


def record_plan(session_id: str, digest: str, path: str) -> Dict[str, Any]:
    return append(session_id, PLAN, digest=digest, path=path)


def read_all(session_id: str) -> List[Dict[str, Any]]:
    """Every entry, in order. A torn line is skipped, never fatal."""
    path = ledger_path(session_id)
    if not path.is_file():
        return []
    out: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        return out
    return out


def is_empty(session_id: str) -> bool:
    return not read_all(session_id)


def _format(record: Dict[str, Any]) -> str:
    kind = record.get("t")
    if kind == PROMPT:
        return f"[demande] {record.get('text', '').strip()}"
    if kind == ANSWER:
        bits = [f"[réponse humaine] Q: {record.get('question', '').strip()}",
                f"  → {record.get('chosen', '').strip()}"]
        if record.get("notes"):
            bits.append(f"  note: {record['notes'].strip()}")
        if record.get("decision"):
            bits.append(f"  (décision {record['decision']})")
        return "\n".join(bits)
    if kind == DECISION:
        return (f"[décision {record.get('id')}] {record.get('status')} — "
                f"{record.get('title', '').strip()}")
    if kind == PLAN:
        return f"[plan approuvé] {record.get('digest', '')[:16]}"
    return f"[{kind}] {json.dumps(record, ensure_ascii=False)}"


def render(session_id: str, max_chars: int = 8000) -> str:
    """The approved scope as sent to the scope-authority reviewer.

    Truncation order is fixed and non-negotiable:
      never dropped  - the first prompt (the original request) and every decision
      dropped first  - the oldest intermediate prompts
      then           - the oldest answers

    A human decision never falls out of a review context. That is what stops a
    reviewer re-raising, at round 2, something you already settled at round 1.
    """
    records = read_all(session_id)
    if not records:
        return ""

    protected_idx = set()
    for i, record in enumerate(records):
        if record.get("t") == DECISION:
            protected_idx.add(i)
    first_prompt = next(
        (i for i, r in enumerate(records) if r.get("t") == PROMPT), None)
    if first_prompt is not None:
        protected_idx.add(first_prompt)

    kept = list(range(len(records)))
    omitted_prompts = 0
    omitted_answers = 0

    def render_kept() -> str:
        parts: List[str] = []
        previous = -1
        for i in sorted(kept):
            if i - previous > 1:
                gap = i - previous - 1
                parts.append(f"[… {gap} entrée(s) intermédiaire(s) omise(s) …]")
            parts.append(_format(records[i]))
            previous = i
        return "\n\n".join(parts)

    text = render_kept()
    if len(text) <= max_chars:
        return text

    # drop oldest droppable prompts, then oldest droppable answers
    for wanted in (PROMPT, ANSWER):
        for i in list(sorted(kept)):
            if len(render_kept()) <= max_chars:
                break
            if i in protected_idx or records[i].get("t") != wanted:
                continue
            kept.remove(i)
            if wanted == PROMPT:
                omitted_prompts += 1
            else:
                omitted_answers += 1

    text = render_kept()
    if len(text) > max_chars:
        # Protected entries alone still overflow: hard-cut, but keep the head,
        # which is where the original request lives.
        text = text[:max_chars] + "\n[… périmètre tronqué …]"
    return text


def summary(session_id: str) -> Dict[str, int]:
    counts = {PROMPT: 0, ANSWER: 0, DECISION: 0, PLAN: 0}
    for record in read_all(session_id):
        kind = record.get("t")
        if kind in counts:
            counts[kind] += 1
    return counts
