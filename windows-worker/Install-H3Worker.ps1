#requires -Version 5.1
[CmdletBinding()]
param([switch]$ForceHash)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$ParentRoot = Join-Path $env:LOCALAPPDATA 'ASKI'
$InstallRoot = Join-Path $ParentRoot 'H3Worker'
$StageRoot = Join-Path $ParentRoot ("H3Worker.stage-" + $PID)
$BackupRoot = Join-Path $ParentRoot ("H3Worker.backup-" + $PID)
$Needle = 'minimax_h3_fl2va_pruned_int8_convrot.safetensors'
$PythonVersion = '3.12.10'
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
$runKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName = 'ASKI-H3-RTX5080-Worker'

function Write-Step([string]$Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Set-PrivateAcl([string]$Path) {
  $sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  & icacls.exe $Path '/inheritance:r' '/grant:r' "*$($sid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Failed to secure ACL: $Path" }
}
function Protect-Token([string]$Token) {
  $plain = [System.Text.Encoding]::UTF8.GetBytes($Token)
  try {
    $cipher = [System.Security.Cryptography.ProtectedData]::Protect(
      $plain, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    return [Convert]::ToBase64String($cipher)
  } finally {
    [Array]::Clear($plain, 0, $plain.Length)
  }
}
function Stop-InstalledWorker([string]$Root) {
  if ([string]::IsNullOrWhiteSpace($Root)) { return }
  $script = Join-Path $Root 'h3_worker.py'
  Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains($script) } |
    ForEach-Object { Invoke-CimMethod -InputObject $_ -MethodName Terminate | Out-Null }
  Start-Sleep -Milliseconds 750
}
function Start-InstalledWorker([string]$Root) {
  $pythonw = Join-Path $Root '.venv\Scripts\pythonw.exe'
  $script = Join-Path $Root 'h3_worker.py'
  if ((Test-Path -LiteralPath $pythonw -PathType Leaf) -and (Test-Path -LiteralPath $script -PathType Leaf)) {
    return Start-Process -FilePath $pythonw -ArgumentList ('"' + $script + '"') -WorkingDirectory $Root -PassThru
  }
  return $null
}
function Test-ModelsDir([string]$Path) {
  if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
  return Test-Path -LiteralPath (Join-Path $Path "diffusion_models\$Needle") -PathType Leaf
}
function Find-ModelsDir {
  $known = @(
    (Join-Path $env:USERPROFILE 'ComfyUI\models'),
    (Join-Path $env:USERPROFILE 'Desktop\ComfyUI\models'),
    (Join-Path $env:USERPROFILE 'Documents\ComfyUI\models'),
    (Join-Path $env:LOCALAPPDATA 'Programs\ComfyUI\models'),
    'D:\ComfyUI\models','D:\AI\ComfyUI\models','E:\ComfyUI\models','E:\AI\ComfyUI\models',
    'F:\ComfyUI\models','F:\AI\ComfyUI\models'
  )
  foreach ($candidate in $known) { if (Test-ModelsDir $candidate) { return (Resolve-Path $candidate).Path } }
  Write-Host 'Known paths did not contain exact H3; scanning the current user profile...' -ForegroundColor Yellow
  $found = Get-ChildItem -LiteralPath $env:USERPROFILE -Filter $Needle -File -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($null -ne $found) { return (Split-Path (Split-Path $found.FullName -Parent) -Parent) }
  throw "Exact H3 model set not found ($Needle)."
}
function Find-ComfyRoot([string]$ModelsDir) {
  $parent = Split-Path $ModelsDir -Parent
  $known = @(
    $parent,
    (Join-Path $env:USERPROFILE 'ComfyUI'),
    (Join-Path $env:USERPROFILE 'Desktop\ComfyUI'),
    (Join-Path $env:USERPROFILE 'Documents\ComfyUI'),
    (Join-Path $env:LOCALAPPDATA 'Programs\ComfyUI'),
    'D:\ComfyUI','D:\AI\ComfyUI','E:\ComfyUI','E:\AI\ComfyUI','F:\ComfyUI','F:\AI\ComfyUI'
  )
  foreach ($candidate in $known) {
    if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'main.py') -PathType Leaf)) {
      return (Resolve-Path $candidate).Path
    }
  }
  $found = Get-ChildItem -LiteralPath $env:USERPROFILE -Filter main.py -File -Recurse -ErrorAction SilentlyContinue |
    Where-Object { $_.DirectoryName -match 'ComfyUI' } | Select-Object -First 1
  if ($null -ne $found) { return $found.DirectoryName }
  throw 'ComfyUI main.py root not found.'
}
function Ensure-Python {
  try {
    & py -3.12 -c 'import sys; assert sys.version_info[:2] == (3, 12)' 2>$null
    if ($LASTEXITCODE -eq 0) { return 'py' }
  } catch {}
  Write-Step "Installing Python $PythonVersion for the current user"
  $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
  Invoke-WebRequest -UseBasicParsing -Uri $PythonUrl -OutFile $installer
  $signature = Get-AuthenticodeSignature -FilePath $installer
  if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
    Remove-Item $installer -Force -ErrorAction SilentlyContinue
    throw 'Python installer signature validation failed.'
  }
  $process = Start-Process -FilePath $installer -ArgumentList '/quiet','InstallAllUsers=0','PrependPath=0','Include_launcher=1','Include_pip=1' -Wait -PassThru
  Remove-Item $installer -Force -ErrorAction SilentlyContinue
  if ($process.ExitCode -ne 0) { throw "Python installer failed: $($process.ExitCode)" }
  return 'py'
}

