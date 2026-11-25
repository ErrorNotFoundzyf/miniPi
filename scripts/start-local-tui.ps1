param(
    [string]$Model = "deepseek-flash",
    [string]$NodeExecutable = "node",
    [switch]$CheckApi,
    [switch]$UiSmoke,
    [switch]$ResetApiKey
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$pythonExecutable = Join-Path $repoRoot ".venv/Scripts/python.exe"
$entryPoint = Join-Path $repoRoot "my-pi-tui/bin/my-agent.js"
$localAgentHome = if ($env:MY_AGENT_HOME) {
    [IO.Path]::GetFullPath($env:MY_AGENT_HOME)
} else {
    Join-Path $repoRoot ".my_agent_core/interactive-home"
}

if (-not (Test-Path -LiteralPath $pythonExecutable)) {
    throw "Python environment missing. Run uv sync from the repository root first."
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot "my-pi-tui/dist/app.js"))) {
    throw "TUI build missing. Run npm install and npm run build first."
}

$nodeVersion = [version]((& $NodeExecutable --version).Trim().TrimStart('v'))
if ($nodeVersion -lt [version]"22.19.0") {
    $bundledNode = Join-Path $env:USERPROFILE ".cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe"
    if (Test-Path -LiteralPath $bundledNode) {
        $NodeExecutable = $bundledNode
        $nodeVersion = [version]((& $NodeExecutable --version).Trim().TrimStart('v'))
    }
    if ($nodeVersion -lt [version]"22.19.0") {
        throw "Node.js 22.19 or newer is required. Pass -NodeExecutable with a compatible node.exe."
    }
}

New-Item -ItemType Directory -Path $localAgentHome -Force | Out-Null
$env:MY_AGENT_HOME = $localAgentHome
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:PYTHONUTF8 = "1"
Set-Location -LiteralPath $repoRoot
$Host.UI.RawUI.WindowTitle = "minPi - DeepSeek"

Write-Host "minPi / DeepSeek 官方 API" -ForegroundColor Cyan
Write-Host "模型: $Model | Node: $nodeVersion | 权限模式: review"
Write-Host "本地数据: $localAgentHome"
Write-Host "首次体验建议: 请用一句话介绍你自己，不要调用工具。"
Write-Host "退出: /quit。首次输入的 API Key 会保存到本地 auth.json，后续启动自动复用。"
Write-Host "更换密钥: 启动时加 -ResetApiKey。"
if ($UiSmoke) {
    Write-Host "本次只运行约 7 秒的离线 UI 动画，无需密钥，不调用模型。" -ForegroundColor Cyan
}

