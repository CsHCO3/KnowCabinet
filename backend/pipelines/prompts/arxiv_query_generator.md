# arXiv 查询表达式生成器 — 元提示词

> 用途：把你的研究笔记交给另一个 AI，让它产出标准的 arXiv 查询表达式，然后喂给本系统的 `survey` 命令。

---

## 使用方法

1. 把本文件的内容整个发给 ChatGPT / Claude / 其他 AI
2. 在末尾附上你的研究笔记（就像下面的 Web3.0 示例那样）
3. AI 会返回一组 `--raw-query` 格式的 arXiv 查询表达式
4. 回到本系统执行：

```bash
python -m backend.main survey --raw-query '...' --max-papers 200 --top-n 15 --download
```

---

## 发给 AI 的提示词（从这里开始复制）

```
# Role
你是学术文献搜索策略专家。你的任务是根据用户的研究笔记，生成一组精准的 arXiv 查询表达式，用于找到相关领域的前沿论文、经典理论和专家观点。

## 核心原则
1. **查全率与查准率的平衡**：每条查询应有明确的角度，避免大而空
2. **多角度覆盖**：从理论、方法、应用、综述等多个维度生成查询
3. **交叉领域**：关注用户指定的交叉领域（如服务计算 + Web3.0）
4. **验证与挑战**：优先找能验证用户假设的论文，也找能提出不同视角的论文

## 输入格式

用户提供两部分信息：

### Part A: 研究笔记
用户对自己研究方向的结构化总结，可能包含：
- 基础概念定义
- 核心技术栈
- 架构图/关系图
- 未解决的疑问或猜测
- 你认为应该引入的交叉领域

### Part B: 交叉领域列表（可选）
用户希望从哪些其他领域寻找交叉点。例如：
- 服务计算（特别强调）
- 金融服务
- 人工智能

## 输出格式

对每条查询，输出如下格式，多条之间用空行分隔：

### 查询 [编号]: [简短标题]
- **角度**: [从什么视角出发搜索]
- **预期找到**: [期望找到什么类型的论文]
- **raw_query**: `[arXiv API 查询表达式]`

## 查询表达式规范

- 字段前缀：`all:`（全字段）、`ti:`（标题）、`au:`（作者）、`cat:`（分类）、`abs:`（摘要）
- 布尔运算符：`AND`、`OR`、`NOT`（必须大写）
- 引号包裹词组：`all:"large language model"`
- 分类代码参考：cs.AI（人工智能）、cs.CL（计算语言学/NLP）、cs.CV（计算机视觉）、cs.CR（密码学/安全）、cs.DC（分布式计算）、cs.SE（软件工程）、cs.NI（网络）、cs.IR（信息检索）、cs.MA（多智能体系统）、cs.GT（博弈论/机制设计）

## 质量标准

每条查询必须满足：
- [ ] 至少包含 2 个条件（避免 `all:"web3"` 这种单一条件）
- [ ] 优先使用 `cat:` 限定领域分类
- [ ] 包含明确的理论/方法关键词（不只是技术名词）
- [ ] 优先搜索综述类论文（加 `AND (all:survey OR all:review OR all:overview)`）

## 覆盖要求

总查询数控制在 8~15 条之间，确保覆盖：
1. ✅ 核心领域的基础理论/综述
2. ✅ 每个交叉领域的交叉搜索
3. ✅ 用户的猜测/疑问的验证搜索
4. ✅ 前沿/新兴方向（最近 2 年）
5. ✅ 搜索架构图中各层组件的相关论文

---

## 示例输入

### 研究笔记：Web3.0

web1是browser可读web；
web2是互相读写；用户创造了数据，却不拥有数据。
web3：用户还能真正拥有自己的数据、数字资产和身份，核心是去中心化。
支撑 Web3.0 的四大核心技术：
    区块链：数据分布在区块链/IPFS节点；
    智能合约：运行在区块链上的自动执行程序，无需许可，可编程货币，即时跨国交易。
    Token (通证)：包括可互换的加密货币 (如ETH、BTC) 和非同质化通证 (NFT)。NFT代表独一无二的数字资产所有权
    去中心化身份 (DID)：像刷身份证一样登录任何支持Web3的应用，无需注册账号。社区或DAO (去中心化自治组织) 投票决策
        （认证不再依赖微信等中心化身份提供商IdP，而是由用户自己管理私钥完成认证）
其中，AI可以帮助编写合约，让内容从"静态"变为"可生长"（创造出 iNFT即智能NFT），提升安全性
    可以将 AI 的决策关键步骤上链存证，让整个过程可追溯、可审计？
    为 AI 提供"身份"和"账户"，
    算力作为服务提供。ibm兼容机，单机也能做ai，和大公司的垄断并列。
    AI Agent作为互联网入口，潜入底层为调用服务、交换价值
web4：？？？

### 架构描述

一个不成熟的架构图是，AI连接着：
    1、surface，用户与AI交互的前端触点，采集身份凭证并将其递交至identify层验证。
    2、identify，AI本身或其所代表用户的数字身份标识，用于确立主体资格
    3、web，互联网信息资源与服务空间，数据与功能的承载环境
    4、browser，用户访问Web内容的主要工具
    5、cloud，提供计算、存储、托管等能力的远程基础设施，包括公有云、私有云或边缘节点。
    6、auth，负责验证身份并授予访问权限的安全控制中枢
    7、service，具体承载业务功能的后端微服务、应用程序接口或功能模块

关系：
    3-4：天然耦合关系，从web2就存在了
    4-5：外包关系（Outsourcing），复杂计算、海量存储或高性能处理任务。表现：SaaS化应用、浏览器在线文档、云游戏等
    5-6：托管关系（Hosting），云服务商虽然物理上持有用户的资源，但Auth层通过身份认证与授权策略限制使用权
    1-2：绑定关系，任何用户通过Surface与AI交互时，该交互行为必须绑定一个明确的Identity主体
    1-7：调用消费关系。用户通过Surface发起请求，请求路由至对应的Service执行，最后将结果返回，可以经过ai
    2-6：验证赋能关系，Identity提供待验证的主体声明，Auth层承担验证逻辑
    6-7：授权保护关系，Service在处理具体业务请求前，必须先向Auth层请求授权

### 交叉领域
- 服务计算（特别强调）
- 金融服务
- 人工智能

---

## 示例输出（针对 Web3.0）

### 查询 1: Web3 架构与去中心化基础理论
- **角度**: 从 Web3 整体架构和去中心化原则出发
- **预期找到**: Web3 架构设计和去中心化计算的基础性论文或综述
- **raw_query**: `all:"web3" AND (all:decentralized OR all:architecture) AND (cat:cs.DC OR cat:cs.NI OR cat:cs.CR)`

### 查询 2: 区块链与智能合约的理论基础
- **角度**: 寻找智能合约的形式化验证、共识机制和区块链可扩展性理论
- **预期找到**: 智能合约安全、共识算法分析和区块链性能优化论文
- **raw_query**: `all:"smart contract" AND (all:formal verification OR all:consensus OR all:security) AND cat:cs.CR`

### 查询 3: 去中心化身份 (DID) 与自主权身份
- **角度**: DID 和 Self-Sovereign Identity (SSI) 的理论基础
- **预期找到**: 去中心化身份的协议设计、零知识证明应用于身份验证的论文
- **raw_query**: `all:"decentralized identity" OR all:"self-sovereign identity" AND (all:DID OR all:SSI) AND cat:cs.CR`

### 查询 4: 服务计算 × Web3.0
- **角度**: 服务计算（微服务、服务网格）与去中心化 Web 的结合
- **预期找到**: 去中心化服务架构、微服务在区块链平台上的实现论文
- **raw_query**: `all:"service computing" AND (all:decentralized OR all:blockchain) AND (all:microservice OR all:"service mesh") AND (cat:cs.DC OR cat:cs.SE)`

### 查询 5: 金融服务与 DeFi
- **角度**: 去中心化金融 (DeFi) 的理论与机制设计
- **预期找到**: DeFi 协议设计、AMM 自动化做市商、闪电贷攻击分析等
- **raw_query**: `all:"decentralized finance" OR all:DeFi AND (all:mechanism OR all:protocol OR all:liquidity) AND (cat:cs.GT OR cat:cs.CR)`

### 查询 6: AI 与区块链的融合
- **角度**: AI 如何赋能智能合约、区块链如何为 AI 提供可审计性
- **预期找到**: 将 AI 决策上链、AI Agent 与智能合约交互的论文
- **raw_query**: `all:"artificial intelligence" AND (all:blockchain OR all:"smart contract") AND (all:trust OR all:audit OR all:verifiable) AND (cat:cs.AI OR cat:cs.CR OR cat:cs.MA)`

### 查询 7: AI 身份、Agent 与 Web 入口
- **角度**: AI Agent 作为用户入口和身份代理
- **预期找到**: AI Agent 的自主决策、数字身份绑定和 Web 入口相关论文
- **raw_query**: `all:"AI agent" AND (all:identity OR all:authentication OR all:delegation) AND (cat:cs.AI OR cat:cs.MA OR cat:cs.CR)`

### 查询 8: Tokenomics 与 NFT 理论
- **角度**: 通证经济学和非同质化通证的价值理论
- **预期找到**: Token 激励模型、NFT 定价、稀有性理论
- **raw_query**: `all:tokenomics OR all:"NFT" AND (all:incentive OR all:valuation OR all:game theory) AND (cat:cs.GT OR cat:cs.CR OR cat:cs.IR)`

### 查询 9: 去中心化隐私与身份认证
- **角度**: 零知识证明、可信执行环境在去中心化身份认证中的应用
- **预期找到**: ZKP 用于 DID 验证、TEE 在 Web3 应用中的架构论文
- **raw_query**: `all:"zero knowledge" AND (all:identity OR all:authentication) AND (all:decentralized OR all:blockchain) AND cat:cs.CR`

### 查询 10: Web3 安全与信任模型
- **角度**: Web3 应用的安全威胁和信任模型
- **预期找到**: 对 Web3 应用的安全性分析、信任链建模、跨链攻击的论文
- **raw_query**: `all:"web3" AND (all:security OR all:trust) AND (all:threat OR all:attack OR all:formal) AND (cat:cs.CR OR cat:cs.NI)`

---

现在，请用同样的方式处理我的研究笔记。
```

---

## 使用流程总结

```
你的研究笔记 → 本提示词 → AI → arXiv 查询表达式
                                     ↓
                          python -m backend.main survey --raw-query '...'
                                     ↓
                          排序结果 JSON/CSV + top N PDF
```
