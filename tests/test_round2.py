"""The round-2 planner: anchoring, delta, triggers, and the guarantees.

The planner decides *what a second round would do*. It runs no Codex, opens no
decision and writes no journal, so everything here is asserted directly against
the plan object or against `crux route --explain`.

Four properties are load-bearing and each has its own class:

  anchoring    a round that cannot account for itself yields no targeted delta,
               only the v0.1 fallback (fail-open, I1).
  delta        computed from content hashes of the working tree, so `sed`, an
               external editor and Edit are the same event (edits.jsonl is never
               read).
  triggers     nothing is selected without a named trigger, nothing excluded
               without a named reason, and `deferred` alone never re-opens.
  authority    the scope authority is in every plan with a delta, and neither
               `max_selected` nor an override may remove it.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from ._support import CruxTestCase

from crux import (baseline, cli, config, decisions, findings, paths, round2,
                  state)

MVP = ("code-quality", "architecture", "security")
AUTHORITIES = {"code-quality", "architecture"}


# --------------------------------------------------------------- builders ---
def finding(fid: str = "R1F1", reviewer: str = "code-quality", **kwargs) -> dict:
    base = dict(id=fid, reviewer=reviewer, severity="high", title=f"titre {fid}",
                description="description", file="src/a.py")
    base.update(kwargs)
    return findings.Finding(**base).to_dict()


def record(*, round_no: int = 1, found=(), promoted=None,
           selected=("code-quality",), authority="code-quality",
           files=None, schema: int = 2) -> dict:
    """A round journal exactly as `findings.save_round` writes one."""
    return {
        "round": round_no, "schema": schema, "saved_at": "2026-01-01T00:00:00Z",
        "selected": list(selected), "scope_authority": authority,
        "failed_reviewers": [], "degraded": False,
        "diff": {"fingerprint": "fp", "files": dict(files or {})},
        "findings": list(found), "promoted": dict(promoted or {}),
    }


def plan(*, previous=None, anchor=round2.ANCHORED, delta=("src/a.py",),
         resolutions=None, decision_status=None, candidates=MVP,
         delta_selected=(), delta_evidence=None, never=(), max_selected=4,
         authorities=None, previous_authority="code-quality"):
    return round2.plan(
        previous_record=previous if previous is not None else record(),
        anchor_status=anchor,
        delta_paths=list(delta),
        delta_selected=list(delta_selected),
        delta_evidence=delta_evidence,
        resolutions=resolutions or {},
        decision_status=decision_status or {},
        candidates=list(candidates),
        never=never,
        max_selected=max_selected,
        authority_personas=AUTHORITIES if authorities is None else authorities,
        previous_authority=previous_authority)


# =============================================================== anchoring ===
class Anchoring(CruxTestCase):
    """No anchor, no targeted delta. The fallback is named, not implied."""

    def test_a_legacy_journal_is_not_anchorable(self):
        result = plan(anchor=round2.NOT_ANCHORABLE, previous=None)
        self.assertEqual(result.fallback, round2.FALLBACK_FULL_ROUTER)
        self.assertFalse(result.anchored)
        self.assertEqual(result.selected_reviewers, [])
        self.assertTrue(any("non ancrable" in w for w in result.warnings))

    def test_every_refusal_yields_the_same_fallback(self):
        for status in (round2.NO_PREVIOUS_ROUND, round2.NOT_ANCHORABLE,
                       round2.UNREADABLE, round2.INCONSISTENT):
            with self.subTest(status=status):
                result = plan(anchor=status, previous=None)
                self.assertEqual(result.fallback, round2.FALLBACK_FULL_ROUTER)
                self.assertTrue(result.warnings)

    def test_an_anchored_round_carries_no_fallback(self):
        result = plan()
        self.assertIsNone(result.fallback)
        self.assertTrue(result.anchored)
        self.assertEqual(result.previous_round, 1)

    def test_schema_1_on_disk_is_refused(self):
        directory = paths.ensure_dir(paths.session_dir("s1") / "findings")
        (directory / "round-1.json").write_text(
            json.dumps(record(schema=1)), encoding="utf-8")
        found, status = round2.previous_round("s1")
        self.assertIsNone(found)
        self.assertEqual(status, round2.NOT_ANCHORABLE)

    def test_no_round_at_all(self):
        self.assertEqual(round2.previous_round("s1"),
                         (None, round2.NO_PREVIOUS_ROUND))

    def test_an_unreadable_journal_is_refused_not_skipped(self):
        """`load_rounds` drops a corrupt file; anchoring on the round before it
        would erase every path touched in between from the delta."""
        directory = paths.ensure_dir(paths.session_dir("s1") / "findings")
        (directory / "round-1.json").write_text(
            json.dumps(record()), encoding="utf-8")
        (directory / "round-2.json").write_text("{ tronqué", encoding="utf-8")
        found, status = round2.previous_round("s1")
        self.assertIsNone(found)
        self.assertEqual(status, round2.UNREADABLE)

    def test_journals_disagreeing_with_the_session_state_are_refused(self):
        directory = paths.ensure_dir(paths.session_dir("s1") / "findings")
        (directory / "round-1.json").write_text(
            json.dumps(record()), encoding="utf-8")
        found, status = round2.previous_round("s1", expected_round=3)
        self.assertIsNone(found)
        self.assertEqual(status, round2.INCONSISTENT)

    def test_the_anchor_matches_findings_round_is_anchorable(self):
        """One definition of anchorable, not two."""
        self.assertTrue(findings.round_is_anchorable(record()))
        self.assertFalse(findings.round_is_anchorable(record(schema=1)))


# =================================================================== delta ===
class Delta(CruxTestCase):
    """Hashes of the tree, on both sides. Nothing else."""

    def test_a_modified_file_is_in_the_delta(self):
        self.assertEqual(round2.compute_delta({"a.py": "aaa"}, {"a.py": "bbb"}),
                         ["a.py"])

    def test_a_created_file_is_in_the_delta(self):
        self.assertEqual(round2.compute_delta({}, {"new.py": "aaa"}), ["new.py"])

    def test_a_deleted_file_is_in_the_delta(self):
        self.assertEqual(round2.compute_delta({"gone.py": "aaa"},
                                              {"gone.py": None}), ["gone.py"])

    def test_a_file_that_never_existed_is_not_a_delta(self):
        self.assertEqual(round2.compute_delta({"x.py": None}, {"x.py": None}), [])

    def test_an_untouched_file_is_not_a_delta(self):
        self.assertEqual(round2.compute_delta({"a.py": "aaa"}, {"a.py": "aaa"}), [])

    def test_the_delta_is_sorted_and_therefore_deterministic(self):
        got = round2.compute_delta({"b": "1", "a": "1", "c": "1"},
                                   {"b": "2", "a": "2", "c": "2"})
        self.assertEqual(got, ["a", "b", "c"])

    def test_a_change_made_by_a_shell_script_is_seen(self):
        """Written without going through Edit/Write: no edits.jsonl entry."""
        self.write("src/a.py", "print(1)\n")
        before = baseline.content_map(self.repo, ["src/a.py"])
        (self.repo / "src" / "a.py").write_text("print(2)\n", encoding="utf-8")
        after = baseline.content_map(self.repo, ["src/a.py"])
        self.assertEqual(round2.compute_delta(before, after), ["src/a.py"])
        self.assertEqual(state.edited_paths("s1"), [])

    def test_the_delta_ignores_the_edits_journal_entirely(self):
        """A journal that lies changes nothing: the bytes decide."""
        self.write("src/a.py", "print(1)\n")
        current = baseline.content_map(self.repo, ["src/a.py"])
        state.record_edit("s1", "src/a.py", "not-the-real-hash")
        state.record_edit("s1", "src/ghost.py", "deadbeef")
        self.assertEqual(round2.compute_delta(current, current), [])

    def test_delta_since_widens_to_paths_the_round_never_saw(self):
        self.write("src/a.py", "print(1)\n")
        self.write("src/b.py", "print(2)\n")
        journal = record(files=baseline.content_map(self.repo, ["src/a.py"]))
        self.write("src/b.py", "print(3)\n")
        self.assertEqual(round2.delta_since(journal, self.repo, ["src/b.py"]),
                         ["src/b.py"])

    def test_an_unrecorded_path_is_a_delta_because_it_cannot_be_compared(self):
        """`None` outside the recorded set means "never recorded", not
        "was absent" - reading the second for the first loses deletions."""
        self.assertEqual(
            round2.compute_delta({"a.py": "aaa"},
                                 {"a.py": "aaa", "gone.py": None},
                                 recorded={"a.py"}),
            ["gone.py"])

    def test_deleting_a_file_the_round_never_diffed_stays_in_the_delta(self):
        """R1F1: a file that existed and was untouched at round 1 is absent
        from that round's journal. Deleting it afterwards used to read
        None -> None on both sides and vanish - taking the scope authority
        with it, since an empty delta requires none."""
        self.write("src/kept.py", "print('inchange')\n")
        self.write("src/other.py", "print('relu au round 1')\n")
        self.commit("un fichier suivi, propre au moment du round")
        journal = record(
            files=baseline.content_map(self.repo, ["src/other.py"]))
        (self.repo / "src" / "kept.py").unlink()
        self.assertEqual(
            round2.delta_since(journal, self.repo, ["src/kept.py"]),
            ["src/kept.py"])

    def test_delta_since_needs_no_help_for_a_path_the_round_knew(self):
        self.write("src/a.py", "print(1)\n")
        journal = record(files=baseline.content_map(self.repo, ["src/a.py"]))
        (self.repo / "src" / "a.py").unlink()
        self.assertEqual(round2.delta_since(journal, self.repo), ["src/a.py"])


# ================================================================ triggers ===
class Contested(CruxTestCase):
    def test_a_rejected_finding_contests_its_reviewer(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "rejected", "reason": "faux positif"}})
        self.assertIn(round2.T_CONTESTED, result.triggers_for("security"))
        self.assertIn("security", result.selected_reviewers)

    def test_an_unanswered_finding_contests_its_reviewer(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={})
        self.assertIn(round2.T_CONTESTED, result.triggers_for("security"))

    def test_deferred_alone_does_not_contest(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "deferred"}})
        self.assertNotIn(round2.T_CONTESTED, result.triggers_for("security"))
        self.assertNotIn("security", result.selected_reviewers)
        self.assertEqual(result.row("security").exclusion_reason,
                         round2.X_DEFERRED_ONLY)

    def test_deferred_can_still_come_back_through_another_trigger(self):
        """`deferred` is not a ban on the reviewer, only on this reason."""
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "deferred"}},
            delta_selected=["security"],
            delta_evidence={"security": ["external_input"]})
        self.assertEqual(result.triggers_for("security"), [round2.T_NEW_SURFACE])

    def test_accepted_alone_does_not_contest(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "accepted"}})
        self.assertNotIn(round2.T_CONTESTED, result.triggers_for("security"))

    def test_a_merged_finding_contests_every_reviewer_that_made_it(self):
        """`reviewers` is the list to reason with, never the "a+b" string."""
        merged = finding("R1F1", "code-quality")
        merged["reviewer"] = "code-quality+security"
        merged["reviewers"] = ["code-quality", "security"]
        result = plan(previous=record(found=[merged],
                                      selected=("code-quality", "security")))
        self.assertIn(round2.T_CONTESTED, result.triggers_for("security"))
        self.assertIn(round2.T_CONTESTED, result.triggers_for("code-quality"))

    def test_a_reviewer_named_in_never_is_excluded_despite_a_finding(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            never=("security",))
        self.assertNotIn("security", result.selected_reviewers)
        self.assertEqual(result.row("security").exclusion_reason, round2.X_NEVER)


class FixVerification(CruxTestCase):
    def test_accepted_plus_a_delta_asks_the_reviewer_to_verify(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "accepted"}},
            delta=("src/a.py",))
        self.assertIn(round2.T_FIX_VERIFICATION, result.triggers_for("security"))

    def test_a_fix_in_another_file_still_asks_for_verification(self):
        """A correction is routinely cross-file; restricting to the finding's
        own file is how a fix goes unverified."""
        result = plan(
            previous=record(found=[finding("R1F1", "security", file="src/a.py")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "accepted"}},
            delta=("src/totally/elsewhere.py",))
        self.assertIn(round2.T_FIX_VERIFICATION, result.triggers_for("security"))

    def test_accepted_without_a_delta_verifies_nothing(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "accepted"}},
            delta=())
        self.assertEqual(result.selected_reviewers, [])
        self.assertEqual(result.row("security").exclusion_reason,
                         round2.X_NO_TRIGGER)


class NewSurface(CruxTestCase):
    def test_the_delta_router_can_bring_in_an_absent_reviewer(self):
        result = plan(previous=record(selected=("code-quality",)),
                      delta_selected=["security"],
                      delta_evidence={"security": ["external_input"]})
        self.assertIn("security", result.selected_reviewers)
        self.assertEqual(result.triggers_for("security"), [round2.T_NEW_SURFACE])

    def test_the_always_signal_is_not_evidence_about_the_delta(self):
        """`always` fires on every context that exists, so it says nothing
        about this one. code-quality still reaches the plan - as the scope
        authority - but not under `new_surface`."""
        result = plan(previous=record(),
                      delta_selected=["code-quality"],
                      delta_evidence={"code-quality": ["always"]})
        self.assertNotIn(round2.T_NEW_SURFACE,
                         result.triggers_for("code-quality"))

    def test_a_real_signal_on_an_always_reviewer_is_evidence(self):
        result = plan(previous=record(),
                      delta_selected=["code-quality"],
                      delta_evidence={"code-quality": ["always", "magnitude"]})
        self.assertIn(round2.T_NEW_SURFACE, result.triggers_for("code-quality"))

    def test_an_empty_delta_admits_no_new_surface(self):
        result = plan(previous=record(), delta=(),
                      delta_selected=["security"],
                      delta_evidence={"security": ["external_input"]})
        self.assertNotIn("security", result.selected_reviewers)


# =============================================================== authority ===
class ScopeAuthority(CruxTestCase):
    def test_a_non_empty_delta_always_carries_the_authority(self):
        result = plan(previous=record(found=[]), delta=("src/a.py",))
        self.assertIsNotNone(result.scope_authority)
        self.assertIn(result.scope_authority, result.selected_reviewers)
        self.assertIn(round2.T_SCOPE_AUTHORITY,
                      result.triggers_for(result.scope_authority))

    def test_the_authority_is_present_with_no_finding_and_no_signal(self):
        result = plan(previous=record(found=[]), delta=("src/a.py",),
                      delta_selected=[], delta_evidence={})
        self.assertEqual(result.selected_reviewers, ["code-quality"])

    def test_an_empty_delta_needs_no_authority(self):
        result = plan(previous=record(found=[]), delta=())
        self.assertIsNone(result.scope_authority)
        self.assertEqual(result.selected_reviewers, [])

    def test_the_role_does_not_move_because_the_delta_changed_shape(self):
        """Continuity: architecture is a scope authority and is the only
        reviewer triggered, but round 1's authority was code-quality and stays
        it. Deriving the role from the current selection would make the most
        important role in the system a function of the delta's content."""
        result = plan(
            previous=record(found=[finding("R1F1", "architecture")],
                            selected=("code-quality", "architecture")),
            previous_authority="code-quality")
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertIn("code-quality", result.selected_reviewers)
        self.assertEqual(result.triggers_for("code-quality"),
                         [round2.T_SCOPE_AUTHORITY])

    def test_an_already_selected_authority_persona_keeps_the_role(self):
        """With no usable previous authority, the selection decides - and a
        real authority persona in it is preferred to adding another reviewer."""
        result = plan(
            previous=record(found=[finding("R1F1", "architecture")],
                            selected=("code-quality", "architecture")),
            previous_authority=None)
        self.assertEqual(result.scope_authority, "architecture")
        self.assertNotIn("code-quality", result.selected_reviewers)

    def test_an_inherited_previous_authority_does_not_win_continuity(self):
        """`--only security` leaves `security` as an *inherited* authority.
        A persona that never declared the mandate must not keep it by
        continuity when a real one is available."""
        result = plan(
            previous=record(found=[], selected=("security",),
                            authority="security"),
            previous_authority="security")
        self.assertEqual(result.scope_authority, "code-quality")

    def test_without_a_selected_authority_a_real_one_is_added(self):
        result = plan(
            previous=record(found=[finding("R1F1", "security")],
                            selected=("code-quality", "security")))
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertEqual(set(result.selected_reviewers),
                         {"code-quality", "security"})

    def test_the_previous_authority_wins_over_an_arbitrary_one(self):
        result = plan(previous=record(authority="architecture"),
                      previous_authority="architecture",
                      delta=("src/a.py",))
        self.assertEqual(result.scope_authority, "architecture")

    def test_with_no_authority_persona_the_role_is_still_inherited(self):
        """v0.1 behaviour, preserved: the role is never left unassigned, even
        when no available persona declares scope_authority."""
        result = plan(previous=record(found=[finding("R1F1", "security")],
                                      selected=("security",)),
                      candidates=("security",), authorities=set(),
                      previous_authority=None)
        self.assertEqual(result.scope_authority, "security")
        self.assertIn("security", result.selected_reviewers)
        self.assertEqual(result.warnings, [])

    def test_with_no_reviewer_at_all_the_plan_says_so(self):
        result = plan(previous=record(found=[]), candidates=(),
                      authorities=set(), previous_authority=None)
        self.assertIsNone(result.scope_authority)
        self.assertEqual(result.selected_reviewers, [])
        self.assertTrue(any("autorité de périmètre" in w
                            for w in result.warnings))

    def test_enforce_scope_authority_is_idempotent(self):
        self.assertEqual(round2.enforce_scope_authority(["a", "b"], "a"),
                         ["a", "b"])
        self.assertEqual(round2.enforce_scope_authority(["b"], "a"), ["a", "b"])
        self.assertEqual(round2.enforce_scope_authority(["b"], None), ["b"])


