# Veille juridique notariale : mise en route (≈20 min, une seule fois)

## 1. GitHub
1. Créez un compte sur https://github.com (gratuit).
2. Cliquez « + » > « New repository ». Nom : `veille-notariale`. Choisissez **Public**. Créez.
3. « uploading an existing file » : glissez TOUT le contenu du dossier dézippé (dossiers `.github` et `docs` compris ; sur Mac, Cmd+Maj+. affiche les fichiers cachés). « Commit changes ».

## 2. PISTE (accès officiel gratuit aux API de l'État)
1. Créez un compte sur https://piste.gouv.fr, puis une **application**.
2. Souscrivez aux API **Légifrance** et **Judilibre** (accès gratuit).
3. Notez le **client ID** et le **client secret** de l'application.

## 3. Secrets GitHub
Dépôt > Settings > Secrets and variables > Actions > New repository secret :
- `PISTE_CLIENT_ID` = votre client ID
- `PISTE_CLIENT_SECRET` = votre client secret
- (facultatif, gratuit, résumés IA) `GEMINI_API_KEY` — voir ci-dessous comment l'obtenir
- (facultatif, alternative payante) `ANTHROPIC_API_KEY` — utilisée seulement si `GEMINI_API_KEY` est absent

## 4. Publication de la page
Settings > Pages > Source : « Deploy from a branch » > branche `main`, dossier `/docs` > Save.

## 5. Premier lancement
Onglet Actions > « Veille notariale » > Run workflow. Attendez la coche verte.
Votre page : https://VOTRE-NOM.github.io/veille-notariale/

Ensuite tout est automatique (chaque matin). En cas d'erreur : ouvrez le run, copiez le journal, envoyez-le à Claude.

## 6. Obtenir la clé gratuite Gemini (facultatif, pour le résumé IA)
1. Allez sur https://aistudio.google.com/apikey et connectez-vous avec un compte Google.
2. Cliquez sur « Create API key ». Aucune carte bancaire n'est demandée pour le niveau gratuit.
3. Copiez la clé (elle commence par `AIza...`).
4. Dans GitHub : Settings > Secrets and variables > Actions > New repository secret. Nom : `GEMINI_API_KEY`, valeur : la clé copiée.

Le résumé apparaîtra automatiquement sous vos textes, étiqueté « Résumé automatique (IA), à vérifier ». Le quota gratuit est largement suffisant pour ce volume ; s'il est un jour dépassé, la page continue de fonctionner normalement, simplement sans résumé ce jour-là.

## 7. Installer comme une application (facultatif mais recommandé)
Ouvrez votre page sur votre téléphone.
- **iPhone (Safari)** : bouton Partager > « Sur l'écran d'accueil ».
- **Android (Chrome)** : menu ⋮ > « Ajouter à l'écran d'accueil » (ou « Installer l'application »).
- **Ordinateur (Chrome/Edge)** : icône d'installation dans la barre d'adresse.

Elle s'ouvre alors en plein écran, comme une vraie application, avec sa propre icône. Le contenu reste toujours à jour automatiquement puisqu'il est généré chaque matin par le même processus.

**Important :** cette « application » est en réalité votre page web hébergée gratuitement par GitHub. Il n'existe pas d'app séparée qui irait chercher les données de son côté : une automatisation doit toujours tourner quelque part avec accès à internet, et GitHub Actions (étape 5) est ce moteur, gratuit et fiable. C'est cette même page qui s'installe comme une app ET qui reçoit les mises à jour automatiques.
