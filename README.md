# Xbox Downloader + SmartGlass

Extraire tout le ZIP dans un NOUVEAU dossier sur le PC Windows. Ce dossier
remplace les versions copiees dans la conversation. Ne pas melanger les versions.

## Fichiers a conserver ensemble

- Lancer_Xbox.bat : seul fichier a double-cliquer.
- xbox_suite.py : interface complete, bouton SmartGlass.
- xbox_app.py : interface de base et serveur HTTP.
- xbox_dns_spoof.py : DNS et relais.
- xbox_notifications.py : detection des transferts, source directe, page message.
- xbox_smartglass.py : connexion Microsoft et commandes SmartGlass.
- xbox_devtools.py : acces au portail Xbox en mode developpeur.
- requirements.txt : versions des dependances installees par le BAT.
- README.md : cette notice.

Les fichiers test_*.py sont optionnels pour l'utilisation, utiles pour verifier
le programme. Aucun compte, mot de passe ou jeton n'est fourni dans cette archive.

## Demarrage Windows

1. Installer Python 3.12 pour Windows avec Tcl/Tk et le lanceur Python.
2. Extraire le ZIP entier, par exemple dans Documents/XboxDownloader.
3. Clic droit sur Lancer_Xbox.bat > Executer en tant qu'administrateur pour HTTP/DNS.
4. Au premier lancement, le BAT cree .venv et installe les dependances depuis PyPI.
   Les lancements suivants reutilisent ce dossier. Garder une connexion Internet.
5. Le BAT laisse la fenetre noire ouverte et affiche les erreurs. Son journal est
   xbox_demarrage.log, dans le dossier de l'application.

Le lancement Windows du BAT doit encore etre verifie sur le PC de l'utilisateur.
L'interface Tk a ete ouverte et manipulee dans un ecran virtuel Linux.

## Lancer des applications avec SmartGlass

L'integration utilise le fournisseur SmartGlass de xbox-webapi (OpenXbox) via le
service Microsoft xccs.xboxlive.com. Ce n'est pas l'ancienne bibliotheque locale
xbox-smartglass-core, qui impose des dependances anciennes.

1. Sur la Xbox : Parametres > Appareils et connexions > Fonctionnalites a distance.
   Activer les fonctions a distance et utiliser un compte Microsoft associe a cette
   console. Les libelles exacts peuvent varier selon la mise a jour Xbox.
2. Dans le programme : bouton SmartGlass : lancer une application Xbox.
3. Cliquer Connexion Microsoft / Charger les consoles.
4. Se connecter dans le navigateur du PC via le client public OpenXbox. Aucun mot de
   passe Microsoft n'est demande dans le programme ou dans la conversation.
   Le retour de connexion utilise localhost:8080/auth/callback ; le port 8080 du
   PC doit etre libre. La tentative expire apres deux minutes.
5. Choisir la console, charger les applications installees, choisir une application.
6. Cliquer Lancer l'application sur la Xbox.

Une commande acceptee par le service n'est pas automatiquement un lancement reussi.
L'interface attend une confirmation pendant 15 secondes ; sinon elle indique que le
lancement n'est pas confirme. Une erreur de controle distant est affichee.
Certains paquets/systemes peuvent ne pas etre listes avec un identifiant Store
exploitable. Le programme ne pretend pas lancer ceux que l'API ne retourne pas.

Option : cocher Lancer cette application au premier transfert apres Activer,
puis activer les services HTTP/DNS. La cible est figee au demarrage. Une seule
commande automatique est envoyee par activation, meme avec plusieurs requetes Range.
Arreter puis reactiver pour changer la cible. L'automatisation est decochee par defaut.

Les jetons restent hors du dossier du programme :
Windows : %LOCALAPPDATA%/XboxDownloader/smartglass_tokens.json
Ne pas partager ce fichier. Sa suppression deconnecte localement le programme.
Aucun lancement sur une vraie console n'a pu etre teste dans le cloud.

## HTTP/DNS et message

Pour ces fonctions, mettre PC et Xbox sur le meme reseau local et utiliser un
profil reseau Windows Prive. Choisir l'URL ou le fichier, cliquer Activer puis
attendre ACTIF. Configurer le DNS manuel Xbox avec l'IP locale du PC indiquee.

La case Imposer ma source aux requetes HTTP est active par defaut : une URL en
mode redirection est servie par le proxy du PC, sans renvoyer 302 a la console.
Un fichier local unique repond aux noms demandes ; pour un dossier, les noms
doivent correspondre. Range et HEAD sont pris en charge. Les erreurs de source
restent des erreurs : aucun repli automatique n'est ajoute.

Cela s'applique seulement aux requetes HTTP atteignant le PC. Le programme ne
force pas la console a accepter un autre paquet et ne modifie pas HTTPS, les
signatures, l'integrite ou les licences. Un transfert signale correspond aux
premiers octets envoyes, pas a une confirmation d'installation.

Dans Edge sur Xbox, ouvrir une fois http://IP-DU-PC/message et garder la page
active. Elle affiche automatiquement « non non leo interdit » apres un transfert,
en interrogeant /status toutes les deux secondes. Cela ne cree pas une fenetre
systeme Xbox. Le lancement d'Edge via SmartGlass ne transmet pas une URL arbitraire.
La page ne bloque pas les telechargements. La sonde 443 est uniquement un diagnostic,
elle ne fournit pas HTTPS.

