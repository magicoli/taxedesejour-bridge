# TODO — taxesejour

## Group invoice Beds24 (à intégrer)
Beds24 a une notion de **facturation groupée** (group booking / masterId) qui
relie plusieurs réservations d'un même client en une seule transaction.

Actuellement le regroupement en une déclaration se fait via `_same_client()`
(masterId OU email OU nom) + chevauchement de dates. Il faut vérifier/exploiter
plus directement l'info "group invoice" de Beds24 :
- confirmer le champ exact (masterId ? groupId ? une facture commune ?)
- s'en servir comme signal primaire de regroupement (plus fiable que email/nom)
- gérer le cas où les membres d'un groupe ont des emails différents (actuellement
  `_same_client` ne matche pas si un membre a un email et l'autre non, même à nom égal)

## Limite connue — fusion par client
`_same_client` : même email OU (pas d'email des deux côtés ET même nom).
Si une résa a un email et l'autre pas, pas de fusion même à nom identique.

## Intégration bokit-light (PHP) — plus tard
- Logique de calcul centralisée dans `config.py` (fonctions pures portables).
- Le `Row` / CSV à 16 colonnes = contrat de données stable pour l'import.
