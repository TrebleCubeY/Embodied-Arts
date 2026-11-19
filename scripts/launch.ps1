<#
.SYNOPSIS
  一臂三身 · 演示启动器。一条命令把 WSL 桥、Windows 后端、两块观众屏上的页面都拉起来。

.EXAMPLE
  # 最常用：按默认档（停住）起全套，两块外接屏各一窗
  .\scripts\start_demo.cmd

.EXAMPLE
  # 本机单屏验证：两个窗口并排，左边数字层右边代码
  .\scripts\start_demo.cmd -SideBySide

.EXAMPLE
  # 不要机械臂，只演屏幕（桥挂了/教室没场地时兜底）
  .\scripts\start_demo.cmd -NoRobot -SideBySide

.EXAMPLE
  # 直接进遥操
  .\scripts\start_demo.cmd -Mode teleop

.NOTES
  前提（少一样就会在对应那一步报红）：
    Windows  D:\conda_envs\so101-twin 里有 websockets；装了 Chrome
    WSL      Ubuntu，$HOME/miniconda3/envs/lerobot 里装了 lerobot
    USB      两条 CH343 串口线插着，且 usbipd 已经 bind（bind 只需要做过一次）
    校准     ~/.cache/huggingface/lerobot/calibration 下有 zyf_follower / zyf_leader
    零位     robot/offsets.json 存在（换过校准就要重量）
#>
[CmdletBinding()]
param(
  [ValidateSet('hold', 'teleop', 'keyframes')]
  [string]$Mode = 'hold',

  [ValidateSet('both', 'twin', 'code')]
  [string]$View = 'both',

  # 本机只有一块屏时用：两个窗口各占半屏
  [switch]$SideBySide,

  # 指定屏幕编号（先跑 -ListScreens 看编号）。-1 表示自动挑
  [int]$ScreenA = -1,
  [int]$ScreenB = -1,

  # 交换两块屏上的视图
  [switch]$Swap,

  # 不起 WSL 桥，用动作库当替身：屏幕能演全套，臂不动
  [switch]$NoRobot,

  [switch]$NoBrowser,

  # 跳过 usbipd attach（设备已经挂好时用，省一次管理员提示）
  [switch]$NoAttach,

  # 只打印屏幕编号就退出
  [switch]$ListScreens,

  [int]$Port = 8000,
  [int]$WsPort = 8001,
  [string]$Wsl = 'Ubuntu'
)

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $PSScriptRoot
$Tmp = Join-Path $Root '.tmp'
New-Item -ItemType Directory -Path $Tmp -Force | Out-Null

$Py = 'D:\conda_envs\so101-twin\Scripts\python.exe'
$Chrome = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
$BridgePort = 8765

function To-WslPath([string]$p) {
  $d = $p.Substring(0, 1).ToLower()
  $rest = $p.Substring(2).Replace('\', '/')
  return "/mnt/$d$rest"
}
$WslRoot = To-WslPath $Root

function Write-Step([string]$n, [string]$text) {
  Write-Host ''
  Write-Host ("[{0}] {1}" -f $n, $text) -ForegroundColor Cyan
}
function Write-Ok([string]$text) { Write-Host ("     OK  {0}" -f $text) -ForegroundColor Green }
function Write-Note([string]$text) { Write-Host ("     ..  {0}" -f $text) -ForegroundColor DarkGray }
function Write-Warn2([string]$text) { Write-Host ("     !   {0}" -f $text) -ForegroundColor Yellow }
function Write-Err2([string]$text) { Write-Host ("     X   {0}" -f $text) -ForegroundColor Red }

function Test-Tcp([int]$p, [int]$timeoutSec = 10) {
  $end = (Get-Date).AddSeconds($timeoutSec)
  while ((Get-Date) -lt $end) {
    try {
      $c = New-Object Net.Sockets.TcpClient
      $c.Connect('127.0.0.1', $p)
      $c.Close()
      return $true
    } catch {
      Start-Sleep -Milliseconds 250
    }
  }
  return $false
}

function Stop-Port([int]$p) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) {
    $proc = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    $name = if ($proc) { $proc.ProcessName } else { '?' }
    Write-Warn2 ("端口 {0} 被 {1}({2}) 占着，结束它" -f $p, $name, $c.OwningProcess)
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 300
  }
}

