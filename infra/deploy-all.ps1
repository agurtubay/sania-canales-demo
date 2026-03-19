<#
.SYNOPSIS
    Deploy ALL infrastructure for the Sanitas ACS Voice & WhatsApp PoC from scratch.
.DESCRIPTION
    Creates every Azure resource, wires them together, builds the app, and deploys.
    Secrets are stored in Key Vault and referenced by the Container App via managed identity.

    Resources created:
      - Log Analytics Workspace + Application Insights
      - Key Vault (RBAC mode, stores all secrets)
      - Azure Container Registry
      - Azure Communication Services (+ system MI for Speech access)
      - Azure AI Services (OpenAI model deployment)
      - Azure Cognitive Services (Speech / TTS)
      - Azure Cosmos DB (serverless, conversation memory)
      - Container Apps Environment + Container App (system MI)
      - Event Grid subscriptions (WhatsApp + Voice)

    After this script completes you still need to:
      1. Purchase a phone number in ACS  (Portal > Phone Numbers)
      2. Configure WhatsApp channel       (Portal > Channels > WhatsApp)
#>
param(
    [string]$Project        = "sania-wa-poc",
    [string]$Location       = "northeurope",
    [string]$SpeechLocation = "westeurope",
    [string]$AILocation     = "swedencentral",
    [string]$ResourceGroup  = "rg-$Project",
    [string]$AIModel        = "gpt-4.1-mini",
    [string]$AIModelVersion = "2025-04-14",
    [string]$AISku          = "S0"
)

$ErrorActionPreference = "Stop"

# ── Derived resource names ────────────────────────────────────────────
$lawName    = "law-$Project"
$appiName   = "appi-$Project"
$kvName     = "kv-$Project"
$acrName    = "acr" + ($Project -replace '-','')   # alphanumeric only
$acsName    = "acs-$Project"
$aiName     = "ai-$Project"
$cogName    = "cog-$Project"
$cosmosName = "cosmos-$Project"
$caeName    = "cae-$Project"
$caName     = "ca-$Project"
$imageName  = "sania-app"
$imageTag   = "latest"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    $msg" -ForegroundColor Green }
function Write-Note($msg) { Write-Host "    $msg" -ForegroundColor Yellow }

function Assert-Az([string]$step) {
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FATAL: '$step' failed (exit code $LASTEXITCODE)" -ForegroundColor Red
        throw "$step failed"
    }
}

$subscriptionId = az account show --query id -o tsv
Write-Host "Subscription: $subscriptionId" -ForegroundColor DarkGray
Write-Host "Resource Group: $ResourceGroup" -ForegroundColor DarkGray

# ══════════════════════════════════════════════════════════════════════
# PHASE 1 — Foundation
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating Resource Group '$ResourceGroup' in $Location..."
az group create --name $ResourceGroup --location $Location --output none
Assert-Az "Create Resource Group"
Write-Ok "Resource Group ready."

Write-Step "Creating Log Analytics Workspace '$lawName'..."
az monitor log-analytics workspace create `
    --resource-group $ResourceGroup --workspace-name $lawName `
    --location $Location --output none
Assert-Az "Create Log Analytics Workspace"
$lawId = az monitor log-analytics workspace show `
    --resource-group $ResourceGroup --workspace-name $lawName `
    --query id -o tsv
$lawCustomerId = az monitor log-analytics workspace show `
    --resource-group $ResourceGroup --workspace-name $lawName `
    --query customerId -o tsv
$lawKey = az monitor log-analytics workspace get-shared-keys `
    --resource-group $ResourceGroup --workspace-name $lawName `
    --query primarySharedKey -o tsv
Write-Ok "Log Analytics ready."

Write-Step "Creating Application Insights '$appiName'..."
az monitor app-insights component create `
    --app $appiName --resource-group $ResourceGroup --location $Location `
    --workspace $lawId --output none
Assert-Az "Create Application Insights"
$appiConnStr = az monitor app-insights component show `
    --app $appiName --resource-group $ResourceGroup `
    --query connectionString -o tsv
Write-Ok "Application Insights ready."

