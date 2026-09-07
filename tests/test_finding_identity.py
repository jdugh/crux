"""Sub-milestone 1a: the three identities of a finding.

Nothing here selects reviewers or runs a second round - that is 1b and 1c. What
is pinned here is identity, because the round 2 targeting that comes next is only
as sound as its answer to "is this the same finding as last round, and has it
been arbitrated?".

The bug this file exists to keep closed: ids used to restart at ``F1`` every
round, and ``resolutions.jsonl`` is keyed by id, so a round 1 arbitration made a
round 2 finding look already settled.
"""

from __future__ import annotations

import json

from ._support import CruxTestCase

from crux import decisions, findings, paths, review


def finding(**kwargs) -> findings.Finding:
    base = dict(id="R1F1", reviewer="code-quality", severity="high",
                title="Bug d'index", description="Hors bornes.", file="a.py")
    base.update(kwargs)
    return findings.Finding(**base)


class FindingKey(CruxTestCase):
    """`finding-key-v1`: what it is built from, and what it must ignore."""

    def test_key_is_deterministic(self):
        self.assertEqual(findings.finding_key("src/a.py", "Fuite de handle"),
                         findings.finding_key("src/a.py", "Fuite de handle"))

    def test_key_ignores_path_separator_and_prefix(self):
        reference = findings.finding_key("src/a.py", "T")
        self.assertEqual(findings.finding_key("src\\a.py", "T"), reference)
        self.assertEqual(findings.finding_key("./src/a.py", "T"), reference)
        self.assertEqual(findings.finding_key("/src/a.py", "T"), reference)

    def test_key_ignores_title_punctuation_and_case(self):
        self.assertEqual(findings.finding_key("a.py", "Bug d'index !"),
                         findings.finding_key("a.py", "bug  d index"))

    def test_key_ignores_the_line_number(self):
        """A fix inserted above a finding moves its line, not its identity."""
        self.assertEqual(finding(line=12).key, finding(line=97).key)

    def test_key_ignores_the_reviewer(self):
        """Two personas making the same remark are one remark."""
        self.assertEqual(finding(reviewer="security").key,
                         finding(reviewer="architecture").key)

    def test_key_ignores_description_suggestion_and_severity(self):
        """All three are rewritten after a correction; identity must not move."""
        self.assertEqual(
            finding(description="autre texte", suggestion="autre piste",
                    severity="low").key,
            finding().key)

    def test_key_separates_two_files_with_the_same_title(self):
        self.assertNotEqual(finding(file="a.py").key, finding(file="b.py").key)

    def test_key_separates_two_titles_in_the_same_file(self):
        self.assertNotEqual(finding(title="Fuite").key,
                            finding(title="Course").key)

    def test_key_is_case_sensitive_on_paths(self):
        """Case distinguishes real files on Linux; collapsing it would merge them."""
        self.assertNotEqual(findings.finding_key("src/A.py", "T"),
                            findings.finding_key("src/a.py", "T"))

    def test_key_is_versioned(self):
        self.assertEqual(findings.FINDING_KEY_VERSION, "finding-key-v1")


class FindingIds(CruxTestCase):
    def test_ids_are_scoped_by_round(self):
        self.assertEqual(findings.make_finding_id(2, 3), "R2F3")

    def test_short_and_round_are_recoverable(self):
        self.assertEqual(findings.short_id("R2F3"), "F3")
        self.assertEqual(findings.round_of("R2F3"), 2)

    def test_a_legacy_id_has_no_round(self):
        self.assertEqual(findings.short_id("F3"), "F3")
        self.assertIsNone(findings.round_of("F3"))

    def test_parser_mints_round_scoped_ids(self):
        payload = {"verdict": "changes_requested", "summary": "s", "findings": [
            {"severity": "high", "title": "A", "description": "d"},
            {"severity": "low", "title": "B", "description": "d"},
        ]}
        parsed = findings.parse_reviewer_payload("security", payload, round_no=2)
        self.assertEqual([f.id for f in parsed.findings], ["R2F1", "R2F2"])

    def test_the_same_ordinal_in_two_rounds_is_two_distinct_ids(self):
        payload = {"findings": [{"severity": "high", "title": "A",
                                 "description": "d"}]}
        first = findings.parse_reviewer_payload("security", payload, round_no=1)
        second = findings.parse_reviewer_payload("security", payload, round_no=2)
        self.assertNotEqual(first.findings[0].id, second.findings[0].id)

    def test_start_index_still_offsets_within_a_round(self):
        payload = {"findings": [{"severity": "high", "title": "A",
                                 "description": "d"}]}
        parsed = findings.parse_reviewer_payload("tests", payload,
                                                 start_index=4, round_no=1)
        self.assertEqual(parsed.findings[0].id, "R1F5")


