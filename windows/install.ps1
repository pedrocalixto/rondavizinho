# Instalador do RondaVizinho para Windows (perfil "PC sempre ligado").
#
# Caminho normal (leigo): uma linha no PowerShell, que baixa o código e chama
# este script — ao final o navegador abre no assistente de instalação:
#   irm https://rondavizinho.com.br/get.ps1 | iex
#
# Uso direto (técnico), num PowerShell COMO ADMINISTRADOR, na pasta do projeto:
#   powershell -ExecutionPolicy Bypass -File windows\install.ps1
#
# Parâmetros (para testes e instalações paralelas):
#   -SemTarefas       não registra/inicia as tarefas agendadas nem abre navegador
#   -Dados <pasta>    pasta de dados/config (padrão: %ProgramData%\Vigia)
param(
    [switch]$SemTarefas,
    [string]$Dados = (Join-Path $env:ProgramData "Vigia")
)
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot

Write-Host "== RondaVizinho — instalador Windows ==" -ForegroundColor Cyan

# 1. Python — instala do python.org (sem winget: nem todo Windows tem).
# IMPORTANTE: o Windows traz um ATALHO "python.exe" (Microsoft Store) que o
# Get-Command ACHA mas que só abre a loja ("Python nao encontrado" ao rodar).
# Por isso testamos se o python REALMENTE roda, ignorando esse atalho.
function Test-PythonReal {
    $c = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $c -or $c.Source -like "*\WindowsApps\*") { return $false }
    try { & python.exe --version 2>$null | Out-Null; return ($LASTEXITCODE -eq 0) }
    catch { return $false }
}
if (-not (Test-PythonReal)) {
    Write-Host "Instalando Python (python.org)..."
    $pyExe = "$env:TEMP\python-setup.exe"
    curl.exe -sL -o $pyExe https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe
    # /quiet silencioso; InstallAllUsers p/ máquina; PrependPath põe na frente do PATH
    # (antes do atalho da Store, que fica no PATH de usuário)
    Start-Process $pyExe -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_launcher=1" -Wait
    Remove-Item $pyExe -Force -ErrorAction SilentlyContinue
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
if (-not (Test-PythonReal)) {
    Write-Host "ERRO: nao consegui instalar o Python. Instale manualmente de" -ForegroundColor Red
    Write-Host "https://www.python.org/downloads/ (marque 'Add to PATH') e rode de novo." -ForegroundColor Red
    exit 1
}
python --version

# 2. ffmpeg — download direto para C:\tools\ffmpeg + PATH de MÁQUINA.
# (winget instala no PATH de usuário e às vezes nem entrega o binário — a
# tarefa S4U e sessões sem login não enxergam o ffmpeg no PATH de usuário)
if (-not (Test-Path "C:\tools\ffmpeg\ffmpeg.exe")) {
    Write-Host "Baixando ffmpeg (essentials)..."
    $zip = "$env:TEMP\ffmpeg.zip"
    curl.exe -sL -o $zip https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip
    Expand-Archive -Path $zip -DestinationPath "$env:TEMP\ffx" -Force
    $bin = (Get-ChildItem "$env:TEMP\ffx" -Directory | Select-Object -First 1).FullName + "\bin"
    New-Item -ItemType Directory -Force -Path "C:\tools\ffmpeg" | Out-Null
    Copy-Item "$bin\ffmpeg.exe" "C:\tools\ffmpeg\" -Force
    Remove-Item $zip, "$env:TEMP\ffx" -Recurse -Force
    $mp = [Environment]::GetEnvironmentVariable("Path", "Machine")
    if ($mp -notlike "*C:\tools\ffmpeg*") {
        [Environment]::SetEnvironmentVariable("Path", $mp + ";C:\tools\ffmpeg", "Machine")
    }
    $env:Path += ";C:\tools\ffmpeg"
}
ffmpeg -version | Select-Object -First 1

# 3. pasta de dados; a config quem escreve é o ASSISTENTE web (passo a passo
# no navegador) — sem config ainda = instalação nova, modo assistente
New-Item -ItemType Directory -Force -Path $Dados | Out-Null
$cfg = Join-Path $Dados "config.json"
$env:VIGIA_CONFIG = $cfg
$assistente = -not (Test-Path $cfg)

# 4. valida — exit code de exe nativo NAO dispara $ErrorActionPreference, checa na mao
Push-Location $repo
python -m vigia --check
if ($LASTEXITCODE -ne 0) {
    Pop-Location
    Write-Host "ERRO: self-check falhou — o download pode ter vindo corrompido." -ForegroundColor Red
    exit 1
}
if (-not $assistente) {
    # já existe config (reinstalação/atualização): prova real antes de agendar
    Write-Host "Config existente — rodando teste ponta-a-ponta (frame -> IA -> Telegram)..." -ForegroundColor Cyan
    python -m vigia --teste
    if ($LASTEXITCODE -ne 0) {
        Pop-Location
        Write-Host "ERRO: teste ponta-a-ponta falhou (veja acima). NADA foi agendado." -ForegroundColor Red
        Write-Host "Corrija e rode o instalador de novo — ele retoma daqui." -ForegroundColor Red
        exit 1
    }
}
Pop-Location

if ($SemTarefas) {
    Write-Host "OK (-SemTarefas): dependências e código prontos em $repo; nada agendado." -ForegroundColor Yellow
    exit 0
}

# 5. tarefas agendadas: iniciam com o Windows e reiniciam se caírem.
# Caminho COMPLETO do pythonw: a tarefa agendada NAO herda o PATH recem-criado
# pelo instalador do Python (o serviço do Agendador usa um ambiente cacheado),
# então "pythonw.exe" pelo nome puro falharia e nada subiria na porta 8080.
$pyw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pyw) { $pyw = ((Get-Command python.exe).Source) -replace 'python\.exe$', 'pythonw.exe' }

