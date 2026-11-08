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
- 可选择收入是否计入余额，默认不计入。
- `转账-转入`始终只记录，不计入余额。
- 支出分类只需设置限额。
- 花销从总余额自动扣减，不提供手动分配。
- 分类余额 = 分类限额 − 该分类已花金额。
- 默认支出分类：转账-转出、吃饭、游乐、购物、小金猪（存钱）。
- 默认收入分类：转账-转入。
- 购物统一包含衣服、学习用品、生活用品等，不再细分默认子类。
- 支持支付宝 CSV、微信 XLSX、账单截图和手动记账。
- CSV/XLSX 按交易单号去重；同一截图按图片内容去重。
- 明确交易先按生活常识规则分类，其余再交给 DeepSeek；无法判断时保持未分类。

## 月度余额

```text
可用余额 = 总余额 +（收入计入开启时，非转账收入合计）− 本月支出
```

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

当前构建产物：

- 文件：`build\apk\manman_ledger.apk`
- 应用名：慢慢记账
- 包名：`local.personal.manman_ledger`
- SHA-256：`41F379239FA9964BD3D39A852B231167399147507FE93A1F8C15F20D68C6EC36`

## 平台差异

- Android 文件选择由系统提供；若系统未返回文件内容，App 会明确提示读取失败。
- DeepSeek 截图识别和模型辅助分类需要互联网。
- 其余功能不依赖电脑、局域网或外部服务。