class Reviewers(CruxTestCase):
    def test_reviewers_is_backfilled_from_the_display_string(self):
        self.assertEqual(finding(reviewer="code-quality").reviewers,
                         ["code-quality"])

    def test_dedupe_merges_the_reviewer_list(self):
        payload = {"findings": [{"severity": "high", "title": "Fuite",
                                 "description": "d", "file": "a.py"}]}
        one = findings.parse_reviewer_payload("security", payload, round_no=1)
        two = findings.parse_reviewer_payload("architecture", payload,
                                              start_index=1, round_no=1)
        merged, notes = findings.dedupe([one, two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].reviewers, ["security", "architecture"])
        self.assertEqual(merged[0].reviewer, "security+architecture")
        self.assertTrue(notes)

    def test_dedupe_keys_on_the_finding_key(self):
        """One identity function, not two that could drift apart."""
        payload = {"findings": [{"severity": "high", "title": "Fuite !",
                                 "description": "d", "file": "./a.py"}]}
        other = {"findings": [{"severity": "critical", "title": "fuite",
                               "description": "autre", "file": "a.py"}]}
        one = findings.parse_reviewer_payload("security", payload, round_no=1)
        two = findings.parse_reviewer_payload("tests", other, start_index=1,
                                              round_no=1)
        merged, _ = findings.dedupe([one, two])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].severity, "critical")


class RoundJournal(CruxTestCase):
    def test_round_record_carries_selection_authority_and_diff(self):
        findings.save_round(
            "s1", 1, [finding()], {},
            selected=["code-quality", "security"],
            scope_authority="code-quality",
            diff={"fingerprint": "abc", "files": {"a.py": "deadbeef"}})
        record = findings.load_rounds("s1")[0]
        self.assertEqual(record["selected"], ["code-quality", "security"])
        self.assertEqual(record["scope_authority"], "code-quality")
        self.assertEqual(record["diff"]["fingerprint"], "abc")
        self.assertEqual(record["diff"]["files"]["a.py"], "deadbeef")

    def test_persisted_findings_carry_key_and_reviewers(self):
        findings.save_round("s1", 1, [finding()], {})
        raw = findings.load_rounds("s1")[0]["findings"][0]
        self.assertEqual(raw["key"], finding().key)
        self.assertEqual(raw["reviewers"], ["code-quality"])

    def test_a_v01_round_file_still_loads_and_is_backfilled(self):
        """No migration: a journal written before v0.2 must replay as-is."""
        legacy = {
            "round": 1, "saved_at": "2026-01-01T00:00:00Z",
            "findings": [{"id": "F1", "reviewer": "code-quality+security",
                          "severity": "high", "title": "Bug d'index",
                          "description": "Hors bornes.", "file": "a.py"}],
            "promoted": {},
        }
        path = findings.findings_path("s1", 1)
        path.write_text(json.dumps(legacy), encoding="utf-8")
        record = findings.load_rounds("s1")[0]
        self.assertEqual(record["selected"], [])
        self.assertIsNone(record["scope_authority"])
        self.assertEqual(record["diff"], {"fingerprint": "", "files": {}})
        raw = record["findings"][0]
        self.assertEqual(raw["key"], finding().key)
        self.assertEqual(raw["reviewers"], ["code-quality", "security"])

    def test_from_dict_replays_a_legacy_finding(self):
        replayed = findings.Finding.from_dict(
            {"id": "F1", "reviewer": "security", "severity": "high",
             "title": "Bug d'index", "description": "d", "file": "a.py",
             "unknown_future_field": 1})
        self.assertEqual(replayed.reviewers, ["security"])
        self.assertEqual(replayed.key, finding().key)

    def test_rounds_are_ordered_numerically_beyond_nine(self):
        for round_no in (1, 2, 10):
            findings.save_round("s1", round_no,
                                [finding(id=f"R{round_no}F1")], {})
        self.assertEqual([r["round"] for r in findings.load_rounds("s1")],
                         [1, 2, 10])


