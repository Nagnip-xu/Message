param([switch]$NoBrowser)

$ErrorActionPreference = 'Stop'
$browserJob = $null

function Test-Panel([string]$Address) {
    try {
        $request = [System.Net.WebRequest]::Create("$Address/api/health")
        $request.Proxy = $null
        $request.Timeout = 2000
        $response = $request.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            try { $health = $reader.ReadToEnd() | ConvertFrom-Json }
            finally { $reader.Dispose() }
            return $health.app -eq 'telegram-feishu-relay' -and $health.ok -eq $true
        } finally { $response.Dispose() }
    } catch { return $false }
}

try {
    $configPath = Join-Path $PSScriptRoot 'data\server-panel.json'
    if (Test-Path -LiteralPath $configPath) {
        $config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } else {
        Write-Host '首次使用：配置服务器管理通道。此配置只保存在本机 data 目录。' -ForegroundColor Cyan
        $serverAddress = Read-Host '服务器 IP 或域名'
        $loginName = Read-Host 'SSH 登录名（回车默认 root）'
        if ([string]::IsNullOrWhiteSpace($loginName)) { $loginName = 'root' }
        $config = [pscustomobject]@{
            server = $serverAddress.Trim()
            user = $loginName.Trim()
            ssh_port = 22
            local_port = 18765
            remote_port = 8765
        }
    }

    if ($config.server -notmatch '^[A-Za-z0-9][A-Za-z0-9.-]*$' -or
        $config.user -notmatch '^[A-Za-z_][A-Za-z0-9_.-]*$') {
        throw '服务器地址或登录名格式不正确，请检查 data\server-panel.json。'
    }
    foreach ($name in @('ssh_port', 'local_port', 'remote_port')) {
        $port = 0
        if (-not [int]::TryParse([string]$config.$name, [ref]$port) -or $port -lt 1 -or $port -gt 65535) {
            throw "配置中的 $name 必须为 1–65535 的端口号。"
        }
    }
    if (-not (Test-Path -LiteralPath $configPath)) {
        New-Item -ItemType Directory -Path (Split-Path -Parent $configPath) -Force | Out-Null
        $config | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding UTF8
    }

    $url = "http://127.0.0.1:$($config.local_port)"
    $destination = "$($config.user)@$($config.server)"
    Write-Host "服务器：$destination   管理面板：$url/" -ForegroundColor Cyan
    Write-Host '此页面查看服务器状态；不会启动电脑上的本地监听程序。'

    if (Test-Panel $url) {
        Write-Host '管理通道已经可用，直接打开面板。' -ForegroundColor Green
        if (-not $NoBrowser) { Start-Process "$url/" }
        exit 0
    }

    $listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Loopback, [int]$config.local_port)
    try { $listener.Start() }
    catch { throw "本机端口 $($config.local_port) 已被占用，且无法读取管理面板。请关闭旧通道，或修改 data\server-panel.json 中的 local_port 后重试。" }
    finally { $listener.Stop() }

    $sshPath = Join-Path $env:WINDIR 'System32\OpenSSH\ssh.exe'
    if (-not (Test-Path -LiteralPath $sshPath)) {
        $sshCommand = Get-Command ssh.exe -ErrorAction SilentlyContinue
        if (-not $sshCommand) { throw '未安装 SSH 客户端。请到 Windows 设置 → 系统 → 可选功能，安装 OpenSSH 客户端。' }
        $sshPath = $sshCommand.Source
    }

    Write-Host ''
    Write-Host '请按提示输入服务器密码；输入时不显示字符属于正常现象。'
    Write-Host '首次连接时，请核对 SSH 主机指纹后按提示确认。'
    Write-Host '连接成功后自动打开浏览器。请保留此窗口，可将它最小化。' -ForegroundColor Yellow
    Write-Host '关闭窗口或按 Ctrl+C 只会断开管理通道，服务器监听仍继续运行。'
    Write-Host '如果提示连接失败，窗口会保留错误信息；修复后再次双击即可。'
    Write-Host ''

    # The foreground SSH process owns authentication; this job only waits for the page.
    $browserJob = Start-Job -ArgumentList $url, ([bool]$NoBrowser) -ScriptBlock {
        param($panelUrl, $skipBrowser)
        $deadline = [DateTime]::UtcNow.AddMinutes(5)
        while ([DateTime]::UtcNow -lt $deadline) {
            try {
                $request = [System.Net.WebRequest]::Create("$panelUrl/api/health")
                $request.Proxy = $null
                $request.Timeout = 2000
                $response = $request.GetResponse()
                try {
                    $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
                    try { $health = $reader.ReadToEnd() | ConvertFrom-Json }
                    finally { $reader.Dispose() }
                } finally { $response.Dispose() }
                if ($health.app -eq 'telegram-feishu-relay' -and $health.ok -eq $true) {
                    if (-not $skipBrowser) { Start-Process "$panelUrl/" }
                    return
                }
            } catch { }
            Start-Sleep -Milliseconds 700
        }
    }

    $sshArguments = @(
        '-N', '-T', '-p', [string]$config.ssh_port,
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ConnectTimeout=15',
        '-o', 'ConnectionAttempts=1',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        '-L', "127.0.0.1:$($config.local_port):127.0.0.1:$($config.remote_port)",
        $destination
    )
    & $sshPath @sshArguments
    if ($LASTEXITCODE -ne 0) { throw "SSH 管理通道已断开（退出码 $LASTEXITCODE）。请检查网络、服务器地址和登录密码，再次双击启动器重连。" }
} catch {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
} finally {
    if ($browserJob) {
        Stop-Job -Job $browserJob -ErrorAction SilentlyContinue
        Remove-Job -Job $browserJob -Force -ErrorAction SilentlyContinue
    }
}
