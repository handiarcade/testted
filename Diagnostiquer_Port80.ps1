# Diagnostic en lecture seule. Aucun service ni reglage Windows n'est modifie.
$ErrorActionPreference = 'Continue'
$Report = New-Object System.Text.StringBuilder
$LogPath = Join-Path $PSScriptRoot 'diagnostic_port80.txt'

function Write-Diagnostic([string] $Text) {
    [void] $Report.AppendLine($Text)
    Write-Host $Text
}

function Show-Netsh([string] $Title, [string[]] $Arguments) {
    Write-Diagnostic ''
    Write-Diagnostic $Title
    $Lines = @(& netsh.exe @Arguments 2>&1)
    $ExitCode = $LASTEXITCODE
    foreach ($Line in $Lines) { Write-Diagnostic ([string] $Line) }
    if ($ExitCode -ne 0) {
        Write-Diagnostic "La commande a retourne le code $ExitCode. Relancer en administrateur si l'acces est refuse."
    }
}

function Test-TcpBind([int] $Port) {
    $Listener = $null
    try {
        $Listener = New-Object System.Net.Sockets.TcpListener -ArgumentList ([System.Net.IPAddress]::Any), $Port
        $Listener.Server.ExclusiveAddressUse = $true
        $Listener.Start()
        Write-Diagnostic "Test de liaison TCP $Port : REUSSI sur 0.0.0.0."
    } catch {
        $Cause = $_.Exception
        while ($Cause.InnerException) { $Cause = $Cause.InnerException }
        if ($Cause -is [System.Net.Sockets.SocketException]) {
            Write-Diagnostic "Test de liaison TCP $Port : ECHEC, WinError $($Cause.NativeErrorCode), SocketError $($Cause.SocketErrorCode)."
        } else {
            Write-Diagnostic "Test de liaison TCP $Port : ECHEC ($($Cause.GetType().Name))."
        }
    } finally {
        if ($null -ne $Listener) { $Listener.Stop() }
    }
}

Write-Diagnostic 'Diagnostic du port HTTP 80 - Xbox Downloader'
Write-Diagnostic 'Aucun service arrete, aucune regle supprimee, aucune reservation modifiee.'
Write-Diagnostic 'Fermez Xbox Downloader avant ce test pour eviter de mesurer ses propres ports.'

try {
    $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object System.Security.Principal.WindowsPrincipal -ArgumentList $Identity
    $IsAdmin = $Principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-Diagnostic "Terminal administrateur : $IsAdmin"
} catch {
    Write-Diagnostic 'Impossible de verifier le niveau administrateur.'
}

Write-Diagnostic ''
Write-Diagnostic 'Processus qui ecoutent sur le port TCP 80 :'
$Listeners = @()
if (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue) {
    try {
        $Listeners = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 80 })
        foreach ($Listener in $Listeners) {
            $ProcessName = '(nom indisponible)'
            try { $ProcessName = (Get-Process -Id $Listener.OwningProcess -ErrorAction Stop).ProcessName } catch {}
            Write-Diagnostic "Adresse=$($Listener.LocalAddress) Port=80 PID=$($Listener.OwningProcess) Processus=$ProcessName"
        }
        if ($Listeners.Count -eq 0) { Write-Diagnostic 'Aucun processus en ecoute detecte par Get-NetTCPConnection.' }
    } catch {
        Write-Diagnostic 'Get-NetTCPConnection a echoue ; consulter la sortie netstat ci-dessous.'
    }
} else {
    Write-Diagnostic 'Get-NetTCPConnection indisponible sur cette version de Windows.'
}

# Ne conserver que les lignes TCP dont le port LOCAL vaut 80.
Write-Diagnostic ''
Write-Diagnostic 'Netstat : lignes TCP avec port local 80 (une connexion etablie n est pas un ecouteur) :'
$Netstat80 = @(& netstat.exe -ano -p tcp 2>&1 | Where-Object {
    $Fields = ([string] $_).Trim() -split '\s+'
    $Fields.Count -ge 5 -and $Fields[0] -eq 'TCP' -and $Fields[1] -match ':80$'
})
if ($Netstat80.Count) {
    foreach ($Line in $Netstat80) { Write-Diagnostic ([string] $Line) }
} else {
    Write-Diagnostic 'Aucune ligne TCP locale sur le port 80.'
}

Write-Diagnostic ''
Write-Diagnostic 'Tests temporaires de liaison TCP, fermes immediatement apres chaque test :'
Test-TcpBind 80
Test-TcpBind 8080
Write-Diagnostic '8080 sert uniquement de comparaison : le DNS ne transporte pas le port HTTP.'
Write-Diagnostic 'Un succes sur 8080 ne remplace pas le port 80 pour les telechargements natifs Xbox.'

Show-Netsh 'Plages de ports TCP exclues en IPv4 :' @('interface', 'ipv4', 'show', 'excludedportrange', 'protocol=tcp')
Show-Netsh 'Plages de ports TCP exclues en IPv6 :' @('interface', 'ipv6', 'show', 'excludedportrange', 'protocol=tcp')
Show-Netsh 'HTTP.sys : liste des adresses IP d ecoute (lecture seule) :' @('http', 'show', 'iplisten')

if (@($Listeners | Where-Object { $_.OwningProcess -eq 4 }).Count -gt 0) {
    Write-Diagnostic ''
    Write-Diagnostic 'PID 4 detecte : informations de processus HTTP.sys uniquement.'
    Write-Diagnostic 'Les URL et les details de files de requetes ne sont pas reproduits.'
    $State = @(& netsh.exe http show servicestate view=requestq verbose=no 2>&1)
    $StateExit = $LASTEXITCODE
    $ProcessLines = @($State | Where-Object {
        ([string] $_) -match '(?i)(process|processus|\bPID\b)' -and
        ([string] $_) -notmatch '://'
    })
    foreach ($Line in $ProcessLines) { Write-Diagnostic ([string] $Line) }
    if ($StateExit -ne 0) {
        Write-Diagnostic "Lecture HTTP.sys impossible (code $StateExit). Relancer le diagnostic en administrateur."
    } elseif ($ProcessLines.Count -eq 0) {
        Write-Diagnostic 'Aucune ligne de processus HTTP.sys reconnue dans la sortie.'
    }
}

Write-Diagnostic ''
Write-Diagnostic 'Interpretation :'
Write-Diagnostic '- WinError 10048 / AddressAlreadyInUse : adresse ou port deja utilise.'
Write-Diagnostic '- WinError 10013 / AccessDenied : acces au socket refuse ; reservation, liaison exclusive ou logiciel de securite possibles.'
Write-Diagnostic '- 10013 ne prouve pas a lui seul un manque de droits administrateur.'
Write-Diagnostic '- Si le port 80 appartient a une plage exclue, ne pas supprimer cette plage automatiquement.'
Write-Diagnostic '- PID 4 indique un service Windows utilisant le noyau ; ne pas terminer ce processus.'
Write-Diagnostic '- Le pare-feu concerne notamment les connexions entrantes ; une regle ne libere pas un port deja occupe.'
Write-Diagnostic '- Les URLACL HTTP.sys ne sont pas un remede general au bind d un serveur Python utilisant un socket TCP brut.'
Write-Diagnostic ''
Write-Diagnostic "Journal : $LogPath"
try {
    $Report.ToString() | Set-Content -LiteralPath $LogPath -Encoding UTF8 -ErrorAction Stop
} catch {
    Write-Host 'Impossible d enregistrer le journal. Copier la sortie ou utiliser un dossier accessible en ecriture.'
    exit 2
}
exit 0