class ResolutionAcrossRounds(CruxTestCase):
    """The regression that motivated the whole sub-milestone."""

    def setUp(self):
        super().setUp()
        findings.save_round("s1", 1, [finding(id="R1F1")], {})
        findings.save_round(
            "s1", 2, [finding(id="R2F1", title="Autre bug", file="b.py")], {})

    def test_a_round_1_resolution_does_not_settle_a_round_2_finding(self):
        findings.resolve("s1", "R1F1", "accepted")
        settled = findings.resolutions("s1")
        self.assertIn("R1F1", settled)
        self.assertNotIn("R2F1", settled)

    def test_a_short_id_matching_two_rounds_is_refused(self):
        with self.assertRaises(findings.AmbiguousFinding) as ctx:
            findings.resolve("s1", "F1", "accepted")
        self.assertEqual(sorted(ctx.exception.candidates), ["R1F1", "R2F1"])
        self.assertEqual(findings.resolutions("s1"), {})

    def test_the_ambiguity_message_names_the_full_ids(self):
        with self.assertRaises(findings.AmbiguousFinding) as ctx:
            findings.find_finding("s1", "F1")
        message = str(ctx.exception)
        self.assertIn("R1F1", message)
        self.assertIn("R2F1", message)

    def test_a_full_id_is_never_ambiguous(self):
        self.assertEqual(findings.find_finding("s1", "R2F1")["file"], "b.py")

    def test_an_unknown_reference_is_still_a_key_error(self):
        self.assertIsNone(findings.find_finding("s1", "R9F9"))


class ShortIdWhenUnambiguous(CruxTestCase):
    def setUp(self):
        super().setUp()
        findings.save_round("s1", 1, [finding(id="R1F1")], {})

    def test_a_short_id_is_accepted_while_it_designates_one_finding(self):
        entry = findings.resolve("s1", "F1", "accepted")
        self.assertEqual(entry["id"], "R1F1")

    def test_the_resolution_is_written_under_the_full_id(self):
        findings.resolve("s1", "F1", "accepted")
        self.assertIn("R1F1", findings.resolutions("s1"))
        self.assertNotIn("F1", findings.resolutions("s1"))

    def test_the_resolution_records_the_reference_actually_given(self):
        findings.resolve("s1", "F1", "rejected", reason="faux positif")
        entry = findings.resolutions("s1")["R1F1"]
        self.assertEqual(entry["given"], "F1")
        self.assertEqual(entry["round"], 1)
        self.assertEqual(entry["key"], finding().key)

    def test_a_legacy_id_still_resolves_by_itself(self):
        """v0.1 sessions in flight keep working."""
        findings.save_round("legacy", 1, [finding(id="F1")], {})
        self.assertEqual(findings.resolve("legacy", "F1", "accepted")["id"],
                         "F1")


class PromotionKeepsIdentity(CruxTestCase):
    def test_a_decision_promoted_from_a_finding_carries_its_key(self):
        promoted = finding(requires_human_decision=True)
        decision = decisions.propose(
            "s1", title=promoted.title, why=promoted.description,
            from_findings=[promoted.id], finding_key=promoted.key)
        self.assertEqual(decisions.get("s1", decision.id).finding_key,
                         promoted.key)

    def test_a_decision_without_a_finding_has_no_key(self):
        """An unknown identity must never be able to suppress a question."""
        decision = decisions.propose("s1", title="T", why="W")
        self.assertEqual(decisions.get("s1", decision.id).finding_key, "")

    def test_a_v01_decision_record_still_loads(self):
        decision = decisions.propose("s1", title="T", why="W")
        path = decisions.ledger_path("s1")
        record = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        record.pop("finding_key")
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
        self.assertEqual(decisions.get("s1", decision.id).finding_key, "")


