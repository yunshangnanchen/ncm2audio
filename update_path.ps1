# -*- coding: utf-8 -*-
# update_path.ps1 - add / remove this folder from the USER PATH registry value.
# Called by 安装到PATH.bat.  Keep this file ASCII-only.

param([string]$Action = 'add')

$ErrorActionPreference = 'Stop'

# directory of THIS script == project root
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$dir = $dir.TrimEnd('\')

# --- read raw (unexpanded) user Path, preserving value kind ---------------
$key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
$kind = [Microsoft.Win32.RegistryValueKind]::ExpandString
try { $kind = $key.GetValueKind('Path') } catch { }
$raw = $key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
if (-not $raw) { $raw = '' }
$parts = @($raw -split ';' | Where-Object { $_ -ne '' })

$exists = $false
foreach ($p in $parts) {
    if ($p.TrimEnd('\') -ieq $dir) { $exists = $true; break }
}

$changed = $false
if ($Action -eq 'remove') {
    if ($exists) {
        $parts = @($parts | Where-Object { $_.TrimEnd('\') -ine $dir })
        $changed = $true
        Write-Host "[OK] removed from user PATH: $dir"
    } else {
        Write-Host "[--] not in user PATH, nothing to do: $dir"
    }
} else {
    if ($exists) {
        Write-Host "[--] already in user PATH: $dir"
    } else {
        $parts += $dir
        $changed = $true
        Write-Host "[OK] added to user PATH: $dir"
    }
}

if ($changed) {
    $key.SetValue('Path', ($parts -join ';'), $kind)

    # broadcast WM_SETTINGCHANGE so Explorer / new terminals pick it up
    Add-Type -Namespace Win32 -Name NativeMethods -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true, CharSet = System.Runtime.InteropServices.CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(System.IntPtr hWnd, uint Msg, System.UIntPtr wParam, string lParam, uint fuFlags, uint uTimeout, out System.UIntPtr lpdwResult);
'@
    $result = [UIntPtr]::Zero
    [Win32.NativeMethods]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero,
        'Environment', 2, 5000, [ref]$result) | Out-Null

    Write-Host ''
    Write-Host 'Done. Open a NEW terminal window, then verify with:'
    Write-Host '    ncm2audio --version'
}

# show final command check (old terminals may not see it yet)
Write-Host ''
$found = Get-Command ncm2audio -ErrorAction SilentlyContinue
if ($found) {
    Write-Host "ncm2audio resolves to: $($found.Source)"
} else {
    Write-Host 'ncm2audio is not visible in THIS window yet (normal).'
    Write-Host 'Open a new terminal and run:  ncm2audio --version'
}
