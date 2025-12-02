# **AGENTS.md**

## 🧭 Rôle de ce document

Ce document définit les **règles générales**, **bonnes pratiques**, et **contraintes de non-régression** que tout agent OpenAI (ex. Codex) doit respecter lors du développement du projet.

Il sert :

* de référence stable,
* de garde-fou pour préserver la logique existante,
* de guide pour organiser le code proprement,
* d’assurance contre les modifications destructives.

La logique fonctionnelle détaillée de l’algorithme de détection et gestion des shots se trouve dans **`Algo.md`** et doit toujours être respectée strictement.

---

# ✅ **1. Principes généraux**

### 1.1. Toujours préserver la logique métier définie dans `Algo.md`

* **Algo.md est la source de vérité** concernant le comportement attendu.
* Tout changement de comportement doit être explicitement voulu et validé.
* Si une modification peut affecter la détection ou la gestion des shots :

  * demander confirmation,
  * proposer l’impact détaillé,
  * ne jamais modifier silencieusement ces parties.

### 1.2. Priorité absolue : **ne pas casser ce qui fonctionne**

* Avant chaque modification :

  * analyser les dépendances,
  * vérifier si le changement peut casser une fonctionnalité existante,
  * respecter les conventions déjà présentes.
* Lorsqu’une refactorisation est nécessaire :

  * la réaliser **par petites étapes**,
  * vérifier que le comportement reste identique.

### 1.3. Les modifications doivent être **incrémentales et testables**

* Pas de refonte géante en une étape.
* Chaque commit doit :

  * être autonome,
  * fonctionner indépendamment,
  * ne pas dépendre de modifications ultérieures hypothétiques.

---

# 🧱 **2. Architecture et organisation**

### 2.1. Découper proprement le code

Actuellement, l’un des objectifs du projet est d’améliorer l’organisation générale.
Les futures modifications doivent tendre vers :

* séparation logique en plusieurs fichiers ou modules :

  * `shot_manager.py`
  * `watcher.py`
  * `config.py`
  * `gui/`
  * `utils/`
  * etc.
* éviter les très longs fichiers monolithiques,
* regrouper les responsabilités par rôle (single-responsibility principle).

### 2.2. Ne pas introduire de dépendances inutiles

* Utiliser uniquement les bibliothèques déjà employées ou standards.
* Ne pas charger le projet de frameworks lourds non nécessaires.

### 2.3. Rendre les nouveaux modules **compatibles avec l’existant**

* Interfaces cohérentes,
* conventions de nommage homogènes,
* préserver les comportements existants dans les parties critiques.

---

# ⚙️ **3. Gestion de configuration**

### 3.1. Ajouter de nouveaux paramètres doit être fait proprement

* Ils doivent être centralisés dans un module (ex. `config.py`).
* Ils doivent disposer de :

  * une valeur par défaut,
  * une validation,
  * une documentation claire,
  * une compatibilité ascendante avec les anciennes configurations.

### 3.2. Toute modification du format d’un fichier de configuration doit être :

* rétrocompatible **ou**
* accompagnée d’un convertisseur explicite.

---

# 🧪 **4. Tests et non-régression**

### 4.1. Vérifier la compatibilité avec les scénarios critiques

Les scénarios fondamentaux décrits dans **Algo.md** (cas simple, retards, multi-shots, triggers proches, orphans, timeouts…) doivent **toujours fonctionner**.

### 4.2. Chaque nouvelle fonctionnalité doit avoir ses propres tests

* Tests unitaires si possible,
* sinon tests manuels décrits dans le PR,
* vérification du comportement avant / après modification.

### 4.3. Toujours tester les cas limites

Exemples :

* images en double,
* image très en retard,
* multi-shots simultanés,
* absence totale d’un dossier,
* perte temporaire du cloud.

---

# 🖥️ **5. Interface graphique (GUI)**

### 5.1. La GUI doit rester simple, explicite et robuste

* Pas de complexité inutile,
* chaque contrôle doit avoir un effet clair,
* pas de comportements implicites qui surprendraient l’utilisateur.

### 5.2. Toute nouvelle interaction GUI doit :

* être regroupée dans un module dédié,
* ne pas détourner la logique interne (ShotManager reste la référence),
* être documentée,
* conserver la cohérence des labels, couleurs et statuts existants.

---

# 🔐 **6. Style, lisibilité et documentation**

### 6.1. Commenter ce qui est non trivial

* Les blocs critiques doivent être documentés.
* Toute logique complexe doit renvoyer vers `Algo.md`.

### 6.2. Style Python cohérent

* Conventions PEP8,
* fonctions courtes et lisibles,
* noms explicites.

### 6.3. Mise à jour systématique de la documentation

* Quand la logique change : mettre à jour `Algo.md`.
* Quand une interface change : mettre à jour le README.

---

# 🧩 **7. Collaboration entre agents**

### 7.1. L’agent doit produire du code **modulaire, clair et stable**

* Toujours penser à long terme.
* Ne pas produire de patchs chaotiques.

### 7.2. L’agent doit expliquer ses modifications

Chaque contribution doit inclure :

* **ce qui a été fait**,
* **pourquoi**,
* **quel impact potentiel**,
* **comment tester**.

---

# 📌 **8. Red Flags — Ce que l’agent ne doit jamais faire**

* ❌ Modifier la logique de détection/gestion des shots sans raison.
* ❌ Supprimer une fonctionnalité existante sans demande explicite.
* ❌ Introduire des comportements implicites non documentés.
* ❌ Ajouter du code dupliqué ou non structuré.
* ❌ Faire des modifications massives non demandées.
* ❌ Réécrire entièrement un module sans plan.

---

# 🏁 **Conclusion**

L’objectif du projet est de maintenir un système :

* **précis**,
* **robuste**,
* **modulaire**,
* **facile à faire évoluer**,
* tout en garantissant la **non-régression** de la logique métier, décrite dans `Algo.md`.

Toutes les contributions automatiques ou assistées doivent respecter ce cadre.