class ContentMap(CruxTestCase):
    """The per-round snapshot a later round diffs against."""

    def test_it_hashes_the_working_tree(self):
        self.write("a.py", "X = 1\n")
        first = review.content_map(self.repo, ["a.py"])
        self.assertTrue(first["a.py"])
        self.write("a.py", "X = 2\n")
        self.assertNotEqual(review.content_map(self.repo, ["a.py"])["a.py"],
                            first["a.py"])

    def test_a_missing_file_maps_to_none(self):
        self.assertIsNone(review.content_map(self.repo, ["gone.py"])["gone.py"])

    def test_it_does_not_depend_on_the_tool_that_wrote_the_file(self):
        """Baseline independence, v0.1 constraint: sed and Edit look alike."""
        self.write("a.py", "X = 1\n")
        by_editor = review.content_map(self.repo, ["a.py"])["a.py"]
        (self.repo / "a.py").write_bytes(b"X = 1\n")
        self.assertEqual(review.content_map(self.repo, ["a.py"])["a.py"],
                         by_editor)

    def test_a_directory_in_the_way_is_not_an_error(self):
        (self.repo / "d").mkdir()
        self.assertIsNone(review.content_map(self.repo, ["d"])["d"])


class ReviewWritesTheEnrichedJournal(CruxTestCase):
    """The integration point 1b will read from.

    Asserting ``save_round`` alone was not enough: what matters is that
    ``review.run`` actually hands it the selection, the authority and the
    content map. Codex is stubbed at the subprocess boundary, as elsewhere.
    """

    def setUp(self):
        super().setUp()
        from crux import baseline, codex, config, intent, state
        self.write("src/app.py", "APP = 1\n")
        self.commit("init")
        self.cfg = config.load(self.repo)
        baseline.capture("s1", self.repo, self.cfg)
        intent.record_prompt("s1", "Ajoute un compteur.")
        self.write("src/app.py", "APP = 2\n")
        state.record_edit("s1", "src/app.py", None)

        real_run_many, real_ensure = codex.run_many, codex.ensure_available
        codex.run_many = self.fake_run_many
        codex.ensure_available = lambda cfg: None
        self.addCleanup(setattr, codex, "run_many", real_run_many)
        self.addCleanup(setattr, codex, "ensure_available", real_ensure)

    def fake_run_many(self, jobs, *args, **kwargs):
        from crux import codex
        return [codex.CodexOutcome(reviewer=reviewer, duration=0.1, payload={
            "verdict": "changes_requested", "reviewer": reviewer,
            "summary": "s",
            "findings": [{"severity": "high", "confidence": "high",
                          "title": f"Remarque de {reviewer}",
                          "description": "d", "file": "src/app.py", "line": 1}],
        }) for reviewer, _prompt in jobs]

    def journal(self):
        review.run("s1", self.repo, self.cfg)
        return findings.load_rounds("s1")[0]

    def test_the_journal_records_the_selected_reviewers(self):
        record = self.journal()
        self.assertTrue(record["selected"])
        self.assertIn("code-quality", record["selected"])

    def test_the_journal_records_the_scope_authority(self):
        self.assertEqual(self.journal()["scope_authority"], "code-quality")

    def test_the_journal_records_the_fingerprint_and_content_map(self):
        record = self.journal()
        self.assertTrue(record["diff"]["fingerprint"])
        self.assertEqual(set(record["diff"]["files"]), {"src/app.py"})
        self.assertTrue(record["diff"]["files"]["src/app.py"])

    def test_the_content_map_matches_the_tree_at_review_time(self):
        record = self.journal()
        self.assertEqual(record["diff"]["files"]["src/app.py"],
                         review.content_map(self.repo, ["src/app.py"])["src/app.py"])

    def test_findings_from_a_real_run_carry_round_scoped_ids(self):
        record = self.journal()
        self.assertTrue(record["findings"])
        for raw in record["findings"]:
            self.assertEqual(findings.round_of(raw["id"]), 1)
            self.assertTrue(raw["key"])
            self.assertTrue(raw["reviewers"])