function Get-SerialBusids {
  $rows = @()
  $lines = @()
  try { $lines = (usbipd list 2>$null) -split "`r?`n" } catch { return $rows }
  foreach ($line in $lines) {
    $t = $line.Trim()
    if ($t -notmatch '^\d+-\d+') { continue }
    $parts = $t -split '\s{2,}'
    if ($parts.Count -lt 4) { continue }
    if ($parts[1] -ne '1a86:55d3') { continue }   # CH343 串口
    $rows += [pscustomobject]@{ BusId = $parts[0]; Name = $parts[2]; State = $parts[3] }
  }
  return $rows
}

# ───────────────────────────────────────────────────────── 屏幕

Add-Type -AssemblyName System.Windows.Forms
$screens = @([System.Windows.Forms.Screen]::AllScreens | Sort-Object { $_.Bounds.X })

if ($ListScreens) {
  Write-Host ''
  Write-Host '屏幕编号（-ScreenA / -ScreenB 用这个）' -ForegroundColor Cyan
  for ($i = 0; $i -lt $screens.Count; $i++) {
    $s = $screens[$i]
    $tag = if ($s.Primary) { '主屏' } else { '外接' }
    Write-Host ("  {0}  {1}  {2}  {3}x{4} @({5},{6})" -f $i, $tag, $s.DeviceName, $s.Bounds.Width, $s.Bounds.Height, $s.Bounds.X, $s.Bounds.Y)
  }
  Write-Host ''
  return
}

Write-Host ''
Write-Host '一臂三身 · 演示启动' -ForegroundColor White
Write-Host ("  模式 {0}   视图 {1}   端口 {2}/{3}" -f $Mode, $View, $Port, $WsPort) -ForegroundColor DarkGray

# ───────────────────────────────────────────────────────── 1 端口

Write-Step 1 '清理端口'
foreach ($p in @($Port, $WsPort, $BridgePort)) { Stop-Port $p }
Write-Ok ("{0} / {1} / {2} 都空着" -f $Port, $WsPort, $BridgePort)

# ───────────────────────────────────────────────────────── 2 USB

$bridgeReady = $false
if ($NoRobot) {
  Write-Step 2 '跳过桥（-NoRobot：动作库当替身，臂不会动）'
} else {
  Write-Step 2 '把两个串口挂进 WSL'
  $devs = Get-SerialBusids
  if ($devs.Count -eq 0) {
    Write-Err2 'usbipd 里没看到 CH343 串口 —— 线没插，或者 usbipd 没装'
    Write-Note '插好线后跑一次（管理员 PowerShell）：usbipd list'
  } else {
    foreach ($d in $devs) {
      if ($d.State -eq 'Attached') {
        Write-Ok ("{0} 已挂（{1}）" -f $d.BusId, $d.Name)
        continue
      }
      if ($NoAttach) { Write-Warn2 ("{0} 没挂上，-NoAttach 跳过" -f $d.BusId); continue }
      $out = (usbipd attach --wsl --busid $d.BusId 2>&1) -join ' '
      if ($out -match 'error|拒绝|denied|管理员|administrator') {
        Write-Err2 ("{0} 挂不上：{1}" -f $d.BusId, $out)
        Write-Note 'usbipd attach 要管理员权限：用「以管理员身份运行」重开这个脚本'
      } else {
        Write-Ok ("{0} 已 attach（{1}）" -f $d.BusId, $d.Name)
      }
    }
  }

  # 等设备出现在 WSL 里
  $devOk = $false
  for ($i = 0; $i -lt 20; $i++) {
    $ls = (wsl -d $Wsl -- bash -lc 'ls /dev/ttyACM* 2>/dev/null' 2>$null) -join ' '
    if ($ls -match 'ttyACM') { $devOk = $true; break }
    Start-Sleep -Milliseconds 500
  }
  if ($devOk) { Write-Ok 'WSL 里能看到 /dev/ttyACM*' }
  else { Write-Err2 'WSL 里没有 /dev/ttyACM* —— attach 没成功，或 WSL 需要重开一次' }

  if ($devOk) {
    Write-Step 3 '起桥（WSL，后台）'
    $script = $WslRoot + '/scripts/_bridge.sh'
    $out = (wsl -d $Wsl -- bash $script $Mode 2>&1) -join "`n"
    Write-Note $out
    if (Test-Tcp $BridgePort 15) {
      Write-Ok ("桥在 ws://127.0.0.1:{0} 上应答" -f $BridgePort)
      $bridgeReady = $true
    } else {
      Write-Err2 '桥没起来 —— 看 .tmp\bridge.log 最后几行：'
      if (Test-Path (Join-Path $Tmp 'bridge.log')) {
        Get-Content (Join-Path $Tmp 'bridge.log') -Tail 8 | ForEach-Object { Write-Host ("       $_") -ForegroundColor DarkGray }
      }
    }
  }
}

