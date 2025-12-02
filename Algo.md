## 1. Contexte expérimental et contraintes

### 1.1. Acquisition physique

* Lors d’un **shot** (tir laser / expérience), **toutes les caméras** sont déclenchées **en même temps**.
* En pratique :

  * Chaque caméra est branchée sur **un PC différent**.
  * Le système d’acquisition et Windows écrivent la date dans le champ **Modified Time** du fichier.
  * Ces horloges ne sont **pas parfaitement synchronisées** :

    * certaines images apparaissent dans le système de fichiers **quelques secondes après** les autres,
    * il arrive qu’une caméra plante et :

      * n’écrive **jamais** l’image,
      * ou écrive l’image **très en retard** (par ex. > 1 minute après).

### 1.2. Cloud et synchronisation des dossiers

* Chaque PC écrit les images dans un dossier local, qui est ensuite **synchronisé sur le cloud**.
* Un PC central récupère tous ces dossiers de caméras via la synchro cloud.
* La synchro n’est **pas instantanée** :

  * typiquement, les mises à jour ne sont poussées que **toutes les ~30 secondes**,
  * donc les images d’un même shot peuvent arriver sur le PC central dans un ordre et à des instants très variés.
* La seule information temporelle fiable pour regrouper les images d’un même shot est donc le **Modified Time** conservé par le système de fichiers (copié dans `dt` dans le code).

### 1.3. Rythme des shots

* En règle générale, il n’y a **pas plus d’un shot par minute**.
* Mais on doit être robuste aux cas où :

  * une caméra met **> 1 minute** à produire son image,
  * les images d’un **shot n+1** arrivent sur le cloud **avant** que l’image très en retard du shot *n* n’apparaisse,
  * malgré ça, la chronologie des `Modified Time` reste cohérente (l’image du shot n a un `mtime` cohérent avec les autres).

### 1.4. Objectif logiciel

* Sur le PC central, le code :

  * surveille tous les dossiers RAW synchronisés,
  * détecte automatiquement un **shot** dès qu’un **fichier trigger** apparaît dans un des dossiers des caméras trigger,
  * regroupe autour de ce trigger **exactement une image par caméra** dont le `Modified Time` est dans une **fenêtre temporelle ±(full_window/2)** autour du trigger,
  * gère la possibilité de **2 shots en parallèle** (cas où les images de 2 shots se chevauchent dans le temps côté cloud),
  * ferme un shot soit :

    * dès que toutes les caméras attendues ont donné une image,
    * soit à l’expiration du **timeout**, en signalant les caméras manquantes.

---

## 2. Cas de figure possibles (côté données)

### 2.1. Cas simple : toutes les caméras écrivent vite, un seul shot

* Toutes les images des caméras pour le shot S arrivent dans un intervalle de quelques secondes.
* Il n’y a **qu’un seul trigger** dans cette période.
* Aucun autre shot n’est lancé pendant ce temps.

### 2.2. Caméra en retard (mais dans la fenêtre)

* Le trigger arrive à `t0`.
* Certaines caméras mettent jusqu’à `t0 + (full_window/2)` pour créer leurs fichiers.
* On reste dans la même fenêtre temporelle.
* Le timeout est plus long que `full_window`, donc la caméra en retard peut tout de même être prise en compte avant la fermeture du shot.

### 2.3. Caméra très en retard (> full_window, mais < timeout)

* Le trigger est à `t0`.
* Une caméra sort son image à `t0 + 40 s` par exemple, alors que `full_window = 10 s` (fenêtre `t0 ± 5 s`).
* Le fichier a donc un `Modified Time` hors de la fenêtre.
* Au moment où le fichier est vu par le code :

  * la fenêtre du shot S **ne contient pas** ce `dt` → l’image ne sera pas associée à S.
  * selon le timing global et les autres triggers, ce fichier :

    * peut être vu comme une image “orpheline”,
    * ou être aspiré par un autre shot si un trigger plus tard crée une nouvelle fenêtre qui le contient.

### 2.4. Caméra qui ne produit jamais d’image

* Le trigger arrive pour un shot S.
* Pour une ou plusieurs caméras, aucune image n’est jamais créée ou synchronisée.
* Le shot expire lorsque le timeout est atteint, avec ces caméras marquées comme **manquantes**.