class KeyCollisions(CruxTestCase):
    """Same file, same normalised title, two genuinely different findings.

    `finding-key-v1` is built from two fields, so it CAN collide. The rule that
    follows: a false equivalence is worse than a duplicate, because it would let
    one finding's arbitration settle another, disguise a fresh remark as a
    reiteration, or suppress a human decision nobody was ever asked. When the
    schema cannot prove two findings are the same, they stay apart.
    """

    COLLIDING = {"verdict": "changes_requested", "summary": "s", "findings": [
        {"severity": "high", "confidence": "high", "file": "src/app.py",
         "line": 12, "title": "Gestion d'erreur absente",
         "description": "Le bloc try autour de l'ouverture avale l'exception."},
        {"severity": "high", "confidence": "high", "file": "src/app.py",
         "line": 88, "title": "gestion d'erreur absente !",
         "description": "La fermeture du socket n'est jamais tentée."},
    ]}

    def merged(self, *payloads):
        results = []
        index = 0
        for reviewer, payload in payloads:
            parsed = findings.parse_reviewer_payload(
                reviewer, payload, start_index=index, round_no=1)
            index += len(parsed.findings)
            results.append(parsed)
        return findings.dedupe(results)[0]

    def test_the_two_findings_do_collide_on_the_key(self):
        """The premise: without a discriminator they would be one."""
        merged = self.merged(("code-quality", self.COLLIDING))
        self.assertEqual(merged[0].key, merged[1].key)

    def test_they_are_not_merged(self):
        merged = self.merged(("code-quality", self.COLLIDING))
        self.assertEqual(len(merged), 2)
        self.assertEqual([f.line for f in merged], [12, 88])

    def test_they_keep_distinct_ids(self):
        merged = self.merged(("code-quality", self.COLLIDING))
        self.assertEqual([f.id for f in merged], ["R1F1", "R1F2"])

    def test_they_have_distinct_identities(self):
        merged = self.merged(("code-quality", self.COLLIDING))
        self.assertEqual([f.occurrence for f in merged], [1, 2])
        self.assertNotEqual(merged[0].identity, merged[1].identity)

    def test_arbitrating_one_does_not_settle_the_other(self):
        """The consequence that matters most."""
        merged = self.merged(("code-quality", self.COLLIDING))
        findings.save_round("s1", 1, merged, {})
        findings.resolve("s1", "R1F1", "rejected", reason="faux positif")
        self.assertEqual([f.id for f in findings.unarbitrated("s1", "high")],
                         ["R1F2"])

    def test_a_second_reviewer_does_not_merge_into_an_ambiguous_key(self):
        """Once the key is claimed twice, no slot can be chosen without guessing."""
        single = {"findings": [dict(self.COLLIDING["findings"][0])]}
        merged = self.merged(("code-quality", self.COLLIDING),
                             ("security", single))
        self.assertEqual(len(merged), 3)
        self.assertEqual([f.reviewers for f in merged],
                         [["code-quality"], ["code-quality"], ["security"]])

    def test_a_colliding_key_is_reported_as_ambiguous(self):
        merged = self.merged(("code-quality", self.COLLIDING))
        self.assertEqual(findings.ambiguous_keys(merged), {merged[0].key})

    def test_a_clean_round_has_no_ambiguous_key(self):
        payload = {"findings": [
            {"severity": "high", "title": "A", "description": "d",
             "file": "a.py"},
            {"severity": "high", "title": "B", "description": "d",
             "file": "a.py"}]}
        self.assertEqual(findings.ambiguous_keys(self.merged(("cq", payload))),
                         set())

    def test_the_occurrence_survives_the_round_journal(self):
        merged = self.merged(("code-quality", self.COLLIDING))
        findings.save_round("s1", 1, merged, {})
        raws = findings.load_rounds("s1")[0]["findings"]
        self.assertEqual([r["occurrence"] for r in raws], [1, 2])
        self.assertNotEqual(raws[0]["identity"], raws[1]["identity"])

    def test_a_v01_finding_replays_with_occurrence_one(self):
        legacy = {"round": 1, "findings": [
            {"id": "F1", "reviewer": "cq", "severity": "high", "title": "T",
             "description": "d", "file": "a.py"}], "promoted": {}}
        findings.findings_path("s1", 1).write_text(json.dumps(legacy),
                                                   encoding="utf-8")
        raw = findings.load_rounds("s1")[0]["findings"][0]
        self.assertEqual(raw["occurrence"], 1)
        self.assertEqual(raw["identity"], raw["key"] + "#1")


