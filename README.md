# 慢慢记账

纯 Android/Flet 本地记账应用。界面、业务逻辑和 SQLite 全部运行在手机进程内，不需要电脑、HTTP 后端或局域网服务。

## 架构

```text
Flet 界面
  ↓ 直接调用
LocalLedgerGateway
  ↓
共享业务服务
  ├─ 月度余额
  ├─ 分类与限额
  ├─ 账单导入与去重
  ├─ 生活常识规则分类
  ├─ DeepSeek 分类与截图识别
  └─ SQLite
```

Android 使用 Flet 提供的 `FLET_APP_STORAGE_DATA` 目录保存 `data.db`。DeepSeek API Key 通过 Flet `SharedPreferences` 保存，只有 DeepSeek 请求访问外网 HTTPS。

## 项目结构

```text
.
├─ AGENT.md
├─ README.md
├─ main.py
├─ pyproject.toml
├─ requirements.txt
├─ build_android.ps1
├─ frontend/
│  ├─ app.py                 # Flet 界面
│  └─ local_api.py           # 本地业务适配器，不使用 HTTP
└─ backend/
   ├─ application.py         # 业务用例编排
   ├─ config.py              # 应用私有路径和 DeepSeek 配置
   ├─ db.py                  # SQLite
   ├─ schemas.py
   ├─ money.py
   ├─ config/
   │  └─ statement_columns.json
   └─ services/
      ├─ balance.py
      ├─ ledger.py
      ├─ statements.py
      ├─ screenshot.py
      ├─ classifier.py
      ├─ imports.py
      ├─ deepseek.py
      ├─ prompts.py
      └─ default_categories.py
```

## 功能

- 按月记录总余额，支持查看和修改。
- 月度余额自动承接上月期末余额。
- 收入与转账分别记录 `affectBalance`，可逐笔控制是否影响余额。
- 转账不进入收入或支出统计，只作为独立余额流转。
- 特殊账单单独置顶，并可控制是否从余额扣除。
- 特殊账单在界面中显示为“特殊关注”。
- 支出分类只需设置限额。
- 花销从总余额自动扣减，不提供手动分配。
- 分类余额 = 分类限额 − 该分类已花金额。
- 默认支出分类：转账-转出、吃饭、游乐、购物、小金猪（存钱）。
- 默认收入分类：转账-转入。
- 购物统一包含衣服、学习用品、生活用品等，不再细分默认子类。
- 支持支付宝 CSV、微信 XLSX、账单截图和手动记账。
- CSV/XLSX 按交易单号去重；同一截图按图片内容去重。
- 明确交易先按生活常识规则分类，其余再交给 DeepSeek；无法判断时保持未分类。
- 支持现金、微信、支付宝、银行卡、信用卡和其他支付方式。
- 月度页面按支付方式汇总，账单页可按支付方式筛选。
- 转账转出可标记为已退还，优先关联账单中已有的退款转入，不重复新增金额。
- 找不到已有退款转入时只标记状态并提示补录；支持撤销退还。
- 转账与退款账单自动默认计入余额和花销并高亮显示，详细开关收纳在“余额设置”中。
- 普通账单完全不展示余额和花销开关，由系统按账单类型自动处理。
- 只有转账、退款账单显示折叠的余额与花销选项；默认均勾选。
- 每月可随时手动设置本月余额，也可一键恢复使用上月结转。
- 首次启动显示新手引导，设置页可随时重新查看。

## 月度余额

```text
本月期末余额 = 上月期末余额
             + Σ(影响余额的收入)
             + Σ(影响余额的转账转入)
             − Σ(影响余额且计入花销的支出)
             − Σ(影响余额且计入花销的转账转出)
```

特殊支出或特殊转出还必须满足 `deductFromBalance=true`。

## 账单字段

- `type`：`income`、`expense`、`transfer`
- `direction`：收入/转入为 `income`，支出/转出为 `expense`
- `isSpecial`：是否特殊账单
- `deductFromBalance`：特殊账单是否从余额扣除
- `affectBalance`：是否影响余额
- `paymentMethod`：支付方式
- `isRefunded`：转出是否已退还
- `refundOfBillId`：退款记录关联的原转出账单
- `countInBalance`：是否计入余额花销

## 首次使用

1. 安装 APK 并打开。
2. 在“设置”中填写 DeepSeek API Key。
3. 在“月度”中输入总余额。
4. 编辑各支出分类的限额。
5. 导入账单或手动记账。

不配置 DeepSeek Key 时，月度设置、分类、手动记账、账单查看、CSV/XLSX 导入和明确规则分类仍可离线使用；截图识别和含糊账单的模型分类会明确提示未配置。

## 构建 APK

```powershell
powershell -ExecutionPolicy Bypass -File .\build_android.ps1
```

首次构建会安装 Flutter、JDK 17 和 Android SDK。APK 输出在 `build\apk`。

当前 v1.1.1 构建产物位于 `build\apk\manman_ledger.apk`。

- 包名：`local.personal.manman_ledger`
- SHA-256：`9399455B91838C3818162F1CDE617F009ED864C437489BA6F70B29A18BA49B4A`

## 平台差异

- Android 文件选择由系统提供；若系统未返回文件内容，App 会明确提示读取失败。
- DeepSeek 截图识别和模型辅助分类需要互联网。
- 其余功能不依赖电脑、局域网或外部服务。

