# 安全策略

## 报告问题

**请不要开公开 issue。** 用 GitHub 的
[私密安全公告](https://github.com/zcweng/home-jarvis/security/advisories/new) 报告，
或直接发邮件给仓库维护者（见 GitHub 个人资料）。

请尽量给出：复现步骤、影响范围、版本（`git rev-parse --short HEAD`）、相关日志片段。
**贴日志前先删掉 `HA_TOKEN`、`DEEPSEEK_KEY`、小米账号 ID、真实 entity_id。**

## 支持范围

项目当前处于 **P0（本机闭环）**，只保证 `main` 分支最新提交。安全修复直接进 `main`，不维护旧分支。

## 这个项目的安全边界（自建自用前必须读）

`home-jarvis` 是**本机自用**的语音管家，它的威胁模型跟公网服务完全不同：

| 资产 | 位置 | 风险 |
|---|---|---|
| Home Assistant 长期令牌 | `.env`（600） | 泄漏 = 你家所有设备可被远程控制 |
| 小米账号 token | `docker/config/.storage/`（HA 明文写入） | 泄漏 = 米家账号被接管 |
| DeepSeek API Key | `.env` | 泄漏 = 你的额度被刷 |
| 麦克风 / 音箱 | 本机声卡 | 常开录音 |

因此：

1. **别把 Home Assistant 暴露到公网。** `docker-compose.yml` 只做了本机端口映射，如需远程请用
   Tailscale / WireGuard，或反代 + 强鉴权 + TLS；
2. **`.env` / `docker/config/` / `config/aliases.yaml` 已在 `.gitignore` 里**，fork 或 PR 前
   确认 `git diff --cached` 干净；不小心提交了令牌请立刻在 HA 里吊销该令牌并重新生成；
3. **磁盘加密**（macOS FileVault / Linux LUKS）是最后一道防线；
4. **LLM 有真实的设备控制权**：模型只输出中文设备名与动作，`entity_id` 在本地映射，
   所以模型无法编造任意实体；但 `announce` 可以让音箱念任意文本，家里有访客时请自行判断。

## 已知的非问题

- 米家云对不可用设备返回 200 但不执行 —— 代码已前置查在线状态并如实拒绝，属已修；
- 音箱 `play_text` 走小米云 TTS，勿扰时段会静音 —— 属产品行为，不是漏洞；
- 模型偶尔会「编造已执行」 —— 由 `jarvis/intent.py` + `chat_with_guard()` 兜底，
  若发现能在零工具调用时仍宣称已执行，欢迎按上面的渠道报告（附上 prompt 与日志）。
