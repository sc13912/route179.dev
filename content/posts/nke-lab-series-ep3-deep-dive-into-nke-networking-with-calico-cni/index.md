---
title: "NKE lab series – Ep3: Deep dive into NKE networking with Calico CNI"
date: 2024-08-08T20:11:10
slug: nke-lab-series-ep3-deep-dive-into-nke-networking-with-calico-cni
tags: ["Kubernetes", "NKE", "Nutanix", "Calico"]
wordpress_id: 1162
cover:
  image: "screenshot-2024-08-27-172631.png"
  alt: "NKE lab series – Ep3: Deep dive into NKE networking with Calico CNI"
---
This is the 3rd episode of our NKE lab series. Previously, I have walked through:

- [How to deploy a NKE-enabled Kubernetes cluster in a nested Nutanix CE environment](/2024/08/08/nutanix-kubernetes-engine-nke-lab-series-ep1-create-a-nke-enabled-kubernetes-cluster-on-nutanix-community-edition-ce/)
- [How to provide persistent storage to your NKE clusters using 2x Nutanix CSI options ](/2024/08/08/nke-lab-series-ep2-deploy-a-multi-tier-web-application-on-a-nke-enabled-kubernetes-cluster-using-nutanix-csi/)

In this episode, we’ll deep dive into the NKE networking spaces by exploring the following:

- PART-1: Exploring Calico CNI deployment models within a NKE cluster
- PART-2: Applying standard Kubernetes network policy in a NKE cluster
- PART-3: Leveraging Calico specific policies in a NKE cluster

## **pre-requisites**

