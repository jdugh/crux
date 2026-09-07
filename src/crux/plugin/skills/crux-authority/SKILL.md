---
name: crux-authority
description: Contrat d'autorité Crux — qui décide de quoi, et comment traiter un rapport de review. À charger dès que le gate Crux est armé et avant de traiter un rapport de review, d'arbitrer un finding, ou dès qu'un changement fonctionnel non demandé apparaît nécessaire.
---

# Contrat d'autorité

Trois acteurs, trois compétences. **Un constat n'est pas une décision.**

| Acteur | Autorité | Ne peut jamais |
|---|---|---|
| **L'humain** | Tout ce qui est fonctionnel, produit, ou hors de ce qui a été demandé | — |
| **Toi (Claude)** | Le technique, **à l'intérieur** du périmètre approuvé | Décider seul d'un changement fonctionnel. Clore une décision humaine. |
| **Codex** | Aucune. Il produit des constats argumentés | Imposer une correction |

## Table de compétence

| Type de décision | Qui décide | Comment |
|---|---|---|
| Nommage, structure interne, algorithme, refactoring local | Toi | `crux resolve` |
| Bug réel signalé par un reviewer | Toi : corrige | `crux resolve --status accepted` |
| Faux positif | Toi : rejette avec une raison technique | `crux resolve --status rejected --reason "..."` |
| Vrai problème mais hors de la demande | Toi : diffère | `crux resolve --status deferred --reason "..."` |
| Ajout d'une fonctionnalité non demandée | **L'humain** | `crux decision propose` |
| Suppression d'une fonctionnalité existante | **L'humain** | idem |
| Changement d'un comportement visible | **L'humain** | idem |
| Modification significative de l'UX | **L'humain** | idem |
| Abandon d'une capacité pour simplifier | **L'humain** | idem |
| Introduction d'une incompatibilité | **L'humain** | idem |
| Changement d'une API publique | **L'humain** | idem |
| Changement de format de données observable | **L'humain** | idem |
| Toute décision produit non couverte par la demande | **L'humain** | idem |

## Traiter un rapport de review

Pour chaque finding, dans cet ordre :

1. **Vérifie-le dans le code avant d'agir.** Un reviewer peut se tromper. Ne
   corrige jamais sur la seule foi du rapport.
2. **S'il est réel et technique** → corrige, puis
   `crux resolve --id <ID> --status accepted`.
3. **Si c'est un faux positif** → `crux resolve --id <ID> --status rejected
   --reason "<raison technique, pas une opinion>"`. Un rejet argumenté est une
   résolution parfaitement valide.
4. **S'il est légitime mais hors de la tâche demandée** →
   `crux resolve --id <ID> --status deferred --reason "..."`.
5. **S'il implique un changement fonctionnel** → ce n'est pas ta décision. Va
   à la section suivante.

Ce qui est interdit, ce n'est pas de rejeter — c'est d'ignorer en silence.

## Quand un changement de périmètre apparaît

Que tu le remarques toi-même ou qu'un reviewer le signale, la procédure est la
même et elle n'a pas de variante :

1. **Interromps cette partie du travail.** N'écris pas le changement « en
   attendant ».
2. **Explique pourquoi** il semble nécessaire, en une ou deux phrases.
3. **Expose les alternatives réalistes**, y compris celle de ne rien changer.
4. **Ouvre la décision** :
   ```
   crux decision propose --title "<quoi>" --why "<pourquoi>" \
     --alt "<alternative 1>" --alt "<alternative 2>" --files "a.py,b.py"
   ```
   La commande te rend un identifiant (`D3`) et la question exacte à poser.
5. **Pose la question** avec `AskUserQuestion`, en mettant `Scope D3` dans le
   champ `header` et en reprenant les options telles quelles, option de refus
   comprise.
6. **Attends la réponse.** Elle clôt la décision automatiquement.
7. **Reprends** conformément à ce que l'humain a répondu, littéralement.

## Ce que tu ne peux pas faire

- Tu ne peux pas clore une décision humaine. Aucune commande `crux` ne le
  permet : `crux decision resolve` exige un terminal interactif et est refusée
  par une règle `deny`. N'essaie pas de la contourner — c'est structurel, pas
  déclaratif.
- Tu ne peux pas terminer ton tour tant qu'une décision est en attente sans
  avoir posé la question. Le hook `Stop` te renverra.
- Tu ne peux pas résoudre par `crux resolve` un finding promu en décision : la
  commande refusera, et elle te dira vers quelle décision aller.
- `crux decision withdraw --id <ID> --reason "..."` existe pour le seul cas où
  la décision est devenue sans objet (le code a changé). Ce n'est pas une porte
  de sortie, et la décision reste au registre.

## Si Crux est en panne

Codex indisponible, réseau coupé, quota atteint : ce sont des pannes techniques,
elles ne bloquent rien. Signale-le en une ligne et termine normalement.

Une décision humaine en attente, en revanche, n'est pas une panne. Elle tient,
même si tout le reste est cassé.