$gatilho = New-ScheduledTaskTrigger -AtStartup
$config = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
          -ExecutionTimeLimit (New-TimeSpan -Days 3650) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# S4U: roda mesmo sem usuario logado (boot pos-queda de luz), sem armazenar senha.
# O default (Interactive) so roda com o usuario logado — inutil como appliance.
$quem = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType S4U -RunLevel Highest

$acao = New-ScheduledTaskAction -Execute $pyw -Argument "-m vigia" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "VigiaVizinhanca" -Action $acao -Trigger $gatilho `
    -Settings $config -Principal $quem -Force | Out-Null

$acaoWeb = New-ScheduledTaskAction -Execute $pyw `
    -Argument "webui\server.py --porta 8080" -WorkingDirectory $repo
Register-ScheduledTask -TaskName "VigiaWeb" -Action $acaoWeb -Trigger $gatilho `
    -Settings $config -Principal $quem -Force | Out-Null

# AQUECE a verificação do antivírus: na 1ª instalação o Defender escaneia o
# pythonw.exe e os .py recém-extraídos no PRIMEIRO uso, o que atrasa (>1 min) o
# lançamento da tarefa e faria o navegador abrir antes do painel subir. Rodar o
# --check com o MESMO pythonw força esse escaneamento agora (e valida o painel),
# então a tarefa lança rápido logo em seguida.
Write-Host "Preparando o painel..." -ForegroundColor Cyan
try { Start-Process $pyw -ArgumentList "webui\server.py --check" -WorkingDirectory $repo -Wait -WindowStyle Hidden } catch {}

Start-ScheduledTask -TaskName "VigiaWeb"

if ($assistente) {
    # o daemon só entra em serviço quando o assistente terminar; espera o painel
    # subir de fato ANTES de abrir o navegador (senão o Chrome bate na porta
    # antes do bind e mostra "recusou conexão").
    Write-Host "Iniciando o assistente..." -ForegroundColor Cyan
    # 127.0.0.1 (NAO "localhost"): localhost resolve p/ IPv6 ::1 primeiro, que
    # trava ate o timeout (o servidor escuta so IPv4), fazendo o loop nunca
    # detectar o painel mesmo com ele no ar.
    $ok = $false
    for ($i = 0; $i -lt 45; $i++) {
        Start-Sleep -Seconds 1
        try { Invoke-WebRequest "http://127.0.0.1:8080/" -UseBasicParsing -TimeoutSec 2 | Out-Null
              $ok = $true; break } catch {}
    }
    if ($ok) {
        Write-Host "`nInstalado! Abrindo o assistente no navegador..." -ForegroundColor Green
        try { Start-Process "http://localhost:8080" }   # nao-fatal (headless/sem navegador)
        catch { Write-Host "Abra http://localhost:8080 no navegador para continuar." -ForegroundColor Yellow }
    } else {
        Write-Host "`nInstalado, mas o painel demorou a responder. Abra" -ForegroundColor Yellow
        Write-Host "http://localhost:8080 no navegador em alguns segundos." -ForegroundColor Yellow
    }
    Write-Host "Siga os passos no navegador — o assistente acha seu DVR e testa tudo." -ForegroundColor Green
} else {
    # reinstalação/atualização (já tem config): o daemon também entra em serviço
    Start-ScheduledTask -TaskName "VigiaVizinhanca"
    Write-Host "`nRondaVizinho instalado e rodando. Mande /status pro seu bot no Telegram." -ForegroundColor Green
    Write-Host "Painel: http://localhost:8080 (histórico, fotos e gráfico)" -ForegroundColor Green
}
Write-Host "Logs: Agendador de Tarefas > VigiaVizinhanca / VigiaWeb. Config: $cfg"
