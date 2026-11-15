# BybitAITrade 接入记录

核验日期：2026-11-15。本文不包含 API Key、Secret、数据库密码或 OAuth 令牌。

## 账户与权限

| 项目 | 已核验值 |
|---|---|
| 主账户 UID | `587220933` |
| 交易子账户 | `BybitAITrade`，标准子账户 |
| 子账户用户名 | `BybitSumruhTcCnc` |
| 子账户 UID | `602623515` |
| API 备注 | `bybitbot-ai-20261114` |
| 签名 | 系统生成 HMAC |
| API 模式 | 读写，`readOnly=0` |
| 非空权限组 | `Spot: [SpotTrade]`、`Options: [OptionsTrade]`、`Derivatives: [DerivativesTrade]` |
| 提现、资产划转 | 未开放；`Wallet` 为空 |
| 普通合约订单/持仓、兑换、理财 | `ContractTrade`、`Exchange`、`Earn` 均为空 |
| REST IP 白名单 | `161.33.210.35`，NAS 当前代理出口 |
| 账户模式 | UTA 2.0，`REGULAR_MARGIN` |

Bybit 的期权权限与 USDC 衍生品权限合并，涵盖期权和 USDC 永续。服务端在请求清空 `Derivatives` 后仍返回 `DerivativesTrade`；应以以上实际返回权限为准。本地配置允许的产品仍为 `spot`、`option`。

创建表单选择的权限与初次 API 查询返回值不一致。已通过 `/v5/user/update-sub-api` 收紧权限，再独立调用 `/v5/user/query-api` 核验上述结果。

Bybit 创建页提示私有 WebSocket 请求不受 API IP 白名单约束。当前项目 worker 主链路使用 REST 轮询。更换 NAS 代理出口后，应先更新白名单并重新核验，不能据此认为所有接入通道均受同一 IP 限制。

## 凭据及配置位置

| 位置 | 文件 |
|---|---|
| Mac API 凭据 | `/Users/nicholaswang/Desktop/code/bybit/secrets/bybit-ai-trade.json` |
| NAS API 凭据 | `/home/nicholas/.config/bybitbot/credentials.bybit-ai-trade.json` |
| 容器内 API 凭据 | `/run/bybitbot/credentials.bybit-ai-trade.json` |
| NAS 待启用配置 | `/home/nicholas/.config/bybitbot/config.ai-trade.staged.yaml` |
| 容器内待启用配置 | `/run/bybitbot/config.ai-trade.staged.yaml` |
| NAS 独立数据库凭据 | `/home/nicholas/.config/bybitbot/db-ai-trade.json` |
| 本机无密核验结果 | `var/bybit-ai-trade-verification.json` |

API 凭据文件均为当前用户所有、`0600` 权限；本机 `secrets/` 为 `0700`。NAS 副本已与本机文件校验一致。密钥目录和运行产物被 Git 忽略。

原有 `Bybit_AI_Sub`（UID `587685736`）保留。其普通 API 管理入口不可用，外部 AI OAuth 接口在成功授权后返回空账户列表，本次使用新建的标准子账户接入。临时 OAuth 凭据不用于交易适配器。

## 验证结果与运行状态

- 使用 NAS 代理及磁盘上的新凭据完成主网签名读取，核验子账户、主账户关系、权限及 REST IP 白名单。
- 统一账户权益为 `0 USD`，期权持仓为 0，现货及期权未成交订单均为 0。
- 独立数据库为 `bybitbot_live_ai602623515`，使用独立登录角色和随机密码；迁移 head 为 `8a4d17c2f905`，成交记录为 0。
- 新配置通过检查；`live_allowed=false`，两个策略均禁用。
- 当前运行服务继续使用原 `paper` 配置；未替换运行配置或启用实盘 worker。
- 验证未提交交易订单，不能据此视为成交、撤单、交割或全部实盘能力验收通过。

## 实盘启用前

需要确定资金、策略和风险限额，配置实际告警并完成对应阶段验收。期权单位、`reduceOnly` 等能力仍须取得真实验证证据；实盘门槛不得用虚构记录绕过。

现有备份脚本覆盖 paper 数据库，新实盘数据库还需纳入备份与恢复演练。首次风险基线、接管确认及策略启用应按 `README.md`、`docs/acceptance.md` 和 NAS 运维手册执行。现有示例预算不是用户已确认的实盘授权。
