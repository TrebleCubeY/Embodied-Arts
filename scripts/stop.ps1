<#
.SYNOPSIS
  停止演示：先停后端，再给桥发 SIGINT（桥会释放扭矩，臂会软下来）。

.NOTES
  顺序不能反。先停桥再停后端也行，但那样页面上会先"断流"再关，多一次误导。
  反过来：后端先停 → 页面停止刷新 → 再让臂松开，看得清楚。
#>
[CmdletBinding()]
param(
  [int]$Port = 8000,
  [int]$WsPort = 8001,
  [string]$Wsl = 'Ubuntu',
  [switch]$KeepBridge
)

$ErrorActionPreference = 'Continue'

# wsl.exe 吐的是 UTF-8，控制台默认按 GBK 解，桥那几行中文会变成问号
try {
  [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
  $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$Root = Split-Path -Parent $PSScriptRoot
$Tmp = Join-Path $Root '.tmp'

function To-WslPath([string]$p) {
  $d = $p.Substring(0, 1).ToLower()
  $rest = $p.Substring(2).Replace('\', '/')
  return "/mnt/$d$rest"
}
$WslRoot = To-WslPath $Root

function Stop-Port([int]$p) {
  $conns = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $conns) {
    Write-Host ("  结束端口 {0} 上的进程 {1}" -f $p, $c.OwningProcess) -ForegroundColor DarkGray
    Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
  }
}

Write-Host ''
Write-Host '1) 停后端' -ForegroundColor Cyan
$pidFile = Join-Path $Tmp 'backend.pid'
if (Test-Path $pidFile) {
  $bpid = Get-Content $pidFile | Select-Object -First 1
  if ($bpid) {
    Stop-Process -Id ([int]$bpid) -Force -ErrorAction SilentlyContinue
    Write-Host ("  已停 pid {0}" -f $bpid) -ForegroundColor Green
  }
  Remove-Item $pidFile -ErrorAction SilentlyContinue
} else {
  Write-Host '  没有 pid 文件，按端口停' -ForegroundColor DarkGray
}
foreach ($p in @($Port, $WsPort)) { Stop-Port $p }

if ($KeepBridge) {
  Write-Host ''
  Write-Host '2) 保留桥（-KeepBridge）—— 机械臂仍然使能' -ForegroundColor Yellow
  return
}

Write-Host ''
Write-Host '2) 停桥' -ForegroundColor Cyan
Write-Host '   桥会释放扭矩，机械臂会软下来 —— 现在托住它' -ForegroundColor Yellow
$script = $WslRoot + '/scripts/stop_bridge.sh'
$out = (wsl -d $Wsl -- bash $script 2>&1) -join "`n"
Write-Host $out

Write-Host ''
Write-Host '3) 可选：把 WSL 整个收掉（下次启动要重新 attach 设备）' -ForegroundColor DarkGray
Write-Host '   wsl --shutdown' -ForegroundColor DarkGray
Write-Host ''
