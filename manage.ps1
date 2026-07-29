param(
    [ValidateSet("start", "stop", "restart", "status")]
    [string]$Action = "status"
)

$backend_port = 8000
$frontend_port = 5173
$backend_dir = "D:\project\tq-iquant-web\main"
$frontend_dir = "D:\project\tq-iquant-web\web"

function Get-BackendPid {
    $p = Get-Process -Name python* -ErrorAction SilentlyContinue | Where-Object {
        $_.Id -in (netstat -ano | Select-String ":${backend_port}.*LISTENING" | ForEach-Object { $_ -split '\s+' | Select-Object -Last 1 })
    }
    return $p
}

function Get-FrontendPid {
    $p = Get-Process -Name node* -ErrorAction SilentlyContinue | Where-Object {
        $_.Id -in (netstat -ano | Select-String ":${frontend_port}.*LISTENING" | ForEach-Object { $_ -split '\s+' | Select-Object -Last 1 })
    }
    return $p
}

switch ($Action) {
    "start" {
        $bp = Get-BackendPid
        if (-not $bp) {
            Write-Host "Starting backend on port $backend_port ..." -ForegroundColor Cyan
            $env:TQ_DB_PASSWORD = ""
            $log = Join-Path $backend_dir "server.log"
            Start-Process -NoNewWindow -FilePath "uv" -ArgumentList "run uvicorn core.main:app --host 127.0.0.1 --port $backend_port" -WorkingDirectory $backend_dir -RedirectStandardOutput $log
            Start-Sleep 3
            Write-Host "  backend started (PID: $(Get-BackendPid | Select-Object -ExpandProperty Id))" -ForegroundColor Green
        } else {
            Write-Host "  backend already running (PID: $($bp.Id))" -ForegroundColor Yellow
        }

        $fp = Get-FrontendPid
        if (-not $fp) {
            Write-Host "Starting frontend on port $frontend_port ..." -ForegroundColor Cyan
            $log = Join-Path $frontend_dir "vite.log"
            Start-Process -NoNewWindow -FilePath "cmd" -ArgumentList "/c npm run dev -- --host 0.0.0.0" -WorkingDirectory $frontend_dir -RedirectStandardOutput $log
            Start-Sleep 3
            Write-Host "  frontend started (PID: $(Get-FrontendPid | Select-Object -ExpandProperty Id))" -ForegroundColor Green
        } else {
            Write-Host "  frontend already running (PID: $($fp.Id))" -ForegroundColor Yellow
        }

        Write-Host "`nBackend:  http://127.0.0.1:$backend_port" -ForegroundColor Cyan
        Write-Host "Frontend: http://127.0.0.1:$frontend_port" -ForegroundColor Cyan
    }

    "stop" {
        $bp = Get-BackendPid
        if ($bp) {
            Stop-Process -Id $bp.Id -Force
            Write-Host "Backend stopped" -ForegroundColor Green
        } else {
            Write-Host "Backend not running" -ForegroundColor Yellow
        }

        $fp = Get-FrontendPid
        if ($fp) {
            Stop-Process -Id $fp.Id -Force
            Write-Host "Frontend stopped" -ForegroundColor Green
        } else {
            Write-Host "Frontend not running" -ForegroundColor Yellow
        }
    }

    "restart" {
        . $PSCommandPath -Action stop
        Start-Sleep 2
        . $PSCommandPath -Action start
    }

    "status" {
        $bp = Get-BackendPid
        if ($bp) {
            Write-Host "Backend:  RUNNING (PID: $($bp.Id)) http://127.0.0.1:$backend_port" -ForegroundColor Green
        } else {
            Write-Host "Backend:  STOPPED" -ForegroundColor Red
        }

        $fp = Get-FrontendPid
        if ($fp) {
            Write-Host "Frontend: RUNNING (PID: $($fp.Id)) http://127.0.0.1:$frontend_port" -ForegroundColor Green
        } else {
            Write-Host "Frontend: STOPPED" -ForegroundColor Red
        }
    }
}
