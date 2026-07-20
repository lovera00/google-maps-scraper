<#
.SYNOPSIS
  Todo-en-uno para correr el scraper desatendido el fin de semana:
  refresca proxies -> lanza run.py supervisado -> auto-reinicia con pool fresco,
  con GUARD de RAM que baja workers si la memoria se dispara.

.DESCRIPTION
  Ciclo por cada lanzamiento:
    1. Refresca el pool de proxies (proxy_hunt.py + proxy_maps_test.py) -> proxies.txt.
    2. Genera un config derivado (config.weekend.<tag>.gen.yaml) con el valor
       ACTUAL de workers (sin tocar tu config.yaml) y lanza run.py --resume.
    3. Vigila el proceso cada 30s:
         - salida 0  (fin / Ctrl+C)            -> PARA.
         - salida !=0 (abort IP directa/crash) -> enfria CooldownMin y reinicia.
         - corre sano RefreshHours             -> reinicio programado (pool fresco).
         - GUARD RAM: si RAM >= RamHighPct durante RamSamples muestras seguidas
           -> avisa Telegram y (salvo -NoAutoReduce) reinicia bajando workers
           en WorkerStep, con piso MinWorkers. Solo alerta si ya esta en el piso.
    4. Limpia Chromium huerfano entre reinicios.

  Telegram: usa las MISMAS env vars que el scraper ($env:TELEGRAM_BOT_TOKEN y
  $env:TELEGRAM_CHAT_ID). Si no estan seteadas, los avisos se omiten en silencio.

  Ctrl+C corta el supervisor y el run.py hijo (y su Chromium).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\weekend.ps1
  powershell -ExecutionPolicy Bypass -File .\weekend.ps1 -RamHighPct 82 -WorkerStep 10
  powershell -ExecutionPolicy Bypass -File .\weekend.ps1 -TasksFile data\shards\shard_001.jsonl -Db data\shard_001.db
