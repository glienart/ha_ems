# Changelog

## 0.6.4

### Changed
- **Sélecteur de date façon Home Assistant** sur les onglets **Consumption** et
  **Analysis** : barre avec icône calendrier (ouvre le sélecteur natif), bouton
  **Auj.** pour revenir à aujourd'hui, chevrons précédent/suivant et libellé de
  période centré (jour / mois / année selon l'agrégation choisie).

## 0.6.3

### Added
- **Stats de coût sur Consumption** : nouvelles cartes **Revenu**, **Dépenses** et
  **Total** (coût net en €) au-dessus des graphes, calculées sur la période
  sélectionnée (jour / mois / année). Le Total passe en vert quand il s'agit d'un
  crédit net (revenu > dépenses).

## 0.6.2

### Added
- **Comparatif Réel vs Prévisionnel** sur l'onglet **Analysis** : graphique
  combiné barres + lignes affichant le solaire et la consommation réels (kWh
  mesurés) avec, en superposition pointillée, les prévisions du plan 24h (quand
  la date sélectionnée est aujourd'hui).
- **Sélecteur de date** sur **Analysis** (boutons précédent/suivant + champ date)
  pour naviguer jour par jour, comme sur l'onglet Consumption.

### Changed
- **Add-on** : suppression de `ingress_port` (valeur par défaut 8099), passage au
  nouveau format `map` (`type: data` / `read_only`), et retrait des architectures
  dépréciées `armhf` et `armv7` (non supportées depuis Home Assistant 2025.12).

## 0.6.1

### Added
- **Énergie depuis de vrais capteurs (kWh)** : nouveaux réglages « Energy meters
  (kWh) » pour les compteurs réseau (import/export), solaire, maison et
  charge/décharge batterie. L'add-on lit les compteurs cumulatifs et calcule les
  deltas par intervalle (gestion des remises à zéro). Tout compteur laissé vide
  retombe sur l'intégration du capteur de puissance correspondant.
- **Récap en haut de Consumption** : totaux kWh **Réseau / Maison / Solaire /
  Batterie** sur la période choisie (jour / mois / année).

### Changed
- Le **prix en €** reste calculé par l'add-on à partir du tarif EPEX effectif
  (coût = kWh importés × tarif conso ; revenu = kWh exportés × tarif injection) —
  aucun capteur de prix requis.

## 0.6.0

### Changed
- **Refonte en 3 onglets** : **Live** (tout en kW — mode, mesures, flux, graphe de
  puissance), **Consumption** (kWh & € — graphes conso/coût + sélecteur de date),
  **Analysis** (EPEX, prévisions 24h, plan batterie).
- **Internationalisation (i18n)** : l'interface suit la langue configurée dans
  Home Assistant quand elle est disponible (anglais et français fournis, base
  anglaise par défaut, extensible). Détection via la langue du frontend HA.
- Le sélecteur Heure/Jour/Année est désormais sur l'onglet **Consumption**.

### À venir (étape 2)
- kWh & € mesurés depuis de vrais **capteurs d'énergie** HA configurables
  (réseau import/export, production, conso, **recharge VE**) au lieu d'être
  calculés par intégration de la puissance — corrige la conso « linéaire ».

## 0.5.33

### Changed
- **Planificateur — fin des charges réseau inutiles la nuit** : la charge depuis
  le réseau dans les créneaux les moins chers est désormais limitée au **déficit**
  d'énergie que la prévision solaire ne couvrira pas. Si le surplus solaire prévu
  de la journée suffit à remplir la batterie, aucune charge réseau n'est planifiée
  — inutile de payer le réseau la nuit quand le solaire gratuit arrive.

## 0.5.32

### Changed
- **Barre d'onglets** : fond blanc (couleur de carte du thème) sur toute la
  largeur. L'onglet actif est désormais en **gras et noir** (couleur de texte du
  thème) avec un soulignement noir, au lieu du bleu d'accent.

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
