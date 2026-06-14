# HA EMS -- Release script
# Usage: .\release.ps1 0.3.5
# Requires: git, gh (GitHub CLI) -- install gh from https://cli.github.com

param(
    [Parameter(Mandatory=$true)]
    [string]$Version
)

$Tag = "v$Version"
$ConfigPath = "ha_ems\config.yaml"
$ErrorActionPreference = "Stop"

Write-Host "Releasing $Tag..." -ForegroundColor Cyan

# 1. Update version in config.yaml
$content = Get-Content $ConfigPath -Raw
$current = [regex]::Match($content, 'version: "([^"]+)"').Groups[1].Value
if (-not $current) { Write-Error "Could not find version in $ConfigPath"; exit 1 }

Write-Host "Bumping $current -> $Version"
$content = $content -replace "version: `"$current`"", "version: `"$Version`""
Set-Content $ConfigPath $content -NoNewline

# 2. Git add, commit, push
git add $ConfigPath
git add ha_ems\
git commit -m "chore: release $Tag"
git push

# 3. Create GitHub release
if (Get-Command gh -ErrorAction SilentlyContinue) {
    gh release create $Tag --title $Tag --notes "Release $Tag"
    Write-Host "Release $Tag created on GitHub" -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "gh CLI not found. Create the release manually:" -ForegroundColor Yellow
    Write-Host "  https://github.com/glienart/ha_ems/releases/new?tag=$Tag"
}

Write-Host "Done." -ForegroundColor Green