class AmbiguousKeyBlocksCrossReviewerMerge(CruxTestCase):
    """Merging needs the key to designate exactly one finding.

    Once one reviewer has produced two findings sharing a key, a third finding
    from another reviewer cannot be attributed: it may answer either. Folding it
    into the first slot would be a guess, and a wrong guess makes one finding's
    arbitration settle another. It stays separate.
    """

    TWO = {"findings": [
        {"severity": "high", "confidence": "high", "file": "src/api.py",
         "line": 10, "title": "Validation manquante",
         "description": "Le corps de la requête n'est pas validé."},
        {"severity": "high", "confidence": "high", "file": "src/api.py",
         "line": 64, "title": "validation manquante !",
         "description": "Les paramètres de requête ne sont pas validés."},
    ]}
    ONE = {"findings": [
        {"severity": "high", "confidence": "high", "file": "src/api.py",
         "line": 10, "title": "Validation manquante",
         "description": "Entrée non validée, angle sécurité."},
    ]}

    def merged(self, *payloads):
        results, index = [], 0
        for reviewer, payload in payloads:
            parsed = findings.parse_reviewer_payload(
                reviewer, payload, start_index=index, round_no=1)
            index += len(parsed.findings)
            results.append(parsed)
        return findings.dedupe(results)[0]

    def test_a_third_finding_on_an_ambiguous_key_is_not_merged(self):
        merged = self.merged(("code-quality", self.TWO), ("security", self.ONE))
        self.assertEqual(len(merged), 3)

    def test_it_keeps_its_own_reviewer(self):
        merged = self.merged(("code-quality", self.TWO), ("security", self.ONE))
        self.assertEqual([f.reviewers for f in merged],
                         [["code-quality"], ["code-quality"], ["security"]])

    def test_it_gets_its_own_occurrence_and_identity(self):
        merged = self.merged(("code-quality", self.TWO), ("security", self.ONE))
        self.assertEqual([f.occurrence for f in merged], [1, 2, 3])
        self.assertEqual(len({f.identity for f in merged}), 3)

    def test_arbitrating_one_settles_none_of_the_others(self):
        merged = self.merged(("code-quality", self.TWO), ("security", self.ONE))
        findings.save_round("s1", 1, merged, {})
        findings.resolve("s1", merged[0].id, "rejected", reason="faux positif")
        self.assertEqual(
            [f.id for f in findings.unarbitrated("s1", "high")],
            [merged[1].id, merged[2].id])

    def test_a_single_prior_finding_still_merges_across_reviewers(self):
        """The regression guard: the rule must not disable ordinary dedup."""
        merged = self.merged(("code-quality", self.ONE), ("security", self.ONE))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].reviewers, ["code-quality", "security"])

    def test_a_reviewer_never_merges_with_itself(self):
        merged = self.merged(("code-quality", self.ONE), ("code-quality", self.ONE))
        self.assertEqual(len(merged), 2)

    def test_order_of_reviewers_does_not_change_the_outcome(self):
        """Ambiguity is a property of the key, not of arrival order."""
        first = self.merged(("code-quality", self.TWO), ("security", self.ONE))
        second = self.merged(("security", self.ONE), ("code-quality", self.TWO))
        self.assertEqual(len(first), len(second))
        self.assertEqual(sorted(len(f.reviewers) for f in first),
                         sorted(len(f.reviewers) for f in second))