Write-Step 'Checking private package files'
$required = @('h3_worker.py','server.py','model-manifest.json','requirements.txt','README.md','Uninstall-H3Worker.ps1','worker-token.txt')
foreach ($name in $required) {
  if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name) -PathType Leaf)) { throw "Package file missing: $name" }
}
$tokenFile = Join-Path $PSScriptRoot 'worker-token.txt'
$token = (Get-Content -LiteralPath $tokenFile -Raw).Trim()
if ($token.Length -lt 32) { throw 'worker-token.txt is invalid.' }
$protectedToken = Protect-Token $token

Write-Step 'Locating exact H3 models and ComfyUI'
$modelsDir = Find-ModelsDir
$comfyDir = Find-ComfyRoot $modelsDir
Write-Host "Models: $modelsDir"
Write-Host "ComfyUI: $comfyDir"
$pageFileMiB = (Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | Measure-Object -Property AllocatedBaseSize -Sum).Sum
if ($null -eq $pageFileMiB) { $pageFileMiB = 0 }
if ([int64]$pageFileMiB -lt 49152) {
  Write-Warning "Allocated pagefile is $pageFileMiB MiB. Exact H3 on 16GB VRAM is safer with a 64GB+ system-managed pagefile."
}

$launcher = Ensure-Python
Write-Step 'Building a private staged installation'
New-Item -ItemType Directory -Force -Path $ParentRoot | Out-Null
Set-PrivateAcl $ParentRoot
Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $BackupRoot -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $StageRoot | Out-Null
Set-PrivateAcl $StageRoot
foreach ($name in @('h3_worker.py','server.py','model-manifest.json','requirements.txt','README.md','Uninstall-H3Worker.ps1')) {
  Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $StageRoot $name) -Force
}
$config = [ordered]@{
  api_base = 'https://h3-video-web.vercel.app'
  worker_token_dpapi = $protectedToken
  comfy_url = 'http://127.0.0.1:8188'
  comfy_dir = $comfyDir
  models_dir = $modelsDir
  comfy_args = @('--lowvram','--reserve-vram','1.5')
}
$configJson = $config | ConvertTo-Json -Depth 4
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText((Join-Path $StageRoot 'config.json'), $configJson, $utf8NoBom)
Set-PrivateAcl $StageRoot