# ============================================================ max_selected ===
class MaxSelected(CruxTestCase):
    def contested_everywhere(self, **kwargs):
        found = [finding(f"R1F{i}", name)
                 for i, name in enumerate(MVP, start=1)]
        return plan(previous=record(found=found, selected=MVP),
                    candidates=MVP, **kwargs)

    def test_the_cap_is_applied(self):
        result = self.contested_everywhere(max_selected=2)
        self.assertEqual(len(result.selected_reviewers), 2)

    def test_the_cap_never_evicts_the_authority(self):
        result = self.contested_everywhere(max_selected=1)
        self.assertEqual(result.selected_reviewers, [result.scope_authority])
        self.assertIn(round2.T_SCOPE_AUTHORITY,
                      result.triggers_for(result.scope_authority))

    def test_a_reviewer_dropped_by_the_cap_keeps_its_triggers_on_record(self):
        result = self.contested_everywhere(max_selected=1)
        dropped = [r for r in result.rows if not r.selected]
        self.assertTrue(dropped)
        for row in dropped:
            self.assertEqual(row.exclusion_reason, round2.X_MAX_SELECTED)
            self.assertTrue(row.triggers, "un écarté garde ses déclencheurs")

    def test_the_cap_warns_when_it_bites(self):
        result = self.contested_everywhere(max_selected=1)
        self.assertTrue(any("max_selected" in w for w in result.warnings))

    def test_no_reviewer_is_ever_dropped_without_a_reason(self):
        """Whatever the cap, the two structural rules hold together."""
        for limit in (1, 2, 3, 4, 10):
            with self.subTest(max_selected=limit):
                result = self.contested_everywhere(max_selected=limit)
                for row in result.rows:
                    if row.selected:
                        self.assertTrue(row.triggers)
                    else:
                        self.assertTrue(row.exclusion_reason)
                self.assertIn(result.scope_authority,
                              result.selected_reviewers)

    def test_the_cap_is_deterministic(self):
        first = self.contested_everywhere(max_selected=2).selected_reviewers
        for _ in range(5):
            self.assertEqual(
                self.contested_everywhere(max_selected=2).selected_reviewers,
                first)

    def test_contested_outranks_new_surface_under_the_cap(self):
        """Authority first, then an open obligation, then a new surface."""
        result = plan(
            previous=record(found=[finding("R1F1", "architecture")],
                            selected=("code-quality", "architecture")),
            delta_selected=["security"],
            delta_evidence={"security": ["external_input"]},
            authorities={"code-quality"}, max_selected=2)
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertEqual(result.selected_reviewers,
                         ["code-quality", "architecture"])
        self.assertEqual(result.row("security").exclusion_reason,
                         round2.X_MAX_SELECTED)
        self.assertEqual(result.triggers_for("security"),
                         [round2.T_NEW_SURFACE])

    def test_an_authority_that_is_also_contested_needs_no_extra_slot(self):
        """When round 1's authority is `architecture`, it carries the role
        itself rather than dragging a second authority into the plan."""
        result = plan(
            previous=record(found=[finding("R1F1", "architecture")],
                            selected=("code-quality", "architecture"),
                            authority="architecture"),
            previous_authority="architecture",
            delta_selected=["security"],
            delta_evidence={"security": ["external_input"]},
            max_selected=2)
        self.assertEqual(result.scope_authority, "architecture")
        self.assertEqual(result.selected_reviewers,
                         ["architecture", "security"])


