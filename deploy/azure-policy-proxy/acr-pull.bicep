targetScope = 'resourceGroup'

@description('Name of the existing Azure Container Registry.')
param registryName string

@description('Stable managed identity name used to generate the role assignment GUID.')
param identityName string

@description('Object ID of the dedicated proxy managed identity.')
param identityPrincipalId string

var acrPullRoleDefinitionId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource acrPullRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identityName, acrPullRoleDefinitionId)
  scope: registry
  properties: {
    roleDefinitionId: acrPullRoleDefinitionId
    principalId: identityPrincipalId
    principalType: 'ServicePrincipal'
    description: 'Allows the CGA Azure Policy proxy identity to pull only its runtime image.'
  }
}

output resourceId string = acrPullRoleAssignment.id