# ───────────────────────────────────────────────────────── 3 后端

Write-Step 4 '起后端（Windows，后台）'
if (-not (Test-Path $Py)) { Write-Err2 ("找不到 {0}" -f $Py); Write-Note 'conda 环境名是不是叫 so101-twin？'; return }

$src = 'motion-library'
if (-not $NoRobot -and $bridgeReady) { $src = 'remote' }
elseif (-not $NoRobot -and -not $bridgeReady) {
  Write-Warn2 '桥没起来，退回动作库数据源：屏幕能演，臂不会动'
}

$pyArgs = @('-u', 'twin/server/main.py', '--source', $src, '--http-port', $Port, '--ws-port', $WsPort)
if ($src -eq 'remote') { $pyArgs += @('--remote-url', "ws://localhost:$BridgePort") }

$backend = Start-Process -FilePath $Py -ArgumentList $pyArgs -WorkingDirectory $Root -PassThru -WindowStyle Hidden `
  -RedirectStandardOutput (Join-Path $Tmp 'backend.log') `
  -RedirectStandardError (Join-Path $Tmp 'backend.err.log')
$backend.Id | Set-Content (Join-Path $Tmp 'backend.pid')

if (Test-Tcp $Port 15) {
  Write-Ok ("页面在 http://127.0.0.1:{0}/   数据源 {1}" -f $Port, $src)
} else {
  Write-Err2 '后端没起来 —— 看 .tmp\backend.err.log'
  Get-Content (Join-Path $Tmp 'backend.err.log') -Tail 8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ("       $_") -ForegroundColor DarkGray }
}

# ───────────────────────────────────────────────────────── 4 窗口