# ========================================== human decisions stay untouched ===
class PromotedFindings(CruxTestCase):
    def promoted_plan(self, status):
        return plan(
            previous=record(
                found=[finding("R1F1", "security", requires_human_decision=True)],
                promoted={"R1F1": "D1"}, selected=("code-quality", "security")),
            decision_status={"D1": status})

    def test_pending_human_does_not_relaunch_the_reviewer(self):
        result = self.promoted_plan(decisions.PENDING)
        self.assertNotIn("security", result.selected_reviewers)
        self.assertEqual(result.row("security").exclusion_reason,
                         round2.X_PENDING_HUMAN)

    def test_a_settled_decision_does_not_contest(self):
        for status in (decisions.APPROVED, decisions.REJECTED, decisions.NOTED,
                       decisions.AUTO_APPROVED, decisions.WITHDRAWN):
            with self.subTest(status=status):
                result = self.promoted_plan(status)
                self.assertNotIn(round2.T_CONTESTED,
                                 result.triggers_for("security"))
                self.assertEqual(result.row("security").exclusion_reason,
                                 round2.X_HUMAN_SETTLED)

    def test_a_mixed_bag_of_dispositions_is_not_given_an_overstated_reason(self):
        """`deferred_only` must mean only deferred. A reviewer holding a
        deferred finding *and* an accepted one gets the honest generic reason."""
        result = plan(
            previous=record(found=[finding("R1F1", "security"),
                                   finding("R1F2", "security")],
                            selected=("code-quality", "security")),
            resolutions={"R1F1": {"status": "deferred"},
                         "R1F2": {"status": "accepted"}},
            delta=())
        self.assertEqual(result.row("security").exclusion_reason,
                         round2.X_NO_TRIGGER)

    def test_a_missing_decision_stays_with_the_human(self):
        """Fail-closed on the decision side: an unreadable or absent ledger
        must not downgrade a promoted finding into something Claude may
        re-review away."""
        result = self.promoted_plan("")
        self.assertNotIn("security", result.selected_reviewers)
        self.assertEqual(result.row("security").exclusion_reason,
                         round2.X_PENDING_HUMAN)

    def test_the_planner_opens_and_closes_nothing(self):
        decisions.propose("s1", title="t", why="w")
        before = [d.to_dict() for d in decisions.load_all("s1")]
        plan(previous=record(
            found=[finding("R1F1", "security", requires_human_decision=True)],
            promoted={"R1F1": "D1"}, selected=("code-quality", "security")),
            decision_status={"D1": decisions.PENDING})
        self.assertEqual([d.to_dict() for d in decisions.load_all("s1")], before)


