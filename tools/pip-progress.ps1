# 实时显示 pip 安装进度（在新开的 PowerShell 窗口里运行）
#   powershell -ExecutionPolicy Bypass -File D:\trash-sorter\tools\pip-progress.ps1
#
# 为什么看"缓存大小"而不是百分比：
#   输出被重定向到文件时，pip 的进度条（\r 刷新）无法正常显示，
#   但每个包下载完成都会往 pip 缓存里写文件 —— 缓存增长量就是真实下载进度。

$ErrorActionPreference = 'SilentlyContinue'
$py = 'D:\py310\python.exe'
$log = 'D:\trash-sorter\logs\pip-install.log'

$cache = (& $py -m pip cache dir 2>$null)
if (-not $cache) { $cache = Join-Path $env:LOCALAPPDATA 'pip\cache' }

$lastMb = $null
$lastTime = Get-Date

while ($true) {
  $sum = (Get-ChildItem $cache -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
  $mb = if ($sum) { $sum / 1MB } else { 0 }

  $now = Get-Date
  $dt = ($now - $lastTime).TotalSeconds
  $rate = if ($null -ne $lastMb -and $dt -gt 0) { ($mb - $lastMb) / $dt } else { 0 }
  $lastMb = $mb
  $lastTime = $now

  # pip 还在跑吗
  $running = $false
  Get-Process python -ErrorAction SilentlyContinue | ForEach-Object {
    $cmd = (Get-CimInstance Win32_Process -Filter ("ProcessId=" + $_.Id)).CommandLine
    if ($cmd -match 'pip install') { $running = $true }
  }

  Clear-Host
  Write-Host '================ pip 安装实时进度 ================' -ForegroundColor Cyan
  Write-Host ('  时间        : ' + $now.ToString('HH:mm:ss'))
  Write-Host ('  pip 状态    : ' + $(if ($running) { '正在安装…' } else { '空闲 / 已结束' })) -ForegroundColor $(if ($running) { 'Yellow' } else { 'Green' })
  Write-Host ('  已下载缓存  : {0:N1} MB   (每秒 +{1:N2} MB)' -f $mb, $rate)
  if ($rate -gt 0) {
    $bar = '#' * [Math]::Min(40, [Math]::Max(1, [int]($rate * 4)))
    Write-Host ('  速度条      : ' + $bar) -ForegroundColor Green
  }
  Write-Host ''
  Write-Host '  ---- 日志最后 12 行 ----' -ForegroundColor DarkGray
  Get-Content $log -Tail 12 -Encoding UTF8 | ForEach-Object { Write-Host ('  ' + $_) }
  Write-Host ''
  Write-Host '  （Ctrl+C 退出查看，不会中断安装）' -ForegroundColor DarkGray
  Write-Host '=================================================' -ForegroundColor Cyan

  Start-Sleep -Seconds 2
}