Apres utilisation : Arreter puis restaurer le DNS automatique sur la console.
Les regles de pare-feu sont limitees au reseau local prive et a la session.
Un arret force peut laisser des regles XboxDownloader-... a retirer manuellement.
Les portproxy existants ne sont pas modifies.

## Verification developpeur

Avec les dependances installees :
python -m unittest -v test_xbox test_notifications test_forced_source test_smartglass test_suite test_http_startup test_smartglass_api_compat test_devtools

Les tests HTTP/DNS utilisent des serveurs locaux temporaires. Les tests SmartGlass
utilisent des API simulees et un vrai callback HTTP local ; aucun compte reel et
aucune console ne sont contactes. Les tests d'interface necessitent un ecran Tk.
Sous Linux sans ecran, seuls ces tests d'interface sont ignores.

## Port HTTP 80 refuse au demarrage

Si DNS demarre mais HTTP echoue, fermer l'application puis lancer
Diagnostiquer_Port80.bat par clic droit > Executer en tant qu'administrateur.
Conserver Diagnostiquer_Port80.ps1 a cote du BAT. Le rapport diagnostic_port80.txt
indique les processus en ecoute, les exclusions de ports TCP, des informations
HTTP.sys et le resultat de tests temporaires de liaison 80/8080. Aucun service
n'est arrete et aucune reservation ni regle systeme n'est modifiee.
Si une politique empeche le script PowerShell, le BAT produit un diagnostic CMD
plus limite sans modifier cette politique.

WinError 10013 peut venir d'une exclusion TCP, d'une liaison exclusive ou d'une
restriction de securite. WinError 10048 indique une adresse ou un port occupe.
Le nouveau message HTTP conserve le code et le texte de l'erreur d'origine.
Les droits administrateur ne liberent pas un port deja occupe ou reserve.

Le port 8080 sert seulement a comparer les restrictions : remplacer silencieusement
80 par 8080 ne permettrait pas aux telechargements Xbox natifs de fonctionner,
car une reponse DNS n'indique pas le port HTTP.

Les scripts de diagnostic Windows doivent etre executes sur le PC concerne ;
ils n'ont pas pu etre testes dans l'environnement Linux. Les tests Python
reproduisent le refus d'acces et le conflit avec un vrai port local deja occupe.

## Echec pendant Charger les applications installees

La liste des consoles peut fonctionner alors que le modele de reponse des
applications du SDK OpenXbox rejette des metadonnees d'installation absentes ou
nulles. La lecture compatible conserve le statut du service et les identifiants
Store, mais n'exige pas les metadonnees inutiles au lancement.
Elle ne transforme pas un refus HTTP ou Xbox en succes. Les objets sans identifiant
Store restent exclus de la selection.

Le message d'erreur indique maintenant l'etape : connexion Microsoft/Xbox, liste
des consoles, chargement des applications ou lancement. Les erreurs de format,
reseau, proxy, delai et authentification sont distinguees sans afficher les jetons
ou les valeurs contenues dans les reponses Microsoft.

Si le probleme persiste, transmettre le nouveau message affiche dans l'application
(aucun fichier de jetons n'est necessaire).

## Dossiers et applications du mode developpeur

Dans la fenetre SmartGlass, cliquer Dossiers / applications du mode developpeur.
Cette fenetre n'exige pas de connexion Microsoft SmartGlass : elle ouvre le portail
local de la console dans le navigateur du PC.

1. Sur la Xbox, activer le mode developpeur et ouvrir Dev Home. Le changement depuis
   le mode normal necessite un redemarrage.
2. Dans Remote Access Settings, activer Xbox Device Portal et definir ses identifiants.
3. Saisir dans l'outil l'IP, le nom reseau ou l'URL HTTPS affichee dans Dev Home.
   Le port par defaut est 11443. Ne pas utiliser l'adresse du PC a la place de la Xbox.
4. Le bouton de donnees ouvre le portail ; choisir File Explorer, puis le paquet
   de l'application pour explorer les donnees accessibles de l'application sideloadee.
5. Pour les applications dev : dans le portail, My games & apps > Actions > Launch.

Le navigateur gere l'authentification et la confiance du certificat du portail.
L'outil ne stocke pas ces identifiants et ne desactive aucune verification TLS.
Il demande l'ouverture du portail, sans pretendre avoir confirme une connexion.

SmartGlass en mode normal ne fournit ni l'exploration des dossiers proteges des
applications retail ni le lancement direct d'un paquet installe seulement en mode dev.
Les applications dev ne sont pas ajoutees artificiellement a la liste SmartGlass.

Pour une application dont vous etes l'editeur, une version compatible Xbox peut
eventuellement etre distribuee en mode normal via le Microsoft Store, y compris
en audience privee pour des comptes de test. Cela requiert l'admissibilite Xbox,
Partner Center et la certification : une copie du paquet dev ne suffit pas.
Les package flights concernent une application deja publiee et compatible Xbox.
Le type et les sources de Handiarcade restent necessaires pour evaluer cette voie.

Sources Microsoft :
- https://github.com/MicrosoftDocs/windows-dev-docs/blob/docs/uwp/debug-test-perf/device-portal.md
- https://github.com/MicrosoftDocs/windows-dev-docs/blob/docs/hub/apps/publish/beta-testing-and-targeted-distribution.md
- https://github.com/MicrosoftDocs/windows-dev-docs/blob/docs/uwp/apps-for-xbox/partner-center-xbox.md