# ================================================================ identity ===
class Identity(CruxTestCase):
    def test_an_ambiguous_key_is_never_matched(self):
        """Two findings colliding on `finding_key` are absent from the index:
        a lookup returns nothing and the caller treats them as unmatched."""
        one = finding("R1F1", "security", title="Même titre", file="src/a.py")
        two = finding("R1F2", "security", title="Même titre", file="src/a.py")
        self.assertEqual(one["key"], two["key"])
        self.assertEqual(round2.safe_key_index([one, two]), {})

    def test_an_unambiguous_key_is_indexed(self):
        one = finding("R1F1", "security", title="Un", file="src/a.py")
        two = finding("R1F2", "security", title="Deux", file="src/b.py")
        index = round2.safe_key_index([one, two])
        self.assertEqual(set(index), {one["key"], two["key"]})

    def test_a_key_collision_never_settles_the_other_finding(self):
        """Arbitrating one of two colliding findings must leave the other
        unanswered - which keeps its reviewer contested."""
        one = finding("R1F1", "security", title="Même titre", file="src/a.py")
        two = finding("R1F2", "security", title="Même titre", file="src/a.py")
        result = plan(previous=record(found=[one, two],
                                      selected=("code-quality", "security")),
                      resolutions={"R1F1": {"status": "accepted"}})
        self.assertIn(round2.T_CONTESTED, result.triggers_for("security"))

    def test_dispositions_are_keyed_by_id_not_by_content(self):
        one = finding("R1F1", "security", title="Même titre", file="src/a.py")
        two = finding("R1F2", "security", title="Même titre", file="src/a.py")
        got = round2.dispositions(record(found=[one, two]),
                                  {"R1F1": {"status": "accepted"}}, {})
        self.assertEqual(got, {"R1F1": round2.D_ACCEPTED,
                               "R1F2": round2.D_UNANSWERED})


