param([string]$Action = 'status')

# infosystem Web 服务管理器（唯一后台托管实现）
# 用法: bash server.sh start|stop|restart|status|log
#       powershell -NoProfile -ExecutionPolicy Bypass -File server.ps1 <action>
#
# 注意：本文件必须保存为 UTF-8 with BOM —— PS 5.1 对无 BOM 文件按 ANSI/GBK 解析，
#       中文会错位并报出「花括号不匹配」之类假错。
# 分离启动由 run_api.py --launch（DETACHED_PROCESS）负责，不在这里用 Start-Process：
#       否则子进程继承调用方的 stdout 管道，Git Bash 会卡住不返回。
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$py = if ($env:APP_PYTHON) { $env:APP_PYTHON } else { 'D:\Anaconda\envs\infosystem\python.exe' }
$pidFile = Join-Path $root 'data\.server.pid'
$logFile = Join-Path $root 'data\.server.log'
$errFile = Join-Path $root 'data\.server.err.log'
$port = if ($env:APP_PORT) { [int]$env:APP_PORT } elseif ($env:PORT) { [int]$env:PORT } else { 8765 }
# 默认单 worker：api.services.task_store（进程内 dict + JSON）、
# task_executor._cancel_flags、main.py 的 remote-collect 轮询线程都是进程内状态；
# 多 worker 会让任务互相看不见、轮询重复、取消失效。想提并发先外移这些状态。
$workers = if ($env:APP_WORKERS) { [int]$env:APP_WORKERS } else { 1 }
$env:APP_PORT = "$port"
$env:APP_WORKERS = "$workers"

function Get-ListenPids {
    @(Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Get-ServerProcs {
    # 父进程：命令行含 run_api.py；worker：父进程的直系子进程
    $parents = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'run_api\.py' })
    $all = @($parents)
    foreach ($p in $parents) {
        $all += @(Get-CimInstance Win32_Process -Filter "ParentProcessId=$($p.ProcessId)" -ErrorAction SilentlyContinue)
    }
    $all | Where-Object { $_ } | Select-Object -ExpandProperty ProcessId -Unique
}

switch ($Action) {
    'start' {
        $existing = @(Get-ListenPids)
        if ($existing.Count -gt 0) {
            Write-Output "端口 $port 已被占用 (PID: $($existing -join ', ')) — 先 stop 再 start"
            exit 1
        }
        if (-not (Test-Path $py)) {
            Write-Output "找不到 python: $py（可用 APP_PYTHON 环境变量覆盖）"
            exit 1
        }
        New-Item -ItemType Directory -Force -Path (Join-Path $root 'data') | Out-Null
        # --launch 自己写日志与 .server.pid
        & $py "$root\run_api.py" --launch
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Milliseconds 500
            if ((Get-ListenPids).Count -gt 0) { break }
        }
        $running = @(Get-ListenPids)
        if ($running.Count -gt 0) {
            Write-Output "已启动 (监听 PID $($running -join ', '), workers=$workers) -> http://127.0.0.1:$port"
            Write-Output "日志: $logFile"
        } else {
            Write-Output "启动失败（15s 内未监听 $port），请查看:"
            Write-Output "  $logFile"
            Write-Output "  $errFile"
            if (Test-Path $errFile) { Get-Content -LiteralPath $errFile -Tail 15 }
            exit 1
        }
    }
    'stop' {
        $targets = @(Get-ListenPids) + @(Get-ServerProcs)
        $targets = $targets | Where-Object { $_ } | Select-Object -Unique
        if ($targets.Count -gt 0) {
            foreach ($procId in $targets) {
                taskkill /PID $procId /T /F 2>&1 | Out-Null
            }
            Write-Output "已停止 (PID: $($targets -join ', '))"
        } else {
            Write-Output '未运行'
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }
    'restart' {
        & $PSCommandPath 'stop'
        Start-Sleep -Seconds 1
        & $PSCommandPath 'start'
    }
    'status' {
        $running = @(Get-ListenPids)
        if ($running.Count -gt 0) {
            $procs = @(Get-ServerProcs)
            Write-Output "运行中 -> http://127.0.0.1:$port (监听 PID: $($running -join ', '); 进程组: $($procs -join ', '))"
        } else {
            Write-Output '未运行'
        }
    }
    'log' {
        if (Test-Path $logFile) {
            Get-Content -LiteralPath $logFile -Tail 50
        } else {
            Write-Output '无日志（尚未启动？）'
        }
    }
    default {
        Write-Output '用法: bash server.sh start|stop|restart|status|log'
    }
}
