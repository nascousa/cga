targetScope = 'subscription'

@description('Azure region for the managed identity deployment.')
param location string = 'eastus'

@description('Existing resource group that contains the shared ACR and Container Apps environment.')
param resourceGroupName string = 'azurepg-icm-automation'

@description('Name of the dedicated Azure Policy proxy user-assigned managed identity.')
param identityName string = 'cga-azure-policy-proxy-mi'

@description('Name of the existing Azure Container Registry.')
param registryName string = 'azurepgicmwfmdevhrcqsoldbnqao'

var readerRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'acdd72a7-3385-48ef-bd42-f606fba81ae7')

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-03-01' existing = {
  name: resourceGroupName
}

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'cgaAzurePolicyProxyIdentity'
  scope: resourceGroup
  params: {
    name: identityName
    location: location
    enableTelemetry: false
    tags: {
      component: 'azure-policy-monitor'
      managedBy: 'CGA'
      purpose: 'read-only-policy-proxy'
    }
  }
}

module acrPullRoleAssignment './acr-pull.bicep' = {
  name: 'cgaAzurePolicyProxyAcrPull'
  scope: resourceGroup
  params: {
    registryName: registryName
    identityName: identityName
    identityPrincipalId: identity.outputs.principalId
  }
}

resource subscriptionReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(subscription().id, identityName, readerRoleDefinitionId)
  properties: {
    roleDefinitionId: readerRoleDefinitionId
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    description: 'Read-only Azure Policy and Activity Log access for the CGA monitor proxy.'
  }
}

output identityResourceId string = identity.outputs.resourceId
output identityClientId string = identity.outputs.clientId
output identityPrincipalId string = identity.outputs.principalId
output acrPullRoleAssignmentId string = acrPullRoleAssignment.outputs.resourceId
output readerRoleAssignmentId string = subscriptionReader.id