Write-Step "Creating Key Vault '$kvName' (RBAC mode)..."
# Recover from soft-delete if exists, otherwise create
$kvDeleted = az keyvault list-deleted --query "[?name=='$kvName'] | length(@)" -o tsv
if ($kvDeleted -gt 0) {
    Write-Note "Purging soft-deleted vault '$kvName'..."
    az keyvault purge --name $kvName
}
az keyvault create `
    --name $kvName --resource-group $ResourceGroup --location $Location `
    --enable-rbac-authorization --output none
Assert-Az "Create Key Vault"
$kvUri = "https://$kvName.vault.azure.net"
$kvId = az keyvault show --name $kvName --resource-group $ResourceGroup --query id -o tsv
# Grant ourselves Key Vault admin so we can store secrets
$currentUser = az ad signed-in-user show --query id -o tsv
az role assignment create --assignee $currentUser --role "Key Vault Secrets Officer" --scope $kvId --output none
Assert-Az "Assign KV Secrets Officer to current user"
Write-Ok "Key Vault ready ($kvUri)."

# ══════════════════════════════════════════════════════════════════════
# PHASE 2 — Container Registry
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating Container Registry '$acrName'..."
az acr create --name $acrName --resource-group $ResourceGroup `
    --location $Location --sku Basic --admin-enabled true --output none
Assert-Az "Create ACR"
$acrServer = "$acrName.azurecr.io"
$acrCreds = az acr credential show --name $acrName --resource-group $ResourceGroup | ConvertFrom-Json
$acrUser = $acrCreds.username
$acrPass = $acrCreds.passwords[0].value
Write-Ok "ACR ready ($acrServer)."

# ══════════════════════════════════════════════════════════════════════
# PHASE 3 — Azure Communication Services
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating ACS '$acsName'..."
az communication create --name $acsName --resource-group $ResourceGroup `
    --location Global --data-location Europe --output none
Assert-Az "Create ACS"
Write-Ok "ACS created."

# Enable system-assigned MI on ACS
Write-Step "Enabling ACS managed identity..."
$acsResourceId = "/subscriptions/$subscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.Communication/CommunicationServices/$acsName"
az resource update --ids $acsResourceId --set identity.type=SystemAssigned --api-version 2023-04-01 --output none
Assert-Az "Enable ACS managed identity"
$acsMIPrincipal = az resource show --ids $acsResourceId --query identity.principalId -o tsv
Write-Ok "ACS MI enabled (principal: $acsMIPrincipal)."

# Get ACS keys
$acsKey = az communication list-key --name $acsName --resource-group $ResourceGroup --query primaryKey -o tsv
$acsHostName = az communication show --name $acsName --resource-group $ResourceGroup --query hostName -o tsv
if (-not $acsHostName) {
    $acsHostName = "$acsName.communication.azure.com"
}
$acsEndpoint = "https://$acsHostName/"
Write-Ok "ACS endpoint: $acsEndpoint"

# ══════════════════════════════════════════════════════════════════════
# PHASE 4 — Azure AI Services (OpenAI)
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating AI Services '$aiName' in $AILocation..."
$aiDeleted = az cognitiveservices account list-deleted --query "[?name=='$aiName'] | length(@)" -o tsv
if ($aiDeleted -gt 0) {
    Write-Note "Purging soft-deleted AI Services '$aiName'..."
    az cognitiveservices account purge --name $aiName --resource-group $ResourceGroup --location $AILocation
}
az cognitiveservices account create --name $aiName `
    --resource-group $ResourceGroup --location $AILocation `
    --kind AIServices --sku $AISku --custom-domain $aiName --output none --yes
Assert-Az "Create AI Services"
$aiEndpoint = az cognitiveservices account show --name $aiName `
    --resource-group $ResourceGroup --query properties.endpoint -o tsv
$aiId = az cognitiveservices account show --name $aiName `
    --resource-group $ResourceGroup --query id -o tsv
Write-Ok "AI Services ready ($aiEndpoint)."

