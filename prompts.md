# Prompts for the agent (paste one at a time, in order)

Your project (Vrio) is one model with several views. AS-IS and TO-BE are two VIEWS of it,
so the agent must use the views to tell them apart (the model contains duplicate copies of some
elements, one per view).

Setup message (once, at the start of the chat):

> Tu es un architecte d'entreprise. Tu as un serveur MCP ArchiMate en lecture seule sur le
> projet « Vrio ». Utilise UNIQUEMENT ses outils, n'invente rien. Commence par lister les vues
> (archimate_list_views). Les vues « Vrio - Architecture existante (AS-IS) » et
> « Vrio - Architecture cible (TO-BE) » définissent respectivement l'état actuel et l'état cible :
> utilise archimate_get_view pour savoir quels éléments appartiennent à chacune.
> Si un outil renvoie "Rate limit exceeded", attends quelques secondes et réessaie. Réponds en français.

## 1. Inventaire → « Référentiel du portefeuille applicatif »
> Pour la vue AS-IS, liste tous les composants applicatifs (ApplicationComponent). Pour
> chacun, donne : les services applicatifs qu'il réalise (Realization), les objets de données
> qu'il utilise (Access) et les processus métier qu'il soutient (via les services, Serving).
> Présente le résultat dans un tableau « Référentiel du portefeuille applicatif ».

## 2. Détection des redondances
> Dans la vue AS-IS, trouve les services applicatifs réalisés par plus d'un composant.
> Pour chaque redondance, nomme les composants concernés et propose lequel retirer, en
> justifiant. Relie ce constat à « Documentation dispersée et incohérente » et à l'objectif
> « Rationaliser le portefeuille applicatif ».

## 3. Analyse d'écarts AS-IS → TO-BE
> Compare les éléments des vues AS-IS et TO-BE par nom et par type. Donne trois listes :
> supprimés (seulement en AS-IS), ajoutés (seulement en TO-BE), conservés ou transformés
> (ex. « Données clients » → « Données clients centralisées »). Les éléments qui portent le
> même nom dans les deux vues sont des copies : traite-les comme conservés. Termine par un tableau.

## 4. Documentation
> Pour chaque composant applicatif de la vue TO-BE, génère une fiche avec ce modèle :
> Nom / Type / Services réalisés / Données utilisées / Processus soutenus / Hébergement
> (nœud ou artefact lié) / Statut (conservé, consolidé, nouveau) / Remarques.
> Puis rédige une recommandation de migration de 10 lignes maximum : étapes, risques,
> retrait de l'ancienne application de facturation, gouvernance du portefeuille.
