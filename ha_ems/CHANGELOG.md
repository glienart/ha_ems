# Changelog

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