Write-Step "Deploying model '$AIModel'..."
az cognitiveservices account deployment create `
    --name $aiName --resource-group $ResourceGroup `
    --deployment-name $AIModel --model-name $AIModel `
    --model-version $AIModelVersion --model-format OpenAI `
    --sku-capacity 10 --sku-name Standard --output none
Assert-Az "Deploy AI model"
Write-Ok "Model '$AIModel' deployed."

# ══════════════════════════════════════════════════════════════════════
# PHASE 5 — Cognitive Services (Speech / TTS)
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating Cognitive Services '$cogName' in $SpeechLocation..."
$cogDeleted = az cognitiveservices account list-deleted --query "[?name=='$cogName'] | length(@)" -o tsv
if ($cogDeleted -gt 0) {
    Write-Note "Purging soft-deleted Cognitive Services '$cogName'..."
    az cognitiveservices account purge --name $cogName --resource-group $ResourceGroup --location $SpeechLocation
}
az cognitiveservices account create --name $cogName `
    --resource-group $ResourceGroup --location $SpeechLocation `
    --kind CognitiveServices --sku S0 --custom-domain $cogName --output none --yes
Assert-Az "Create Cognitive Services (Speech)"
$cogEndpoint = az cognitiveservices account show --name $cogName `
    --resource-group $ResourceGroup --query properties.endpoint -o tsv
$cogId = az cognitiveservices account show --name $cogName `
    --resource-group $ResourceGroup --query id -o tsv
Write-Ok "Cognitive Services ready ($cogEndpoint)."

# Grant ACS MI -> Cognitive Services User (for TTS/STT)
Write-Step "Granting ACS MI 'Cognitive Services User' on Speech..."
az role assignment create --assignee $acsMIPrincipal --role "Cognitive Services User" --scope $cogId --output none
Assert-Az "Assign ACS MI -> Cognitive Services User"
Write-Ok "ACS -> Cognitive Services RBAC assigned."

# ══════════════════════════════════════════════════════════════════════
# PHASE 6 — Cosmos DB (serverless)
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating Cosmos DB '$cosmosName' (serverless) in $Location..."
az cosmosdb create --name $cosmosName --resource-group $ResourceGroup `
    --locations regionName=$Location --capabilities EnableServerless `
    --kind GlobalDocumentDB --output none
Assert-Az "Create Cosmos DB"
Write-Ok "Cosmos DB account ready."

Write-Step "Creating database 'sania-bot' and container 'conversations'..."
az cosmosdb sql database create --account-name $cosmosName `
    --resource-group $ResourceGroup --name sania-bot --output none
Assert-Az "Create Cosmos database"
az cosmosdb sql container create --account-name $cosmosName `
    --resource-group $ResourceGroup --database-name sania-bot `
    --name conversations --partition-key-path "/conversationId" --output none
Assert-Az "Create Cosmos container"
$cosmosConn = az cosmosdb keys list --name $cosmosName `
    --resource-group $ResourceGroup --type connection-strings `
    --query "connectionStrings[0].connectionString" -o tsv
$cosmosEndpoint = az cosmosdb show --name $cosmosName `
    --resource-group $ResourceGroup --query documentEndpoint -o tsv
Write-Ok "Cosmos DB ready ($cosmosEndpoint)."

# ══════════════════════════════════════════════════════════════════════
# PHASE 7 — Store secrets in Key Vault
# ══════════════════════════════════════════════════════════════════════
Write-Step "Storing secrets in Key Vault..."
Start-Sleep -Seconds 10  # wait for RBAC propagation
az keyvault secret set --vault-name $kvName --name acs-access-key --value $acsKey --output none
Assert-Az "Store ACS key in KV"
az keyvault secret set --vault-name $kvName --name cosmos-connection-string --value $cosmosConn --output none
Assert-Az "Store Cosmos conn string in KV"
Write-Ok "Secrets stored in Key Vault."

