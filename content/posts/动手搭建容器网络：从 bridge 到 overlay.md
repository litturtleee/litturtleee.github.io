---
title: 动手搭建容器网络：从 bridge 到 overlay
date: 2026-04-23T00:00:00+08:00
draft: false
tags:
  - 网络
description: 基于ip命令搭建容器网络
---
>🐣 作者水平有限，内容仅供参考，如有错误欢迎评论指出。

# 引言

容器技术已经成为现代应用部署的事实标准，我们日常都在用 Docker、Kubernetes，容器之间似乎"天然"就能通信。但这背后到底是怎么实现的？

笔者的上一篇博客《[容器网络基础](https://litturtleee.github.io/posts/%E5%AE%B9%E5%99%A8%E7%BD%91%E7%BB%9C%E5%9F%BA%E7%A1%80/)》已经从概念层面介绍了 veth pair、bridge、iptables 以及 VXLAN / VTEP 等核心机制。本文作为姊妹篇，将完全基于原生的 `ip` 命令，在虚拟机上**手动实现** bridge 单机容器网络和 overlay 跨主机容器网络，通过动手实操加深对容器网络的理解。

全文分三个部分：

- 先简单介绍后面会频繁用到的 `ip` 命令族
- 基于 `ip` 命令搭建 bridge 容器网络，模拟 Docker 单机场景
- 在此基础上引入 VXLAN，搭建 overlay 跨主机容器网络

如果对容器网络的基础概念不太熟悉，建议先阅读《[容器网络基础](https://litturtleee.github.io/posts/%E5%AE%B9%E5%99%A8%E7%BD%91%E7%BB%9C%E5%9F%BA%E7%A1%80/)》。

---
# ip 命令

`ip`命令来自`iproute2`工具包，是现代Linux网络管理的标准工具。
```
ip [选项] <对象> <操作> [参数]
```

核心子命令对象有：

| 对象      | 对应内核实体          |
| ------- | --------------- |
| `link`  | 网络接口（二层设备）      |
| `addr`  | 接口上的协议地址        |
| `route` | 路由表条目           |
| `neigh` | 邻居表（ARP/NDP 缓存） |
| `rule`  | 策略路由规则          |
| `netns` | 网络命名空间          |

通用的操作命令有：`show`(查看)、`add`(添加)、`del`(删除)、`set`(修改)、`flush`(清空)。几乎所有的子命令都支持缩写，如`ip addr`可以写成`ip a`，`ip route`可以写成`ip r`。

下面对每个对象作用以及`show`回显做一个介绍，后续搭建容器网络的过程中会大量用到。

## `ip link`

`ip link`用于管理**二层网络接口**，包括物理网卡、以及后文会用到的 veth、bridge、VTEP 等虚拟设备。

```
root@node1:~# ip link
1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 qdisc noqueue state UNKNOWN mode DEFAULT group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
2: ens160: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP mode DEFAULT group default qlen 1000
    link/ether 00:0c:29:c7:c2:5b brd ff:ff:ff:ff:ff:ff
    altname enp2s0
```

以`ens160`这一项为例，各字段含义如下：

- `2`：接口索引号，内核唯一标识
- `ens160`：接口名
- `<BROADCAST,MULTICAST,UP,LOWER_UP>`：接口 flags
	- `BROADCAST`：支持广播
	- `MULTICAST`：支持组播
	- `UP`：管理上已启用（`ip link set <dev> up` 会打上这个标记）
	- `LOWER_UP`：物理层已连通（类似网线插好）
- `mtu 1500`：最大传输单元
- `state UP`：接口实际运行状态
- `link/ether 00:0c:29:c7:c2:5b`：接口MAC地址
- `brd ff:ff:ff:ff:ff:ff`：广播MAC地址

## `ip addr`

`ip addr`用于管理**接口上的 IP 地址**（三层地址）。相比`ip link`的回显，它会额外展示出接口上的 IP 信息。

```
root@node1:~# ip addr show ens160
2: ens160: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc fq_codel state UP group default qlen 1000
    link/ether 00:0c:29:c7:c2:5b brd ff:ff:ff:ff:ff:ff
    inet 172.16.39.134/24 metric 100 brd 172.16.39.255 scope global dynamic ens160
       valid_lft 1002sec preferred_lft 1002sec
    inet6 fe80::20c:29ff:fec7:c25b/64 scope link 
       valid_lft forever preferred_lft forever
```

二三层信息中二层部分和`ip link`一致，主要关注多出来的 IP 相关字段：

- `inet 172.16.39.134/24`：IPv4 地址及掩码
- `brd 172.16.39.255`：该网段的广播地址
- `scope`：地址作用域
	- `global`：全局可用
	- `link`：仅本链路可用
	- `host`：仅本机可用
- `valid_lft / preferred_lft`：地址有效期，静态配置一般为`forever`
- `inet6 fe80::.../64`：IPv6 地址

## `ip route`

`ip route`用于管理**路由表**，决定某个目的地 IP 的数据包该往哪发。

```
root@node1:~# ip route show
default via 172.16.39.2 dev ens160 proto dhcp src 172.16.39.134 metric 100 
172.16.39.0/24 dev ens160 proto kernel scope link src 172.16.39.134 metric 100 
172.16.39.2 dev ens160 proto dhcp scope link src 172.16.39.134 metric 100
```

上面三条路由分别表示：

- 第一条（默认路由）：去往任意目的地的包，经`172.16.39.2`网关、从`ens160`出
- 第二条（直连路由）：去往`172.16.39.0/24`网段的包直接从`ens160`出，无需网关
- 第三条（直连路由）：去往`172.16.39.2`的包直接从`ens160`出

关键字段：

- `via <gw>`：下一跳网关
- `dev <iface>`：出接口
- `proto`：路由来源
	- `kernel`：内核自动生成（给接口配 IP 时会自动添加对应的直连路由）
	- `dhcp`：DHCP 下发
	- `static`：手动配置
- `scope`：路由作用域，`link`表示目的地就在同一链路上，无需网关
- `src <ip>`：从本机发出时使用的源 IP
- `metric`：优先级，数字越小越优先

## `ip neigh`

`ip neigh`用于查看**邻居表**，即 ARP缓存，**本质是 IP 到 MAC 的映射**。

```
root@node1:~# ip neigh show
172.16.39.135 dev ens160 lladdr 00:0c:29:9c:83:1f STALE 
```

每条记录代表一个邻居缓存项：

- `172.16.39.135`：邻居 IP
- `dev ens160`：从哪个接口学到的
- `lladdr 00:0c:29:9c:83:1f`：对端的 MAC 地址
- 末尾状态：
	- `REACHABLE`：近期通信过，可用
	- `STALE`：已过期但保留，下次使用前会重新验证
	- `DELAY`：等待回包验证中
	- `FAILED`：探测失败

## `ip rule`

`ip rule`用于管理**策略路由规则**，决定一个数据包该查哪一张路由表。

```
root@node1:~# ip rule show
0:      from all lookup local
32766:  from all lookup main
32767:  from all lookup default
```

这是系统默认的三条规则。Linux 其实有多张路由表，平时`ip route show`看到的只是其中的`main`表。

字段说明：

- 行首数字（`0`/`32766`/`32767`）：规则优先级，数字越小越先匹配
- `from all`：匹配条件，`all`表示任意源 IP（也可按源地址、入接口、fwmark等条件匹配）
- `lookup <table>`：匹配后查哪张表
	- `local`：存放本机 IP、广播地址相关的路由
	- `main`：最常用的主路由表
	- `default`：兜底表，默认为空

策略路由的意义在于：可以基于源 IP、入接口等条件走不同的路由表，实现更灵活的分流。

## `ip netns`

`ip netns`用于管理**网络命名空间（Network Namespace）**，它是 Linux 内核提供的网络隔离机制：每个 netns 拥有独立的网络协议栈（接口、路由表、iptables 规则等都各自独立）。

```
root@node1:~# ip netns list
n2
n1
```

>`ip netns add <nsname>`创建的网络命名空间不依赖于进程，所以其将网络命名空间接口`bind mount`到了`/var/run/nets/`下，而运行时例如Docker创建的容器命名空间的入口是暴露在`/proc/<pid>/ns/net`。所以使用`ip netns list`时是看不到容器的网络命名空间的。

最常用的操作是`ip netns exec <ns> <cmd>`，在指定 netns 内执行命令，例如：

```
root@node1:/proc/1675/ns# ip netns exec n1 ip a
1: lo: <LOOPBACK> mtu 65536 qdisc noop state DOWN group default qlen 1000
    link/loopback 00:00:00:00:00:00 brd 00:00:00:00:00:00
```

这条命令的含义是：查看 `n1` 这个命名空间中的 IP 地址信息，相当于"进入容器内执行 `ip addr`"。

---

# bridge容器网络

bridge容器网络是单机容器间通信最常见的方式，具体原理可以查看《[容器网络基础](https://litturtleee.github.io/posts/%E5%AE%B9%E5%99%A8%E7%BD%91%E7%BB%9C%E5%9F%BA%E7%A1%80/)》。其原理可以概括为，基于`veth`和`bridge`设备实现二层网络通信，当容器内需要与宿主机外通信时则基于`iptables`实现NAT。

下面将通过上面介绍的`ip`命令族，在虚拟机上模拟实现bridge容器网络。

## 创建bridge

首先在宿主机上创建网桥，给网桥设备分配IP地址，并启用我们创建的网桥设备。

```
root@node1:~# ip link add br0 type bridge
root@node1:~# ip addr add 10.20.1.1/24 dev br0
root@node1:~# ip link set br0 up
```

配置完成后我们可以通过`ip addr`和`ip route`查看网络地址和路由的变化

```
root@node1:~# ip addr show br0
3: br0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 qdisc noqueue state DOWN group default qlen 1000
    link/ether da:80:0e:09:8e:76 brd ff:ff:ff:ff:ff:ff
    inet 10.20.1.1/24 scope global br0
       valid_lft forever preferred_lft forever

root@node1:~# ip route show 10.20.1.0/24
10.20.1.0/24 dev br0 proto kernel scope link src 10.20.1.1 linkdown 
```

可以看到`br0`成功创建并启用，其ip地址为`10.20.1.1/24`，并且内核为我们生成了一条路由。这条路由表示发往`10.20.1.0/24`网段的包可以从网桥`br0`发出。

## 创建veth

有了网桥后，我们可以创建两个网络命名空间来模拟宿主机上的两个容器。

```
root@node1:~# ip netns add ns1
root@node1:~# ip netns add ns2

root@node1:~# ip netns list
ns2
ns1
```

创建好两个命名空间后，我们再创建两个`veth`设备。

```
root@node1:~# ip link add veth0 type veth peer name veth0-peer
root@node1:~# ip link add veth1 type veth peer name veth1-peer

# 再通过ip link show查看
root@node1:~# ip link show
4: veth0-peer@veth0: <BROADCAST,MULTICAST,M-DOWN> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000
    link/ether c6:33:09:b1:d1:f6 brd ff:ff:ff:ff:ff:ff
5: veth0@veth0-peer: <BROADCAST,MULTICAST,M-DOWN> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000
    link/ether 06:52:d6:7d:9d:20 brd ff:ff:ff:ff:ff:ff
6: veth1-peer@veth1: <BROADCAST,MULTICAST,M-DOWN> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000
    link/ether be:30:dc:57:3c:79 brd ff:ff:ff:ff:ff:ff
7: veth1@veth1-peer: <BROADCAST,MULTICAST,M-DOWN> mtu 1500 qdisc noop state DOWN mode DEFAULT group default qlen 1000
    link/ether c6:cb:d9:96:81:ba brd ff:ff:ff:ff:ff:ff
```

`ip link show`命令可以看到添加上的veth设备信息。接着我们分别将设备一端连接到`br0`上，一端设置到网络命名空间内。

```
# veth一端连接到br0
root@node1:~# ip link set veth0 master br0
root@node1:~# ip link set veth1 master br0
# 启用veth设备
root@node1:~# ip link set veth0 up
root@node1:~# ip link set veth1 up
# 将另一端设置到不同的命名空间内
root@node1:~# ip link set veth0-peer netns ns1
root@node1:~# ip link set veth1-peer netns ns2
```

紧接着我们在网络命名空间内去设置 veth 设备的 IP，并启用设备。

```
root@node1:~# ip netns exec ns1 ip addr add 10.20.1.2/24 dev veth0-peer
root@node1:~# ip netns exec ns2 ip addr add 10.20.1.3/24 dev veth1-peer

root@node1:~# ip netns exec ns1 ip link set up veth0-peer
root@node1:~# ip netns exec ns2 ip link set up veth1-peer
```

这个时候可以进入任意的命名空间内，查看一下当前的网络配置

```
root@node1:~# ip netns exec ns1 ip addr
4: veth0-peer@if5: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP group default qlen 1000
    link/ether c6:33:09:b1:d1:f6 brd ff:ff:ff:ff:ff:ff link-netnsid 0
    inet 10.20.1.2/24 scope global veth0-peer
       valid_lft forever preferred_lft forever
    inet6 fe80::c433:9ff:feb1:d1f6/64 scope link 
       valid_lft forever preferred_lft forever

root@node1:~# ip netns exec ns1 ip route
10.20.1.0/24 dev veth0-peer proto kernel scope link src 10.20.1.2
```

可以看到我们的 `veth-peer` 已经分配好 IP，设备也是 UP 的状态，同时内核也为其配置好了路由。这个时候我们在两个网络命名空间内相互 ping 是没有问题的。

```
root@node1:~# ip netns exec ns1 ping 10.20.1.3
PING 10.20.1.3 (10.20.1.3) 56(84) bytes of data.
64 bytes from 10.20.1.3: icmp_seq=1 ttl=64 time=0.118 ms
64 bytes from 10.20.1.3: icmp_seq=2 ttl=64 time=0.107 ms
```

## 配置iptables

现在单机容器间相互通信就完成了，容器间通过 `br0` 网桥完成了二层的网络通信。但是容器内如果想要和外界通信还需要通过 NAT。Docker 正是基于 iptables 规则实现了 SNAT 和 DNAT，接下来我们逐步模拟实现。

>确保开启内核转发
>```
>cat /proc/sys/net/ipv4/ip_forward
># 若输出为0 则执行
>sysctl -w net.ipv4.ip_forward=1
>```

配置网络命名空间内的默认路由，让命名空间内出去的包能够经由 `br0` 转发到宿主机。Docker 就是在容器启动时完成默认路由的配置。

```
# 网络命名空间内配置默认路由
root@node1:~# ip netns exec ns2 ip route add default via 10.20.1.1 dev veth1-peer
# 查看配置结果
root@node1:~# ip netns exec ns2 ip route show
default via 10.20.1.1 dev veth1-peer 
10.20.1.0/24 dev veth1-peer proto kernel scope link src 10.20.1.3
```

仅配置默认路由后，网络命名空间仍然无法与外界通信，因为这时候数据包的源 IP 是网络命名空间内的 IP，外部设备是不认识这个 IP 的。所以需要配置 iptables 来完成网络地址转换，即 SNAT。

```
# 配置NAT表的POSTROUTING链
# 从10.20.1.0/24网段出发的包都从ens160发出并做ip地址转换
root@node1:~# iptables -t nat -A POSTROUTING -s 10.20.1.0/24 -o ens160 -j MASQUERADE

# 查看iptables配置是否生效
root@node1:~# iptables -t nat -L -n -v
Chain POSTROUTING (policy ACCEPT 1 packets, 136 bytes)
 pkts bytes target     prot opt in     out     source               destination         
    0     0 MASQUERADE  0    --  *      ens160  10.20.1.0/24         0.0.0.0/0      
```

这个时候我们尝试 ping 8.8.8.8（Google 的公共 DNS 服务器）

```
root@node1:~# ip netns exec ns2 ping 8.8.8.8
PING 8.8.8.8 (8.8.8.8) 56(84) bytes of data.
64 bytes from 8.8.8.8: icmp_seq=1 ttl=127 time=0.966 ms
64 bytes from 8.8.8.8: icmp_seq=2 ttl=127 time=1.18 ms
```

完成以上操作后，可以看到网络命名空间能够成功地 ping 通外部网络了。但是外部网络想要主动访问网络命名空间，还缺少 DNAT。

```
# 配置NAT表的PREROUTING链
# 将宿主机的8080端口流量转发到网络命名空间80端口上
root@node1:~# iptables -t nat -A PREROUTING -d 172.16.39.134 -p tcp --dport 8080 -j DNAT --to-destination 10.20.1.3:80

# 查看iptables验证规则是否生效
root@node1:~# iptables -t nat -L -n -v
Chain PREROUTING (policy ACCEPT 0 packets, 0 bytes)
 pkts bytes target     prot opt in     out     source               destination         
    0     0 DNAT       6    --  *      *       0.0.0.0/0            172.16.39.134        tcp dpt:8080 to:10.20.1.3:80

```

此时当外部访问宿主机的 8080 端口时，数据包的目标 IP 会被改写为网络命名空间内的 IP，目标端口会被改写为 80。我们可以在网络命名空间内监听 80 端口，然后在另一台虚拟机上向这台虚拟机的 8080 端口发送消息进行验证。

```
# 在node1上进入ns2监听80
root@node1:~# ip netns exec ns2 nc -lp 80
hello world

# 在node2上尝试与node1通信
root@node2:~# telnet 172.16.39.134 8080
Trying 172.16.39.134...
Connected to 172.16.39.134.
hello world
```

至此，我们就完成了 Docker 默认 bridge 网络的模拟实现。可以看到，Docker 在整个过程中关键就是做了三件事：**创建 bridge、创建 veth、配置 iptables**。

---

# overlay容器网络

bridge 是单机容器网络的默认实现方式，但正因为它是单机网络，没办法实现容器间跨主机的通信，也就无法支持大规模跨主机的容器集群。

为了让不同宿主机上的容器像在同一个网络内一样通信，于是有了 overlay 网络。overlay 本质是在物理网络之上构建一层虚拟网络，将容器间的数据包封装后通过宿主机网络传输，从而解决跨主机通信的问题。定义听起来比较玄乎，其实核心思路就是"套娃"：在原本容器间通信的数据包外再套一层，通过宿主机之间的路由传输。

overlay 网络目前有多种实现方式，本文主要实现基于 VXLAN 的 overlay 网络，常见的 Flannel、Calico 插件都支持 VXLAN。关于 VXLAN 的实现原理可以参考《[容器网络基础](https://litturtleee.github.io/posts/%E5%AE%B9%E5%99%A8%E7%BD%91%E7%BB%9C%E5%9F%BA%E7%A1%80/)》。

>环境准备：需要两个虚拟机节点，且都需要开启内核转发。其次在两个节点上各创建一个网络命名空间，通过 veth 连接到宿主机的 bridge 网桥上。相关操作可以参考上一节的[[动手搭建容器网络：从 bridge 到 overlay#创建bridge|创建 bridge]]和[[动手搭建容器网络：从 bridge 到 overlay#创建veth|创建 veth]]。

## 创建VTEP

VTEP（VXLAN Tunnel Endpoint）是 VXLAN 网络中的隧道端点设备，负责 VXLAN 数据包的封装与解封，本质是一个二层设备。

- 封装：将容器发出的**原始二层帧**封装成 VXLAN UDP 报文，通过宿主机网络传输
- 解封装：收到 VXLAN UDP 报文后，剥掉外层头部，还原出原始二层帧交给目标容器

```sh
ip link add <name> type vxlan \
	id <VNI> \
	dstport 4789 \ 
	local 192.168.1.1 \
	nolearning
```

解释下相关参数：

- `id <VNI>`：VXLAN 网络标识，用以区分隔离不同的 VXLAN 网络
- `dstport 4789`：VXLAN 封装时外层 UDP 的目的端口，4789 是 IANA 标准端口
- `local 192.168.1.1`：封装后外层 IP 头的源地址，一般配置为宿主机的物理网卡 IP
- `nolearning`：禁止 MAC 地址自动学习，这种情况下需要上层控制平面维护静态的 FDB 表

分别完成两个节点的VTEP配置

```
# node1
root@node1:~# ip link add vxlan0 type vxlan id 100 dstport 4789 local 192.168.31.168 nolearning
root@node1:~# ip addr add 10.20.1.0/32 dev vxlan0
root@node1:~# ip link set up vxlan0

root@node1:~# ip addr show vxlan0
6: vxlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UNKNOWN group default qlen 1000
    link/ether b6:57:54:da:a9:d5 brd ff:ff:ff:ff:ff:ff
    inet 10.20.1.0/32 scope global vxlan0
       valid_lft forever preferred_lft forever
    inet6 fe80::b457:54ff:feda:a9d5/64 scope link 
       valid_lft forever preferred_lft forever

# node2
root@node2:~# ip link add vxlan0 type vxlan id 100 dstport 4789 local 192.168.31.202 nolearning
root@node2:~# ip addr add 10.20.2.0/32 dev vxlan0
root@node2:~# ip link set up vxlan0

root@node2:~# ip addr show vxlan0
6: vxlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UNKNOWN group default qlen 1000
    link/ether 66:72:70:04:8b:6d brd ff:ff:ff:ff:ff:ff
    inet 10.20.2.0/32 scope global vxlan0
       valid_lft forever preferred_lft forever
    inet6 fe80::6472:70ff:fe04:8b6d/64 scope link 
       valid_lft forever preferred_lft forever
```

>这里给 VTEP 配置 IP，主要是为了在下一步能基于这个 IP 写入 ARP 表条目（将对端 VTEP 的 IP 映射到其 MAC 地址）。

## 配置路由

接下来完成两个节点上的路由配置，一共两条：

- 网络命名空间内的默认路由（容器 → 宿主机 `br0`），这条路由正常由容器运行时维护
- 宿主机上到对端容器子网的路由（`br0` → `vxlan0`），这条路由正常由网络插件维护

```
# node1
root@node1:~# ip netns exec ns1 ip route add default via 10.20.1.1 dev veth0-peer
# onlink 告诉内核这个下一跳就在这个设备上直连，不需要链路层探测
root@node1:~# ip route add 10.20.2.0/24 via 10.20.2.0 dev vxlan0 onlink

# node2
root@node2:~# ip netns exec ns1 ip route add default via 10.20.2.1 dev veth0-peer
root@node2:~# ip route add 10.20.1.0/24 via 10.20.1.0 dev vxlan0 onlink
```


## 配置ARP表和FDB表

完成上面的路由配置后，理论上从容器内发出、目的地是另一个宿主机上容器的数据包，就能走到 VTEP 设备了。但此时 VTEP 虽然知道了对端 VTEP 的 IP 地址，却并不知道对端的 MAC 地址，而 VXLAN 封装内层二层帧时又必须填对端的 MAC。

这部分工作正常由网络插件完成。以 Flannel 为例，每个节点上都会有一个 flanneld 守护进程，启动时向 etcd 注册自己的网络信息，同时也会监听 etcd 上其他节点的网络信息，然后据此在本机维护 ARP 表和 FDB 表。

```
# node1
# 配置ARP表，将对端VTEP设备的IP路由到对端VTEP设备的MAC地址上
root@node1:~# ip neigh add 10.20.2.0 lladdr 66:72:70:04:8b:6d dev vxlan0 nud permanent
# 配置FDB表，将对端VTEP设备的MAC地址上路由到vxlan0接口出，同时记录对端宿主机IP
root@node1:~# bridge fdb add 66:72:70:04:8b:6d dev vxlan0 dst 192.168.31.202 self permanent

# node2
# 配置ARP表，将对端VTEP设备的IP路由到对端VTEP设备的MAC地址上
root@node2:~# ip neigh add 10.20.1.0 lladdr b6:57:54:da:a9:d5 dev vxlan0 nud permanent
# 配置FDB表，将对端VTEP设备的MAC地址上路由到vxlan0接口出，同时记录对端宿主机IP
root@node2:~# bridge fdb add b6:57:54:da:a9:d5 dev vxlan0 dst 192.168.31.168 self permanent
```

简单来说，ARP 表记录了 IP 到 MAC 的映射，FDB 表记录了 MAC 到"出接口 + 对端宿主机 IP"的映射。完成这两个关键表项的配置后，我们就可以直接在两个命名空间内相互 ping 了。

```
# node1
root@node1:~# ip netns exec ns1 ping 10.20.2.2
PING 10.20.2.2 (10.20.2.2) 56(84) bytes of data.
64 bytes from 10.20.2.2: icmp_seq=1 ttl=62 time=0.603 ms
64 bytes from 10.20.2.2: icmp_seq=2 ttl=62 time=0.945 ms

# node2
root@node2:~# ip netns exec ns1 ping 10.20.1.2
PING 10.20.1.2 (10.20.1.2) 56(84) bytes of data.
64 bytes from 10.20.1.2: icmp_seq=1 ttl=62 time=1.03 ms
64 bytes from 10.20.1.2: icmp_seq=2 ttl=62 time=0.820 ms
```

---


# 总结

本文基于原生的 `ip` 命令，在虚拟机上手动模拟实现了两种典型的容器网络：

**bridge 容器网络** 是 Docker 单机模式下的默认方案，核心动作可以归纳为三步：

1. 创建并启用 bridge 网桥，作为容器间通信的"二层交换机"
2. 为每个"容器"（网络命名空间）创建 veth pair，一端接入网桥、一端放入命名空间
3. 配置 iptables 的 SNAT / DNAT 规则，让容器能够与外部网络双向通信

**overlay 容器网络** 解决了 bridge 无法跨主机通信的痛点，核心思想是在物理网络之上构建一层虚拟隧道。关键动作包括：

1. 两端各创建一个 VTEP 设备，作为 VXLAN 隧道的端点，负责数据包的封装与解封
2. 配置路由，让目标为对端容器子网的数据包能够进入 VTEP
3. 维护 ARP 表和 FDB 表，让 VTEP 知道对端 VTEP 的 MAC 地址及其所在宿主机 IP

在实际的容器平台中，Docker 以及 Kubernetes 的 CNI 插件（Flannel、Calico 等）帮我们屏蔽了这些细节，但自动化背后的核心动作，和本文手工敲的 `ip` 命令并无本质区别。理解这一层，在容器网络排障、方案选型，乃至自己动手实现 CNI 插件时都会带来实质性的帮助。