$opened = @()
if ($NoBrowser) {
  Write-Step 5 '跳过窗口（-NoBrowser）'
} else {
  Write-Step 5 '开窗口'

  function Open-App([string]$url, [int]$x, [int]$y, [int]$w, [int]$h, [string]$profile) {
    $a = @(
      "--app=$url",
      "--window-position=$x,$y",
      "--window-size=$w,$h",
      "--user-data-dir=$profile",
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-features=Translate,MediaRouter'
    )
    Start-Process -FilePath $Chrome -ArgumentList $a | Out-Null
  }

  $base = "http://127.0.0.1:$Port/"
  $pA = Join-Path $Tmp 'chrome-twin'
  $pB = Join-Path $Tmp 'chrome-code'

  if ($SideBySide -or $screens.Count -lt 2) {
    if (-not $SideBySide -and $screens.Count -lt 2) {
      Write-Warn2 '只检测到一块屏，按并排处理（真演示时用两块外接屏）'
    }
    $s = $screens[0].Bounds
    $halfW = [int]($s.Width / 2)
    $leftUrl = $base + '?view=twin'
    $rightUrl = $base + '?view=code'
    if ($Swap) { $leftUrl, $rightUrl = $rightUrl, $leftUrl }
    Open-App $leftUrl $s.X $s.Y $halfW $s.Height $pA
    Start-Sleep -Milliseconds 600
    Open-App $rightUrl ($s.X + $halfW) $s.Y $halfW $s.Height $pB
    Write-Ok '两个窗口并排（左数字层 / 右代码）'
    $opened += 'twin + code（并排，屏 0）'
  } elseif ($ScreenA -ge 0 -and $ScreenB -ge 0) {
    $sa = $screens[$ScreenA]
    $sb = $screens[$ScreenB]
    $uA = $base + '?view=twin'
    $uB = $base + '?view=code'
    if ($Swap) { $uA, $uB = $uB, $uA }
    Open-App $uA $sa.Bounds.X $sa.Bounds.Y $sa.Bounds.Width $sa.Bounds.Height $pA
    Start-Sleep -Milliseconds 600
    Open-App $uB $sb.Bounds.X $sb.Bounds.Y $sb.Bounds.Width $sb.Bounds.Height $pB
    Write-Ok ("屏 {0} → 数字层，屏 {1} → 代码" -f $ScreenA, $ScreenB)
    $opened += ("twin@屏{0} code@屏{1}" -f $ScreenA, $ScreenB)
  } else {
    # 自动：跳过主屏（操作者自己那块），取两块外接
    $ext = @($screens | Where-Object { -not $_.Primary })
    if ($ext.Count -ge 2) {
      $sa = $ext[0]; $sb = $ext[1]
      $uA = $base + '?view=twin'
      $uB = $base + '?view=code'
      if ($Swap) { $uA, $uB = $uB, $uA }
      Open-App $uA $sa.Bounds.X $sa.Bounds.Y $sa.Bounds.Width $sa.Bounds.Height $pA
      Start-Sleep -Milliseconds 600
      Open-App $uB $sb.Bounds.X $sb.Bounds.Y $sb.Bounds.Width $sb.Bounds.Height $pB
      Write-Ok ("自动挑了两块外接屏：{0} / {1}" -f $sa.DeviceName, $sb.DeviceName)
      Write-Note '位置不对就加 -ListScreens 看编号，再用 -ScreenA / -ScreenB 指定'
      $opened += 'twin + code（自动选屏）'
    } else {
      # 只有一块外接屏：主屏放代码，外接屏放数字层
      $sa = $ext[0]
      $sb = $screens[0]
      Open-App ($base + '?view=twin') $sa.Bounds.X $sa.Bounds.Y $sa.Bounds.Width $sa.Bounds.Height $pA
      Start-Sleep -Milliseconds 600
      Open-App ($base + '?view=code') $sb.Bounds.X $sb.Bounds.Y $sb.Bounds.Width $sb.Bounds.Height $pB
      Write-Warn2 '只找到一块外接屏，代码那块放到了主屏上'
      $opened += 'twin@外接 code@主屏'
    }
  }
}

# ───────────────────────────────────────────────────────── 清单

Write-Host ''
Write-Host '─────────────────────────────────────────────' -ForegroundColor DarkGray
$bridgeText = if ($NoRobot) { '未起（-NoRobot）' } elseif ($bridgeReady) { "ws://127.0.0.1:$BridgePort" } else { '失败' }
Write-Host ("  桥      {0}" -f $bridgeText)
Write-Host ("  后端    http://127.0.0.1:{0}/  数据源 {1}" -f $Port, $src)
Write-Host ("  窗口    {0}" -f ($(if ($opened.Count) { $opened -join '  ' } else { '未开' })))
Write-Host ("  档位    {0}" -f $Mode)
Write-Host ''
Write-Host '  键 1 遥操跟随   2 自动·预设   3 停住   0 暂停/继续'
Write-Host '  停止：scripts\stop_demo.cmd' -ForegroundColor DarkGray
Write-Host '─────────────────────────────────────────────' -ForegroundColor DarkGray
Write-Host ''

if (-not $NoRobot -and -not $bridgeReady) {
  Write-Host '提示：桥没起来时页面仍然能用（动作库驱动），修好 USB 后重跑这个脚本即可。' -ForegroundColor Yellow
}
