# GitHub Secrets 配置指南

## 必需的 Secrets

在 GitHub 仓库的 **Settings → Secrets and variables → Actions** 中配置以下 secrets：

### 代理配置（可选但推荐）

| Secret 名称 | 说明 | 示例 |
|------------|------|------|
| `PROXY_URL` | 全局默认代理 URL | `http://proxy.example.com:8080` |
| `BINANCE_PROXY` | Binance 专用代理（可选） | `http://proxy.example.com:8080` |
| `OKX_PROXY` | OKX 专用代理（可选） | `http://proxy.example.com:8080` |
| `BYBIT_PROXY` | Bybit 专用代理（可选） | `http://proxy.example.com:8080` |

**注意**：如果未配置代理，某些交易所 API 可能无法访问。

### 钉钉通知配置（必需）

| Secret 名称 | 说明 | 获取方式 |
|------------|------|---------|
| `DINGTALK_CLIENT_ID` | 钉钉应用 Client ID | 钉钉开发者后台 |
| `DINGTALK_CLIENT_SECRET` | 钉钉应用 Client Secret | 钉钉开发者后台 |
| `DINGTALK_ROBOT_CODE` | 钉钉机器人代码 | 钉钉机器人配置 |
| `DINGTALK_CONVERSATION_ID` | 钉钉群聊 ID | 钉钉群设置 |

### 钉钉配置获取步骤

1. **创建钉钉应用**
   - 访问 [钉钉开发者后台](https://open-dev.dingtalk.com/)
   - 创建企业自建应用
   - 获取 Client ID 和 Client Secret

2. **添加机器人到群**
   - 在钉钉群中设置 → 智能群助手 → 添加机器人
   - 选择自定义机器人
   - 获取机器人代码和群 ID

3. **配置应用权限**
   - 确保应用有发送消息的权限
   - 配置可信 IP 地址（GitHub Actions 的 IP 范围）

## 工作流说明

### 定时触发
- 每 5 分钟自动运行一次
- 与监控系统的检测周期匹配

### 手动触发
- 可以在 GitHub Actions 页面手动触发
- 支持选择是否开启 debug 模式

### 日志查看
- 运行日志在 GitHub Actions 页面查看
- 失败时会自动上传日志文件作为 artifact

## 使用示例

### 1. 配置 Secrets
在 GitHub 仓库设置中添加上述 secrets。

### 2. 验证配置
```bash
# 本地测试配置
uv run coin-radar
```

### 3. 推送代码
```bash
git add .github/workflows/run-monitor.yml
git commit -m "Add GitHub Actions workflow for monitor"
git push
```

### 4. 触发运行
- 等待定时触发（每 5 分钟）
- 或手动在 GitHub Actions 页面触发

## 注意事项

1. **代理配置**：建议使用稳定的代理服务，确保能访问交易所 API
2. **运行时间**：GitHub Actions 免费账户每月有 2000 分钟的额度
3. **并发限制**：免费账户最多同时运行 20 个 job
4. **日志清理**：失败日志保留 7 天，成功日志保留 90 天

## 故障排查

### 无法连接交易所
- 检查代理配置是否正确
- 验证代理服务器是否可用
- 查看运行日志中的网络错误信息

### 钉钉通知失败
- 验证钉钉应用权限
- 检查群聊 ID 是否正确
- 确认 IP 地址在白名单中

### 运行超时
- 检查是否获取数据过多
- 考虑增加超时时间配置
- 减少监控的交易对数量