### 2.5. Deux shots successifs, bien espacés (> 1 minute)

* Shot 1 : trigger à `t1`.
* Shot 2 : trigger à `t2 ≈ t1 + 60 s` ou plus.
* Les fenêtres temporelles ne se recouvrent pas :

  * `[t1 - W/2, t1 + W/2]` et `[t2 - W/2, t2 + W/2]` sont séparées.
* Les images se répartissent proprement dans les deux shots.

### 2.6. Deux shots où les images se chevauchent côté cloud

* Les triggers physiques sont espacés d’au moins 1 min, mais côté **cloud** :

  * les images de shot 2 arrivent rapidement,
  * les images de shot 1 sont, pour certaines caméras, très en retard.
* Résultat : au niveau de la machine centrale, on voit des fichiers dans l’ordre :

  * quelques images de S1,
  * toutes les images de S2,
  * puis des images en retard de S1.
* L’algorithme doit :

  * **se baser sur le Modified Time**, pas sur l’ordre d’arrivée réseau,
  * permettre d’avoir **2 shots ouverts** en même temps (S1 et S2),
  * assigner chaque image au bon shot en fonction de son `dt` et des fenêtres.

---

## 3. Comment le code gère chaque cas

### 3.1. Détection d’un shot et fenêtre temporelle

1. Lorsqu’un fichier `.tif` arrive, le code lit son **Modified Time** (`mtime`) et le convertit en `datetime` (`dt`). 
2. Il décide si ce fichier est un **trigger** :

   * caméra ∈ `trigger_cameras`,
   * nom de fichier contenant le mot-clé global (`"shot"` par défaut).
3. Pour un trigger, on crée une **fenêtre** centrée sur `dt` :

   * `window_start = dt - full_window/2`
   * `window_end   = dt + full_window/2`. 

Cette fenêtre sert à décider quelles images appartiennent au même shot.

---

### 3.2. Cas 1 : réutiliser un shot déjà ouvert (multi-trigger / parallélisme)

Avant de créer un nouveau shot, le code essaie de **réutiliser** un shot déjà existant : 

* Pour chaque shot `s` dans `open_shots` :

  * `s["status"] == "collecting"`,
  * même date (`date_str`),
  * `dt` du trigger actuel est dans `[s["window_start"], s["window_end"]]`,
  * cette caméra n’a pas encore d’image pour ce shot (`camera not in s["images_by_camera"]`).

Si c’est le cas :

* le trigger est **ajouté** au shot existant,
* il n’y a **pas de nouveau shot** créé.

👉 Ça couvre le cas où plusieurs caméras trigger tirent pour la **même interaction** : elles sont toutes associées au premier shot dont la fenêtre englobe le `Modified Time` du fichier.

---

### 3.3. Cas 2 : création d’un nouveau shot

Si aucun shot existant n’est compatible (fenêtre + caméra non encore utilisée) :

1. On crée un **nouvel index de shot** pour cette date.
2. On parcourt tous les fichiers déjà vus ce jour (`files_by_date[date_str]`) :

   * pour chaque fichier,

     * si son `dt` est dans `[window_start, window_end]`,
     * et que son chemin n’est pas déjà en `assigned_files`,
     * et que sa caméra n’est pas déjà présente dans `images_by_camera`,
     * → on l’ajoute au nouveau shot.
3. On s’assure que le fichier trigger fait partie des images du shot.
4. On ajoute ce shot à `open_shots` avec :

   * `status = "collecting"`,
   * `start_wall_time = datetime.now()` (pour le timeout),
   * `trigger_camera` et `trigger_time` renseignés.

👉 Cela permet d’absorber rétroactivement des images arrivées **avant** le trigger (horloges désalignées, synchro cloud décalée).

---

### 3.4. Cas 3 : images normales (non-trigger) qui arrivent ensuite

Pour chaque nouvelle image non-trigger :

1. Si le fichier a déjà été assigné à un shot, on le saute.
2. On cherche un shot **candidat** dans `open_shots` :

   * même date,
   * `status == "collecting"`,
   * `dt` de l’image dans `[window_start, window_end]` du shot.
3. Si on en trouve un :

   * si cette caméra n’a pas encore contribué au shot, on ajoute l’image,
   * sinon, on log que c’est un doublon pour cette caméra et ce shot → ignoré.
