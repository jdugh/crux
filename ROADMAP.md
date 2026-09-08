# Crux v0.2 — Roadmap

## Principe directeur

Crux n'a pas vocation à remplacer `codex-plugin-cc` ni à réimplémenter toute la plomberie Claude ↔ Codex.

Crux se concentre sur :

* orchestration automatique des reviews ;
* sélection déterministe des reviewers ;
* autorité humaine sur le périmètre ;
* provenance des décisions ;
* continuité de l'intention ;
* reviews multi-rounds ciblées ;
* gouvernance des agents.

Avant toute nouvelle fonctionnalité générique d'orchestration, vérifier les solutions existantes, notamment `codex-plugin-cc` et Viper 2.0, puis documenter explicitement pourquoi Crux réutilise, adapte ou implémente sa propre solution.

---

## 1 — Targeted Round 2

### 1a — Finding identity & attempt semantics

Status: DONE

* IDs scopés par round ;
* finding identity ;
* collisions sûres ;
* round vs tentative ;
* arbitration obligatoire des findings bloquants ;
* capability probing tri-state ;
* journaux anchorables.

### 1b — Round-2 planner

Status: DONE

* delta déterministe ;
* contested ;
* fix_verification ;
* new_surface ;
* scope_authority ;
* max_selected ;
* overrides sûrs ;
* `crux route --explain`;
* moteur pur, sans Codex.

### 1c — Round-2 execution

Status: DONE

Planner branché dans `review.run()` :

round 1
→ findings
→ arbitrages/corrections Claude
→ delta
→ sélection ciblée
→ round N
→ consolidation
→ arrêt déterministe.

Acquis :

* round 1 inchangé (routeur v0.1) ; round N > 1 piloté par `round2.build` ;
* seuls les reviewers sélectionnés atteignent `codex.run_many` ;
* repli `full_router` journalisé, sans jamais lever `max_rounds` ;
* overrides `--only` / `--add` / `--all` passés par `apply_overrides`, autorité
  de périmètre réintroduite en dernier — aucun override accessible à Claude ne
  peut la retirer ;
* `max_selected` appliqué une seule fois ;
* `max_rounds` = nombre de rounds EXPLOITABLES, plafond absolu appliqué dans
  `review.run()` et non plus seulement dans le hook Stop ;
* arrêt `no_delta` : le plafond est un maximum, jamais une cible ;
* réitération d'un finding rejeté = insistance visible, non bloquante, bornée ;
* décision humaine déjà tranchée jamais rouverte sur identité sûre ;
* `crux report --summary` : rounds, reviewers exécutés / évités, findings,
  décisions humaines.

Le module reste nommé `round2.py` ; son comportement s'applique à tout round
N > 1.

---

## 2 — Review economics

Avant de passer à 7 personas, contrôler explicitement le coût d'une review.

Profils envisagés :

* `light`
* `balanced`
* `thorough`

Principes :

* tests unitaires : aucun Codex réel ;
* tests d'intégration Codex : opt-in ;
* dogfooding : appels réels autorisés ;
* `max_reviewers` respecté ;
* round 2 ciblé par défaut ;
* autorité de périmètre jamais supprimée pour économiser un appel.

Prévoir une visibilité simple :

* reviewers appelés ;
* reviewers évités ;
* rounds effectués ;
* appels Codex réels.

Pas de facturation/token accounting complexe tant que le besoin n'est pas démontré.

### Hook performance

Targets:
- SessionStart median < 1 s where practical
- no network/model calls in hooks
- no unnecessary subprocess probing
- baseline capture remains synchronous
- cache static capability/launcher information only when safe
- expose per-hook timing in doctor


### Hook robustness & latency

SessionStart remains synchronous.

Measured Crux cost:
- ~300 ms gate off
- ~500 ms first armed capture
- ~250 ms resumed baseline

Most startup latency observed by Claude Code is outside Crux.

Priorities:
- bound stdin payload waits;
- bound Git subprocess waits;
- reduce redundant Git subprocesses;
- avoid repeated state load/save;
- never cache baseline or Git status;
- never make baseline capture asynchronous.

Target:
- Crux SessionStart nominal < 500 ms;
- degraded dependencies fail-open quickly and visibly.



### Offline test isolation

- A test not explicitly marked live must never spawn Codex CLI.
- `codex --version` / capability discovery must be mocked or fixture-backed.
- Live Codex tests are opt-in only.
- CI/default development suite must remain fully offline.
---




## 3 — Reviewer personas

Passage progressif de 3 à 7 personas :

* code-quality
* architecture
* security
* tests
* performance
* UX
* release

Le routeur reste déterministe et explicable.

Un profil `balanced` ne doit généralement pas appeler les sept reviewers.

---

## 4 — Plan Review

Ajouter :

* `crux plan`
* `claude-plan`

Review indépendante d'un plan avant implémentation.

Contraintes :

* aucun appel Codex long dans un hook ;
* autorité humaine conservée ;
* étudier d'abord les mécanismes équivalents de Viper / codex-plugin-cc avant implémentation.

---

## 5 — blast_radius

Lorsqu'une décision `pending_human` possède un périmètre de fichiers identifiable :

* empêcher les modifications dans ce périmètre ;
* permettre au reste du travail de continuer lorsque c'est sûr ;
* aucune inférence textuelle fragile ;
* provenance humaine inchangée.

---

## 6 — scope_changes: warn

Mode intermédiaire :

* dérive détectée ;
* visible et journalisée ;
* humain informé ;
* comportement moins bloquant que `ask`.

---

## 7 — scope_changes: auto

À étudier en dernier.

Ne doit être implémenté que si les règles peuvent rester :

* déterministes ;
* auditables ;
* compatibles avec l'autorité humaine ;
* suffisamment conservatrices.

Il est acceptable que cette fonctionnalité soit reportée à une version ultérieure.

---

# Politique de tests Codex

Les tests live ne font pas partie de la boucle de développement standard.

Par défaut :

```text
tests
→ mocks / fixtures
→ aucun appel Codex
```

Les tests Codex réels sont explicitement activés pour :

* modification de l'intégration Codex ;
* fin d'un jalon significatif ;
* validation pré-release.

Le dogfooding `claude-review` reste une validation réelle distincte des tests d'intégration.
