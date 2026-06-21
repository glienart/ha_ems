# Changelog

## 0.6.9

### Fixed
- **Tests de calibration solaire** : mise à jour de `test_solar_calib.py` pour la
  nouvelle API. `factor()` prend désormais `(heure, mois)` et l'apprentissage long
  terme n'a lieu que sur les journées de ciel clair (résidu ≥ 0,80). Les scénarios
  d'apprentissage utilisent maintenant une journée claire (production = 90 % de la
  prévision) et un nouveau test vérifie qu'une journée nuageuse ne contamine pas le
  facteur structurel.

## 0.6.8

### Fixed
- **Sélecteur de date centré** : la barre de période et le sélecteur de date sont
  empilés et centrés sous les graphes ; le libellé de date se met à jour au clic
  sur « Maintenant ».

## 0.6.7

### Changed
- **Calibration solaire heure × mois (288 cellules)** : les facteurs de correction
  structurels passent de 24 valeurs (une par heure) à 24 × 12 = 288 (une par heure
  *et* par mois). Cela capture deux effets réels :
  - **Position du soleil** : à 51 °N, la hauteur solaire à 08 h est ~10° en décembre
    et ~35° en juin — un obstacle qui bloque le panel à faible angle n'impacte plus
    rien en été.
  - **Arbres à feuilles caduques** : un arbre au sud-est peut couper 60 % de la
    production en août (feuilles pleines) et n'avoir aucun effet en février.
  Migration automatique : les 24 anciens facteurs sont copiés sur les 12 mois comme
  point de départ, sans perte d'apprentissage existant.

## 0.6.6

### Changed
- **Refresh solaire de jour uniquement** : l'add-on calcule le lever et le coucher
  du soleil via une formule astronomique (lat/lon, sans librairie externe) et ne
  contacte Forecast.Solar que pendant la fenêtre diurne (±1 h de marge). L'intervalle
  passe de 6 h à 2 h pour être plus réactif aux changements météo — sans dépasser
  le quota de 12 appels/jour même en été (≤8 rafraîchissements sur 16 h de jour).
- **Calibration long terme sur ciel clair seulement** : le facteur de correction
  par heure (ombrage structurel des arbres, bâtiments) n'est mis à jour que
  lorsque le résidu météo du jour est ≥ 0,80. Sur les journées nuageuses, la
  production basse vient des nuages, pas de l'ombrage, et ne doit pas contaminer
  le signal d'apprentissage. La correction intra-journalière (météo du jour) reste
  active tous les jours quel que soit le temps.

## 0.6.5

### Changed
- **Couleurs EPEX sur référence historique fixe** : les barres du graphe et de la
  table de prix utilisent désormais un dégradé continu vert→jaune→rouge basé sur
  les **min/max jamais atteints** (mémorisés par zone), au lieu du min/max du jour
  affiché. Un créneau bon marché reste donc vert quel que soit le reste de la
  journée. Repli sur le min/max du jour tant que l'historique est insuffisant.
- **Prévision solaire plus fiable (correction météo intra-journalière)** : en plus
  du facteur de calibration par heure (biais systématique long terme), l'add-on
  compare en continu la production réelle du jour à ce qui était prévu pour les
  heures de jour déjà écoulées, et applique ce ratio (« météo du jour ») aux
  heures restantes. Un matin nuageux/ensoleillé reforme immédiatement la courbe
  du reste de la journée. Le facteur du jour est affiché sous « Prévisions 24h ».

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
