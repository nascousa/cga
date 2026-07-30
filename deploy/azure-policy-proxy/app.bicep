targetScope = 'resourceGroup'

@description('Azure region of the existing Container Apps environment.')
param location string = 'eastus'

@description('Name of the managed read proxy Container App.')
param containerAppName string = 'cga-azure-policy-proxy'

@description('Name of the existing Container Apps environment.')
param managedEnvironmentName string = 'azurepg-icm-wfm-dev-cae-hrcqsold'

@description('Name of the existing Azure Container Registry.')
param registryName string = 'azurepgicmwfmdevhrcqsoldbnqao'

@description('Name of the dedicated proxy user-assigned managed identity.')
param identityName string = 'cga-azure-policy-proxy-mi'

@description('Fully qualified immutable proxy image reference.')
param image string

@secure()
@description('Shared request key supplied only through the Container App secret store.')
param proxySharedKey string

resource managedEnvironment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: managedEnvironmentName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

module proxyApp 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'cgaAzurePolicyProxyApp'
  params: {
    name: containerAppName
    location: location
    environmentResourceId: managedEnvironment.id
    activeRevisionsMode: 'Single'
    enableTelemetry: false
    ingressAllowInsecure: false
    ingressExternal: true
    ingressTargetPort: 8080
    ingressTransport: 'http'
    maxInactiveRevisions: 1
    managedIdentities: {
      userAssignedResourceIds: [
        identity.id
      ]
    }
    registries: [
      {
        server: registry.properties.loginServer
        identity: identity.id
      }
    ]
    secrets: [
      {
        name: 'proxy-shared-key'
        value: proxySharedKey
      }
    ]
    containers: [
      {
        name: 'proxy'
        image: image
        env: [
          {
            name: 'TARGET_SUBSCRIPTION_ID'
            value: subscription().subscriptionId
          }
          {
            name: 'PROXY_SHARED_KEY'
            secretRef: 'proxy-shared-key'
          }
          {
            name: 'AZURE_CLIENT_ID'
            value: identity.properties.clientId
          }
          {
            name: 'MAX_ACTIVITY_LOOKBACK_MINUTES'
            value: '1440'
          }
          {
            name: 'MAX_RESPONSE_BYTES'
            value: '26214400'
          }
          {
            name: 'AZURE_TIMEOUT_SECONDS'
            value: '30'
          }
          {
            name: 'AZURE_MAX_ATTEMPTS'
            value: '4'
          }
          {
            name: 'MAX_COLLECTION_ITEMS'
            value: '50000'
          }
        ]
        probes: [
          {
            type: 'Startup'
            httpGet: {
              path: '/healthz'
              port: 8080
            }
            failureThreshold: 30
            periodSeconds: 2
            timeoutSeconds: 1
          }
          {
            type: 'Liveness'
            httpGet: {
              path: '/healthz'
              port: 8080
            }
            initialDelaySeconds: 5
            periodSeconds: 30
            timeoutSeconds: 2
          }
          {
            type: 'Readiness'
            httpGet: {
              path: '/healthz'
              port: 8080
            }
            initialDelaySeconds: 2
            periodSeconds: 10
            timeoutSeconds: 2
          }
        ]
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
      }
    ]
    scaleSettings: {
      minReplicas: 0
      maxReplicas: 1
      pollingInterval: 30
      cooldownPeriod: 300
      rules: [
        {
          name: 'http-requests'
          http: {
            metadata: {
              concurrentRequests: '10'
            }
          }
        }
      ]
    }
    tags: {
      component: 'azure-policy-monitor'
      managedBy: 'CGA'
      purpose: 'read-only-policy-proxy'
    }
  }
}

output endpoint string = 'https://${proxyApp.outputs.fqdn}'
output containerAppResourceId string = proxyApp.outputs.resourceId