- a 1-node or 3-node [Nutanix CE 2.0](https://next.nutanix.com/discussion-forum-14/download-community-edition-38417) cluster deployed in nested virtualization depending on your lab compute capacity, as documented [here](https://www.jeroentielen.nl/installing-nutanix-community-edition-ce-on-vmware-esxi-vsphere/) and [here](https://polarclouds.co.uk/nested-nutanix-ce-deployment/)
- a NKE-enabled K8s cluster deployed in Nutanix CE ([see Ep1](/2024/08/08/nutanix-kubernetes-engine-nke-lab-series-ep1-create-a-nke-enabled-kubernetes-cluster-on-nutanix-community-edition-ce/))
- a Guestbook demo app deployed onto the NKE cluster ([see Ep2](/2024/08/08/nke-lab-series-ep2-deploy-a-multi-tier-web-application-on-a-nke-enabled-kubernetes-cluster-using-nutanix-csi/))
- a lab network environment supports VLAN tagging and provides basic infra services such as AD, DNS, NTP etc (these are required when installing the CE cluster)
- a Linux/Mac workstation for managing the Kubernetes cluster, with **Kubectl installed**

## PART-1: Exploring Calico CNI models in NKE

Calico is recognized as the [most popular CNI plugins](https://www.tigera.io/blog/calico-in-2020-the-worlds-most-popular-kubernetes-cni/) within he Kubernetes community, and it has been widely deployed in production thanks to its reliable performance and comprehensive networking and security features.

Calico supports a variety of flexible Kubernetes networking deployment options, including:

- **Non-Overlay network** (most performant model with no encapsulation involved)
  - **Flat L2 mode** (full-mesh BGP peering between k8s nodes to route Pod IPs)
  - **BGP** (uses iBGP router reflectors to reduce peering pressure in a large cluster, can use ToR L3 switch, or just nodes as software reflectors)
- **Overlay Network** (for cross-subnet cluster connectivity, or there is no BGP support)
  - **IP-in-IP** encapsulation
  - **VXLAN** encapsulation

In a NKE cluster, Calico is pre-configured to use the (default) Flat L2 mode, where all K8s nodes are deployed within the same L2 subnet and establish BGP full-mesh to route Pod IP prefixes. This is the most simple yet performant solution and does not introduce additional dataplane encapsulation overhead.

Let’s check our NKE cluster to find out more details. First let’s see what are the Calico-based CNI API resources made available to us.

```
sc@vx-ops02:~$ kubectl api-resources | grep calico
bgpconfigurations                                     crd.projectcalico.org/v1               false        BGPConfiguration
bgpfilters                                            crd.projectcalico.org/v1               false        BGPFilter
bgppeers                                              crd.projectcalico.org/v1               false        BGPPeer
blockaffinities                                       crd.projectcalico.org/v1               false        BlockAffinity
caliconodestatuses                                    crd.projectcalico.org/v1               false        CalicoNodeStatus
clusterinformations                                   crd.projectcalico.org/v1               false        ClusterInformation
felixconfigurations                                   crd.projectcalico.org/v1               false        FelixConfiguration
globalnetworkpolicies                                 crd.projectcalico.org/v1               false        GlobalNetworkPolicy
globalnetworksets                                     crd.projectcalico.org/v1               false        GlobalNetworkSet
hostendpoints                                         crd.projectcalico.org/v1               false        HostEndpoint
ipamblocks                                            crd.projectcalico.org/v1               false        IPAMBlock
ipamconfigs                                           crd.projectcalico.org/v1               false        IPAMConfig
ipamhandles                                           crd.projectcalico.org/v1               false        IPAMHandle
ippools                                               crd.projectcalico.org/v1               false        IPPool
ipreservations                                        crd.projectcalico.org/v1               false        IPReservation
kubecontrollersconfigurations                         crd.projectcalico.org/v1               false        KubeControllersConfiguration
networkpolicies                                       crd.projectcalico.org/v1               true         NetworkPolicy
networksets                                           crd.projectcalico.org/v1               true         NetworkSet
```

If we query the **ippools.crd.projectcalico.org** API we can see the Calico deployment details.

```
sc@vx-ops02:~$ kubectl get ippools.crd.projectcalico.org -o yaml > nke-dev01-calico-ippool.yaml
sc@vx-ops02:~$ cat nke-dev01-calico-ippool.yaml 
...
  spec:
    allowedUses:
    - Workload
    - Tunnel
    blockSize: 26
    cidr: 172.20.0.0/16
    ipipMode: Never
    natOutgoing: true
    nodeSelector: all()
    vxlanMode: Never
kind: List
```

As shown above, we are not using any overlay encapsulation (both IPinIP or VXLAN mode are off). Each K8s node will get a /26 block from the pre-allocated Pod CIDR (172.20.0.0/16) — this will be used to assign IP addresses to the local Pods.

We can also query the **caliconodestatuses.crd.projectcalico.org** to get further details for node networking. However, to use this function we’ll need to create a **CalicoNodeStatus** resource and specify which node and what information we want to collect.

```
sc@vx-ops02:~$ cat calico-node-status.yaml 
---
apiVersion: crd.projectcalico.org/v1
kind: CalicoNodeStatus
metadata:
  name: caliconodestatus-master-0
spec:
  classes:
    - Agent
    - BGP
    - Routes
  node: nke-dev01-89e792-master-0 
  updatePeriodSeconds: 10

---
apiVersion: crd.projectcalico.org/v1
kind: CalicoNodeStatus
metadata:
  name: caliconodestatus-worker-0
spec:
  classes:
    - Agent
    - BGP
    - Routes
  node: nke-dev01-89e792-worker-0 
  updatePeriodSeconds: 10

---
apiVersion: crd.projectcalico.org/v1
kind: CalicoNodeStatus
metadata:
  name: caliconodestatus-worker-1
spec:
  classes:
    - Agent
    - BGP
    - Routes
  node: nke-dev01-89e792-worker-1 
  updatePeriodSeconds: 10


sc@vx-ops02:~$ kubectl apply -f calico-node-status.yaml 
caliconodestatus.crd.projectcalico.org/caliconodestatus-master-0 created
caliconodestatus.crd.projectcalico.org/caliconodestatus-worker-0 created
caliconodestatus.crd.projectcalico.org/caliconodestatus-worker-1 created
```

Once deployed, let it run for 30 seconds to collect the information and we can then query the **caliconodestatuses.crd.projectcalico.org** API:

```
sc@vx-ops02:~$ kubectl get caliconodestatuses.crd.projectcalico.org -o yaml > nke-dev01-caliconodestatus.yaml
sc@vx-ops02:~$ cat nke-dev01-caliconodestatus.yaml 
```

There’s heaps information here but we’ll only focus at this section under the Master node:

```
...
    bgp:
      numberEstablishedV4: 2
      numberEstablishedV6: 0
      numberNotEstablishedV4: 0
      numberNotEstablishedV6: 0
      peersV4:
      - peerIP: 192.168.102.104
        since: "14:19:16"
        state: Established
        type: NodeMesh
      - peerIP: 192.168.102.103
        since: "14:19:26"
        state: Established
        type: NodeMesh

    routes:
      routesV4:
...
      - destination: 172.20.188.64/26
        gateway: 192.168.102.103
        interface: eth0
        learnedFrom:
          peerIP: 192.168.102.103
          sourceType: NodeMesh
        type: FIB
      - destination: 172.20.116.128/26
        gateway: 192.168.102.104
        interface: eth0
        learnedFrom:
          peerIP: 192.168.102.104
          sourceType: NodeMesh
        type: FIB
```

From above we can confirm the master node (192.168.102.102) has established full mesh with the other 2x worker nodes

- Worker-0: 192.168.102.103 (learned Pod routes 172.20.188.64/26 via BGP)
- Worker-1: 192.168.102.104 (learned Pod routes 172.20.116.128/26 via BGP)

and we can further confirm the Pod routes by looking at Pod IP addresses on each worker node:

```
sc@vx-ops02:~$ kubectl get pods --all-namespaces -o wide | grep nke-dev01-89e792-worker-1 | grep 172.20
guestbook        frontend-795b566649-4zkqc                    1/1     Running   0              82m   172.20.116.140    nke-dev01-89e792-worker-1   <none>           <none>
guestbook        frontend-795b566649-8tf76                    1/1     Running   0              82m   172.20.116.139    nke-dev01-89e792-worker-1   <none>           <none>
guestbook        redis-follower-5ffdf87b7d-4lqlr              1/1     Running   0              82m   172.20.116.141    nke-dev01-89e792-worker-1   <none>           <none>
guestbook        redis-leader-c767d6dbb-t5t8j                 1/1     Running   0              82m   172.20.116.142    nke-dev01-89e792-worker-1   <none>           <none>
kube-system      calico-kube-controllers-5cd67d7657-52km8     1/1     Running   0              11h   172.20.116.128    nke-dev01-89e792-worker-1   <none>           <none>
kube-system      coredns-6fb596b5df-p7mhq                     1/1     Running   0              11h   172.20.116.129    nke-dev01-89e792-worker-1   <none>           <none>
metallb-system   controller-77676c78d9-5gvd6                  1/1     Running   0              93m   172.20.116.136    nke-dev01-89e792-worker-1   <none>           <none>
ntnx-system      alertmanager-main-0                          2/2     Running   1 (11h ago)    11h   172.20.116.133    nke-dev01-89e792-worker-1   <none>           <none>
ntnx-system      blackbox-exporter-5458d77cfb-d62kc           3/3     Running   0              11h   172.20.116.132    nke-dev01-89e792-worker-1   <none>           <none>
ntnx-system      csi-snapshot-webhook-756b45fb5c-t9k8k        1/1     Running   0              11h   172.20.116.131    nke-dev01-89e792-worker-1   <none>           <none>
ntnx-system      fluent-bit-v8gdt                             1/1     Running   0              11h   172.20.116.130    nke-dev01-89e792-worker-1   <none>           <none>
sc@vx-ops02:~$ 
sc@vx-ops02:~$ kubectl get pods --all-namespaces -o wide | grep nke-dev01-89e792-worker-0 | grep 172.20
guestbook        frontend-795b566649-j4r75                    1/1     Running   0              82m   172.20.188.73     nke-dev01-89e792-worker-0   <none>           <none>
kube-system      coredns-6fb596b5df-4kkrc                     1/1     Running   0              11h   172.20.188.64     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      csi-snapshot-controller-7d68bf5bd7-6fg9c     1/1     Running   0              11h   172.20.188.65     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      fluent-bit-t9d7c                             1/1     Running   0              11h   172.20.188.66     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      kube-state-metrics-54c97cdfdd-rkm2m          3/3     Running   0              11h   172.20.188.69     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      kubernetes-events-printer-6f44868d47-5sg98   1/1     Running   0              11h   172.20.188.67     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      prometheus-adapter-678c647d87-rbkqc          1/1     Running   0              11h   172.20.188.70     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      prometheus-k8s-0                             2/2     Running   0              11h   172.20.188.71     nke-dev01-89e792-worker-0   <none>           <none>
ntnx-system      prometheus-operator-f57b8d9cb-kpwp9          2/2     Running   0              11h   172.20.188.68     nke-dev01-89e792-worker-0   <none>           <none>
```

## PART-2: USING standard K8s network policy

With Calico deployed in our NKE cluster, we can straight away using standard Kubernetes Network policies to enhance cluster-wide security.

For example, by default we can access the Guestbook service from anywhere within the cluster. We can test this by launching a testpod within the default namespace and curl the frontend service (using K8s DNS format ***service.namespace***)

```
sc@vx-ops02:~$ kubectl run testpod -it  --rm --image=yauritux/busybox-curl -- sh
/home # 
/home # curl frontend.guestbook
<html ng-app="redis">
  <head>
    <title>Guestbook</title>
    <link rel="stylesheet" href="//netdna.bootstrapcdn.com/bootstrap/3.1.1/css/bootstrap.min.css">
    https://ajax.googleapis.com/ajax/libs/angularjs/1.2.12/angular.min.js
    http://controllers.js
    https://cdnjs.cloudflare.com/ajax/libs/angular-ui-bootstrap/2.5.6/ui-bootstrap-tpls.js
  </head>
  <body ng-controller="RedisCtrl">
    <div style="width: 50%; margin-left: 20px">
      <h2>Guestbook</h2>
    <form>
    <fieldset>
    <input ng-model="msg" placeholder="Messages" class="form-control" type="text" name="input"><br>
    <button type="button" class="btn btn-primary" ng-click="controller.onRedis()">Submit</button>
    </fieldset>
    </form>
    <div>
      <div ng-repeat="msg in messages track by $index">
        {{msg}}
      </div>
    </div>
    </div>
  </body>
</html>
```

So how about if I only want to limit a certain namespace to access and consume the guestbook service? For example, we can use the below K8s ingress network policy to limit access to our guestbook service only from the namespace of “testns”.

```
sc@vx-ops02:~$ kubectl create ns testns

sc@vx-ops02:~$ kubectl get ns --show-labels | grep testns
testns            Active   40m     kubernetes.io/metadata.name=testns

sc@vx-ops02:~$ cat net-policy.yaml 
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: mypolicy
  namespace: guestbook
spec:
  policyTypes:
  - Ingress
  ingress:
  - from:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: testns

sc@vx-ops02:~$ kubectl apply -f net-policy.yaml 
networkpolicy.networking.k8s.io/mypolicy created
```

After applying the ingress policy, we should only be able to access the frontend service from within the **testns** namespace. Open another terminal, launch a new testpod in the testns namespace to verify this.

```
sc@vx-ops02:~$  kubectl run testpod -n testns -it  --rm --image=yauritux/busybox-curl -- sh

/home # curl frontend.guestbook
<html ng-app="redis">
  <head>
    <title>Guestbook</title>
    <link rel="stylesheet" href="//netdna.bootstrapcdn.com/bootstrap/3.1.1/css/bootstrap.min.css">
    https://ajax.googleapis.com/ajax/libs/angularjs/1.2.12/angular.min.js
    http://controllers.js
    https://cdnjs.cloudflare.com/ajax/libs/angular-ui-bootstrap/2.5.6/ui-bootstrap-tpls.js
  </head>
  <body ng-controller="RedisCtrl">
    <div style="width: 50%; margin-left: 20px">
      <h2>Guestbook</h2>
    <form>
    <fieldset>
    <input ng-model="msg" placeholder="Messages" class="form-control" type="text" name="input"><br>
    <button type="button" class="btn btn-primary" ng-click="controller.onRedis()">Submit</button>
    </fieldset>
    </form>
    <div>
      <div ng-repeat="msg in messages track by $index">
        {{msg}}
      </div>
    </div>
    </div>
  </body>
</html>
/home # 
```

Back at the previous terminal, we are now unable to connect to the frontend service from the first testpod within the default namespace, cool!

```
/home # curl frontend.guestbook
curl: (28) Failed to connect to frontend.guestbook port 80 after 127280 ms: Operation timed out
```

## PART-3: leveraging Calico specific policies

One of the limitations with standard Kubernetes network policy is that you can only apply permit rules but deny rules are not supported. This makes it difficult to implement a “blacklist” scenario where you want to only block certain conditions but allow the rest.

Good news is that we can use Calico based network policies which supports deny rules.

In the below example, we leverage the Calico CNI to create 2x egress rules for the guestbook namespace to deny outbound access to 8.0.0.0/8 but allow to the rest networks.

```
sc@vx-ops02:~$ cat net-policy1.yaml 
---
apiVersion: crd.projectcalico.org/v1
kind: NetworkPolicy
metadata:
  name: custom-deny-egress
  namespace: guestbook
spec:
  order: 10
  types:
    - Egress
  egress:
    - action: Deny
      destination:
        nets:
          - 8.0.0.0/8
---
apiVersion: crd.projectcalico.org/v1
kind: NetworkPolicy
metadata:
  name: default-allow-egress
  namespace: guestbook
spec:
  order: 20
  types:
    - Egress
  egress:
    - action: Allow
      destination:
        nets:
          - 0.0.0.0/0
```

Now let’s apply the Calico policy.

```
sc@vx-ops02:~$ kubectl apply -f net-policy1.yaml 
networkpolicy.crd.projectcalico.org/custom-deny-egress created
networkpolicy.crd.projectcalico.org/default-allow-egress created
```

To test this, we can simply find a pod in the guestbook namespace, attach to it and run some ping tests.

```
sc@vx-ops02:~$ kubectl get pods -n guestbook 
NAME                              READY   STATUS    RESTARTS   AGE
frontend-795b566649-4zkqc         1/1     Running   0          7h3m
frontend-795b566649-8tf76         1/1     Running   0          7h3m
frontend-795b566649-j4r75         1/1     Running   0          7h3m
redis-follower-5ffdf87b7d-4lqlr   1/1     Running   0          7h3m
redis-leader-c767d6dbb-t5t8j      1/1     Running   0          7h3m

sc@vx-ops02:~$ kubectl exec -ti -n guestbook frontend-795b566649-j4r75 -- sh
# apt-get install iputils-ping

# ping 8.8.8.8
PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
^C
--- 8.8.8.8 ping statistics ---
7 packets transmitted, 0 received, 100% packet loss, time 6ms

# ping 1.1.1.1
PING 1.1.1.1 (1.1.1.1) 56(84) bytes of data.
64 bytes from 1.1.1.1: icmp_seq=1 ttl=55 time=10.4 ms
64 bytes from 1.1.1.1: icmp_seq=2 ttl=55 time=9.33 ms
^C
--- 1.1.1.1 ping statistics ---
2 packets transmitted, 2 received, 0% packet loss, time 2ms
rtt min/avg/max/mdev = 9.330/9.856/10.382/0.526 ms
# 
# ping 192.168.100.5
PING 192.168.100.5 (192.168.100.5) 56(84) bytes of data.
64 bytes from 192.168.100.5: icmp_seq=1 ttl=126 time=0.877 ms
64 bytes from 192.168.100.5: icmp_seq=2 ttl=126 time=0.792 ms
^C
```

As we can see, from the frontend pod we are enable to ping Google but still able to access the rest of networks – exactly what we expected!
