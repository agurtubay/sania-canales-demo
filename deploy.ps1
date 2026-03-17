<#
.SYNOPSIS
    Build and deploy the acs_poc FastAPI app to Azure Web App via ACR.
.DESCRIPTION
    Builds the Docker image in Azure Container Registry and restarts the Web App.
    Requires Azure CLI logged in with access to the subscription.
#>
param(
    [string]$AppName       = "app-sania-wa-poc-py-a1b2c3",
    [string]$ResourceGroup = "rg-sania-whatsapp-poc-weu",
    [string]$AcrName       = "acrsaniawapoc",
    [string]$ImageName     = "sania-app",
    [string]$Tag           = "latest"
)

$ErrorActionPreference = "Stop"

$image = "${ImageName}:${Tag}"

Write-Host "==> Building image '$image' in ACR '$AcrName'..." -ForegroundColor Cyan
az acr build `
    --registry $AcrName `
    --resource-group $ResourceGroup `
    --image $image `
    --file Dockerfile .

if ($LASTEXITCODE -ne 0) { throw "ACR build failed" }

Write-Host "==> Restarting Web App '$AppName'..." -ForegroundColor Cyan
az webapp restart --name $AppName --resource-group $ResourceGroup

Write-Host "==> Deployment complete!" -ForegroundColor Green
Write-Host "    URL: https://$AppName.azurewebsites.net/health"