# ══════════════════════════════════════════════════════════════════════
# PHASE 8 — Build Docker image
# ══════════════════════════════════════════════════════════════════════
Write-Step "Building Docker image in ACR..."
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
az acr build --registry $acrName --resource-group $ResourceGroup `
    --image "${imageName}:${imageTag}" --file Dockerfile . --no-logs
Assert-Az "Build Docker image in ACR"
Write-Ok "Image built: $acrServer/${imageName}:${imageTag}"

# ══════════════════════════════════════════════════════════════════════
# PHASE 9 — Container Apps Environment + App
# ══════════════════════════════════════════════════════════════════════
Write-Step "Creating Container Apps Environment '$caeName'..."
az containerapp env create --name $caeName --resource-group $ResourceGroup `
    --location $Location `
    --logs-destination log-analytics `
    --logs-workspace-id $lawCustomerId `
    --logs-workspace-key $lawKey `
    --output none
Assert-Az "Create Container Apps Environment"
Write-Ok "Environment ready."

Write-Step "Creating Container App '$caName' (with inline secrets initially)..."
$fullImage = "$acrServer/${imageName}:${imageTag}"

# Build env vars array for clean argument passing
$envVars = @(
    "ACS_ENDPOINT=$acsEndpoint",
    "ACS_ACCESS_KEY=secretref:acs-key",
    "AZURE_OPENAI_ENDPOINT=$aiEndpoint",
    "AZURE_OPENAI_DEPLOYMENT=$AIModel",
    "AZURE_OPENAI_API_VERSION=2025-03-01-preview",
    "COGNITIVE_SERVICES_ENDPOINT=$cogEndpoint",
    "COSMOS_ENDPOINT=$cosmosEndpoint",
    "COSMOS_CONNECTION_STRING=secretref:cosmos-conn",
    "COSMOS_DATABASE=sania-bot",
    "COSMOS_CONTAINER=conversations",
    "CONTAINER_APP_NAME=$caName",
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$appiConnStr"
)

az containerapp create `
    --name $caName --resource-group $ResourceGroup --environment $caeName `
    --image $fullImage --target-port 8000 --ingress external `
    --registry-server $acrServer `
    --registry-username $acrUser `
    --registry-password $acrPass `
    --min-replicas 1 --max-replicas 3 --cpu 1.0 --memory 2Gi `
    --system-assigned `
    --secrets "acs-key=$acsKey" "cosmos-conn=$cosmosConn" `
    --env-vars @envVars `
    --output none
Assert-Az "Create Container App"

$fqdn = az containerapp show --name $caName --resource-group $ResourceGroup `
    --query "properties.configuration.ingress.fqdn" -o tsv
$callbackBase = "https://$fqdn"
Write-Ok "Container App running at $callbackBase"

# Set callback URL (needs FQDN which is only known after creation)
az containerapp update --name $caName --resource-group $ResourceGroup `
    --set-env-vars "VOICE_CALLBACK_BASE_URL=$callbackBase" "CALLBACK_BASE=$callbackBase" `
    --output none
Assert-Az "Set callback URL env vars"

# ══════════════════════════════════════════════════════════════════════
# PHASE 10 — RBAC for Container App managed identity
# ══════════════════════════════════════════════════════════════════════
Write-Step "Assigning RBAC roles to Container App managed identity..."
$caPrincipal = az containerapp identity show --name $caName `
    --resource-group $ResourceGroup --query principalId -o tsv

# CA MI -> Key Vault Secrets User
az role assignment create --assignee $caPrincipal --role "Key Vault Secrets User" --scope $kvId --output none
Assert-Az "Assign CA MI -> KV Secrets User"

# CA MI -> Cognitive Services OpenAI User (for token-based OpenAI access)
az role assignment create --assignee $caPrincipal --role "Cognitive Services OpenAI User" --scope $aiId --output none
Assert-Az "Assign CA MI -> Cognitive Services OpenAI User"

# CA MI -> Cosmos DB Built-in Data Contributor (for AAD-based Cosmos access)
$cosmosId = az cosmosdb show --name $cosmosName --resource-group $ResourceGroup --query id -o tsv
az cosmosdb sql role assignment create --account-name $cosmosName --resource-group $ResourceGroup `
    --role-definition-id "00000000-0000-0000-0000-000000000002" `
    --principal-id $caPrincipal --scope $cosmosId --output none