$previousKey = $env:DEEPSEEK_API_KEY
$previousSslKeyLog = $env:SSLKEYLOGFILE
$previousPythonPath = $env:PYTHONPATH
$enteredKey = $null
try {
    # Inherited TLS debugging can crash this Python/OpenSSL build when it opens
    # the external log file. Disable it only for this launcher and its children.
    $env:SSLKEYLOGFILE = $null
    $env:PYTHONPATH = (Join-Path $repoRoot "src") + $(if ($previousPythonPath) { [IO.Path]::PathSeparator + $previousPythonPath } else { "" })
    $hasSavedKey = $false
    if (-not $UiSmoke -and -not $ResetApiKey) {
        & $pythonExecutable -c @'
import sys
from my_agent_llm.auth.manager import AuthManager
from my_agent_llm.auth.schema import ApiKeyCredential
try:
    credential = AuthManager().get_credential("deepseek")
    sys.exit(0 if isinstance(credential, ApiKeyCredential) and credential.resolve_key() else 1)
except Exception:
    sys.exit(2)
'@
        if ($LASTEXITCODE -eq 0) {
            $hasSavedKey = $true
            Write-Host "已找到保存的 DeepSeek 凭据，无需再次输入。" -ForegroundColor Green
        } elseif ($LASTEXITCODE -ne 1) {
            throw "无法检查本地 DeepSeek 凭据，请检查 auth.json 的权限或凭据引用。"
        }
    }
    if (-not $UiSmoke -and ($ResetApiKey -or (-not $hasSavedKey -and -not $env:DEEPSEEK_API_KEY))) {
        $env:DEEPSEEK_API_KEY = $null
        while (-not $env:DEEPSEEK_API_KEY) {
            if ($null -ne $enteredKey) {
                $enteredKey.Dispose()
            }
            $enteredKey = Read-Host "请粘贴 DeepSeek API Key 后按回车（隐藏输入，Ctrl+C 取消）" -AsSecureString
            $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($enteredKey)
            try {
                $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer).Trim()
            } finally {
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
            }
            if ($env:DEEPSEEK_API_KEY -and $env:DEEPSEEK_API_KEY.Contains([char]22)) {
                $env:DEEPSEEK_API_KEY = $null
                Write-Host "检测到 Ctrl+V 控制字符，终端没有实际粘贴密钥。请改用鼠标右键或 Shift+Insert 粘贴。" -ForegroundColor Yellow
                continue
            }
            if ($env:DEEPSEEK_API_KEY -and ($env:DEEPSEEK_API_KEY.Length -lt 20 -or $env:DEEPSEEK_API_KEY -match '[\x00-\x20\x7F]')) {
                $env:DEEPSEEK_API_KEY = $null
                Write-Host "输入过短或含空白/控制字符，请重新复制完整 API Key，并用右键或 Shift+Insert 粘贴。" -ForegroundColor Yellow
                continue
            }
            if (-not $env:DEEPSEEK_API_KEY) {
                Write-Host "未收到密钥。请先粘贴 API Key，再按回车；不要发送到聊天中。" -ForegroundColor Yellow
            }
        }
        & $pythonExecutable -c @'
import os
import sys
from my_agent_llm.auth.manager import AuthManager
try:
    manager = AuthManager()
    profile = manager.load_store().active_profiles.get("deepseek", "default")
    manager.set_api_key("deepseek", os.environ["DEEPSEEK_API_KEY"], profile=profile)
except Exception:
    print("无法保存 DeepSeek 凭据，请检查本地数据目录权限。", file=sys.stderr)
    sys.exit(1)
'@
        if ($LASTEXITCODE -ne 0) {
            throw "DeepSeek 凭据保存失败。"
        }
        Write-Host "DeepSeek 凭据已保存，下次启动无需粘贴。" -ForegroundColor Green
    }

    if ($CheckApi -and -not $UiSmoke) {
        & $pythonExecutable (Join-Path $PSScriptRoot "check_deepseek.py") $Model
        if ($LASTEXITCODE -ne 0) {
            throw "API diagnostic failed. See .my_agent_core/interactive-home/deepseek-diagnostic.json."
        }
    }

    # A visible interactive terminal must not inherit Codex's plain-text defaults.
    $previousTerminalEnvironment = @{}
    foreach ($name in @("NO_COLOR", "FORCE_COLOR", "TERM", "COLORTERM")) {
        $previousTerminalEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
    }
    $restoreConsoleMode = $false
    $consoleHandle = [IntPtr]::Zero
    [uint32]$originalConsoleMode = 0
    try {
        if ($IsWindows -and -not [Console]::IsOutputRedirected) {
            if (-not ("LocalTuiConsoleMode" -as [type])) {
                Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class LocalTuiConsoleMode {
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern IntPtr GetStdHandle(int id);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GetConsoleMode(IntPtr handle, out uint mode);
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetConsoleMode(IntPtr handle, uint mode);
}
'@
            }
            $consoleHandle = [LocalTuiConsoleMode]::GetStdHandle(-11)
            if ([LocalTuiConsoleMode]::GetConsoleMode($consoleHandle, [ref]$originalConsoleMode)) {
                # Process VT sequences and delay wrapping at the right margin.
                # The renderer explicitly emits CRLF between logical rows.
                $interactiveMode = $originalConsoleMode -bor 0x0001 -bor 0x0004 -bor 0x0008
                $restoreConsoleMode = [LocalTuiConsoleMode]::SetConsoleMode($consoleHandle, $interactiveMode)
                if (-not $restoreConsoleMode) {
                    Write-Warning "控制台不支持所需 VT 模式；建议在 VS Code 集成终端或 Windows Terminal 启动。"
                }
            }
        }
        $env:NO_COLOR = $null
        $env:FORCE_COLOR = "3"
        $env:TERM = "xterm-256color"
        $env:COLORTERM = "truecolor"
        if ($UiSmoke) {
            & $NodeExecutable (Join-Path $PSScriptRoot "tui-smoke.mjs")
        } else {
            & $NodeExecutable $entryPoint --python-executable $pythonExecutable --workspace $repoRoot --model "deepseek/$Model" --mode review
        }
    } finally {
        if ($restoreConsoleMode) {
            [LocalTuiConsoleMode]::SetConsoleMode($consoleHandle, $originalConsoleMode) | Out-Null
        }
        foreach ($name in $previousTerminalEnvironment.Keys) {
            [Environment]::SetEnvironmentVariable($name, $previousTerminalEnvironment[$name], "Process")
        }
    }
    if ($LASTEXITCODE -ne 0) {
        throw "TUI exited with code $LASTEXITCODE."
    }
} finally {
    $env:DEEPSEEK_API_KEY = $previousKey
    $env:SSLKEYLOGFILE = $previousSslKeyLog
    $env:PYTHONPATH = $previousPythonPath
    if ($null -ne $enteredKey) {
        $enteredKey.Dispose()
    }
}
