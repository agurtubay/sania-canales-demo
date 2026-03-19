<#
.SYNOPSIS
    Build and deploy the acs_poc FastAPI app to Azure Container App via ACR.
.DESCRIPTION
    Builds the Docker image in Azure Container Registry and updates the Container App.
    Requires Azure CLI logged in with access to the subscription.
#>
param(
    [string]$AppName       = "ca-sania-wa-poc",
    [string]$ResourceGroup = "rg-sania-wa-poc",
    [string]$AcrName       = "acrsaniawapoc",
    [string]$ImageName     = "sania-app",
    [string]$Tag           = "latest"
)

$ErrorActionPreference = "Stop"

$acrServer = "$AcrName.azurecr.io"
$image = "${ImageName}:${Tag}"
$fullImage = "${acrServer}/${image}"

Write-Host "==> Building image '$image' in ACR '$AcrName'..." -ForegroundColor Cyan
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
az acr build `
    --registry $AcrName `
    --resource-group $ResourceGroup `
    --image $image `
    --file Dockerfile . --no-logs

if ($LASTEXITCODE -ne 0) { throw "ACR build failed" }

Write-Host "==> Updating Container App '$AppName' with image '$fullImage'..." -ForegroundColor Cyan
az containerapp update `
    --name $AppName `
    --resource-group $ResourceGroup `
    --image $fullImage `
    --output none

if ($LASTEXITCODE -ne 0) { throw "Container App update failed" }

$fqdn = az containerapp show --name $AppName --resource-group $ResourceGroup --query "properties.configuration.ingress.fqdn" -o tsv

Write-Host "==> Deployment complete!" -ForegroundColor Green
Write-Host "    URL: https://$fqdn"
