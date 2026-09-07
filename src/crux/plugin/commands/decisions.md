---
name: crux-decisions
description: Liste les decisions humaines en attente.
---
Lance `crux decision list --open` et presente les decisions en attente.

Pour chacune, propose a l'utilisateur de la trancher maintenant : pose la
question avec `AskUserQuestion`, en mettant `Scope <ID>` dans le champ `header`
et en reprenant exactement les options listees par `crux decision show --id <ID>`.