# =============================================== structural guarantees ===
class EveryRowIsExplained(CruxTestCase):
    def full_plan(self):
        return plan(
            previous=record(
                found=[finding("R1F1", "security"),
                       finding("R1F2", "architecture")],
                selected=MVP),
            resolutions={"R1F2": {"status": "deferred"}},
            candidates=MVP + ("tests",))

    def test_every_selected_reviewer_has_at_least_one_trigger(self):
        result = self.full_plan()
        self.assertTrue(result.selected_reviewers)
        for row in result.rows:
            if row.selected:
                self.assertTrue(row.triggers, f"{row.reviewer} sans déclencheur")
                for trigger in row.triggers:
                    self.assertIn(trigger, round2.TRIGGER_ORDER)

    def test_every_skipped_reviewer_has_a_reason(self):
        result = self.full_plan()
        skipped = [r for r in result.rows if not r.selected]
        self.assertTrue(skipped)
        for row in skipped:
            self.assertTrue(row.exclusion_reason, f"{row.reviewer} sans raison")
            self.assertIn(row.exclusion_reason, round2.EXCLUSION_TEXT)

    def test_rows_and_selection_agree(self):
        result = self.full_plan()
        self.assertEqual(
            sorted(r.reviewer for r in result.rows if r.selected),
            sorted(result.selected_reviewers))

    def test_order_candidates_normalises_however_reviewers_arrived(self):
        """The pool is built from the journal, the delta router and the config,
        three sources with three orders. Only the normalised order reaches the
        plan."""
        cfg = config.load(self.repo)
        reference = round2.order_candidates(MVP, cfg)
        for arranged in ((MVP[2], MVP[0], MVP[1]), (MVP[1], MVP[2], MVP[0]),
                         tuple(reversed(MVP))):
            self.assertEqual(round2.order_candidates(arranged, cfg), reference)
        self.assertEqual(reference[0], "code-quality",
                         "reviewers.always vient en premier")

    def test_the_order_reviewers_arrive_in_does_not_change_the_plan(self):
        cfg = config.load(self.repo)
        found = [finding("R1F1", "security"), finding("R1F2", "architecture")]
        reference = None
        for arranged in (MVP, tuple(reversed(MVP)), (MVP[1], MVP[2], MVP[0])):
            for order in (found, list(reversed(found))):
                result = plan(
                    previous=record(found=order, selected=arranged),
                    candidates=round2.order_candidates(arranged, cfg))
                snapshot = (result.selected_reviewers,
                            [r.to_dict() for r in result.rows],
                            result.scope_authority)
                if reference is None:
                    reference = snapshot
                self.assertEqual(snapshot, reference)

    def test_the_plan_serialises_to_a_stable_dict(self):
        payload = self.full_plan().to_dict()
        self.assertEqual(json.loads(json.dumps(payload)), payload)
        self.assertEqual(
            set(payload),
            {"previous_round", "anchor_status", "delta_paths",
             "selected_reviewers", "rows", "fallback", "warnings",
             "scope_authority"})