4. Ensuite, on appelle `_maybe_close_if_complete(shot)` :

   * si toutes les caméras attendues sont présentes, le shot se ferme **tout de suite**.

👉 Ça gère les **retards de quelques secondes / dizaines de secondes**, tant que `dt` reste dans la fenêtre définie à partir du trigger.

---

### 3.5. Images “orphelines”

Si aucune fenêtre de shot ouvert ne contient le `dt` de l’image : 

* Le code loggue l’image comme **“Orphan image (no matching open shot window yet)”**,
* mais il la garde dans `files_by_date` :

  * si un trigger **plus tard** crée un nouveau shot dont la fenêtre inclut ce `dt`, l’image sera rattachée à ce shot lors de la création.

👉 C’est utile si une caméra est en avance ou en retard par rapport au trigger, mais que la fenêtre d’un shot futur englobe quand même ce `dt`.

---

### 3.6. Règle “une image par caméra par shot”

Lors de la création d’un shot comme lors de l’ajout ultérieur :

* On ne prend **qu’une seule image** par caméra dans `images_by_camera`.
* Une deuxième image de la même caméra, dans la même fenêtre, est considérée comme un **duplicate** et ignorée.

👉 Ça respecte ton exigence “une image par dossier (caméra) par shot”, même si le système ou l’utilisateur a lancé plusieurs acquisitions dans le même intervalle.

---

### 3.7. Timeout et fermeture des shots

En parallèle, un thread de travail (`_worker_loop`) vérifie régulièrement :

* pour chaque shot `s` avec `status == "collecting"`,
* si `elapsed = (now - s["start_wall_time"]) >= timeout_s`.

Si le timeout est atteint :

* `status = "closing"`,
* on appelle `_close_shot(s)`.

Dans `_close_shot` :

* On calcule la liste des caméras attendues (`expected_cameras`) et celles réellement présentes dans `images_by_camera` → `missing`.
* On copie toutes les images présentes vers `CLEAN_DATA`, sous un nom unique du type :
  `Cam_YYYYMMDD_HHMMSS_shotNNN.tif`.
* On loggue :

  * soit “acquired successfully, all cameras present”,
  * soit “acquired (timeout or complete), but missing cameras: [...]”.
* On loggue aussi un résumé timing (trigger, min/max mtime, first/last camera). 
* On marque le shot comme `closed` et on le retire de `open_shots`.

👉 Ça gère :

* les caméras qui ne produisent **jamais** d’images,
* les caméras très en retard (au-delà du timeout),
* tout en garantissant que les images déjà disponibles sont sauvées.

---

### 3.8. Deux shots en parallèle (fenêtres qui se chevauchent côté cloud)

Grâce à :

* la liste `open_shots` (plusieurs shots avec `status == "collecting"`),
* des **fenêtres temporelles indépendantes** par shot,
* la sélection du **premier** shot dont la fenêtre contient `dt`,

le code peut parfaitement gérer :

* un shot 1 qui attend encore certaines caméras (images très en retard),
* un shot 2 qui se déclenche (nouveau trigger) et se remplit entièrement,
* potentiellement shot 2 qui se ferme **avant** shot 1.

Tant que :

* les `Modified Time` des images de chaque shot restent regroupés près de leur trigger,
* et que `full_window` + `timeout` sont configurés de manière cohérente avec le comportement réel des caméras et de la synchro cloud,

chaque image est rattachée au **bon** shot via son `dt` et la fenêtre `[window_start, window_end]`.

---

## 4. Résumé

En résumé :

* Le contexte (multi-PC, synchro cloud lente, décalage d’horloges, une image par caméra) impose de se baser uniquement sur le **Modified Time**.
* Le code :

  * utilise un **trigger** pour ouvrir un shot,
  * définit une **fenêtre temporelle** autour du mtime du trigger,
  * regroupe toutes les images dont le mtime tombe dans cette fenêtre, une par caméra,
  * garde la trace des images déjà assignées,
  * permet plusieurs shots ouverts simultanément,
  * ferme un shot soit dès que toutes les caméras ont répondu, soit sur timeout,
  * gère les images “orphelines” qui peuvent être récupérées si un shot futur les englobe dans sa fenêtre.
