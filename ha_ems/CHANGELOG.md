# Changelog

## 0.5.31

### Changed
- **Filtre d'historique global** (page Energy) : un seul contrôle
  Heure / Jour / Année + sélecteur de date (avec navigation ‹ ›) pilote
  maintenant les deux graphes Consommation et Coût, au lieu d'un réglage séparé
  par graphe.
  - **Heure** : 24 barres horaires du jour choisi
  - **Jour** : barres journalières du mois choisi
  - **Année** : barres mensuelles de l'année choisie
- L'API `/api/energy/history` accepte un paramètre `date` qui ancre
  l'agrégation sur la période demandée.

## 0.5.30

### Changed
- **Graphes au thème Home Assistant** : les couleurs (texte des axes, légendes,
  grilles) sont désormais reprises des variables de thème HA via les valeurs par
  défaut de Chart.js, au lieu de gris codés en dur. Les graphes s'adaptent au
  mode clair/sombre et aux thèmes personnalisés.

## 0.5.29

### Changed
- **Onglets style Home Assistant** : barre d'onglets soulignés (onglet actif en
  couleur d'accent), cartes aux coins arrondis avec ombre légère pour coller au
  thème HA.
- **URL par onglet** : la page active est mémorisée dans l'URL
  (`#energy` / `#analyse` / `#settings`) — un rafraîchissement reste sur le même
  onglet, et les boutons précédent/suivant du navigateur fonctionnent.
- **Graphes Consommation / Coût** agrandis (même hauteur que le graphe de
  puissance au-dessus).

## 0.5.28

### Added
- **Graphe de prévisions 24h** (onglet Analyse) : production solaire prévue +
  consommation prévue.
- **Apprentissage solaire** : un facteur de correction par heure est appris en
  comparant la production réelle à la prévision Forecast.Solar, puis appliqué aux
  prévisions futures — l'estimation s'adapte progressivement à l'ombrage, à
  l'orientation et à la salissure propres à l'installation. Persisté dans `/data`,
  facteur moyen affiché dans l'onglet Analyse.

### Notes
- La consommation était déjà adaptative (moyenne glissante par heure-de-semaine
  sur 7 jours, persistée depuis 0.5.25).

## 0.5.27

### Changed
- **Refonte des onglets** : l'onglet *Dashboard* est supprimé et fusionné dans
  **Energy** (mode + relevés en direct + décisions + flux + graphe de puissance
  + Consommation/Coût). Nouvel onglet **Analyse** regroupant les prix EPEX et le
  plan batterie 24h.
- **Graphe Consommation** : barres empilées (autoconsommation solaire + import)
  au-dessus de l'axe, export en négatif en dessous ; empilement vertical sur
  écran étroit (responsive).
- **Graphe Prix payé** : coût affiché en négatif sous le revenu (couleurs
  conservées — revenu vert, coût rouge).

## 0.5.26

### Fixed
- **Forecast.Solar rate-limit**: la prévision solaire est désormais mise en cache
  et rafraîchie au maximum toutes les 6 h (au lieu de chaque reconstruction du
  plan, toutes les 30 min). Évite les erreurs 429 du tier gratuit. La dernière
  prévision valide est conservée en cas d'échec. Le cache est invalidé quand les
  réglages panneaux/localisation changent.

### Added
- Suite de tests **pytest** (optimizer, scheduler, parsing EPEX, energy_log,
  historique de consommation).
- **Intégration continue GitHub Actions** : exécution des tests + lint d'add-on
  Home Assistant à chaque push / pull request.

## 0.5.25

### Added
- **Plan batterie 24h** affiché dans l'onglet Energy (exploite enfin l'endpoint
  `/api/forecast`) : action prévue, kW, prix d'achat et raison par heure, avec
  surlignage de l'heure courante.

### Fixed
- **Fuseau horaire** : conversion correcte heure locale → UTC dans le mapping
  des prix EPEX du planificateur. Corrige un décalage d'1–2 h qui désalignait
  prix et créneaux.
- **Persistance de l'historique de consommation** entre redémarrages
  (`/data/consumption_history.json`).
- **Écritures disque du journal d'énergie** limitées (throttle 5 min) avec un
  flush garanti à l'arrêt, au lieu d'une réécriture à chaque tick.
- Redémarrage propre de la boucle d'optimisation après modification de
  l'intervalle de mise à jour (`_loop_task` correctement réassignée).
- La zone EPEX active est maintenant exposée par `/api/epex` (affichage de la
  zone dans le dashboard).