# =============================================================== overrides ===
class Overrides(CruxTestCase):
    """What `--only`, `--add` and `--all` do, and what 1c must enforce."""

    def test_only_cannot_remove_the_scope_authority(self):
        selected, notes = round2.apply_overrides(
            ["code-quality", "security"], authority="code-quality",
            only=["security"], candidates=MVP)
        self.assertIn("code-quality", selected)
        self.assertTrue(any("override" in n for n in notes))

    def test_only_still_restricts_everything_else(self):
        selected, _ = round2.apply_overrides(
            ["code-quality", "security", "architecture"],
            authority="code-quality", only=["security"], candidates=MVP)
        self.assertEqual(set(selected), {"code-quality", "security"})

    def test_add_only_widens(self):
        selected, notes = round2.apply_overrides(
            ["code-quality"], authority="code-quality", add=["security"],
            candidates=MVP)
        self.assertEqual(selected, ["code-quality", "security"])
        self.assertEqual(notes, [])

    def test_all_keeps_the_authority_by_construction(self):
        selected, notes = round2.apply_overrides(
            [], authority="code-quality", select_all=True, candidates=MVP)
        self.assertEqual(selected, list(MVP))
        self.assertEqual(notes, [])

    def test_never_still_wins_over_add(self):
        selected, _ = round2.apply_overrides(
            ["code-quality"], authority="code-quality", add=["security"],
            candidates=MVP, never=["security"])
        self.assertNotIn("security", selected)

    def test_the_gap_in_review_run_is_real_and_documented(self):
        """1b does not change `review.run()`. This asserts the hole it leaves,
        so 1c cannot close it by accident and call it untested: today `--only`
        reassigns the scope role to a persona declaring scope_authority: false.
        """
        from crux import context, router
        cfg = config.load(self.repo)
        ctx = router.RouteContext(paths=["src/a.py"], changed_lines=40,
                                  files_changed=1)
        result = router.select(ctx, cfg, only=["security"],
                               available=context.available_personas(self.repo))
        self.assertEqual(result.selected, ["security"])
        inherited = context.pick_scope_authority(result.selected, self.repo)
        self.assertEqual(inherited, "security")
        persona = context.load_persona("security", self.repo)
        self.assertFalse(persona.scope_authority)
        # And the pure function 1c needs to close it:
        repaired, _ = round2.apply_overrides(
            result.selected, authority="code-quality", only=["security"],
            candidates=MVP)
        self.assertIn("code-quality", repaired)


