---
name: crux-review
description: Lance une review Crux du diff de session courant.
---
Lance `crux review --session $CLAUDE_SESSION_ID` et traite le rapport selon le
contrat d'autorite de la skill `crux-authority`.

Rappel : un finding technique s'arbitre avec `crux resolve`. Un ajout, un retrait
ou un changement de comportement fonctionnel n'est pas ta decision - ouvre
`crux decision propose` puis pose la question avec `AskUserQuestion`.