#>
param(
  [string]$Config       = "config.yaml",
  [string]$TasksFile    = "",
  [string]$Db           = "",
  [int]   $CooldownMin  = 45,      # enfriamiento tras abort por bloqueo con IP directa
  [double]$RefreshHours = 6,       # refresco programado de proxies (0 = off)
  [int]   $MinProxies   = 15,      # aviso si el pool queda por debajo
  [switch]$NoProxyRefresh,
  # --- Guard de RAM ---
  [int]   $RamHighPct   = 85,      # umbral de RAM (% del total) que dispara el guard
  [int]   $RamSamples   = 3,       # muestras seguidas (x30s) sobre el umbral antes de actuar
  [int]   $WorkerStep   = 15,      # cuanto bajar workers en cada disparo
  [int]   $MinWorkers   = 40,      # piso de workers
  [switch]$NoRamGuard,             # desactivar el guard por completo
  [switch]$NoAutoReduce            # guard solo alerta (no baja workers ni reinicia)
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

$stamp  = Get-Date -Format "yyyyMMdd_HHmmss"
$supLog = Join-Path $PSScriptRoot "logs\weekend_$stamp.log"
New-Item -ItemType Directory -Force -Path (Split-Path $supLog) | Out-Null

# Config derivado (por tag, para no chocar si corres 2 shards en paralelo)
$tag = "main"
if ($Db)        { $tag = [System.IO.Path]::GetFileNameWithoutExtension($Db) }
elseif ($TasksFile) { $tag = [System.IO.Path]::GetFileNameWithoutExtension($TasksFile) }
$genCfg = Join-Path $PSScriptRoot ("config.weekend.$tag.gen.yaml")

function Log($msg) {
  $line = "{0} | {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
  Write-Host $line
  Add-Content -Path $supLog -Value $line -Encoding utf8
}

function Get-RamPct {
  $os = Get-CimInstance Win32_OperatingSystem
  return [int][math]::Round(($os.TotalVisibleMemorySize - $os.FreePhysicalMemory) * 100 / $os.TotalVisibleMemorySize, 0)
}

function Send-Telegram($text) {
  $token = $env:TELEGRAM_BOT_TOKEN
  $chat  = $env:TELEGRAM_CHAT_ID
  if ([string]::IsNullOrWhiteSpace($token) -or [string]::IsNullOrWhiteSpace($chat)) { return }
  try {
    $body = @{ chat_id = $chat; text = $text }
    Invoke-RestMethod -Method Post -TimeoutSec 10 `
      -Uri "https://api.telegram.org/bot$token/sendMessage" -Body $body | Out-Null
  } catch {
    Log "AVISO: fallo envio Telegram ($($_.Exception.Message))"
  }
}

function Get-BaseWorkers {
  $raw = [System.IO.File]::ReadAllText($Config)
  $m = [regex]::Match($raw, '(?m)^\s*workers:\s*(\d+)')
  if ($m.Success) { return [int]$m.Groups[1].Value }
  Log "AVISO: no encontre 'workers:' en $Config; usando 50."
  return 50
}

function Write-DerivedConfig($workers) {
  # Copia fiel del config con SOLO el numero de workers reemplazado. Uso .NET
  # File para leer/escribir UTF-8 sin corromper acentos/ñ de las categorias, y
  # sin BOM (PyYAML lo prefiere). Mantiene los ${VAR} intactos para que el
  # loader del scraper expanda las env vars (Telegram, etc.).
  $raw = [System.IO.File]::ReadAllText($Config)
  $new = [regex]::Replace($raw, '(?m)^(\s*workers:\s*)\d+', "`${1}$workers")
  [System.IO.File]::WriteAllText($genCfg, $new)
}

function Count-Proxies {
  if (-not (Test-Path "data\proxies.txt")) { return 0 }
  return (Get-Content "data\proxies.txt" |
          Where-Object { $_ -and -not $_.StartsWith("#") }).Count
}

function Refresh-Proxies {
  if ($NoProxyRefresh) {
    Log "Refresco de proxies SALTEADO (-NoProxyRefresh). Pool actual: $(Count-Proxies)"
    return
  }
  Log "Refrescando proxies (hunt + maps test)... puede tardar unos minutos."
  try {
    & python "scripts\proxy_hunt.py" --concurrency 250 --timeout 7 2>&1 | ForEach-Object { Log "  [hunt] $_" }
    & python "scripts\proxy_maps_test.py" --concurrency 150 --timeout 12 2>&1 | ForEach-Object { Log "  [maps] $_" }
  } catch {
    Log "AVISO: fallo el refresco de proxies ($($_.Exception.Message)). Sigo con el pool que haya."
  }
  $n = Count-Proxies
  Log "Pool de proxies tras refresco: $n usables."
  if ($n -lt $MinProxies) {
    Log "AVISO: pool bajo ($n < $MinProxies). El scraper caera a IP directa mas seguido."
  }
}

function Cleanup-Chromium {
  # run.py sale con os._exit (hard), no cierra Chromium: matamos huerfanos para
  # que no se acumule RAM a lo largo del finde. chrome-headless-shell es de
  # Playwright, no el Chrome normal del usuario.
  try {
    $procs = @(Get-Process "chrome-headless-shell" -ErrorAction SilentlyContinue)
    if ($procs.Count -gt 0) {
      $procs | Stop-Process -Force -ErrorAction SilentlyContinue
      Log "Limpieza: $($procs.Count) procesos chrome-headless-shell terminados."
    }
  } catch {}
}

function Kill-Tree($proc) {
  # Mata el arbol completo (python + node driver + Chromium). VERIFICA que el
  # python realmente murio: taskkill a veces baja el navegador pero deja al
  # python vivo (zombi sin browser). Si sigue vivo, fuerza .Kill(). Espera
  # ACOTADA para no colgar nunca el supervisor (el bug del 18/07 08:10).
  if (-not $proc) { return }
  try { if ($proc.HasExited) { return } } catch { return }
  $id = $proc.Id
  & taskkill /PID $id /T /F 2>&1 | ForEach-Object { Log "  [taskkill] $_" }
  for ($i = 0; $i -lt 10; $i++) {
    try { if ($proc.HasExited) { break } } catch { break }
    Start-Sleep -Milliseconds 500
  }
  try {
    if (-not $proc.HasExited) {
      Log "AVISO: PID $id sobrevivio al taskkill; forzando .Kill()."
      $proc.Kill()
      $proc.WaitForExit(5000) | Out-Null
    }
  } catch {}
  $stillAlive = $true
  try { $stillAlive = -not $proc.HasExited } catch { $stillAlive = $false }
  if ($stillAlive) { Log "AVISO: PID $id AUN vivo tras Kill() (revisar a mano)." }
  else { Log "run.py PID=$id terminado limpio." }
}

# Candado de instancia unica POR tag: evita que dos supervisores (o el usuario
# relanzando sin querer) corran a la vez sobre el mismo shard/DB y se pisen.
# Tags distintos (shard_001 vs shard_002) SI pueden correr en paralelo.
$lockFile = Join-Path $PSScriptRoot ("logs\weekend.$tag.lock")
if (Test-Path $lockFile) {
  $oldPid = (Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1)
  $alive = $false
  if ($oldPid) { $alive = [bool](Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue) }
  if ($alive) {
    Log "Ya hay un supervisor weekend.ps1 (tag=$tag, PID=$oldPid) corriendo. ABORTO para no chocar en la misma BD."
    exit 1
  } else {
    Log "Lock viejo (PID=$oldPid ya no existe); lo piso."
  }
}
Set-Content -Path $lockFile -Value $PID -Encoding ascii

$baseWorkers    = Get-BaseWorkers
$currentWorkers = $baseWorkers

Log "=== weekend.ps1 iniciado (tag=$tag, PID supervisor=$PID) ==="
Log "Config=$Config (base workers=$baseWorkers) TasksFile='$TasksFile' Db='$Db'"
Log "CooldownMin=$CooldownMin RefreshHours=$RefreshHours"
if ($NoRamGuard) {
  Log "Guard de RAM: DESACTIVADO"
} else {
  $mode = "baja workers"; if ($NoAutoReduce) { $mode = "solo alerta" }
  Log "Guard de RAM: umbral $RamHighPct% x$RamSamples muestras -> $mode (step=$WorkerStep, piso=$MinWorkers)"
}
Send-Telegram "Scraper finde: supervisor iniciado (workers=$currentWorkers, guard RAM $RamHighPct%)."

$child = $null
$skipRefreshNext = $false
try {
  $run = 0
  while ($true) {
    $run++
    Log "--- Lanzamiento #$run (workers=$currentWorkers) ---"

    if ($skipRefreshNext) {
      Log "Reinicio por guard: sin refresco de proxies (el pool esta bien)."
      $skipRefreshNext = $false
    } else {
      Refresh-Proxies
    }

    Write-DerivedConfig $currentWorkers
    $pyArgs = @("run.py", "--config", $genCfg)
    if ($TasksFile -ne "") { $pyArgs += @("--tasks-file", $TasksFile) }
    if ($Db -ne "")        { $pyArgs += @("--db", $Db) }

    $child = Start-Process -FilePath "python" -ArgumentList $pyArgs -NoNewWindow -PassThru
    Log "run.py PID=$($child.Id) lanzado con workers=$currentWorkers."

    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $refreshSecs = [int]($RefreshHours * 3600)
    $highCount = 0
    $reason = ""   # "" natural | "scheduled" | "guard"

    while (-not $child.HasExited) {
      Start-Sleep -Seconds 30

      # Refresco programado (proxies frescos tras correr sano un rato)
      if ($RefreshHours -gt 0 -and $sw.Elapsed.TotalSeconds -ge $refreshSecs) {
        Log "Refresco PROGRAMADO tras $RefreshHours h sano: reiniciando con pool fresco."
        $reason = "scheduled"
        break
      }

      # Guard de RAM
      if (-not $NoRamGuard) {
        $pct = Get-RamPct
        if ($pct -ge $RamHighPct) {
          $highCount++
          Log "RAM ALTA: $pct% (>= $RamHighPct%, muestra $highCount/$RamSamples)"
          if ($highCount -ge $RamSamples) {
            if ((-not $NoAutoReduce) -and ($currentWorkers -gt $MinWorkers)) {
              Send-Telegram "⚠️ Scraper: RAM $pct% sostenida. Bajando workers de $currentWorkers a $([math]::Max($MinWorkers, $currentWorkers - $WorkerStep)) y reiniciando."
              $reason = "guard"
              break
            } else {
              $why = "auto-reduce off"; if ($currentWorkers -le $MinWorkers) { $why = "ya en el piso ($MinWorkers)" }
              Log "Guard: RAM $pct% pero $why -> solo alerta, sigo."
              Send-Telegram "⚠️ Scraper: RAM $pct% sostenida ($why). Revisar."
              $highCount = 0   # evitar spamear cada 30s
            }
          }
        } elseif ($highCount -gt 0) {
          Log "RAM normalizada ($pct%); reseteo contador del guard."
          $highCount = 0
        }
      }
    }

    # Detener el hijo si sigue vivo (scheduled/guard) y limpiar Chromium
    Kill-Tree $child
    Cleanup-Chromium

    if ($reason -eq "scheduled") {
      Log "Reinicio programado: sin cooldown."
      continue
    }
    if ($reason -eq "guard") {
      $newWorkers = [math]::Max($MinWorkers, $currentWorkers - $WorkerStep)
      Log "Guard RAM: bajando workers $currentWorkers -> $newWorkers y relanzando (sin cooldown)."
      $currentWorkers = $newWorkers
      $skipRefreshNext = $true
      continue
    }

    # Salida natural del proceso
    $code = $child.ExitCode
    if ($code -eq 0) {
      Log "run.py salio con codigo 0 (fin de pipeline o Ctrl+C). weekend.ps1 PARA."
      Send-Telegram "Scraper finde: pipeline finalizado (codigo 0). Supervisor detenido."
      break
    }

    Log "run.py salio con codigo $code (abort por bloqueo con IP directa / crash)."
    Send-Telegram "Scraper finde: run cayo (codigo $code). Reintento en $CooldownMin min."
    Log "Enfriando $CooldownMin min antes de reintentar (la IP directa venia bloqueada)..."
    Start-Sleep -Seconds ($CooldownMin * 60)
  }
}
finally {
  Kill-Tree $child
  Cleanup-Chromium
  Remove-Item $genCfg -ErrorAction SilentlyContinue
  Remove-Item $lockFile -ErrorAction SilentlyContinue
  Log "=== weekend.ps1 finalizado ==="
}
