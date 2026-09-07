---
name: crux-decide
description: Fait trancher a l'utilisateur une decision Crux en attente.
---
1. Lance `crux decision list --open`.
2. S'il n'y a rien, dis-le et arrete-toi.
3. Sinon, pour la premiere decision en attente, lance
   `crux decision show --id <ID>` pour obtenir la question exacte.
4. Pose-la avec `AskUserQuestion` : `header` = `Scope <ID>`, et les options
   telles quelles, y compris l'option de refus.
5. N'interprete pas la reponse et ne la reformule pas. Le hook `post-ask`
   enregistre la decision automatiquement. Aucune commande `crux` ne peut clore
   une decision humaine - n'essaie pas.
6. Reprends le travail conformement a la reponse obtenue.
