# Crux

**Claude Code construit. Codex CLI conteste. L'humain décide.**

Un gate de review croisée, portable, activable à la demande — et strictement
inerte tant qu'il ne l'est pas.

```
claude          →  Claude Code normal. Rien ne change. Rien ne coûte.
claude-review   →  Claude + gate de review Codex + autorité humaine
```

Conception complète : [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — ce dépôt en
implémente le jalon **v0.1 (MVP)**.

---

## Ce que ça fait

Vous lancez `claude-review` au lieu de `claude`. Claude développe normalement,
pendant que deux hooks silencieux enregistrent vos prompts et les fichiers
touchés. Quand Claude veut terminer son tour, un hook `Stop` l'intercepte en
~200 ms et lui demande de lancer `crux review`. Crux calcule le diff **de
session**, choisit 2 à 4 reviewers Codex spécialisés, les lance en parallèle en
lecture seule, et rend un rapport.

Claude corrige ce qu'il accepte, justifie ce qu'il rejette — et **pour tout ce
qui touche au périmètre fonctionnel, il ouvre une décision et vous la pose**.

## Les six garanties

| | |
|---|---|
| **`claude` est inchangé** | Aucune review, aucun contexte injecté, rien d'écrit dans votre projet. `SessionStart`/`SessionEnd` tiennent seulement un registre local léger dans `~/.crux` associant les sessions aux dépôts. |
| **`codex` est inchangé** | Crux n'écrit jamais dans `~/.codex/config.toml`. |
| **Aucun appel API obligatoire** | Codex s'authentifie avec votre abonnement ChatGPT (`codex login`). |
| **Le diff de session ne rate rien** | Il compare le working tree à l'état exact capturé à l'armement — une modification faite par `sed`, un script ou votre éditeur reste visible. |
| **Aucune écriture dans votre dépôt** | Liste blanche fermée de sous-commandes git en lecture seule. `commit`, `reset`, `checkout`, `stash` lèvent une exception avant d'atteindre le processus. |
| **Le lanceur est sondé** | `crux setup` exécute chaque lanceur candidat avant de l'inscrire dans les hooks. Sous Windows App Control, un `crux.exe` bloqué n'est jamais retenu. |
| **Panne technique = fail-open** | Codex absent, réseau coupé, bug de Crux : la session n'est jamais bloquée. |
| **Décision humaine = fail-closed** | Une décision ouverte tient, même si tout le reste est cassé. Claude ne peut pas la clôturer. |
| **Le verdict est structurel** | Approuvé ou refusé vient de l'action attachée à l'option choisie, jamais de sa formulation. Une réponse libre ne devient jamais une approbation. |

## Installation

```bash
uv tool install git+https://github.com/vous/crux      # ou : pipx install
crux setup                                            # plugin + shims + réglages
crux doctor                                           # diagnostic
cd mon-projet && crux init                            # écrit .crux.yml, désarmé
```

Prérequis : Python ≥ 3.9, Git, [Claude Code](https://claude.com/claude-code),
[Codex CLI](https://developers.openai.com/codex) (`npm i -g @openai/codex` puis
`codex login`).

Une seule dépendance Python obligatoire : **PyYAML**.

## Utilisation

```bash
claude                     # normal, aucun reviewer
claude-review              # gate de review armé pour cette session
/crux:on                   # armer à chaud, en cours de session
/crux:off                  # désarmer (les décisions ouvertes restent ouvertes)
CRUX_DISABLE=1 claude      # coupe-circuit absolu

crux review                # relire le diff de session
crux review security       # un seul reviewer
crux route --explain       # pourquoi ces reviewers, sans rien lancer
crux status                # gate résolu ET sa source
crux decision list --open  # ce qui attend votre décision
crux intent show           # contre quoi la dérive est mesurée
crux doctor --probe        # teste réellement les capacités de Codex
```

## Autorité

| Décision | Qui | Comment |
|---|---|---|
| Nommage, algorithme, refactoring local | Claude | `crux resolve` |
| Bug réel, faux positif, hors-sujet | Claude | `accepted` / `rejected` / `deferred` |
| Ajout, retrait, changement de comportement, API, format | **Vous** | `crux decision propose` → `AskUserQuestion` |

Le statut vient de l'**action** portée par l'option choisie
(`approve_scope_change` / `reject_scope_change`), jamais de son texte. Les pistes
suggérées par un reviewer ne sont pas des options : leur effet sur le périmètre
est inconnu. Une réponse libre laisse la décision ouverte plutôt que d'être
devinée.

Il n'existe **aucun** argument `--by human`. Les statuts `approved_by_human` et
`rejected_by_human` sont écrits par une seule fonction, alimentée par le
`tool_response` réel d'`AskUserQuestion`. Quatre propriétés ferment le
contournement : la commande n'existe pas, une règle `deny` bloque le chemin des
hooks, le repli manuel exige un TTY, et un `tool_use_id` ne sert qu'une fois.

`tests/test_human_provenance.py` vérifie qu'aucun appel CLI issu du chemin agent
ne peut transformer une décision `pending_human` en décision humaine.

## Périmètre de ce jalon (v0.1)

Livré : gate `Stop`, diff de session à baseline exacte (indépendant des outils Edit/Write), périmètre approuvé,
registre de décisions, promotion des findings, 3 personas
(`code-quality` · `security` · `architecture`), routeur à 11 signaux, sonde de
capacité, `doctor`, `init`, `setup`.

Pas encore : plan review, round 2 ciblé, les 4 autres personas, exécution des
tests, modes `advise` / `auto`, notifications. Voir la roadmap de
`ARCHITECTURE.md` §28.

## Développement

```bash
PYTHONPATH=src python -m unittest discover -s tests -t .
```

Les tests nécessitant un vrai binaire `codex` sont marqués comme tests
d'intégration et ignorés proprement lorsqu'il est absent.

## Licence

MIT.