Assert-Az "Assign CA MI -> Cosmos DB Data Contributor"

Write-Ok "RBAC assigned (Key Vault + OpenAI + Cosmos DB)."

# Wait for RBAC propagation then switch secrets to Key Vault references
Write-Step "Switching secrets to Key Vault references..."
Write-Note "Waiting 30s for RBAC propagation..."
Start-Sleep -Seconds 30

$acsKeyRef = "acs-key=keyvaultref:$kvUri/secrets/acs-access-key,identityref:system"
$cosmosConnRef = "cosmos-conn=keyvaultref:$kvUri/secrets/cosmos-connection-string,identityref:system"
az containerapp secret set --name $caName --resource-group $ResourceGroup `
    --secrets $acsKeyRef $cosmosConnRef --output none
Assert-Az "Switch secrets to KV references"
Write-Ok "Secrets now backed by Key Vault."

# ══════════════════════════════════════════════════════════════════════
# PHASE 11 — Event Grid subscriptions
# ══════════════════════════════════════════════════════════════════════
Write-Step "Waiting for app to become healthy..."
$retries = 0
do {
    Start-Sleep -Seconds 5
    try {
        $resp = Invoke-WebRequest -Uri "$callbackBase/health" -UseBasicParsing -TimeoutSec 5
    } catch {
        $resp = $null
    }
    $retries++
} while ((-not $resp -or $resp.StatusCode -ne 200) -and $retries -lt 24)

if ($resp -and $resp.StatusCode -eq 200) {
    Write-Ok "App is healthy."
} else {
    Write-Note "App not yet healthy - Event Grid creation may require validation retry."
}

Write-Step "Creating Event Grid subscriptions..."
$waEndpoint = "$callbackBase/channels/whatsapp/inbound"
az eventgrid event-subscription create `
    --name "acs-wa-inbound-webhook" `
    --source-resource-id $acsResourceId `
    --endpoint $waEndpoint `
    --included-event-types Microsoft.Communication.AdvancedMessageReceived Microsoft.Communication.AdvancedMessageDeliveryStatusUpdated `
    --output none
Assert-Az "Create WhatsApp Event Grid subscription"
Write-Ok "WhatsApp webhook -> $waEndpoint"

$voiceEndpoint = "$callbackBase/channels/voice/incoming"
az eventgrid event-subscription create `
    --name "acs-voice-incoming-webhook" `
    --source-resource-id $acsResourceId `
    --endpoint $voiceEndpoint `
    --included-event-types Microsoft.Communication.IncomingCall `
    --output none
Assert-Az "Create Voice Event Grid subscription"
Write-Ok "Voice webhook -> $voiceEndpoint"

# ══════════════════════════════════════════════════════════════════════
# SUMMARY
# ══════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  DEPLOYMENT COMPLETE" -ForegroundColor Green
Write-Host "======================================================" -ForegroundColor Green
Write-Host "  App URL:        $callbackBase" -ForegroundColor White
Write-Host "  ACR:            $acrServer" -ForegroundColor White
Write-Host "  ACS:            $acsEndpoint" -ForegroundColor White
Write-Host "  AI Services:    $aiEndpoint" -ForegroundColor White
Write-Host "  Speech:         $cogEndpoint" -ForegroundColor White
Write-Host "  Cosmos DB:      $cosmosName / sania-bot / conversations" -ForegroundColor White
Write-Host "  Key Vault:      $kvUri" -ForegroundColor White
Write-Host "  Log Analytics:  $lawName" -ForegroundColor White
Write-Host "  App Insights:   $appiName" -ForegroundColor White
Write-Host "======================================================" -ForegroundColor Green
Write-Host ""
Write-Host "  MANUAL STEPS REMAINING:" -ForegroundColor Yellow
Write-Host "  1. Purchase a phone number in ACS (Portal > Phone Numbers)" -ForegroundColor Yellow
Write-Host "  2. Configure WhatsApp channel    (Portal > Channels > WhatsApp)" -ForegroundColor Yellow
Write-Host ""
