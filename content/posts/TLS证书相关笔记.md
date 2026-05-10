---
title: TLS证书相关笔记
date: 2026-05-10T00:00:00+08:00
draft: false
tags:
  - 网络
description: 理解TLS工作流程与原理
---
>🐣 作者水平有限，内容仅供参考，如有错误欢迎评论指出。

# 引言

笔者之前一直对自签证书的工作流程比较模糊，一些关键概念也理解有误。为了加深印象，本文针对自签证书相关的工作流程和原理进行梳理。

---

# 基本概念

## 三个核心概念

- **私钥 (.key)** — 不公开，不传输。
- **证书 (.crt/.cer/.pem)** — 公开、传输，内部记录了公钥+身份信息+CA签名。
- **CA 证书** — 公开、传输。用于验证别人证书的信任锚。

一句话概括：私钥永远不出门，证书随便发，CA 证书是信任锚。单向只服务端有身份，mTLS 两边都有身份。

## 自签CA流程

自签CA证书普遍使用于内部服务之间的调用验证，公共CA只能用域名所有权证明身份，而通常情况下不会直接给微服务分配公网域名，所以没办法用公共CA给微服务签发证书。

![自签证书工作流程.excalidraw](/images/自签证书工作流程.excalidraw.svg)

自签CA可以通过openssl工具生成

```sh
openssl genrsa -out ca.key 4096
openssl req -x509 -new -nodes -key ca.key -days 3650 \
 -subj "/CN=My CA" -out ca.crt
```

拥有了自己的CA后，就可以生成服务端和客户端的私钥，并且通过CA完成证书的签发。

```sh
# 生成服务端证书
openssl genrsa -out server.key 2048
openssl req -new -key server.key -out server.csr \
  -subj "/CN=service.com"
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out server.crt -days 365 \
  -extfile <(echo "subjectAltName=DNS:service.com,DNS:localhost,IP:127.0.0.1")

# 生成客户端证书
openssl genrsa -out client.key 2048
openssl req -new -key client.key -out client.csr -subj "/CN=client"
openssl x509 -req -in client.csr -CA ca.crt -CAkey ca.key -CAcreateserial \
  -out client.crt -days 365
```

完成了证书生成后，还需要让服务端和客户端都把自签CA证书加入到信任列表中。前面的概念也提到了，验证证书是基于CA证书完成的，而服务端和客户端的证书都是用的自签CA签发的，客户端要验证**服务端**就需要 CA 证书，服务端要验证**客户端**证书同理。

自签CA的信任可以有两种方式：
- 加到操作系统的信任库中，以linux为例可以将CA证书复制到`/usr/local/share/ca-certificates`目录下，并更新CA证书`update-ca-certificates`
- 只在应用程序中加载，简单来说就是在应用内部添加需要信任的根CA证书。以golang服务为例，客户端可以用`tls.Config.RootCAs`，服务端可以用`tls.Config.ClientCAs`来配置CA证书。

自签证书过程中可以生成多种类型的文件，具体用途如下表：

| 后缀        | 内容      | 说明                        |
| --------- | ------- | ------------------------- |
| .pem      | 任意      | base64 文本编码，可以装证书/私钥/拼接多个 |
| .crt/cer  | 证书      | 可能是 PEM 也可能是 DER          |
| .der      | 证书      | 二进制编码                     |
| .key      | 私钥      | 约定俗成                      |
| .csr      | 证书签名请求  | 中间产物，签完就扔                 |
| .p12/.pfx | 私有+证书+链 | PKCS#12，加密打包，两个后缀同一个东西    |

## TLS握手流程

![Drawing TLS握手流程.excalidraw](/images/Drawing TLS握手流程.excalidraw.svg)

1. 客户端发送随机数、支持的算法
2. 服务端发送证书
3. 客户端用 ca.crt 验证 server.crt 的签名
4. 服务端要求客户端证书
5. 客户端发证书
6. 服务端用 ca.crt 验证 client.crt
7. 双方算出对称密钥
8. 后续都用对称密钥加密

--- 

# 总结

1. **私钥与证书分工明确**：私钥不出门，证书可以随便发。CA 私钥只做一件事签发证书。 
2. **自签 CA 服务于私有信任域**：公网用公共 CA，内网用自签 CA。
 3. **server.crt / client.crt** ：出示自己的身份
 4. **server.key / client.key** ： 证明这身份是自己的
 5. **ca.crt** ： 验证对方的身份


