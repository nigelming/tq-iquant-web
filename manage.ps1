param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status"
)

# 8001：8000 被「通达信量化 · Quant Terminal」（另一项目）长期占用，本项目后端改用 8001
$backend_port = 8001
$frontend_port = 5173
$backend_dir = "D:\project\tq-iquant-web\main"
$frontend_dir = "D:\project\tq-iquant-web\web"

# 2026-11-04：本机 netstat 对 LISTENING 行的 PID 归因恒显示 0（安全软件干扰），
# 原 Get-BackendPid/Get-FrontendPid 永远匹配不到 → 误判未运行 → 重复启动 →
# 绑定失败 + 白等 180s 假报 "NOT ready"。
# 改用：存活=API/HTTP 探测；停止=CIM 命令行匹配（uvicorn / vite 关键词），不依赖 netstat。

function Test-BackendAlive {
    try {
        $r = Invoke-RestMethod "http://127.0.0.1:$backend_port/api/status" -TimeoutSec 3
        return ($null -ne $r -and $r.code -eq 0)
    } catch { return $false }
}

function Test-FrontendAlive {
    try {
        $resp = Invoke-WebRequest "http://127.0.0.1:$frontend_port/" -TimeoutSec 3 -UseBasicParsing
        return ($resp.StatusCode -eq 200)
    } catch { return $false }
}

function Get-ProcByCommandLine([string]$pattern) {
    # CIM 查进程命令行（精确匹配 uvicorn core.main / vite dev），不误杀其它 python/node
    try {
        return Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='node.exe' OR Name='uv.exe' OR Name='cmd.exe'" |
            Where-Object { $_.CommandLine -match $pattern }
    } catch { return @() }
}

function Stop-Backend {
    $procs = Get-ProcByCommandLine "uvicorn.*core\.main"
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    if ($procs) { Write-Host "Backend stopped" -ForegroundColor Green }
    else { Write-Host "Backend not running" -ForegroundColor Yellow }
}

function Stop-Frontend {
    $procs = Get-ProcByCommandLine "vite.*--host|npm run dev"
    foreach ($p in $procs) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    # vite 的 node 子进程按父链兜底：命令行含 vite 的 node
    $nodes = Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
        Where-Object { $_.CommandLine -match "vite" }
    foreach ($n in $nodes) {
        try { Stop-Process -Id $n.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
    }
    if ($procs -or $nodes) { Write-Host "Frontend stopped" -ForegroundColor Green }
    else { Write-Host "Frontend not running" -ForegroundColor Yellow }
}

function Wait-BackendReady {
    param([int]$TimeoutSec = 90)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-BackendAlive) { return $true }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

function Wait-FrontendReady {
    param([int]$TimeoutSec = 60)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (Test-FrontendAlive) { return $true }
        Start-Sleep -Milliseconds 800
    }
    return $false
}

switch ($Action) {
    "start" {
        if (Test-BackendAlive) {
            Write-Host "Backend already running (API alive on $backend_port)" -ForegroundColor Yellow
        } else {
            # 残留进程清理（上一轮绑定失败/僵尸 uvicorn）
            Stop-Backend
            Write-Host "Starting backend on port $backend_port ..." -ForegroundColor Cyan
            $log = Join-Path $backend_dir "server.log"
            $err = Join-Path $backend_dir "server.err.log"
            $stdin = Join-Path $backend_dir "stdin.empty"
            if (-not (Test-Path $stdin)) { New-Item -ItemType File -Path $stdin -Force | Out-Null }
            # 0.0.0.0（0017 hermes）：另一台电脑经局域网拉 /api/hermes/signals，
            # 无鉴权为既定决策（单用户局域网工具）；Windows 防火墙首次放行一次。
            Start-Process -WindowStyle Hidden -FilePath "uv" -ArgumentList "run uvicorn core.main:app --host 0.0.0.0 --port $backend_port" -WorkingDirectory $backend_dir -RedirectStandardInput $stdin -RedirectStandardOutput $log -RedirectStandardError $err
            Write-Host "  waiting for backend API (usually 10-30s; first run of the day slower) ..." -ForegroundColor DarkGray
            if (Wait-BackendReady -TimeoutSec 120) {
                Write-Host "  backend ready" -ForegroundColor Green
            } else {
                Write-Host "  backend NOT ready in 120s, check $err" -ForegroundColor Red
            }
        }

        if (Test-FrontendAlive) {
            Write-Host "Frontend already running (HTTP alive on $frontend_port)" -ForegroundColor Yellow
        } else {
            Write-Host "Starting frontend on port $frontend_port ..." -ForegroundColor Cyan
            $log = Join-Path $frontend_dir "vite.log"
            $err = Join-Path $frontend_dir "vite.err.log"
            $stdin = Join-Path $frontend_dir "stdin.empty"
            if (-not (Test-Path $stdin)) { New-Item -ItemType File -Path $stdin -Force | Out-Null }
            Start-Process -WindowStyle Hidden -FilePath "cmd" -ArgumentList "/c npm run dev -- --host 0.0.0.0" -WorkingDirectory $frontend_dir -RedirectStandardInput $stdin -RedirectStandardOutput $log -RedirectStandardError $err
            Write-Host "  waiting for frontend HTTP (first run: vite 预构建较慢) ..." -ForegroundColor DarkGray
            if (Wait-FrontendReady -TimeoutSec 90) {
                Write-Host "  frontend ready" -ForegroundColor Green
            } else {
                Write-Host "  frontend NOT ready in 90s, check $err" -ForegroundColor Red
            }
        }

        Write-Host "`nBackend:  http://127.0.0.1:$backend_port" -ForegroundColor Cyan
        Write-Host "Frontend: http://127.0.0.1:$frontend_port" -ForegroundColor Cyan
        Start-Process "http://127.0.0.1:$frontend_port"
        Write-Host "Browser:  opened http://127.0.0.1:$frontend_port" -ForegroundColor Cyan
    }

    "stop" {
        Stop-Backend
        Stop-Frontend
    }

    "restart" {
        . $PSCommandPath -Action stop
        Start-Sleep 2
        . $PSCommandPath -Action start
    }

    "status" {
        if (Test-BackendAlive) { Write-Host "Backend:  RUNNING (API alive) http://127.0.0.1:$backend_port" -ForegroundColor Green }
        else { Write-Host "Backend:  STOPPED" -ForegroundColor Red }
        if (Test-FrontendAlive) { Write-Host "Frontend: RUNNING (HTTP alive) http://127.0.0.1:$frontend_port" -ForegroundColor Green }
        else { Write-Host "Frontend: STOPPED" -ForegroundColor Red }
    }
}