$venv = Join-Path $StageRoot '.venv'
& $launcher -3.12 -m venv $venv
if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
$stagePython = Join-Path $venv 'Scripts\python.exe'
& $stagePython -m pip install --disable-pip-version-check --require-virtualenv -r (Join-Path $StageRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Worker dependency installation failed.' }

$hadPrevious = Test-Path -LiteralPath $InstallRoot -PathType Container
Stop-InstalledWorker $InstallRoot
try {
  Write-Step 'Running RTX 5080, exact-model SHA-256, node, GPU, and generation self-test'
  $selfTestArgs = @((Join-Path $StageRoot 'h3_worker.py'),'--self-test')
  if ($ForceHash) { $selfTestArgs += '--force-hash' }
  & $stagePython @selfTestArgs
  if ($LASTEXITCODE -ne 0) { throw 'Readiness self-test failed. Existing installation was not replaced.' }

  Write-Step 'Atomically activating the staged installation'
  if ($hadPrevious) { Move-Item -LiteralPath $InstallRoot -Destination $BackupRoot }
  try {
    Move-Item -LiteralPath $StageRoot -Destination $InstallRoot
  } catch {
    if ($hadPrevious -and (Test-Path -LiteralPath $BackupRoot)) {
      Move-Item -LiteralPath $BackupRoot -Destination $InstallRoot
    }
    throw
  }
  Remove-Item -LiteralPath $BackupRoot -Recurse -Force -ErrorAction SilentlyContinue
  Set-PrivateAcl $InstallRoot
} catch {
  Remove-Item -LiteralPath $StageRoot -Recurse -Force -ErrorAction SilentlyContinue
  if (-not (Test-Path -LiteralPath $InstallRoot) -and (Test-Path -LiteralPath $BackupRoot)) {
    Move-Item -LiteralPath $BackupRoot -Destination $InstallRoot
  }
  if (Test-Path -LiteralPath $InstallRoot) { Start-InstalledWorker $InstallRoot | Out-Null }
  throw
}

Write-Step 'Registering current-user startup and starting the new worker'
$pythonw = Join-Path $InstallRoot '.venv\Scripts\pythonw.exe'
$workerScript = Join-Path $InstallRoot 'h3_worker.py'
$runCommand = '"' + $pythonw + '" "' + $workerScript + '"'
New-Item -Path $runKey -Force | Out-Null
Set-ItemProperty -Path $runKey -Name $runName -Value $runCommand
$workerProcess = Start-InstalledWorker $InstallRoot
if ($null -eq $workerProcess) { throw 'Activated worker could not be started.' }
Start-Sleep -Seconds 8
$workerProcess.Refresh()
if ($workerProcess.HasExited) { throw "New worker exited early. See $InstallRoot\worker.log" }
$running = Get-CimInstance Win32_Process -Filter "ProcessId=$($workerProcess.Id)" -ErrorAction SilentlyContinue
if ($null -eq $running -or -not $running.CommandLine.Contains($workerScript)) {
  throw 'Could not verify the newly activated local worker process.'
}

Remove-Item -LiteralPath $tokenFile -Force
$token = $null
$protectedToken = $null

try {
  $status = Invoke-RestMethod -UseBasicParsing -Uri 'https://h3-video-web.vercel.app/api/jobs' -TimeoutSec 20
  if (-not $status.workers.rtx5080.eligible) { throw 'New heartbeat arrived but worker is not eligible.' }
  Write-Host "`nINSTALL OK - RTX 5080 worker is online and eligible." -ForegroundColor Green
} catch {
  Write-Warning "New worker is running locally, but production heartbeat is not eligible yet: $($_.Exception.Message)"
  Write-Host "See $InstallRoot\worker.log"
}