# ============================================================ end to end ===
class AgainstARealRepository(CruxTestCase):
    """`round2.build` over real journals, a real tree and the real router."""

    def arm(self, session="s1"):
        cfg = config.load(self.repo)
        state.set_override(session, "code")
        baseline.capture(session, self.repo, cfg)
        return cfg

    def save_round(self, session, cfg, found=(), selected=("code-quality",),
                   promoted=None, round_no=1):
        diff = baseline.compute(session, self.repo, cfg)
        findings.save_round(
            session, round_no, list(found), dict(promoted or {}),
            selected=list(selected), scope_authority="code-quality",
            diff={"fingerprint": diff.fingerprint,
                  "files": baseline.content_map(self.repo, diff.files)})
        st = state.load(session)
        st.record_successful_round(round_no, diff.fingerprint)
        state.save(st)

    def setUp(self):
        super().setUp()
        self.write("README.md", "projet\n")
        self.commit("initial")
        self.cfg = self.arm()

    def test_an_empty_delta_relaunches_nobody(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        result = round2.build("s1", self.repo, self.cfg)
        self.assertTrue(result.anchored)
        self.assertEqual(result.delta_paths, [])
        self.assertEqual(result.selected_reviewers, [])

    def test_a_modification_after_the_round_is_the_delta(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        self.write("src/app.py", "def f():\n    return 2\n")
        result = round2.build("s1", self.repo, self.cfg)
        self.assertEqual(result.delta_paths, ["src/app.py"])
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertIn("code-quality", result.selected_reviewers)

    def test_a_file_created_after_the_round_is_the_delta(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        self.write("src/new.py", "def g():\n    return 2\n")
        result = round2.build("s1", self.repo, self.cfg)
        self.assertIn("src/new.py", result.delta_paths)

    def test_a_file_deleted_after_the_round_is_the_delta(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        (self.repo / "src" / "app.py").unlink()
        result = round2.build("s1", self.repo, self.cfg)
        self.assertEqual(result.delta_paths, ["src/app.py"])

    def test_a_change_made_outside_edit_write_is_seen(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        # As `sed -i` would: straight to the bytes, no PostToolUse hook.
        (self.repo / "src" / "app.py").write_text(
            "def f():\n    return 99\n", encoding="utf-8", newline="\n")
        self.assertEqual(state.edited_paths("s1"), [])
        result = round2.build("s1", self.repo, self.cfg)
        self.assertEqual(result.delta_paths, ["src/app.py"])

    def test_adding_a_subprocess_brings_security_in_through_new_surface(self):
        """The example case: round 1 was code-quality only; the fix introduces
        a subprocess; round 2 must put security on it."""
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        self.write("src/app.py",
                   "import subprocess\n\n\n"
                   "def f(cmd):\n"
                   "    return subprocess.run(cmd, shell=True)\n")
        result = round2.build("s1", self.repo, self.cfg)
        self.assertIn("security", result.selected_reviewers)
        self.assertIn(round2.T_NEW_SURFACE, result.triggers_for("security"))

    def test_deleting_an_untouched_tracked_file_keeps_the_scope_authority(self):
        """R1F1 end to end: the deletion is the only change since the round,
        so it is the only thing standing between the plan and no authority.

        Armed *after* the file was committed, so its baseline is a real blob:
        the file is clean at round 1 and therefore absent from that round's
        journal, which is precisely the case that used to vanish.
        """
        self.write("src/kept.py", "def kept():\n    return 1\n")
        self.commit("fichier suivi, propre à l'armement")
        cfg = self.arm("s2")
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s2", cfg)
        self.assertNotIn("src/kept.py",
                         findings.load_rounds("s2")[0]["diff"]["files"])
        (self.repo / "src" / "kept.py").unlink()
        result = round2.build("s2", self.repo, cfg)
        self.assertIn("src/kept.py", result.delta_paths)
        self.assertEqual(result.scope_authority, "code-quality")
        self.assertIn("code-quality", result.selected_reviewers)

    def test_reintroducing_a_baseline_subprocess_still_reaches_security(self):
        """R2F1: the file is back at its arming baseline, so the diff against
        that baseline is empty and no line reads as added - yet the delta since
        round 1 genuinely reintroduced the call. Routing on the current content
        of the delta paths is what keeps security in."""
        self.write("src/app.py",
                   "import subprocess\n\n\n"
                   "def f(cmd):\n"
                   "    return subprocess.run(cmd)\n")
        self.commit("le subprocess est dans la baseline")
        cfg = self.arm("s2")
        self.write("src/app.py", "def f(cmd):\n    return None\n")
        self.save_round("s2", cfg)
        # The fix restores exactly the committed content.
        self.write("src/app.py",
                   "import subprocess\n\n\n"
                   "def f(cmd):\n"
                   "    return subprocess.run(cmd)\n")
        self.assertTrue(baseline.compute("s2", self.repo, cfg).is_empty,
                        "le diff contre la baseline doit être vide")
        result = round2.build("s2", self.repo, cfg)
        self.assertEqual(result.delta_paths, ["src/app.py"])
        self.assertIn("security", result.selected_reviewers)
        self.assertIn(round2.T_NEW_SURFACE, result.triggers_for("security"))

    def test_the_router_cap_does_not_erase_the_real_exclusion_reason(self):
        """R3F1: `router.select` applies `max_selected` itself, so reading its
        `selected` list lost the reviewers *it* had dropped - they reached the
        plan with no trigger and were reported as unjustified rather than
        capped. The plan applies the cap once, where it also spares the
        authority."""
        self.write(".crux.yml", "reviewers:\n  max_selected: 1\n")
        cfg = config.load(self.repo)
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", cfg)
        self.write("src/app.py",
                   "import subprocess\n\n\n"
                   "def f(cmd):\n"
                   "    return subprocess.run(cmd, shell=True)\n")
        result = round2.build("s1", self.repo, cfg)
        self.assertEqual(result.selected_reviewers, ["code-quality"])
        self.assertEqual(result.scope_authority, "code-quality")
        row = result.row("security")
        self.assertEqual(row.triggers, [round2.T_NEW_SURFACE])
        self.assertEqual(row.exclusion_reason, round2.X_MAX_SELECTED)
        self.assertTrue(any("max_selected" in w for w in result.warnings))

    def test_eligible_is_the_uncapped_superset_of_selected(self):
        from crux import router
        cfg = config.load(self.repo)
        ctx = router.RouteContext(
            paths=["src/auth/login.py", "src/app.py"],
            added_text="import subprocess\ntoken = jwt.encode(p)\n",
            changed_lines=400, files_changed=9, project_has_tests=True)
        wide = router.select(ctx, cfg, available=MVP)
        self.assertEqual(wide.eligible, wide.selected)
        self.assertFalse(wide.capped)

        self.write(".crux.yml", "reviewers:\n  max_selected: 1\n")
        narrow = router.select(ctx, config.load(self.repo), available=MVP)
        self.assertTrue(narrow.capped)
        self.assertEqual(len(narrow.selected), 1)
        self.assertEqual(narrow.eligible[:1], narrow.selected)
        self.assertLess(len(narrow.selected), len(narrow.eligible))

    def test_a_binary_delta_file_does_not_reach_the_router_as_text(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        (self.repo / "src" / "blob.bin").write_bytes(b"\x00\x01subprocess\x00")
        self.write("src/app.py", "def f():\n    return 2\n")
        result = round2.build("s1", self.repo, self.cfg)
        self.assertNotIn("security", result.selected_reviewers)

    def test_a_single_unreadable_journal_still_yields_a_fallback_plan(self):
        """R2F2: `load_rounds` parses it to nothing, so the session looks like
        it never had a round. `has_round_history` reads the directory."""
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        path = paths.session_dir("s1") / "findings" / "round-1.json"
        path.write_text("{ tronqué", encoding="utf-8")
        self.assertEqual(findings.load_rounds("s1"), [])
        self.assertTrue(round2.has_round_history("s1"))
        result = round2.build("s1", self.repo, self.cfg)
        self.assertEqual(result.anchor_status, round2.UNREADABLE)
        self.assertEqual(result.fallback, round2.FALLBACK_FULL_ROUTER)

    def test_a_legacy_journal_falls_back_to_the_full_router(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        path = paths.session_dir("s1") / "findings" / "round-1.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        result = round2.build("s1", self.repo, self.cfg)
        self.assertEqual(result.fallback, round2.FALLBACK_FULL_ROUTER)
        self.assertEqual(result.delta_paths, [])
        self.assertTrue(any("non ancrable" in w for w in result.warnings))

    def test_a_reviewer_outside_auto_stays_a_candidate_when_it_left_a_finding(self):
        """`crux review --only tests` can leave an unanswered finding; the
        reviewer that raised it is the one that must be asked again."""
        self.write(".crux.yml", "reviewers:\n  auto: [architecture]\n")
        cfg = config.load(self.repo)
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", cfg,
                        found=[findings.Finding.from_dict(
                            finding("R1F1", "security"))],
                        selected=("code-quality", "security"))
        self.write("src/app.py", "def f():\n    return 2\n")
        result = round2.build("s1", self.repo, cfg)
        self.assertIn("security", result.selected_reviewers)
        self.assertIn(round2.T_CONTESTED, result.triggers_for("security"))

    def test_no_codex_call_is_made_by_the_planner(self):
        from crux import codex

        def explode(*args, **kwargs):
            raise AssertionError("1b ne doit exécuter aucun Codex")

        originals = (codex.run_many, codex.ensure_available)
        codex.run_many, codex.ensure_available = explode, explode
        try:
            self.write("src/app.py", "import subprocess\n")
            self.save_round("s1", self.cfg)
            self.write("src/app.py", "import subprocess\nx = 1\n")
            result = round2.build("s1", self.repo, self.cfg)
            self.assertTrue(result.delta_paths)
        finally:
            codex.run_many, codex.ensure_available = originals

    def test_the_planner_never_imports_codex(self):
        """Structural, not behavioural: the module must not even be able to."""
        import ast

        path = (Path(__file__).resolve().parents[1]
                / "src" / "crux" / "round2.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[-1] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(a.name.split(".")[-1] for a in node.names)
                if node.module:
                    imported.add(node.module.split(".")[-1])
        self.assertNotIn("codex", imported)
        self.assertNotIn("review", imported,
                         "importer review réintroduirait codex par transitivité")

    def test_the_planner_writes_nothing_to_the_session(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.save_round("s1", self.cfg)
        self.write("src/app.py", "def f():\n    return 2\n")
        directory = paths.session_dir("s1")
        before = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        round2.build("s1", self.repo, self.cfg)
        after = {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}
        self.assertEqual(after, before)


# ================================================== crux route --explain ===
class RouteExplain(CruxTestCase):
    def setUp(self):
        super().setUp()
        self.write("README.md", "projet\n")
        self.commit("initial")
        self.cfg = config.load(self.repo)
        state.set_override("s1", "code")
        baseline.capture("s1", self.repo, self.cfg)
        self._cwd = Path.cwd()
        import os
        os.chdir(self.repo)

    def tearDown(self):
        import os
        os.chdir(self._cwd)
        super().tearDown()

    def run_cli(self, *argv) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            cli.main(list(argv))
        return buffer.getvalue()

    def seed_round(self, found=(), selected=("code-quality",)):
        diff = baseline.compute("s1", self.repo, self.cfg)
        findings.save_round(
            "s1", 1, list(found), {}, selected=list(selected),
            scope_authority="code-quality",
            diff={"fingerprint": diff.fingerprint,
                  "files": baseline.content_map(self.repo, diff.files)})
        st = state.load("s1")
        st.record_successful_round(1, diff.fingerprint)
        state.save(st)

    def test_plain_route_is_unchanged_and_shows_no_plan(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round()
        self.write("src/app.py", "def f():\n    return 2\n")
        out = self.run_cli("route", "--session", "s1")
        self.assertIn("Signaux déclenchés", out)
        self.assertNotIn("Plan round", out)

    def test_explain_names_the_triggers_and_the_exclusions(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round(
            found=[findings.Finding.from_dict(
                finding("R1F1", "architecture", file="src/app.py"))],
            selected=("code-quality", "architecture"))
        self.write("src/app.py",
                   "import subprocess\n\n\n"
                   "def f(cmd):\n"
                   "    return subprocess.run(cmd, shell=True)\n")
        out = self.run_cli("route", "--session", "s1", "--explain")
        self.assertIn("Plan round 2", out)
        self.assertIn("ancrage : round 1", out)
        self.assertIn("Delta : 1 fichier(s) — src/app.py", out)
        for line in out.splitlines():
            if line.startswith("architecture") and "SELECTED" in line:
                self.assertIn(round2.T_CONTESTED, line)
                break
        else:
            self.fail(f"architecture non SELECTED dans :\n{out}")
        self.assertIn("security", out)
        self.assertIn(round2.T_NEW_SURFACE, out)

    def test_explain_names_a_reason_for_every_skipped_reviewer(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round(
            found=[findings.Finding.from_dict(
                finding("R1F1", "architecture", file="src/app.py"))],
            selected=("code-quality", "architecture"))
        findings.resolve("s1", "R1F1", "deferred")
        self.write("src/app.py", "def f():\n    return 2\n")
        out = self.run_cli("route", "--session", "s1", "--explain")
        skipped = [l for l in out.splitlines() if "SKIPPED" in l]
        self.assertTrue(skipped)
        for line in skipped:
            self.assertTrue(
                any(text in line for text in round2.EXCLUSION_TEXT.values()),
                f"raison non nommée : {line!r}")
        self.assertTrue(any("architecture" in l and "deferred" in l
                            for l in skipped), out)

    def test_explain_is_deterministic(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round()
        self.write("src/app.py", "def f():\n    return 2\n")
        first = self.run_cli("route", "--session", "s1", "--explain")
        for _ in range(3):
            self.assertEqual(self.run_cli("route", "--session", "s1", "--explain"),
                             first)

    def test_explain_announces_the_fallback_on_a_legacy_journal(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round()
        path = paths.session_dir("s1") / "findings" / "round-1.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["schema"] = 1
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.write("src/app.py", "def f():\n    return 2\n")
        out = self.run_cli("route", "--session", "s1", "--explain")
        self.assertIn(round2.FALLBACK_FULL_ROUTER, out)
        self.assertIn("non ancrable", out)

    def test_an_empty_session_diff_does_not_hide_the_plan(self):
        """R1F2: reverting a file to its baseline leaves nothing to review
        against the baseline, while being a real change against the round."""
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round()
        (self.repo / "src" / "app.py").unlink()
        diff = baseline.compute("s1", self.repo, self.cfg)
        self.assertTrue(diff.is_empty, "le diff de session doit être vide")
        out = self.run_cli("route", "--session", "s1", "--explain")
        self.assertIn("Aucune modification de session", out)
        self.assertIn("Plan round 2", out)
        self.assertIn("src/app.py", out)
        self.assertIn("Autorité de périmètre : code-quality", out)

    def test_explain_reports_the_fallback_when_the_only_journal_is_corrupt(self):
        """R2F2 through the CLI: gating on parsed rounds went silent on
        precisely the case that needed reporting."""
        self.write("src/app.py", "def f():\n    return 1\n")
        self.seed_round()
        path = paths.session_dir("s1") / "findings" / "round-1.json"
        path.write_text("{ tronqué", encoding="utf-8")
        self.write("src/app.py", "def f():\n    return 2\n")
        out = self.run_cli("route", "--session", "s1", "--explain")
        self.assertIn("Plan round", out)
        self.assertIn(round2.FALLBACK_FULL_ROUTER, out)
        self.assertIn("illisible", out)

    def test_explain_before_any_round_shows_only_the_v01_answer(self):
        self.write("src/app.py", "def f():\n    return 1\n")
        out = self.run_cli("route", "--session", "s1", "--explain")
        self.assertIn("Signaux déclenchés", out)
        self.assertNotIn("Plan round", out)
